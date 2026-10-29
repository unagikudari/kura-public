"""Alembic environment — asyncpg (no psycopg2 needed).

Two ways in (docs/notes/fresh-install-baseline.md):
  * kura-api startup connects as `kura` — the fail-closed check, a no-op after an upgrade;
  * ops/install/database.py upgrade connects as a superuser with
    KURA_MIGRATE_SESSION_AUTHORIZATION=kura: the one connection becomes `kura` (session_user
    and current_user both, so ownership and the governed functions' checks are as on startup)
    and api/alembic/owner.as_owner may switch to an owner role for owner-only DDL. Nothing is
    granted to `kura` at any point.
"""
import asyncio
import os
import re
from logging.config import fileConfig

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from alembic import context

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = None


def _get_url() -> str:
    return os.environ.get("DATABASE_URL", config.get_main_option("sqlalchemy.url"))


def _become(connection, role: str) -> None:
    """SET SESSION AUTHORIZATION, committed, then both identities checked."""
    if not re.fullmatch(r"[a-z_][a-z0-9_]*", role):
        raise RuntimeError(f"KURA_MIGRATE_SESSION_AUTHORIZATION is not a role name: {role!r}")
    connection.execute(text(f'SET SESSION AUTHORIZATION "{role}"'))
    who = connection.execute(text("SELECT session_user, current_user")).one()
    connection.commit()
    if tuple(who) != (role, role):
        raise RuntimeError(f"migrating as {tuple(who)}, expected {role} for both identities")


def _refuse_empty(connection) -> None:
    """An empty database has no history to replay (0001 assumes szurubooru's tables): it is
    installed from the baseline, not migrated from nothing."""
    found = connection.execute(text(
        "SELECT to_regclass('public.kura_alembic_version') IS NOT NULL, "
        "to_regclass('public.post') IS NOT NULL")).one()
    connection.commit()
    if not any(found):
        raise RuntimeError("this database is empty: install it with ops/install/database.py create "
                           "(docs/notes/fresh-install-baseline.md), not by migrating from 0001")


def _run_migrations(connection):
    role = os.environ.get("KURA_MIGRATE_SESSION_AUTHORIZATION")
    if role:
        _become(connection, role)
    _refuse_empty(connection)
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        version_table="kura_alembic_version",
    )
    with context.begin_transaction():
        context.run_migrations()


async def _run_online():
    engine = create_async_engine(_get_url())
    async with engine.connect() as conn:
        await conn.run_sync(_run_migrations)
    await engine.dispose()


if context.is_offline_mode():
    raise NotImplementedError("offline mode not supported")
else:
    asyncio.run(_run_online())
