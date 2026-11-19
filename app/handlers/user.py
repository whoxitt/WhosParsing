from datetime import datetime, timezone
from aiogram import Router, F
from aiogram.filters import CommandStart
from aiogram.types import Message, CallbackQuery
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy import select
from app.db.session import Session
from app.db.models import User
from app.config import settings
from app.services.access import grant_access
from app.services.ton import TonService

router=Router(); ton=TonService()

def menu(is_active=False):
    kb=InlineKeyboardBuilder()
    kb.button(text='Пробный период',callback_data='trial')
    kb.button(text='Купить доступ',callback_data='buy')
    kb.button(text='Реферальная система',callback_data='ref')
    if is_active: kb.button(text='Парсер',callback_data='parser')
    kb.adjust(1); return kb.as_markup()

def subscribed_kb():
    kb=InlineKeyboardBuilder(); kb.button(text='Подписаться',url=settings.required_channel_url); kb.button(text='Проверить подписку',callback_data='check_sub'); kb.adjust(1); return kb.as_markup()

async def ensure_user(tg):
    async with Session() as db:
        u=await db.get(User,tg.id)
        if not u: u=User(id=tg.id,username=tg.username); db.add(u)
        else: u.username=tg.username
        await db.commit(); return u

async def is_subscribed(bot, uid):
    if not settings.required_channel_id: return True
    try:
        member=await bot.get_chat_member(settings.required_channel_id,uid)
        return member.status in ('member','administrator','creator')
    except Exception: return False

@router.message(CommandStart())
async def start(m:Message,bot):
    args=(m.text or '').split(maxsplit=1)
    u=await ensure_user(m.from_user)
    if len(args)>1 and args[1].startswith('ref_'):
        try:
            ref=int(args[1][4:])
            if ref!=m.from_user.id:
                async with Session() as db:
                    row=await db.get(User,m.from_user.id)
                    if row and not row.referred_by: row.referred_by=ref; await db.commit()
        except ValueError: pass
    if not await is_subscribed(bot,m.from_user.id):
        await m.answer('Для использования бота необходимо подписаться на канал.',reply_markup=subscribed_kb()); return
    active=bool((u.access_until and u.access_until>datetime.now(timezone.utc)) or (u.trial_used and u.trial_profiles_sent < settings.trial_max_profiles))
    await m.answer('Привет! Выбери действие.',reply_markup=menu(active))

@router.callback_query(F.data=='check_sub')
async def check_sub(c:CallbackQuery,bot):
    if await is_subscribed(bot,c.from_user.id):
        u=await ensure_user(c.from_user); active=bool((u.access_until and u.access_until>datetime.now(timezone.utc)) or (u.trial_used and u.trial_profiles_sent < settings.trial_max_profiles))
        await c.message.edit_text('Подписка подтверждена. Выбери действие.',reply_markup=menu(active))
    else: await c.answer('Подписка пока не найдена.',show_alert=True)

@router.callback_query(F.data=='trial')
async def trial(c:CallbackQuery):
    async with Session() as db:
        u=await db.get(User,c.from_user.id)
        if u.trial_used: await c.answer('Пробный период уже использован.',show_alert=True); return
        u.trial_used=True; u.access_until=None; await db.commit()
    await c.message.edit_text(f'Пробный доступ активирован. Лимит: {settings.trial_max_profiles} найденных профиля.',reply_markup=menu(True))

@router.callback_query(F.data=='buy')
async def buy(c:CallbackQuery):
    if not settings.ton_wallet_address or not settings.ton_api_token:
        await c.answer('Оплата пока не настроена администратором.',show_alert=True); return
    memo,amount=await ton.create_invoice(c.from_user.id)
    await c.message.answer(f'Счёт на {amount} TON\nКошелёк: `{settings.ton_wallet_address}`\nКомментарий (memo): `{memo}`\n\nОтправьте точную сумму с указанным комментарием. Зачисление после подтверждения сети.',parse_mode='Markdown')

@router.callback_query(F.data=='ref')
async def referral(c:CallbackQuery,bot):
    me=await bot.get_me(); link=f'https://t.me/{me.username}?start=ref_{c.from_user.id}'
    await c.message.answer(f'Ваша реферальная ссылка:\n{link}\n\nЗа первую подтверждённую покупку приглашённого пользователя вы получите 24 часа доступа.')
