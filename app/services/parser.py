import asyncio, random
from datetime import datetime, timezone, timedelta
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from telethon.tl.types import User as TLUser
from app.config import settings
from app.db.session import Session
from app.db.models import User, TgAccount, Chat, OwnerProfile
from app.services.gift_detector import get_profile_gifts
from app.services.account_manager import AccountManager

class ParserService:
    def __init__(self, accounts: AccountManager):
        self.accounts = accounts
        self.user_locks = {}
        self.chat_locks = {}

    async def _check_and_issue(self, client, sender, chat, user_id, paid_active, send_result):
        if not isinstance(sender, TLUser) or sender.bot or sender.deleted:
            return False
        try:
            count, links = await get_profile_gifts(client, sender)
        except Exception:
            return False
        if count <= 0:
            return False
        async with Session() as db:
            stmt = insert(OwnerProfile).values(
                telegram_user_id=sender.id,
                username=sender.username,
                display_name=' '.join(filter(None, [sender.first_name, sender.last_name])),
                gift_count=count,
                gift_links='\n'.join(links),
                source_chat_title=chat.title,
                source_chat_id=chat.peer_id,
                discovered_by_account_id=chat.account_id,
                issued_to_user_id=user_id,
                issued_at=datetime.now(timezone.utc)
            ).on_conflict_do_nothing(index_elements=['telegram_user_id']).returning(OwnerProfile.id)
            result = await db.execute(stmt)
            inserted = result.scalar_one_or_none()
            if inserted is None:
                return False
            await db.commit()
        try:
            await send_result(sender.username, sender.id, count, links, chat.title)
        except Exception:
            async with Session() as db:
                row = await db.scalar(select(OwnerProfile).where(OwnerProfile.telegram_user_id == sender.id))
                if row and row.issued_to_user_id == user_id:
                    row.issued_to_user_id = None
                    row.issued_at = None
                    await db.commit()
            return False
        if not paid_active:
            async with Session() as db:
                trial_user = await db.get(User, user_id)
                if trial_user:
                    trial_user.trial_profiles_sent += 1
                    await db.commit()
        return True

    async def run_for(self, telegram_user_id: int, send_result, send_status):
        lock = self.user_locks.setdefault(telegram_user_id, asyncio.Lock())
        if lock.locked():
            await send_status('Ваш поиск уже выполняется.')
            return
        async with lock:
            async with Session() as db:
                user = await db.get(User, telegram_user_id)
                now = datetime.now(timezone.utc)
                if not user:
                    await send_status('Пользователь не найден. Выполните /start.')
                    return
                paid_active = bool(user.access_until and user.access_until > now)
                trial_active = bool(user.trial_used and user.trial_profiles_sent < settings.trial_max_profiles)
                if not paid_active and not trial_active:
                    await send_status('Доступ не активен или лимит пробного периода исчерпан. Оформите доступ в главном меню.')
                    return
                if user.last_parser_at and now - user.last_parser_at < timedelta(seconds=settings.parser_cooldown_seconds):
                    left = int(settings.parser_cooldown_seconds - (now - user.last_parser_at).total_seconds())
                    await send_status(f'Следующий запуск будет доступен через {max(left, 1)} сек.')
                    return
                user.last_parser_at = now
                trial_remaining = settings.trial_max_profiles - user.trial_profiles_sent
                await db.commit()

            target = settings.parser_max_results if paid_active else max(0, trial_remaining)
            await send_status('Парсер готов к работе. Идёт поиск...')
            sent = 0
            checked = set()
            started_at = asyncio.get_running_loop().time()
            checked_count = 0

            def elapsed_text():
                seconds = int(asyncio.get_running_loop().time() - started_at)
                return f"{seconds // 60:02d}:{seconds % 60:02d}"
            async with Session() as db:
                chats = (await db.scalars(select(Chat).where(Chat.enabled.is_(True)).order_by(Chat.id))).all()
                accounts = (await db.scalars(select(TgAccount).where(
                    TgAccount.is_enabled.is_(True), TgAccount.is_authorized.is_(True)
                ))).all()
            if not chats or not self.accounts.clients:
                await send_status('Нет активных аккаунтов или чатов. Обратитесь к администратору.')
                return

            # Randomized chat order; each chat is scanned only through the account assigned to it.
            random.shuffle(chats)
            deadline = asyncio.get_running_loop().time() + settings.parser_job_timeout_seconds
            for chat in chats:
                if sent >= target or asyncio.get_running_loop().time() > deadline:
                    break
                await send_status(
                    f"Парсер работает\n"
                    f"Чат: {chat.title}\n"
                    f"Прошло времени: {elapsed_text()}\n"
                    f"Профилей проверено: {checked_count}\n"
                    f"Найдено профилей: {sent}/{target}"
                )
                client = self.accounts.clients.get(chat.account_id)
                if not client:
                    continue
                chat_lock = self.chat_locks.setdefault((chat.account_id, chat.peer_id), asyncio.Lock())
                if chat_lock.locked():
                    continue  # another user's job is already scanning this account/chat pair
                async with chat_lock:
                    try:
                        entity = await client.get_entity(chat.peer_id)
                        candidates = []
                        # First attempt the visible participant list. If Telegram hides/denies it,
                        # fall back to authors of recent messages in this chat.
                        try:
                            async for participant in client.iter_participants(entity):
                                candidates.append(participant)
                                if len(candidates) >= settings.parser_scan_messages_limit * 5:
                                    break
                        except Exception:
                            candidates = []
                        if not candidates:
                            async for msg in client.iter_messages(entity, limit=settings.parser_scan_messages_limit):
                                sender = msg.sender or await msg.get_sender()
                                if sender is not None:
                                    candidates.append(sender)
                        random.shuffle(candidates)
                        for sender in candidates:
                            if sent >= target or asyncio.get_running_loop().time() > deadline:
                                break
                            if not isinstance(sender, TLUser) or sender.id in checked:
                                continue
                            checked.add(sender.id)
                            checked_count += 1
                            if checked_count % 10 == 0:
                                await send_status(
                                    f"Парсер работает\n"
                                    f"Чат: {chat.title}\n"
                                    f"Прошло времени: {elapsed_text()}\n"
                                    f"Профилей проверено: {checked_count}\n"
                                    f"Найдено профилей: {sent}/{target}"
                                )
                            if await self._check_and_issue(client, sender, chat, telegram_user_id, paid_active, send_result):
                                sent += 1
                    except Exception:
                        continue
            await send_status(
                f"Поиск завершён\n"
                f"Последний чат: {chat.title if chats else '—'}\n"
                f"Прошло времени: {elapsed_text()}\n"
                f"Профилей проверено: {checked_count}\n"
                f"Найдено и отправлено новых профилей: {sent}"
            )
