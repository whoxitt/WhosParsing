from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from app.config import settings

url = settings.database_url.replace('postgres://', 'postgresql+asyncpg://').replace('postgresql://','postgresql+asyncpg://')
engine = create_async_engine(url, pool_pre_ping=True, pool_size=5, max_overflow=10)
Session = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)

async def init_db():
    from app.db.models import Base
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
