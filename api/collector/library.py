"""This library's identity (#152): one row in collector_next.library, made once per install
(by the migration that added it, or by a fresh install's initial rows), never copied between
installs. Exported label evidence names it as its origin, so a restore into another library
keeps imported producers apart from that library's own."""
from __future__ import annotations

from sqlalchemy import text


async def library_id(db, create: bool = True) -> str:
    """The id; with `create` (a write transaction) it is made if somehow missing. A read-only
    export only reads it."""
    if create:
        await db.execute(text("INSERT INTO collector_next.library(id) VALUES (gen_random_uuid()) ON CONFLICT DO NOTHING"))
    value = (await db.execute(text("SELECT id FROM collector_next.library"))).scalar_one_or_none()
    if value is None:
        raise RuntimeError("collector_next.library holds no identity; run ops/install/database.py upgrade")
    return str(value)
