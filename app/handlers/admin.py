from aiogram import Router, F
from aiogram.filters import Command
from aiogram.types import Message, CallbackQuery
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy import select, func
from telethon import utils

from app.config import settings
from app.db.session import Session
from app.db.models import TgAccount, Chat
from app.services.account_manager import AccountManager

router = Router()
account_manager: AccountManager | None = None


def admin(m):
    return m.from_user.id in settings.admins


def kb_admin():
    kb = InlineKeyboardBuilder()
    kb.button(text='Аккаунты', callback_data='adm_accounts')
    kb.button(text='Добавить аккаунт', callback_data='adm_add_account')
    kb.button(text='Чаты', callback_data='adm_chats')
    kb.button(text='Мониторинг', callback_data='adm_status')
    kb.adjust(1)
    return kb.as_markup()


@router.message(Command('admin'))
async def admin_home(m: Message):
    if not admin(m):
        return
    await m.answer('Панель администратора', reply_markup=kb_admin())


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
        await c.message.answer('Нет сохранённых аккаунтов. Сначала добавьте аккаунт в разделе «Аккаунты».')
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
    await c.message.answer('Выберите аккаунт — откроется список его чатов:', reply_markup=kb.as_markup())
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
    await c.message.answer(text, reply_markup=markup)
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
        await c.message.answer(f'Синхронизация завершена. Новых чатов: {count}.\n\n{text}', reply_markup=markup)
    except Exception as e:
        await c.message.answer(f'Не удалось синхронизировать аккаунт ID {account_id}: {str(e)[:500]}')


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
    await c.message.answer(f'Чат «{title}» теперь {"включён" if enabled else "выключен"}.\n\n{text}', reply_markup=markup)
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
