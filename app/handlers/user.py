from datetime import datetime, timezone
import logging
from html import escape
from urllib.parse import urlencode
from aiogram import Router, F
from aiogram.filters import CommandStart
from aiogram.types import Message, CallbackQuery
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy import select
from app.db.session import Session
from app.db.models import User, Payment
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
    created = False
    async with Session() as db:
        u=await db.get(User,tg.id)
        if not u:
            u=User(id=tg.id,username=tg.username); db.add(u); created = True
        else: u.username=tg.username
        await db.commit()
    if created:
        from app.services.notifications import notify_admins
        from app.services.formatting import format_user
        await notify_admins(f"Новый пользователь\nЮз: {format_user(tg.username, tg.id)}\nID: {tg.id}")
    return u

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
    from app.services.bot_settings import get_value
    trial_limit = await get_value("trial_max_profiles")
    active=bool(m.from_user.id in settings.admins or (u.access_until and u.access_until>datetime.now(timezone.utc)) or (u.trial_used and u.trial_profiles_sent < trial_limit))
    await m.answer('Привет! Выбери действие.',reply_markup=menu(active))

@router.callback_query(F.data=='check_sub')
async def check_sub(c:CallbackQuery,bot):
    if await is_subscribed(bot,c.from_user.id):
        u=await ensure_user(c.from_user)
        from app.services.bot_settings import get_value
        trial_limit = await get_value("trial_max_profiles")
        active=bool(c.from_user.id in settings.admins or (u.access_until and u.access_until>datetime.now(timezone.utc)) or (u.trial_used and u.trial_profiles_sent < trial_limit))
        await c.message.edit_text('Подписка подтверждена. Выбери действие.',reply_markup=menu(active))
    else: await c.answer('Подписка пока не найдена.',show_alert=True)

@router.callback_query(F.data=='trial')
async def trial(c:CallbackQuery):
    async with Session() as db:
        u=await db.get(User,c.from_user.id)
        if u.trial_used: await c.answer('Пробный период уже использован.',show_alert=True); return
        u.trial_used=True; u.access_until=None; await db.commit()
    from app.services.bot_settings import get_value
    from app.services.notifications import notify_admins
    trial_limit = await get_value("trial_max_profiles")
    from app.services.formatting import format_user
    await notify_admins(f"Активирован пробный доступ\nЮз: {format_user(c.from_user.username, c.from_user.id)}\nЛимит NFT: {trial_limit}")
    await c.message.edit_text(f'Пробный доступ активирован. Лимит: {trial_limit} профилей.',reply_markup=menu(True))

@router.callback_query(F.data=='buy')
async def buy(c:CallbackQuery):
    if not settings.ton_wallet_address or not settings.ton_api_token:
        await c.answer('Оплата пока не настроена администратором.',show_alert=True)
        return
    await c.answer()
    try:
        memo, amount, amount_nano = await ton.create_invoice(c.from_user.id)
    except Exception:
        logging.exception('Could not create TON invoice for user %s', c.from_user.id)
        await c.answer('Не удалось создать счёт. Попробуйте позже.', show_alert=True)
        return
    # Tonkeeper opens a pre-filled transfer: recipient, exact nanoTON amount,
    # and unique invoice memo are all embedded in the payment deep link.
    transfer_query = urlencode({'amount': str(amount_nano), 'text': memo})
    tonkeeper_url = (
        f'https://app.tonkeeper.com/transfer/{settings.ton_wallet_address}'
        f'?{transfer_query}'
    )
    kb = InlineKeyboardBuilder()
    kb.button(text='Оплатить через Tonkeeper', url=tonkeeper_url)
    kb.button(text='Проверить оплату', callback_data='check_payment')
    kb.button(text='История платежей', callback_data='my_payments')
    kb.adjust(1)
    await c.message.answer(
        '<b>Покупка доступа</b>\n'
        '━━━━━━━━━━━━━━━━━━\n'
        f'<b>Сумма:</b> {escape(str(amount))} TON\n'
        f'<b>Комментарий:</b> <code>{escape(memo)}</code>\n'
        f'<b>Счёт действует:</b> {settings.payment_ttl_minutes} мин.\n'
        '━━━━━━━━━━━━━━━━━━\n'
        'Нажмите «Оплатить через Tonkeeper» и подтвердите перевод в кошельке. '
        'Сумма и комментарий будут заполнены автоматически.',
        parse_mode='HTML',
        reply_markup=kb.as_markup(),
        disable_web_page_preview=True,
    )


@router.callback_query(F.data == 'check_payment')
async def check_payment(c: CallbackQuery, bot):
    await c.answer('Проверяю транзакции TON…')
    try:
        await ton.poll(bot)
        async with Session() as db:
            payment = await db.scalar(
                select(Payment)
                .where(Payment.user_id == c.from_user.id)
                .order_by(Payment.created_at.desc())
            )
            user = await db.get(User, c.from_user.id)
            active = bool(user and user.access_until and user.access_until > datetime.now(timezone.utc))
        if payment and payment.status == 'confirmed' and active:
            await c.message.answer('Оплата найдена и подтверждена. Доступ активирован.', reply_markup=menu(True))
        else:
            await c.message.answer(
                'Платёж пока не найден. Проверьте точную сумму и memo. '
                'После подтверждения транзакции в сети нажмите кнопку ещё раз.'
            )
    except Exception:
        logging.exception('Manual TON payment check failed for user %s', c.from_user.id)
        await c.message.answer('Не удалось связаться с TONAPI. Попробуйте немного позже.')


@router.callback_query(F.data == 'my_payments')
async def my_payments(c: CallbackQuery):
    async with Session() as db:
        rows = (await db.scalars(
            select(Payment).where(Payment.user_id == c.from_user.id)
            .order_by(Payment.created_at.desc()).limit(10)
        )).all()
    if not rows:
        await c.message.answer('У вас пока нет платежей.')
    else:
        lines = ['<b>Мои платежи</b>', '']
        for payment in rows:
            status = 'Подтверждён' if payment.status == 'confirmed' else ('Ожидает оплаты' if payment.status == 'pending' else payment.status)
            lines.append(
                f'#{payment.id} · {payment.amount_nano / 1_000_000_000:g} TON · {status}'
            )
        await c.message.answer('\n'.join(lines), parse_mode='HTML')
    await c.answer()


@router.callback_query(F.data=='ref')
async def referral(c:CallbackQuery,bot):
    me=await bot.get_me(); link=f'https://t.me/{me.username}?start=ref_{c.from_user.id}'
    await c.message.answer(f'Ваша реферальная ссылка:\n{link}\n\nЗа первую подтверждённую покупку приглашённого пользователя вы получите 24 часа доступа.')
