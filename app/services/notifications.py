import logging
from app.config import settings

bot = None

async def notify_admins(message: str):
    if bot is None:
        return
    for admin_id in settings.admins:
        try:
            await bot.send_message(admin_id, message)
        except Exception:
            logging.getLogger(__name__).warning("Could not notify admin %s", admin_id, exc_info=True)
