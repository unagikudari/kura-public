"""Refresh only the dashboard's first-screen status, with local reads."""
import asyncio
import json

from sqlalchemy import text

from api.db.session import async_session
from api.services.dashboard_runtime_cache import CACHE, capture
from scripts.compose_extensions import atomic_write
from scripts.dashboard_thermal_sources import read_trainer_csv


async def record():
    temperature = await asyncio.to_thread(read_trainer_csv)
    async with async_session() as db:
        await db.execute(text("SET TRANSACTION READ ONLY"))
        await db.execute(text("SET LOCAL statement_timeout='3s'"))
        snapshot = await capture(db, temperature_fallback=temperature)
    CACHE.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    atomic_write(CACHE, (json.dumps(snapshot, allow_nan=False) + '\n').encode(), mode=0o600)


if __name__ == "__main__":
    asyncio.run(record())
