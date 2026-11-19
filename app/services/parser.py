import asyncio
import logging
import random
from datetime import datetime, timezone, timedelta

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from telethon.tl.types import User as TLUser

from app.config import settings
from app.db.session import Session
from app.db.models import User, TgAccount, Chat, OwnerProfile
from app.services.gift_detector import get_profile_gifts
from app.services.account_manager import AccountManager

logger = logging.getLogger(__name__)


class ParserService:
    def __init__(self, accounts: AccountManager):
        self.accounts = accounts
        self.user_locks: dict[int, asyncio.Lock] = {}
        self.chat_locks: dict[tuple[int, int], asyncio.Lock] = {}
        self.stop_events: dict[int, asyncio.Event] = {}

    def is_running(self, telegram_user_id: int) -> bool:
        event = self.stop_events.get(telegram_user_id)
        return event is not None and not event.is_set()

    def stop(self, telegram_user_id: int) -> bool:
        event = self.stop_events.get(telegram_user_id)
        if event is None or event.is_set():
            return False
        event.set()
        return True

    async def _check_and_issue(self, client, sender, chat, user_id, paid_active, send_result):
        if not isinstance(sender, TLUser) or sender.bot or sender.deleted:
            return False
        try:
            count, links = await get_profile_gifts(client, sender)
        except Exception:
            # Do not count an inaccessible profile as a successful "no gifts" result.
            logger.warning("Could not inspect gifts for Telegram user %s: %s", sender.id, exc_info=True)
            return None
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
            logger.exception("Could not send discovered profile %s to user %s", sender.id, user_id)
            return None
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
            await send_status('Ваш поиск уже выполняется.', False)
            return

        async with lock:
            stop_event = asyncio.Event()
            self.stop_events[telegram_user_id] = stop_event
            try:
                async with Session() as db:
                    user = await db.get(User, telegram_user_id)
                    now = datetime.now(timezone.utc)
                    if not user:
                        await send_status('Пользователь не найден. Выполните /start.', False)
                        return
                    paid_active = bool(user.access_until and user.access_until > now)
                    trial_active = bool(user.trial_used and user.trial_profiles_sent < settings.trial_max_profiles)
                    if not paid_active and not trial_active:
                        await send_status('Доступ не активен или лимит пробного периода исчерпан. Оформите доступ в главном меню.', False)
                        return
                    if user.last_parser_at and now - user.last_parser_at < timedelta(seconds=settings.parser_cooldown_seconds):
                        left = int(settings.parser_cooldown_seconds - (now - user.last_parser_at).total_seconds())
                        await send_status(f'Следующий запуск будет доступен через {max(left, 1)} сек.', False)
                        return
                    user.last_parser_at = now
                    trial_remaining = settings.trial_max_profiles - user.trial_profiles_sent
                    await db.commit()

                target = settings.parser_max_results if paid_active else max(0, trial_remaining)
                sent = 0
                attempted_count = 0
                successful_checks = 0
                access_errors = 0
                checked = set()
                started_at = asyncio.get_running_loop().time()

                def elapsed_text():
                    seconds = int(asyncio.get_running_loop().time() - started_at)
                    return f"{seconds // 60:02d}:{seconds % 60:02d}"

                def progress_text(chat_title: str, prefix: str = 'Парсер работает'):
                    elapsed_seconds = max(1, int(asyncio.get_running_loop().time() - started_at))
                    speed = attempted_count / elapsed_seconds
                    return (
                        f"{prefix}\n"
                        f"Чат: {chat_title}\n"
                        f"Прошло времени: {elapsed_text()}\n"
                        f"Скорость: {speed:.1f} проф./сек.\n"
                        f"Профилей просмотрено: {attempted_count}\n"
                        f"Успешных проверок NFT: {successful_checks}\n"
                        f"Ошибок доступа к профилям: {access_errors}\n"
                        f"Найдено профилей: {sent}/{target}"
                    )

                async with Session() as db:
                    chats = (await db.scalars(select(Chat).where(Chat.enabled.is_(True)).order_by(Chat.id))).all()
                    account_ids = set((await db.scalars(select(TgAccount.id).where(
                        TgAccount.is_enabled.is_(True), TgAccount.is_authorized.is_(True)
                    ))).all())
                if not chats:
                    await send_status('Нет активных чатов. Включите хотя бы один чат в разделе «Чаты».', False)
                    return

                # Scan only chats assigned to enabled accounts.
                random.shuffle(chats)
                deadline = asyncio.get_running_loop().time() + settings.parser_job_timeout_seconds
                last_chat_title = '—'
                for chat in chats:
                    if stop_event.is_set() or sent >= target or asyncio.get_running_loop().time() > deadline:
                        break
                    last_chat_title = chat.title
                    await send_status(progress_text(chat.title), True)
                    if chat.account_id not in account_ids:
                        continue
                    try:
                        client = await self.accounts.ensure_client(chat.account_id)
                    except Exception:
                        logger.exception("Could not restore account %s for chat %s", chat.account_id, chat.title)
                        access_errors += 1
                        await send_status(progress_text(chat.title), True)
                        continue

                    chat_lock = self.chat_locks.setdefault((chat.account_id, chat.peer_id), asyncio.Lock())
                    if chat_lock.locked():
                        continue
                    async with chat_lock:
                        try:
                            entity = await client.get_entity(chat.peer_id)
                            candidates = []
                            try:
                                async for participant in client.iter_participants(entity):
                                    if stop_event.is_set():
                                        break
                                    candidates.append(participant)
                                    if len(candidates) >= settings.parser_scan_messages_limit * 5:
                                        break
                            except Exception:
                                logger.debug("Participant list unavailable for %s; trying recent messages", chat.title, exc_info=True)
                                candidates = []
                            if not candidates and not stop_event.is_set():
                                async for msg in client.iter_messages(entity, limit=settings.parser_scan_messages_limit):
                                    if stop_event.is_set():
                                        break
                                    sender = msg.sender or await msg.get_sender()
                                    if sender is not None:
                                        candidates.append(sender)
                            random.shuffle(candidates)
                            for sender in candidates:
                                if stop_event.is_set() or sent >= target or asyncio.get_running_loop().time() > deadline:
                                    break
                                if not isinstance(sender, TLUser) or sender.bot or sender.deleted or sender.id in checked:
                                    continue
                                checked.add(sender.id)
                                attempted_count += 1
                                outcome = await self._check_and_issue(
                                    client, sender, chat, telegram_user_id, paid_active, send_result
                                )
                                if outcome is None:
                                    access_errors += 1
                                else:
                                    successful_checks += 1
                                    if outcome:
                                        sent += 1
                                if attempted_count % 10 == 0:
                                    await send_status(progress_text(chat.title), True)
                        except Exception:
                            access_errors += 1
                            logger.exception("Parser failed in chat %s (account %s)", chat.title, chat.account_id)
                            await send_status(progress_text(chat.title), True)

                if stop_event.is_set():
                    prefix = '⏹ Поиск остановлен'
                elif asyncio.get_running_loop().time() > deadline:
                    prefix = 'Поиск остановлен: достигнут лимит времени'
                else:
                    prefix = 'Поиск завершён'
                final_text = progress_text(last_chat_title, prefix) + (
                    f"\n\nИтог: успешно проверено {successful_checks} из {attempted_count} профилей."
                )
                if attempted_count and access_errors:
                    final_text += (
                        f"\nНе удалось проверить: {access_errors}. Это не означает, что у этих пользователей нет подарков."
                    )
                if successful_checks == 0 and attempted_count:
                    final_text += (
                        "\n\nВнимание: ни одна проверка NFT не завершилась успешно. Проверьте доступ Telegram-аккаунтов "
                        "и совместимость метода получения подарков; нулевой результат нельзя считать достоверным."
                    )
                await send_status(final_text, False)
            finally:
                self.stop_events.pop(telegram_user_id, None)
