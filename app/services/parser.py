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
        """Return (status, gift_count, links). Notification failures never become access errors."""
        if not isinstance(sender, TLUser) or sender.bot or sender.deleted:
            return "skipped", 0, []

        try:
            count, links = await get_profile_gifts(client, sender)
        except Exception:
            logger.exception(
                "NFT_ACCESS_ERROR account_id=%s chat_id=%s user_id=%s username=%s",
                chat.account_id, chat.peer_id, sender.id, sender.username,
            )
            return "access_error", 0, []

        logger.info(
            "NFT_CHECK account_id=%s chat_id=%s user_id=%s username=%s unique_count=%s links=%s",
            chat.account_id, chat.peer_id, sender.id, sender.username, count, links,
        )
        if count <= 0:
            return "no_gifts", 0, []

        now = datetime.now(timezone.utc)
        async with Session() as db:
            # Insert if new, then lock/read the canonical row. A previous version could
            # leave an unissued row after failing to deliver its Telegram notification.
            stmt = insert(OwnerProfile).values(
                telegram_user_id=sender.id,
                username=sender.username,
                display_name=' '.join(filter(None, [sender.first_name, sender.last_name])),
                gift_count=count,
                gift_links='\n'.join(links),
                source_chat_title=chat.title,
                source_chat_id=chat.peer_id,
                discovered_by_account_id=chat.account_id,
                discovered_at=now,
                issued_to_user_id=None,
                issued_at=None,
            ).on_conflict_do_nothing(index_elements=['telegram_user_id'])
            await db.execute(stmt)
            row = await db.scalar(
                select(OwnerProfile)
                .where(OwnerProfile.telegram_user_id == sender.id)
                .with_for_update()
            )
            if row is None:
                await db.rollback()
                logger.error("NFT_DB_ERROR user_id=%s: row missing after insert", sender.id)
                return "db_error", count, links

            # Keep existing owner details fresh, including when reclaiming an orphaned row.
            row.username = sender.username
            row.display_name = ' '.join(filter(None, [sender.first_name, sender.last_name]))
            row.gift_count = count
            row.gift_links = '\n'.join(links)
            row.source_chat_title = chat.title
            row.source_chat_id = chat.peer_id
            row.discovered_by_account_id = chat.account_id

            if row.issued_to_user_id not in (None, user_id):
                assigned_to_other = row.issued_to_user_id
                await db.commit()
                logger.info(
                    "NFT_ALREADY_ASSIGNED user_id=%s assigned_to=%s",
                    sender.id, assigned_to_other,
                )
                return "already_assigned", count, links

            if row.issued_to_user_id is None:
                row.issued_to_user_id = user_id
                row.issued_at = now

            if row.delivered_at is not None:
                await db.commit()
                logger.info("NFT_ALREADY_DELIVERED user_id=%s recipient=%s", sender.id, user_id)
                return "already_delivered", count, links

            await db.commit()

        try:
            await send_result(sender.username, sender.id, count, links, chat.title)
        except Exception:
            # Do not release the globally unique profile: it is reserved for this user.
            # delivered_at stays NULL, so the next parser run retries the notification.
            logger.exception(
                "NFT_NOTIFICATION_PENDING owner_id=%s recipient_id=%s username=%s",
                sender.id, user_id, sender.username,
            )
            return "notification_pending", count, links

        async with Session() as db:
            row = await db.scalar(
                select(OwnerProfile)
                .where(OwnerProfile.telegram_user_id == sender.id)
                .with_for_update()
            )
            if row and row.issued_to_user_id == user_id and row.delivered_at is None:
                row.delivered_at = datetime.now(timezone.utc)
            trial_user = await db.get(User, user_id)
            if not paid_active and trial_user:
                trial_user.trial_profiles_sent += 1
            await db.commit()

        logger.info(
            "NFT_DELIVERED owner_id=%s recipient_id=%s username=%s gift_count=%s",
            sender.id, user_id, sender.username, count,
        )
        return "sent", count, links

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
                notification_errors = 0
                already_assigned = 0
                checked_successfully: set[int] = set()
                attempted_by_account: set[tuple[int, int]] = set()
                found_names: list[str] = []
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
                        + (f"\nОшибок отправки уведомления: {notification_errors}" if notification_errors else "")
                    )

                async with Session() as db:
                    chats = (await db.scalars(
                        select(Chat).where(Chat.enabled.is_(True)).order_by(Chat.id)
                    )).all()
                    account_ids = set((await db.scalars(select(TgAccount.id).where(
                        TgAccount.is_enabled.is_(True), TgAccount.is_authorized.is_(True)
                    ))).all())
                chats = [chat for chat in chats if chat.account_id in account_ids]
                if not chats:
                    await send_status('Нет активных чатов у авторизованных аккаунтов. Проверьте раздел «Чаты» и статус Telethon-аккаунтов.', False)
                    return

                random.shuffle(chats)
                deadline = asyncio.get_running_loop().time() + settings.parser_job_timeout_seconds
                last_chat_title = '—'
                for chat in chats:
                    if stop_event.is_set() or sent >= target or asyncio.get_running_loop().time() > deadline:
                        break
                    last_chat_title = chat.title
                    await send_status(progress_text(chat.title), True)

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
                                logger.warning(
                                    "PARTICIPANT_ENUM_ERROR account_id=%s chat_id=%s",
                                    chat.account_id, chat.peer_id, exc_info=True,
                                )
                                candidates = []

                            if not candidates and not stop_event.is_set():
                                async for msg in client.iter_messages(entity, limit=settings.parser_scan_messages_limit):
                                    if stop_event.is_set():
                                        break
                                    try:
                                        sender = msg.sender or await msg.get_sender()
                                    except Exception:
                                        logger.warning(
                                            "MESSAGE_SENDER_ERROR account_id=%s chat_id=%s message_id=%s",
                                            chat.account_id, chat.peer_id, msg.id, exc_info=True,
                                        )
                                        continue
                                    if sender is not None:
                                        candidates.append(sender)

                            random.shuffle(candidates)
                            for sender in candidates:
                                if stop_event.is_set() or sent >= target or asyncio.get_running_loop().time() > deadline:
                                    break
                                if not isinstance(sender, TLUser) or sender.bot or sender.deleted:
                                    continue
                                if sender.id in checked_successfully:
                                    continue
                                account_user_key = (chat.account_id, sender.id)
                                if account_user_key in attempted_by_account:
                                    continue
                                attempted_by_account.add(account_user_key)
                                attempted_count += 1

                                outcome, gift_count, links = await self._check_and_issue(
                                    client, sender, chat, telegram_user_id, paid_active, send_result
                                )

                                if outcome == "access_error":
                                    access_errors += 1
                                elif outcome == "db_error":
                                    access_errors += 1
                                    logger.error("Could not save NFT owner %s", sender.id)
                                else:
                                    successful_checks += 1
                                    checked_successfully.add(sender.id)

                                if outcome == "sent":
                                    sent += 1
                                    found_names.append(
                                        f"@{sender.username}" if sender.username else f"ID {sender.id}"
                                    )
                                elif outcome == "notification_pending":
                                    sent += 1
                                    notification_errors += 1
                                    found_names.append(
                                        f"@{sender.username}" if sender.username else f"ID {sender.id}"
                                    )
                                elif outcome == "already_assigned":
                                    already_assigned += 1
                                elif outcome == "already_delivered":
                                    # This profile is already delivered to this user; don't send twice.
                                    pass

                                if attempted_count % 5 == 0:
                                    await send_status(progress_text(chat.title), True)
                        except Exception:
                            access_errors += 1
                            logger.exception(
                                "PARSER_CHAT_ERROR account_id=%s chat_id=%s title=%s",
                                chat.account_id, chat.peer_id, chat.title,
                            )
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
                if found_names:
                    final_text += "\n\nНайдены владельцы NFT:\n" + "\n".join(found_names)
                if notification_errors:
                    final_text += (
                        f"\n\nВнимание: {notification_errors} уведомлений не доставлено. "
                        "Профили сохранены и закреплены за вами; отправка будет повторена при следующем запуске."
                    )
                if already_assigned:
                    final_text += f"\nУже выданы другим пользователям: {already_assigned}."
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
