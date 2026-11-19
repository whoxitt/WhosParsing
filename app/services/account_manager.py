import asyncio
import base64
import hashlib
import re
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken
from telethon import TelegramClient
from telethon.errors import SessionPasswordNeededError
from sqlalchemy import select

from app.config import settings
from app.db.session import Session
from app.db.models import TgAccount


class AccountManager:
    def __init__(self):
        self.clients: dict[int, TelegramClient] = {}
        self.pending: dict[int, dict] = {}
        self.setup: dict[int, dict] = {}

    def _fernet(self):
        secret = settings.account_credentials_secret
        if not secret:
            raise RuntimeError(
                "Не задан ACCOUNT_CREDENTIALS_SECRET в переменных Bothost. "
                "Добавьте длинный случайный секрет перед подключением аккаунтов."
            )
        key = base64.urlsafe_b64encode(hashlib.sha256(secret.encode('utf-8')).digest())
        return Fernet(key)

    def _encrypt_hash(self, api_hash: str) -> str:
        return self._fernet().encrypt(api_hash.encode('utf-8')).decode('ascii')

    def _decrypt_hash(self, value: str) -> str:
        try:
            return self._fernet().decrypt(value.encode('ascii')).decode('utf-8')
        except (InvalidToken, ValueError) as exc:
            raise RuntimeError(
                "Не удалось расшифровать API Hash. Проверьте, что ACCOUNT_CREDENTIALS_SECRET "
                "не менялся после сохранения аккаунта."
            ) from exc

    def _client(self, session_name: str, api_id: int, api_hash: str):
        Path(settings.telethon_session_dir).mkdir(parents=True, exist_ok=True)
        path = str(Path(settings.telethon_session_dir) / session_name)
        return TelegramClient(
            path, api_id, api_hash,
            flood_sleep_threshold=settings.telethon_flood_wait_max_seconds,
        )

    def request_api_id(self, admin_id: int):
        self.setup[admin_id] = {"stage": "api_id"}

    def setup_stage(self, admin_id: int):
        item = self.setup.get(admin_id)
        return item.get("stage") if item else None

    def pending_stage(self, admin_id: int):
        item = self.pending.get(admin_id)
        return item.get("stage") if item else None

    async def receive_api_id(self, admin_id: int, value: str):
        if self.setup_stage(admin_id) != "api_id":
            raise ValueError("Сначала нажмите «Добавить аккаунт».")
        raw = value.strip()
        if not raw.isdigit() or not (1 <= len(raw) <= 12):
            raise ValueError("API ID должен быть числом. Найдите его в my.telegram.org.")
        self.setup[admin_id] = {"stage": "api_hash", "api_id": int(raw)}
        return "Теперь отправьте API Hash из my.telegram.org отдельным сообщением."

    async def receive_api_hash(self, admin_id: int, value: str):
        item = self.setup.get(admin_id)
        if not item or item.get("stage") != "api_hash":
            raise ValueError("Сначала отправьте API ID.")
        api_hash = value.strip()
        if not re.fullmatch(r"[0-9a-fA-F]{32}", api_hash):
            raise ValueError("API Hash должен состоять из 32 шестнадцатеричных символов. Проверьте значение на my.telegram.org.")
        # Fail early if encryption key is not configured; never persist credentials in plaintext.
        self._encrypt_hash(api_hash)
        item["api_hash"] = api_hash
        item["stage"] = "phone"
        return "API ID и API Hash приняты. Теперь отправьте номер телефона в международном формате, например +79990000000."

    async def restore_all(self):
        async with Session() as db:
            rows = (await db.scalars(select(TgAccount).where(TgAccount.is_enabled.is_(True)))).all()
            for row in rows:
                client = None
                try:
                    if row.api_id and row.api_hash_encrypted:
                        api_id = int(row.api_id)
                        api_hash = self._decrypt_hash(row.api_hash_encrypted)
                    elif settings.telegram_api_id and settings.telegram_api_hash:
                        # Backward compatibility for accounts created before per-account API credentials.
                        api_id, api_hash = settings.telegram_api_id, settings.telegram_api_hash
                    else:
                        row.status = "credentials_missing"
                        row.last_error = "Для аккаунта не сохранены API ID/API Hash"
                        continue
                    client = self._client(row.session_name, api_id, api_hash)
                    await client.connect()
                    if await client.is_user_authorized():
                        self.clients[row.id] = client
                        row.is_authorized, row.status, row.last_error = True, "online", None
                    else:
                        await client.disconnect()
                        row.is_authorized, row.status = False, "reauthorization_required"
                except Exception as exc:
                    row.status, row.last_error = "error", str(exc)[:1000]
                    if client is not None:
                        try:
                            await client.disconnect()
                        except Exception:
                            pass
            await db.commit()

    async def start_login(self, admin_id: int, phone: str):
        setup = self.setup.get(admin_id)
        if not setup or setup.get("stage") != "phone":
            raise ValueError("Сначала отправьте API ID и API Hash.")
        phone = re.sub(r"[^+0-9]", "", phone)
        if not phone.startswith("+") or len(phone) < 8:
            raise ValueError("Введите номер в международном формате, например +79990000000")
        old = self.pending.pop(admin_id, None)
        if old:
            try: await old["client"].disconnect()
            except Exception: pass
        name = "acc_" + re.sub(r"\D", "", phone)
        client = self._client(name, setup["api_id"], setup["api_hash"])
        try:
            await client.connect()
            sent = await client.send_code_request(phone)
        except Exception:
            await client.disconnect()
            raise
        self.pending[admin_id] = {
            "client": client, "phone": phone, "name": name,
            "phone_code_hash": sent.phone_code_hash,
            "api_id": setup["api_id"], "api_hash": setup["api_hash"],
            "stage": "code",
        }
        self.setup.pop(admin_id, None)
        return "Код отправлен. Введите код из Telegram одним сообщением."

    async def _save_authorized(self, admin_id: int):
        item = self.pending[admin_id]
        client = item["client"]
        me = await client.get_me()
        encrypted_hash = self._encrypt_hash(item["api_hash"])
        async with Session() as db:
            row = await db.scalar(select(TgAccount).where(TgAccount.phone == item["phone"]))
            if row is None:
                row = TgAccount(phone=item["phone"], session_name=item["name"])
                db.add(row)
            row.api_id = item["api_id"]
            row.api_hash_encrypted = encrypted_hash
            row.is_authorized = True
            row.is_enabled = True
            row.status = "online"
            row.last_error = None
            await db.commit()
            await db.refresh(row)
            self.clients[row.id] = client
        self.pending.pop(admin_id, None)
        return f"Аккаунт подключён: {me.first_name or item['phone']} (ID {row.id}). API Hash сохранён в зашифрованном виде."

    async def finish_login(self, admin_id: int, code: str):
        item = self.pending.get(admin_id)
        if not item or item.get("stage") != "code":
            raise ValueError("Сначала запросите код авторизации.")
        code = re.sub(r"\s+", "", code)
        if not code:
            raise ValueError("Введите код из Telegram.")
        try:
            await item["client"].sign_in(phone=item["phone"], code=code, phone_code_hash=item["phone_code_hash"])
        except SessionPasswordNeededError:
            item["stage"] = "password"
            return "NEED_2FA"
        return await self._save_authorized(admin_id)

    async def finish_password(self, admin_id: int, password: str):
        item = self.pending.get(admin_id)
        if not item or item.get("stage") != "password":
            raise ValueError("Сейчас пароль двухэтапной авторизации не ожидается.")
        if not password:
            raise ValueError("Введите пароль 2FA.")
        await item["client"].sign_in(password=password)
        return await self._save_authorized(admin_id)

    async def disconnect(self, account_id: int):
        client = self.clients.pop(account_id, None)
        if client: await client.disconnect()
        async with Session() as db:
            row = await db.get(TgAccount, account_id)
            if row:
                row.status = "disconnected"
                row.is_authorized = False
                await db.commit()

    async def close(self):
        for client in list(self.clients.values()):
            try: await client.disconnect()
            except Exception: pass
        for item in self.pending.values():
            try: await item["client"].disconnect()
            except Exception: pass
