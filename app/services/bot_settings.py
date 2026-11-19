from sqlalchemy import select
from app.db.session import Session
from app.db.models import BotSetting

DEFAULTS = {
    "parser_max_results": 10,
    "trial_max_profiles": 3,
    "parser_cooldown_seconds": 300,
    "access_hours": 24,
    "ton_price": 5,
}

async def get_value(key: str) -> int:
    async with Session() as db:
        row = await db.get(BotSetting, key)
        return int(row.value) if row else int(DEFAULTS[key])

async def set_value(key: str, value: int):
    if key not in DEFAULTS:
        raise ValueError("Unknown setting")
    value = int(value)
    if value < 0 or value > 1000000:
        raise ValueError("Value out of range")
    async with Session() as db:
        row = await db.get(BotSetting, key)
        if row is None:
            row = BotSetting(key=key, value=str(value))
            db.add(row)
        else:
            row.value = str(value)
        await db.commit()
