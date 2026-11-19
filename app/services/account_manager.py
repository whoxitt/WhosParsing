import asyncio, re
from pathlib import Path
from telethon import TelegramClient
from telethon.errors import SessionPasswordNeededError, FloodWaitError
from sqlalchemy import select
from app.config import settings
from app.db.session import Session
from app.db.models import TgAccount

class AccountManager:
    def __init__(self):
        self.clients: dict[int, TelegramClient] = {}
        self.pending: dict[int, dict] = {}
        self.lock = asyncio.Lock()

    def _client(self, session_name: str):
        path = str(Path(settings.telethon_session_dir) / session_name)
        return TelegramClient(path, settings.telegram_api_id, settings.telegram_api_hash,
                              flood_sleep_threshold=settings.telethon_flood_wait_max_seconds)

    async def restore_all(self):
        async with Session() as db:
            rows = (await db.scalars(select(TgAccount).where(TgAccount.is_enabled.is_(True)))).all()
            for row in rows:
                client = self._client(row.session_name)
                try:
                    await client.connect()
                    authorized = await client.is_user_authorized()
                    if authorized:
                        self.clients[row.id] = client
                        row.is_authorized, row.status, row.last_error = True, 'online', None
                    else:
                        await client.disconnect()
                        row.is_authorized, row.status = False, 'reauthorization_required'
                except Exception as e:
                    row.status, row.last_error = 'error', str(e)[:1000]
            await db.commit()

    async def start_login(self, admin_id: int, phone: str):
        phone = re.sub(r'[^+0-9]', '', phone)
        if not phone.startswith('+') or len(phone) < 8: raise ValueError('Введите номер в международном формате, например +79990000000')
        name = 'acc_' + re.sub(r'\D','',phone)
        client = self._client(name)
        await client.connect()
        sent = await client.send_code_request(phone)
        self.pending[admin_id] = {'client': client, 'phone': phone, 'name': name, 'phone_code_hash': sent.phone_code_hash}
        return 'Код отправлен. Введите его цифрами; пробелы будут удалены.'

    async def finish_login(self, admin_id: int, code: str, password: str | None = None):
        p = self.pending.get(admin_id)
        if not p: raise ValueError('Сначала запросите код авторизации.')
        code = re.sub(r'\s+', '', code)
        try:
            await p['client'].sign_in(phone=p['phone'], code=code, phone_code_hash=p['phone_code_hash'])
        except SessionPasswordNeededError:
            if not password: return 'NEED_2FA'
            await p['client'].sign_in(password=password)
        me = await p['client'].get_me()
        async with Session() as db:
            row = await db.scalar(select(TgAccount).where(TgAccount.phone == p['phone']))
            if row is None:
                row = TgAccount(phone=p['phone'], session_name=p['name'])
                db.add(row)
            row.is_authorized, row.status, row.last_error = True, 'online', None
            await db.commit(); await db.refresh(row)
            self.clients[row.id] = p['client']
        self.pending.pop(admin_id, None)
        return f'Аккаунт подключён: {me.first_name or p["phone"]} (ID {row.id})'

    async def disconnect(self, account_id: int):
        client = self.clients.pop(account_id, None)
        if client: await client.disconnect()
        async with Session() as db:
            row = await db.get(TgAccount, account_id)
            if row: row.status='disconnected'; row.is_authorized=False; await db.commit()

    async def close(self):
        for client in list(self.clients.values()):
            await client.disconnect()
        for p in self.pending.values():
            await p['client'].disconnect()
