import re
from aiogram import Router, F
from aiogram.filters import Command
from aiogram.types import Message, CallbackQuery
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy import select
from app.config import settings
from app.db.session import Session
from app.db.models import TgAccount, Chat
from app.services.account_manager import AccountManager

router=Router(); account_manager: AccountManager|None=None

def admin(m): return m.from_user.id in settings.admins

def kb_admin():
    kb=InlineKeyboardBuilder(); kb.button(text='Аккаунты',callback_data='adm_accounts'); kb.button(text='Добавить аккаунт',callback_data='adm_add_account'); kb.button(text='Чаты',callback_data='adm_chats'); kb.button(text='Мониторинг',callback_data='adm_status'); kb.adjust(1); return kb.as_markup()

@router.message(Command('admin'))
async def admin_home(m:Message):
    if not admin(m): return
    await m.answer('Панель администратора',reply_markup=kb_admin())

@router.callback_query(F.data=='adm_accounts')
async def accounts(c:CallbackQuery):
    if c.from_user.id not in settings.admins: return
    async with Session() as db: rows=(await db.scalars(select(TgAccount))).all()
    text='Аккаунты:\n'+'\n'.join(f'#{x.id} {x.phone} — {x.status} — '+('включён' if x.is_enabled else 'выключен') for x in rows)
    await c.message.answer(text or 'Аккаунтов пока нет.')

@router.callback_query(F.data=='adm_add_account')
async def add_account(c:CallbackQuery):
    if c.from_user.id not in settings.admins: return
    await c.message.answer('Отправьте номер телефона в международном формате, например +79990000000.')

@router.message(F.text.regexp(r'^\+?[0-9 ()-]{8,20}$'))
async def phone_input(m:Message):
    if not admin(m): return
    try: await m.answer(await account_manager.start_login(m.from_user.id,m.text))
    except Exception as e: await m.answer(f'Не удалось запросить код: {str(e)[:300]}')

@router.message(F.text.regexp(r'^[0-9 ]{4,12}$'))
async def code_input(m:Message):
    if not admin(m): return
    try:
        result=await account_manager.finish_login(m.from_user.id,m.text)
        await m.answer('Требуется пароль двухэтапной авторизации. Отправьте пароль отдельным сообщением.' if result=='NEED_2FA' else result)
    except Exception as e: await m.answer(f'Ошибка авторизации: {str(e)[:300]}')

@router.message(F.text, F.reply_to_message)
async def password_input(m:Message):
    if not admin(m): return
    try:
        result=await account_manager.finish_login(m.from_user.id,'',m.text)
        await m.answer(result)
    except Exception as e: await m.answer(f'Ошибка 2FA: {str(e)[:300]}')

@router.callback_query(F.data=='adm_chats')
async def chats(c:CallbackQuery):
    if c.from_user.id not in settings.admins: return
    async with Session() as db:
        rows=(await db.scalars(select(Chat))).all()
    await c.message.answer('Чаты по аккаунтам:\n'+('\n'.join(f'#{x.id} acc:{x.account_id} {x.title} — '+('активен' if x.enabled else 'выключен') for x in rows) or 'Чаты пока не добавлены.')+'\n\nДобавление: отправьте /sync_chats ID_АККАУНТА')

@router.message(Command('sync_chats'))
async def sync_chats(m:Message):
    if not admin(m): return
    parts=m.text.split()
    if len(parts)!=2 or not parts[1].isdigit(): await m.answer('Формат: /sync_chats ID_АККАУНТА'); return
    aid=int(parts[1]); client=account_manager.clients.get(aid)
    if not client: await m.answer('Аккаунт не подключён.'); return
    count=0
    async for dialog in client.iter_dialogs():
        if not (dialog.is_group or dialog.is_channel): continue
        entity=dialog.entity
        async with Session() as db:
            row=await db.scalar(select(Chat).where(Chat.account_id==aid,Chat.peer_id==entity.id))
            if row is None:
                row=Chat(account_id=aid,peer_id=entity.id,title=dialog.name or str(entity.id),username=getattr(entity,'username',None)); db.add(row); count+=1
            else: row.title=dialog.name or row.title
            await db.commit()
    await m.answer(f'Синхронизация завершена. Добавлено новых чатов: {count}.')

@router.callback_query(F.data=='adm_status')
async def status(c:CallbackQuery):
    if c.from_user.id not in settings.admins: return
    await c.message.answer(f'Активных Telethon-сессий: {len(account_manager.clients)}\nАдминистраторы: {len(settings.admins)}')

@router.message(Command('chat_enable'))
async def chat_enable(m:Message):
    if not admin(m): return
    parts=m.text.split()
    if len(parts)!=2 or not parts[1].isdigit(): await m.answer('Формат: /chat_enable ID_ЧАТА'); return
    async with Session() as db:
        row=await db.get(Chat,int(parts[1]))
        if not row: await m.answer('Чат не найден.'); return
        row.enabled=True; await db.commit()
    await m.answer('Чат включён.')

@router.message(Command('chat_disable'))
async def chat_disable(m:Message):
    if not admin(m): return
    parts=m.text.split()
    if len(parts)!=2 or not parts[1].isdigit(): await m.answer('Формат: /chat_disable ID_ЧАТА'); return
    async with Session() as db:
        row=await db.get(Chat,int(parts[1]))
        if not row: await m.answer('Чат не найден.'); return
        row.enabled=False; await db.commit()
    await m.answer('Чат отключён.')

@router.message(Command('chat_delete'))
async def chat_delete(m:Message):
    if not admin(m): return
    parts=m.text.split()
    if len(parts)!=2 or not parts[1].isdigit(): await m.answer('Формат: /chat_delete ID_ЧАТА'); return
    async with Session() as db:
        row=await db.get(Chat,int(parts[1]))
        if not row: await m.answer('Чат не найден.'); return
        await db.delete(row); await db.commit()
    await m.answer('Чат удалён из списка парсинга.')

@router.message(Command('account_disable'))
async def account_disable(m:Message):
    if not admin(m): return
    parts=m.text.split()
    if len(parts)!=2 or not parts[1].isdigit(): await m.answer('Формат: /account_disable ID_АККАУНТА'); return
    aid=int(parts[1])
    async with Session() as db:
        row=await db.get(TgAccount,aid)
        if not row: await m.answer('Аккаунт не найден.'); return
        row.is_enabled=False; await db.commit()
    await m.answer('Аккаунт отключён от распределения. Сессия сохранена.')

@router.message(Command('account_enable'))
async def account_enable(m:Message):
    if not admin(m): return
    parts=m.text.split()
    if len(parts)!=2 or not parts[1].isdigit(): await m.answer('Формат: /account_enable ID_АККАУНТА'); return
    aid=int(parts[1])
    async with Session() as db:
        row=await db.get(TgAccount,aid)
        if not row: await m.answer('Аккаунт не найден.'); return
        row.is_enabled=True; await db.commit()
    await m.answer('Аккаунт включён. Если сессия авторизована, он будет участвовать в парсинге.')
