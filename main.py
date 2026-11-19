import asyncio, logging
from datetime import datetime, timezone
from aiogram import Bot, Dispatcher
from aiogram.types import InlineKeyboardBuilder
from sqlalchemy import select
from app.config import settings
from app.db.session import init_db, Session, engine
from app.db.models import User
from app.handlers import user, admin
from app.services.account_manager import AccountManager
from app.services.parser import ParserService
from app.services.ton import TonService

logging.basicConfig(level=getattr(logging,settings.log_level.upper(),logging.INFO),format='%(asctime)s %(levelname)s %(name)s: %(message)s')
accounts=AccountManager(); parser=ParserService(accounts); ton=TonService()

async def parser_callback(c, bot):
    await c.answer()
    kb=InlineKeyboardBuilder(); kb.button(text='Погнали',callback_data='parser_go')
    await c.message.answer('Парсер кпопниц и гоев и альтушек готов к работе',reply_markup=kb.as_markup())

async def parser_go(c, bot):
    await c.answer()
    async def status(text): await c.message.answer(text)
    async def result(username, uid, count, links, chat):
        name='@'+username if username else f'Telegram ID: {uid}'
        gift_info=f'Количество коллекционных NFT: {count}'
        if count==1 and links: gift_info=links[0]
        text=f'{name}\n{gift_info}\nИсточник: {chat}'
        kb=InlineKeyboardBuilder(); kb.button(text='Перейти в профиль',url=f'tg://user?id={uid}')
        await bot.send_message(c.from_user.id,text,reply_markup=kb.as_markup())
    asyncio.create_task(parser.run_for(c.from_user.id,result,status))

async def main():
    if not settings.bot_token: raise RuntimeError('BOT_TOKEN is missing')
    if not settings.database_url: raise RuntimeError('DATABASE_URL is missing')
    if not settings.telegram_api_id or not settings.telegram_api_hash: logging.warning('Telethon credentials are not configured; parser accounts cannot connect.')
    await init_db(); await accounts.restore_all()
    admin.account_manager=accounts
    bot=Bot(settings.bot_token); dp=Dispatcher()
    user.router.callback_query.register(lambda c: parser_callback(c,bot), lambda c: c.data=='parser')
    user.router.callback_query.register(lambda c: parser_go(c,bot), lambda c: c.data=='parser_go')
    dp.include_router(admin.router); dp.include_router(user.router)
    async def ton_worker():
        while True:
            try: await ton.poll(bot)
            except Exception: logging.exception('TON payment polling failed')
            await asyncio.sleep(settings.payment_poll_seconds)
    worker=asyncio.create_task(ton_worker())
    try: await dp.start_polling(bot)
    finally:
        worker.cancel(); await accounts.close(); await bot.session.close(); await engine.dispose()

if __name__=='__main__': asyncio.run(main())
