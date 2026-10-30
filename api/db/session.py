"""Async SQLAlchemy engine and session factory."""

from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker

from api.config import DATABASE_URL

# pool_pre_ping: a pooled connection that PostgreSQL closed (a restart, an idle timeout) is
# replaced before use instead of surfacing as the first request's 503 or 500.
engine = create_async_engine(DATABASE_URL, echo=False, pool_size=5, max_overflow=10, pool_pre_ping=True)
async_session = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


async def get_db() -> AsyncSession:
    async with async_session() as session:
        yield session
