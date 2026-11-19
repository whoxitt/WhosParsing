from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from app.config import settings
from sqlalchemy import text

url = settings.database_url.replace('postgres://', 'postgresql+asyncpg://').replace('postgresql://','postgresql+asyncpg://')
engine = create_async_engine(url, pool_pre_ping=True, pool_size=5, max_overflow=10)
Session = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)

async def init_db():
    from app.db.models import Base
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        # Additive migration for existing PostgreSQL installations.
        await conn.execute(text("ALTER TABLE tg_accounts ADD COLUMN IF NOT EXISTS api_id BIGINT"))
        await conn.execute(text("ALTER TABLE tg_accounts ADD COLUMN IF NOT EXISTS api_hash_encrypted TEXT"))
        # Tracks whether a discovered NFT owner notification reached the requesting user.
        # Existing orphaned owner_profiles rows remain eligible for delivery.
        await conn.execute(text("ALTER TABLE owner_profiles ADD COLUMN IF NOT EXISTS delivered_at TIMESTAMPTZ"))
