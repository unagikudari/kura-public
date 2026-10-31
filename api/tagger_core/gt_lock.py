"""One lock for everything that writes GT labels or demotes the post they sit on (#158).

The GT writers (the pin syncs, the per-pool pass, character seeding) read a post as ok and
then insert; a duplicate merge demotes a post and retracts its labels. Without a common lock a
writer can read "ok", wait on the merge's row lock through its foreign key, and land a label
on the post the merge just hid. Each side takes this transaction-scoped advisory lock first,
so whichever goes second sees the other's result. It is released at commit or rollback.
"""
from __future__ import annotations

GT_WRITE_LOCK_KEY = 0x6B7572615F677477      # "kura_gtw"
SQL = "SELECT pg_advisory_xact_lock(%d)" % GT_WRITE_LOCK_KEY


async def hold(db) -> None:
    """Take the lock in `db`'s current transaction (an async SQLAlchemy session or connection)."""
    from sqlalchemy import text
    await db.execute(text(SQL))
