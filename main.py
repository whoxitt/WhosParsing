import asyncio, logging
from aiogram import Bot, Dispatcher, F
from aiogram.utils.keyboard import InlineKeyboardBuilder
from app.config import settings
from app.db.session import init_db, Session, engine
from app.handlers import user, admin
from app.services.account_manager import AccountManager
from app.services.parser import ParserService
from app.services.ton import TonService

logging.basicConfig(level=getattr(logging,settings.log_level.upper(),logging.INFO),format='%(asctime)s %(levelname)s %(name)s: %(message)s')
accounts=AccountManager(); parser=ParserService(accounts); ton=TonService()

async def parser_callback(c, bot):
    await c.answer()
    kb = InlineKeyboardBuilder()
    kb.button(text='Начать поиск', callback_data='parser_go')
    await c.message.answer('Готов начать поиск коллекционных подарков.', reply_markup=kb.as_markup())


async def parser_go(c, bot):
    await c.answer()
    status_message = await c.message.answer('Подготавливаю парсер…')

    async def status(text):
        try:
            await status_message.edit_text(text)
        except Exception:
            await bot.send_message(c.from_user.id, text)

    async def result(username, uid, count, links, chat):
        name = '@' + username if username else f'Telegram ID: {uid}'
        gift_info = f'Количество коллекционных NFT: {count}'
        if count == 1 and links:
            gift_info = links[0]
        text = f'{name}\n{gift_info}\nИсточник: {chat}'
        kb = InlineKeyboardBuilder()
        kb.button(text='Перейти в профиль', url=f'tg://user?id={uid}')
        await bot.send_message(c.from_user.id, text, reply_markup=kb.as_markup())

    async def run_job():
        try:
            await parser.run_for(c.from_user.id, result, status)
        except Exception:
            logging.exception("Parser job failed for user %s", c.from_user.id)
            await status("Произошла ошибка во время поиска. Подробности записаны в журнал.")

    asyncio.create_task(run_job())

async def main():
    if not settings.bot_token: raise RuntimeError('BOT_TOKEN is missing')
    if not settings.database_url: raise RuntimeError('DATABASE_URL is missing')
    await init_db(); await accounts.restore_all()
    admin.account_manager=accounts
    bot=Bot(settings.bot_token); dp=Dispatcher()
    user.router.callback_query.register(parser_callback, F.data == 'parser')
    user.router.callback_query.register(parser_go, F.data == 'parser_go')
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
