from aiogram import Router, F
from aiogram.filters import Command
from aiogram.types import Message, CallbackQuery
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy import select, func, delete, update
from telethon import utils

from app.config import settings
from app.db.session import Session
from app.db.models import TgAccount, Chat, User, OwnerProfile, Payment, ReferralReward
from app.services.account_manager import AccountManager
from app.services.gift_detector import inspect_profile_gifts

router = Router()
account_manager: AccountManager | None = None
parser_service = None
pending_gift_diagnostics: dict[int, int] = {}


def admin(m):
    return m.from_user.id in settings.admins


def kb_admin():
    kb = InlineKeyboardBuilder()
    kb.button(text='Аккаунты', callback_data='adm_accounts')
    kb.button(text='Добавить аккаунт', callback_data='adm_add_account')
    kb.button(text='Чаты', callback_data='adm_chats')
    kb.button(text='Мониторинг', callback_data='adm_status')
    kb.button(text='Настройки парсера и доступа', callback_data='adm_settings')
    kb.button(text='Пользователи и доступы', callback_data='adm_users')
    kb.button(text='Платежи', callback_data='adm_payments')
    kb.button(text='Очистить найденные NFT-профили', callback_data='adm_db_reset')
    kb.adjust(1)
    return kb.as_markup()


@router.message(Command('admin'))
async def admin_home(m: Message):
    if not admin(m):
        return
    await m.answer('АДМИН-ПАНЕЛЬ\n\nВыберите раздел управления.', reply_markup=kb_admin())


@router.callback_query(F.data == 'adm_accounts')
async def accounts(c: CallbackQuery):
    if c.from_user.id not in settings.admins:
        await c.answer('Нет доступа', show_alert=True)
        return
    async with Session() as db:
        rows = (await db.scalars(select(TgAccount).order_by(TgAccount.id))).all()
    if not rows:
        await c.message.answer('Аккаунтов пока нет.')
    else:
        kb = InlineKeyboardBuilder()
        for row in rows:
            state = '🟢' if row.is_authorized and row.is_enabled else '⚪'
            kb.button(text=f'{state} {row.phone} · ID {row.id}', callback_data=f'acc_chats:{row.id}')
        kb.adjust(1)
        await c.message.answer('Подключённые и сохранённые аккаунты:', reply_markup=kb.as_markup())
    await c.answer()


@router.callback_query(F.data == 'adm_add_account')
async def add_account(c: CallbackQuery):
    if c.from_user.id not in settings.admins:
        await c.answer('Нет доступа', show_alert=True)
        return
    if c.message.chat.type != 'private':
        await c.answer('Добавление аккаунта доступно только в личном чате с ботом.', show_alert=True)
        return
    account_manager.request_api_id(c.from_user.id)
    await c.message.answer('Добавление Telethon-аккаунта.\n1) Отправьте API ID из https://my.telegram.org.')
    await c.answer()


@router.message(F.text & ~F.text.startswith('/'))
async def auth_text_input(m: Message):
    if not admin(m) or not account_manager or m.chat.type != 'private':
        return

    setup_stage = account_manager.setup_stage(m.from_user.id)
    if setup_stage in {'api_id', 'api_hash', 'phone'}:
        value = m.text.strip()
        try:
            if setup_stage == 'api_id':
                result = await account_manager.receive_api_id(m.from_user.id, value)
            elif setup_stage == 'api_hash':
                result = await account_manager.receive_api_hash(m.from_user.id, value)
            else:
                result = await account_manager.start_login(m.from_user.id, value)
            await m.answer(result)
        except Exception as e:
            await m.answer(f'Не удалось обработать данные: {str(e)[:300]}')
        finally:
            try:
                await m.delete()
            except Exception:
                pass
        return

    stage = account_manager.pending_stage(m.from_user.id)
    if stage == 'code':
        try:
            result = await account_manager.finish_login(m.from_user.id, m.text)
            if result == 'NEED_2FA':
                await m.answer('Включена двухэтапная защита Telegram. Теперь отправьте пароль 2FA отдельным сообщением.')
            else:
                await m.answer(result)
        except Exception as e:
            await m.answer(f'Не удалось войти. Проверьте код и попробуйте ещё раз.\n{str(e)[:250]}')
        finally:
            try:
                await m.delete()
            except Exception:
                pass
        return

    if stage == 'password':
        try:
            await m.answer(await account_manager.finish_password(m.from_user.id, m.text))
        except Exception as e:
            await m.answer(f'Ошибка пароля 2FA: {str(e)[:250]}')
        finally:
            try:
                await m.delete()
            except Exception:
                pass
        return

    # One-off NFT diagnostic input requested from an account's settings page.
    diagnostic_account_id = pending_gift_diagnostics.pop(m.from_user.id, None)
    if diagnostic_account_id is not None:
        raw = m.text.strip()
        if raw.startswith('https://t.me/'):
            raw = raw.split('t.me/', 1)[1].split('?', 1)[0].strip('/')
        raw = raw.lstrip('@')
        if not raw or '/' in raw or raw.startswith('+'):
            await m.answer('Отправьте публичный username, например @My_name_sly. Диагностика отменена — нажмите кнопку ещё раз для повтора.')
            return
        status_message = await m.answer('Проверяю профиль через Telegram MTProto…')
        try:
            client = await account_manager.ensure_client(diagnostic_account_id)
            entity = await client.get_entity(raw)
            result = await inspect_profile_gifts(client, entity)
            username = getattr(entity, 'username', None)
            display_name = ' '.join(filter(None, [getattr(entity, 'first_name', None), getattr(entity, 'last_name', None)]))
            lines = [
                'Диагностика коллекционных NFT завершена',
                f'Профиль: @{username}' if username else f'Профиль: {display_name or entity.id}',
                f'Telethon: {result["telethon_version"]}',
                f'MTProto-метод: {result["request_class"]}',
                f'Тип ответа: {result["response_class"]}',
                f'Количество по Telegram: {result["reported_count"] if result["reported_count"] is not None else "не указано"}',
                f'Получено записей подарков: {result["returned_items"]}',
                f'Страниц ответа: {result["pages"]}',
                f'Коллекционных StarGiftUnique: {result["unique_count"]}',
            ]
            if result['gift_types']:
                lines.append('Типы подарков: ' + ', '.join(f'{k}: {v}' for k, v in result['gift_types'].items()))
            if result['wrapper_types']:
                lines.append('Типы записей: ' + ', '.join(f'{k}: {v}' for k, v in result['wrapper_types'].items()))
            if result['links']:
                lines.append('NFT-ссылки:\n' + '\n'.join(result['links'][:30]))
            elif result['returned_items'] == 0:
                lines.append('Telegram вернул пустой список. Это не ошибка фильтра StarGiftUnique: сам MTProto-ответ не содержал подарков.')
            else:
                lines.append('Подарки получены, но среди них не обнаружен конструктор StarGiftUnique.')
            if result['pagination_limit_reached']:
                lines.append('Внимание: достигнут защитный лимит страниц (100); список мог быть неполным.')
            await status_message.edit_text('\n'.join(lines))
        except Exception as exc:
            await status_message.edit_text(
                'Ошибка диагностики NFT\n'
                f'Аккаунт ID: {diagnostic_account_id}\n'
                f'Профиль: @{raw}\n'
                f'Ошибка: {type(exc).__name__}: {str(exc)[:1200]}'
            )
        finally:
            try:
                await m.delete()
            except Exception:
                pass
        return


async def account_keyboard(account_id: int):
    async with Session() as db:
        account = await db.get(TgAccount, account_id)
        if not account:
            return None, None
        chats = (await db.scalars(
            select(Chat).where(Chat.account_id == account_id).order_by(Chat.title)
        )).all()
        phone = account.phone
    kb = InlineKeyboardBuilder()
    kb.button(text='🔎 Проверить NFT у профиля', callback_data=f'gift_diag:{account_id}')
    kb.button(text='🔄 Синхронизировать чаты', callback_data=f'acc_sync:{account_id}')
    for chat in chats:
        mark = '🟢' if chat.enabled else '⚪'
        kb.button(text=f'{mark} {chat.title[:45]}', callback_data=f'chat_toggle:{chat.id}')
    kb.button(text='⬅️ К списку аккаунтов', callback_data='adm_chats')
    kb.adjust(1)
    return f'Аккаунт {phone} (ID {account_id})\nЧатов в базе: {len(chats)}\nНажмите на чат, чтобы включить или выключить его для парсинга.', kb.as_markup()


@router.callback_query(F.data == 'adm_chats')
async def chats(c: CallbackQuery):
    if c.from_user.id not in settings.admins:
        await c.answer('Нет доступа', show_alert=True)
        return
    async with Session() as db:
        rows = (await db.scalars(select(TgAccount).order_by(TgAccount.id))).all()
        counts = dict((await db.execute(
            select(Chat.account_id, func.count(Chat.id)).group_by(Chat.account_id)
        )).all())
    if not rows:
        await c.message.edit_text('Нет сохранённых аккаунтов. Сначала добавьте аккаунт в разделе «Аккаунты».')
        await c.answer()
        return
    kb = InlineKeyboardBuilder()
    for row in rows:
        state = '🟢' if row.is_authorized and row.is_enabled else '⚠️'
        kb.button(
            text=f'{state} {row.phone} · чатов: {counts.get(row.id, 0)}',
            callback_data=f'acc_chats:{row.id}',
        )
    kb.adjust(1)
    await c.message.edit_text('Выберите аккаунт — откроется список его чатов:', reply_markup=kb.as_markup())
    await c.answer()


@router.callback_query(F.data.startswith('acc_chats:'))
async def account_chats(c: CallbackQuery):
    if c.from_user.id not in settings.admins:
        await c.answer('Нет доступа', show_alert=True)
        return
    account_id = int(c.data.split(':', 1)[1])
    text, markup = await account_keyboard(account_id)
    if markup is None:
        await c.answer('Аккаунт не найден', show_alert=True)
        return
    await c.message.edit_text(text, reply_markup=markup)
    await c.answer()


@router.callback_query(F.data.startswith('gift_diag:'))
async def start_gift_diagnostic(c: CallbackQuery):
    if c.from_user.id not in settings.admins:
        await c.answer('Нет доступа', show_alert=True)
        return
    if c.message.chat.type != 'private':
        await c.answer('Диагностика доступна только в личном чате с ботом.', show_alert=True)
        return
    account_id = int(c.data.split(':', 1)[1])
    async with Session() as db:
        account = await db.get(TgAccount, account_id)
        if not account:
            await c.answer('Аккаунт не найден', show_alert=True)
            return
        if not account.is_enabled:
            await c.answer('Сначала включите этот аккаунт.', show_alert=True)
            return
    pending_gift_diagnostics[c.from_user.id] = account_id
    await c.message.answer(
        f'Диагностика NFT через аккаунт {account_id}.\n'
        'Отправьте username владельца, например @My_name_sly.\n'
        'Будут показаны типы объектов, число подарков и ссылки t.me/nft.'
    )
    await c.answer()


async def sync_account_chats(account_id: int) -> int:
    if account_manager is None:
        raise RuntimeError('Менеджер аккаунтов ещё не запущен.')
    client = await account_manager.ensure_client(account_id)
    count = 0
    async for dialog in client.iter_dialogs():
        if not (dialog.is_group or dialog.is_channel):
            continue
        entity = dialog.entity
        peer_id = utils.get_peer_id(entity)
        async with Session() as db:
            row = await db.scalar(
                select(Chat).where(Chat.account_id == account_id, Chat.peer_id == peer_id)
            )
            if row is None:
                row = Chat(
                    account_id=account_id,
                    peer_id=peer_id,
                    title=dialog.name or str(entity.id),
                    username=getattr(entity, 'username', None),
                )
                db.add(row)
                count += 1
            else:
                row.title = dialog.name or row.title
                row.username = getattr(entity, 'username', None)
            await db.commit()
    return count


@router.callback_query(F.data.startswith('acc_sync:'))
async def sync_account_button(c: CallbackQuery):
    if c.from_user.id not in settings.admins:
        await c.answer('Нет доступа', show_alert=True)
        return
    account_id = int(c.data.split(':', 1)[1])
    await c.answer('Синхронизирую чаты…')
    try:
        count = await sync_account_chats(account_id)
        text, markup = await account_keyboard(account_id)
        await c.message.edit_text(f'Синхронизация завершена. Новых чатов: {count}.\n\n{text}', reply_markup=markup)
    except Exception as e:
        await c.message.edit_text(f'Не удалось синхронизировать аккаунт ID {account_id}: {str(e)[:500]}')


@router.callback_query(F.data.startswith('chat_toggle:'))
async def toggle_chat(c: CallbackQuery):
    if c.from_user.id not in settings.admins:
        await c.answer('Нет доступа', show_alert=True)
        return
    chat_id = int(c.data.split(':', 1)[1])
    async with Session() as db:
        row = await db.get(Chat, chat_id)
        if not row:
            await c.answer('Чат не найден', show_alert=True)
            return
        row.enabled = not row.enabled
        account_id = row.account_id
        title = row.title
        enabled = row.enabled
        await db.commit()
    text, markup = await account_keyboard(account_id)
    await c.message.edit_text(f'Чат «{title}» теперь {"включён" if enabled else "выключен"}.\n\n{text}', reply_markup=markup)
    await c.answer()


@router.message(Command('sync_chats'))
async def sync_chats(m: Message):
    if not admin(m):
        return
    parts = (m.text or '').split()
    if len(parts) != 2 or not parts[1].isdigit():
        await m.answer('Формат: /sync_chats ВНУТРЕННИЙ_ID_АККАУНТА. Посмотреть ID можно в /admin → Чаты.')
        return
    account_id = int(parts[1])
    try:
        count = await sync_account_chats(account_id)
        await m.answer(f'Синхронизация завершена. Новых чатов: {count}. Откройте /admin → Чаты → аккаунт {account_id}, чтобы настроить список.')
    except Exception as e:
        await m.answer(f'Не удалось синхронизировать аккаунт ID {account_id}: {str(e)[:500]}')


@router.callback_query(F.data == 'adm_status')
async def status(c: CallbackQuery):
    if c.from_user.id not in settings.admins:
        await c.answer('Нет доступа', show_alert=True)
        return
    await c.message.answer(f'Активных Telethon-сессий: {len(account_manager.clients)}\nАдминистраторы: {len(settings.admins)}')
    await c.answer()


@router.message(Command('chat_enable'))
async def chat_enable(m: Message):
    if not admin(m):
        return
    parts = m.text.split()
    if len(parts) != 2 or not parts[1].isdigit():
        await m.answer('Формат: /chat_enable ID_ЧАТА')
        return
    async with Session() as db:
        row = await db.get(Chat, int(parts[1]))
        if not row:
            await m.answer('Чат не найден.')
            return
        row.enabled = True
        await db.commit()
    await m.answer('Чат включён.')


@router.message(Command('chat_disable'))
async def chat_disable(m: Message):
    if not admin(m):
        return
    parts = m.text.split()
    if len(parts) != 2 or not parts[1].isdigit():
        await m.answer('Формат: /chat_disable ID_ЧАТА')
        return
    async with Session() as db:
        row = await db.get(Chat, int(parts[1]))
        if not row:
            await m.answer('Чат не найден.')
            return
        row.enabled = False
        await db.commit()
    await m.answer('Чат отключён.')


@router.message(Command('chat_delete'))
async def chat_delete(m: Message):
    if not admin(m):
        return
    parts = m.text.split()
    if len(parts) != 2 or not parts[1].isdigit():
        await m.answer('Формат: /chat_delete ID_ЧАТА')
        return
    async with Session() as db:
        row = await db.get(Chat, int(parts[1]))
        if not row:
            await m.answer('Чат не найден.')
            return
        await db.delete(row)
        await db.commit()
    await m.answer('Чат удалён из списка парсинга.')


@router.message(Command('account_disable'))
async def account_disable(m: Message):
    if not admin(m):
        return
    parts = m.text.split()
    if len(parts) != 2 or not parts[1].isdigit():
        await m.answer('Формат: /account_disable ID_АККАУНТА')
        return
    account_id = int(parts[1])
    async with Session() as db:
        row = await db.get(TgAccount, account_id)
        if not row:
            await m.answer('Аккаунт не найден.')
            return
        row.is_enabled = False
        await db.commit()
    await m.answer('Аккаунт отключён от распределения. Сессия сохранена.')


@router.message(Command('account_enable'))
async def account_enable(m: Message):
    if not admin(m):
        return
    parts = m.text.split()
    if len(parts) != 2 or not parts[1].isdigit():
        await m.answer('Формат: /account_enable ID_АККАУНТА')
        return
    account_id = int(parts[1])
    async with Session() as db:
        row = await db.get(TgAccount, account_id)
        if not row:
            await m.answer('Аккаунт не найден.')
            return
        row.is_enabled = True
        await db.commit()
    await m.answer('Аккаунт включён. При необходимости сессия восстановится при синхронизации или перезапуске.')


@router.callback_query(F.data == 'adm_db_reset')
async def db_reset_prompt(c: CallbackQuery):
    if c.from_user.id not in settings.admins:
        await c.answer('Нет доступа', show_alert=True)
        return
    kb = InlineKeyboardBuilder()
    kb.button(text='⚠️ Да, удалить найденные NFT-профили', callback_data='adm_db_reset_confirm')
    kb.button(text='Отмена', callback_data='adm_db_reset_cancel')
    kb.adjust(1)
    await c.message.edit_text(
        '⚠️ Очистка результатов парсинга\n\n'
        'Будут удалены только найденные NFT-профили из базы (таблица owner_profiles).\n'
        'Пользователи, доступ и пробные лимиты, платежи, реферальные награды, '
        'Telethon-аккаунты, чаты и настройки парсинга останутся без изменений.\n\n'
        'Это действие нельзя отменить. Продолжить?',
        reply_markup=kb.as_markup()
    )
    await c.answer()


@router.callback_query(F.data == 'adm_db_reset_cancel')
async def db_reset_cancel(c: CallbackQuery):
    if c.from_user.id not in settings.admins:
        await c.answer('Нет доступа', show_alert=True)
        return
    await c.message.edit_text('Очистка результатов парсинга отменена.', reply_markup=kb_admin())
    await c.answer('Отменено')


@router.callback_query(F.data == 'adm_db_reset_confirm')
async def db_reset_confirm(c: CallbackQuery):
    if c.from_user.id not in settings.admins:
        await c.answer('Нет доступа', show_alert=True)
        return

    # Не очищаем результаты во время активного парсинга: он мог бы тут же создать их снова.
    if parser_service is not None and any(
        not event.is_set() for event in parser_service.stop_events.values()
    ):
        await c.answer('Сначала остановите все активные поиски.', show_alert=True)
        return

    await c.answer('Очищаю найденные NFT-профили…')
    try:
        async with Session() as db:
            result = await db.execute(delete(OwnerProfile))
            deleted_count = max(result.rowcount or 0, 0)
            await db.commit()

        await c.message.edit_text(
            f'✅ Результаты парсинга очищены.\n\nУдалено NFT-профилей: {deleted_count}.\n\n'
            'Все остальные данные, аккаунты, чаты, настройки и доступы сохранены.',
            reply_markup=kb_admin()
        )
    except Exception as exc:
        await c.message.edit_text(
            '❌ Не удалось очистить найденные NFT-профили.\n'
            f'Ошибка: {type(exc).__name__}: {str(exc)[:700]}',
            reply_markup=kb_admin()
        )




# --- Business controls: runtime limits, users, access and payment ledger ---


def _user_label(username, user_id):
    from app.services.formatting import format_user
    return format_user(username, user_id)


async def _resolve_user_reference(reference: str):
    reference = reference.strip()
    async with Session() as db:
        if reference.isdigit():
            return await db.get(User, int(reference))
        username = reference.lstrip('@').strip()
        if not username:
            return None
        return await db.scalar(select(User).where(func.lower(User.username) == username.lower()))


@router.callback_query(F.data == 'adm_settings')
async def admin_settings_page(c: CallbackQuery):
    if c.from_user.id not in settings.admins:
        await c.answer('Нет доступа', show_alert=True); return
    from app.services.bot_settings import get_value
    keys = [
        ('parser_max_results', 'Максимум профилей за поиск'),
        ('trial_max_profiles', 'Лимит пробного доступа'),
        ('parser_cooldown_seconds', 'Кулдаун пользователей, сек.'),
        ('access_hours', 'Длительность покупки, часов'),
        ('ton_price', 'Цена доступа, TON'),
    ]
    lines = ['НАСТРОЙКИ', '']
    kb = InlineKeyboardBuilder()
    for key, title in keys:
        value = await get_value(key)
        lines.append(f'{title}: {value}')
        kb.button(text=f'{title} · {value}', callback_data=f'adm_edit:{key}')
    kb.button(text='Назад', callback_data='adm_home')
    kb.adjust(1)
    await c.message.edit_text('\n'.join(lines) + '\n\nНажмите на параметр, чтобы изменить его.', reply_markup=kb.as_markup())
    await c.answer()


@router.callback_query(F.data == 'adm_home')
async def admin_home_callback(c: CallbackQuery):
    if c.from_user.id not in settings.admins:
        await c.answer('Нет доступа', show_alert=True); return
    await c.message.edit_text('АДМИН-ПАНЕЛЬ\n\nВыберите раздел управления.', reply_markup=kb_admin())
    await c.answer()


@router.callback_query(F.data.startswith('adm_edit:'))
async def admin_edit_setting(c: CallbackQuery):
    if c.from_user.id not in settings.admins:
        await c.answer('Нет доступа', show_alert=True); return
    key = c.data.split(':', 1)[1]
    choices = {
        'parser_max_results': [1, 3, 5, 10, 20, 50, 100],
        'trial_max_profiles': [1, 3, 5, 10, 20],
        'parser_cooldown_seconds': [0, 60, 300, 600, 1800, 3600],
        'access_hours': [1, 6, 12, 24, 72, 168, 720],
        'ton_price': [1, 2, 3, 5, 10, 20],
    }
    if key not in choices:
        await c.answer('Неизвестная настройка', show_alert=True); return
    from app.services.bot_settings import get_value
    current = await get_value(key)
    kb = InlineKeyboardBuilder()
    for value in choices[key]:
        prefix = 'Текущая · ' if value == current else ''
        kb.button(text=f'{prefix}{value}', callback_data=f'adm_set:{key}:{value}')
    kb.button(text='Назад', callback_data='adm_settings')
    kb.adjust(3, 1)
    await c.message.edit_text(f'Изменение настройки: {key}\nТекущее значение: {current}', reply_markup=kb.as_markup())
    await c.answer()


@router.callback_query(F.data.startswith('adm_set:'))
async def admin_set_setting(c: CallbackQuery):
    if c.from_user.id not in settings.admins:
        await c.answer('Нет доступа', show_alert=True); return
    _, key, raw = c.data.split(':', 2)
    from app.services.bot_settings import set_value
    try:
        await set_value(key, int(raw))
    except Exception as exc:
        await c.answer(f'Ошибка: {str(exc)[:100]}', show_alert=True); return
    await admin_settings_page(c)


@router.callback_query(F.data == 'adm_users')
async def admin_users_page(c: CallbackQuery):
    if c.from_user.id not in settings.admins:
        await c.answer('Нет доступа', show_alert=True); return
    from datetime import datetime, timezone
    async with Session() as db:
        total = await db.scalar(select(func.count(User.id))) or 0
        active = await db.scalar(select(func.count(User.id)).where(User.access_until > datetime.now(timezone.utc))) or 0
        recent = (await db.scalars(select(User).order_by(User.created_at.desc()).limit(12))).all()
    lines = [f'ПОЛЬЗОВАТЕЛИ: {total}', f'Активные доступы: {active}', '', 'Последние пользователи:']
    kb = InlineKeyboardBuilder()
    for u in recent:
        label = _user_label(u.username, u.id)
        lines.append(f'{label} · ID {u.id}')
        kb.button(text=label[:60], callback_data=f'adm_user:{u.id}')
    kb.button(text='Назад', callback_data='adm_home')
    kb.adjust(1)
    await c.message.edit_text('\n'.join(lines) + '\n\nВыдача по username: /grant @username ЧАСЫ\nОтзыв: /revoke @username', reply_markup=kb.as_markup())
    await c.answer()


@router.callback_query(F.data.startswith('adm_user:'))
async def admin_user_detail(c: CallbackQuery):
    if c.from_user.id not in settings.admins:
        await c.answer('Нет доступа', show_alert=True); return
    uid = int(c.data.split(':', 1)[1])
    from datetime import datetime, timezone
    async with Session() as db:
        u = await db.get(User, uid)
        payments = (await db.scalars(select(Payment).where(Payment.user_id == uid).order_by(Payment.created_at.desc()).limit(5))).all()
        profiles = await db.scalar(select(func.count(OwnerProfile.id)).where(OwnerProfile.issued_to_user_id == uid)) or 0
    if not u:
        await c.answer('Пользователь не найден', show_alert=True); return
    until = u.access_until.strftime('%d.%m.%Y %H:%M UTC') if u.access_until else 'нет'
    active = bool(u.access_until and u.access_until > datetime.now(timezone.utc))
    label = _user_label(u.username, uid)
    text = (
        'КАРТОЧКА ПОЛЬЗОВАТЕЛЯ\n\n'
        f'Юз: {label}\nID: {uid}\nСоздан: {u.created_at:%d.%m.%Y %H:%M}\n'
        f'Доступ активен: {"да" if active else "нет"}\nДоступ до: {until}\n'
        f'Пробный использован: {"да" if u.trial_used else "нет"}\n'
        f'Профилей в пробном: {u.trial_profiles_sent}\nВыдано NFT-профилей: {profiles}\n'
        f'Платежей: {len(payments)}'
    )
    kb = InlineKeyboardBuilder()
    for hours in (24, 72, 168, 720):
        kb.button(text=f'+{hours} ч.', callback_data=f'adm_grant:{uid}:{hours}')
    kb.button(text='Отозвать доступ', callback_data=f'adm_revoke:{uid}')
    kb.button(text='Платежи пользователя', callback_data=f'adm_userpayments:{uid}')
    kb.button(text='Назад к пользователям', callback_data='adm_users')
    kb.adjust(2, 1, 1, 1)
    await c.message.edit_text(text, reply_markup=kb.as_markup())
    await c.answer()


@router.callback_query(F.data.startswith('adm_grant:'))
async def admin_grant_callback(c: CallbackQuery, bot):
    if c.from_user.id not in settings.admins:
        await c.answer('Нет доступа', show_alert=True); return
    _, raw_uid, raw_hours = c.data.split(':', 2)
    uid, hours = int(raw_uid), int(raw_hours)
    from app.services.access import grant_access
    async with Session() as db:
        target_user = await db.get(User, uid)
        target_username = target_user.username if target_user else None
    await grant_access(uid, hours)
    try:
        await bot.send_message(uid, f'Администратор выдал вам доступ на {hours} ч.')
    except Exception:
        pass
    kb = InlineKeyboardBuilder()
    kb.button(text='Открыть пользователя', callback_data=f'adm_user:{uid}')
    kb.button(text='К списку пользователей', callback_data='adm_users')
    kb.adjust(1)
    await c.message.edit_text(f'Доступ выдан\nЮз: {_user_label(target_username, uid)}\nСрок: {hours} ч.', reply_markup=kb.as_markup())
    await c.answer('Готово')


@router.callback_query(F.data.startswith('adm_revoke:'))
async def admin_revoke(c: CallbackQuery, bot):
    if c.from_user.id not in settings.admins:
        await c.answer('Нет доступа', show_alert=True); return
    uid = int(c.data.split(':', 1)[1])
    async with Session() as db:
        u = await db.get(User, uid)
        target_username = u.username if u else None
        if u:
            u.access_until = None
            await db.commit()
    try:
        await bot.send_message(uid, 'Ваш доступ отключён администратором.')
    except Exception:
        pass
    kb = InlineKeyboardBuilder()
    kb.button(text='Открыть пользователя', callback_data=f'adm_user:{uid}')
    kb.button(text='К списку пользователей', callback_data='adm_users')
    kb.adjust(1)
    await c.message.edit_text(f'Доступ отозван\nЮз: {_user_label(target_username, uid)}', reply_markup=kb.as_markup())
    await c.answer('Готово')


@router.callback_query(F.data == 'adm_payments')
async def admin_payments(c: CallbackQuery):
    if c.from_user.id not in settings.admins:
        await c.answer('Нет доступа', show_alert=True); return
    async with Session() as db:
        rows = (await db.execute(
            select(Payment, User.username).join(User, User.id == Payment.user_id)
            .order_by(Payment.created_at.desc()).limit(15)
        )).all()
        total = await db.scalar(select(func.count(Payment.id))) or 0
        confirmed = await db.scalar(select(func.count(Payment.id)).where(Payment.status == 'confirmed')) or 0
        revenue_nano = await db.scalar(select(func.coalesce(func.sum(Payment.amount_nano), 0)).where(Payment.status == 'confirmed')) or 0
        pending = await db.scalar(select(func.count(Payment.id)).where(Payment.status == 'pending')) or 0
    lines = [
        'ПЛАТЕЖИ', '', f'Всего счетов: {total}', f'Успешных: {confirmed}',
        f'Ожидают оплаты: {pending}', f'Получено: {revenue_nano / 1_000_000_000:g} TON', '',
        'Последние платежи:'
    ]
    kb = InlineKeyboardBuilder()
    for payment, username in rows:
        label = _user_label(username, payment.user_id)
        status = 'оплачен' if payment.status == 'confirmed' else ('ожидает' if payment.status == 'pending' else payment.status)
        lines.append(f'#{payment.id} · {label} · {payment.amount_nano / 1_000_000_000:g} TON · {status}')
        kb.button(text=f'#{payment.id} · {label[:24]} · {status}', callback_data=f'adm_payment:{payment.id}')
    kb.button(text='Обновить', callback_data='adm_payments')
    kb.button(text='Назад', callback_data='adm_home')
    kb.adjust(1)
    await c.message.edit_text('\n'.join(lines), reply_markup=kb.as_markup())
    await c.answer()


@router.callback_query(F.data.startswith('adm_payment:'))
async def admin_payment_detail(c: CallbackQuery):
    if c.from_user.id not in settings.admins:
        await c.answer('Нет доступа', show_alert=True); return
    payment_id = int(c.data.split(':', 1)[1])
    async with Session() as db:
        row = (await db.execute(
            select(Payment, User.username).join(User, User.id == Payment.user_id)
            .where(Payment.id == payment_id)
        )).first()
    if not row:
        await c.answer('Платёж не найден', show_alert=True); return
    payment, username = row
    status = 'Подтверждён' if payment.status == 'confirmed' else ('Ожидает оплаты' if payment.status == 'pending' else payment.status)
    text = (
        f'ПЛАТЁЖ #{payment.id}\n\nЮз: {_user_label(username, payment.user_id)}\n'
        f'Сумма: {payment.amount_nano / 1_000_000_000:g} TON\nСтатус: {status}\n'
        f'Создан: {payment.created_at}\nИстекает: {payment.expires_at}\n'
        f'Memo: {payment.memo}\nTX: {payment.tx_hash or "—"}'
    )
    kb = InlineKeyboardBuilder()
    kb.button(text='Открыть пользователя', callback_data=f'adm_user:{payment.user_id}')
    kb.button(text='К списку платежей', callback_data='adm_payments')
    kb.adjust(1)
    await c.message.edit_text(text, reply_markup=kb.as_markup())
    await c.answer()


@router.callback_query(F.data.startswith('adm_userpayments:'))
async def admin_user_payments(c: CallbackQuery):
    if c.from_user.id not in settings.admins:
        await c.answer('Нет доступа', show_alert=True); return
    uid = int(c.data.split(':', 1)[1])
    async with Session() as db:
        u = await db.get(User, uid)
        rows = (await db.scalars(select(Payment).where(Payment.user_id == uid).order_by(Payment.created_at.desc()).limit(15))).all()
    if not u:
        await c.answer('Пользователь не найден', show_alert=True); return
    kb = InlineKeyboardBuilder()
    lines = [f'ПЛАТЕЖИ: {_user_label(u.username, uid)}', '']
    for p in rows:
        status = 'оплачен' if p.status == 'confirmed' else ('ожидает' if p.status == 'pending' else p.status)
        lines.append(f'#{p.id} · {p.amount_nano / 1_000_000_000:g} TON · {status}')
        kb.button(text=f'#{p.id} · {status}', callback_data=f'adm_payment:{p.id}')
    kb.button(text='Назад к пользователю', callback_data=f'adm_user:{uid}')
    kb.adjust(1)
    await c.message.edit_text('\n'.join(lines), reply_markup=kb.as_markup())
    await c.answer()


@router.message(Command('grant'))
async def admin_grant_command(m: Message, bot):
    if not admin(m): return
    parts = (m.text or '').split()
    if len(parts) != 3 or not parts[2].isdigit() or int(parts[2]) <= 0:
        await m.answer('Формат: /grant @username ЧАСЫ или /grant TELEGRAM_ID ЧАСЫ')
        return
    user = await _resolve_user_reference(parts[1])
    if not user:
        await m.answer('Пользователь не найден. Он должен хотя бы один раз запустить бота командой /start.')
        return
    uid, hours = user.id, int(parts[2])
    from app.services.access import grant_access
    async with Session() as db:
        target_user = await db.get(User, uid)
        target_username = target_user.username if target_user else None
    await grant_access(uid, hours)
    try:
        await bot.send_message(uid, f'Администратор выдал вам доступ на {hours} ч.')
    except Exception:
        pass
    await m.answer(f'Доступ выдан.\nЮз: {_user_label(user.username, uid)}\nСрок: {hours} ч.')


@router.message(Command('revoke'))
async def admin_revoke_command(m: Message, bot):
    if not admin(m): return
    parts = (m.text or '').split()
    if len(parts) != 2:
        await m.answer('Формат: /revoke @username или /revoke TELEGRAM_ID')
        return
    user = await _resolve_user_reference(parts[1])
    if not user:
        await m.answer('Пользователь не найден.')
        return
    async with Session() as db:
        row = await db.get(User, user.id)
        row.access_until = None
        await db.commit()
    try:
        await bot.send_message(user.id, 'Ваш доступ отключён администратором.')
    except Exception:
        pass
    await m.answer(f'Доступ отозван.\nЮз: {_user_label(user.username, user.id)}')
