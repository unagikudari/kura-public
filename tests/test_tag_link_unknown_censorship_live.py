"""Setting an artist account's censorship back to "unknown" works (#153): an explicit
"uncensored": null clears it, as clear_uncensored does; a request that does not mention it
leaves it alone. The artists page sends clear_uncensored for "unknown", like the other pages.

Runs on a private, throwaway PostgreSQL cluster; never the application database.
"""
from __future__ import annotations

import asyncio
import socket
import subprocess
import tempfile
from datetime import datetime
from pathlib import Path

import pytest

sa = pytest.importorskip("sqlalchemy")
pytest.importorskip("asyncpg")
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402

from api.db.models import TagLink  # noqa: E402
from api.routers import tags as TR  # noqa: E402

REPO = Path(__file__).resolve().parent.parent


@pytest.fixture(scope="module")
def url():
    bindir = Path(subprocess.check_output(["pg_config", "--bindir"], text=True).strip())
    if not (bindir / "initdb").exists():
        pytest.skip("needs PostgreSQL's initdb")
    with tempfile.TemporaryDirectory(prefix="kura153-") as root:
        subprocess.run([str(bindir / "initdb"), "-D", f"{root}/db", "-A", "trust", "-U", "tester"],
                       check=True, capture_output=True)
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        ctl = [str(bindir / "pg_ctl"), "-D", f"{root}/db"]
        subprocess.run([*ctl, "-l", f"{root}/log", "-o", f"-h 127.0.0.1 -p {port} -k {root} -F", "-w", "start"],
                       check=True, capture_output=True)
        try:
            yield f"tester@127.0.0.1:{port}/postgres"
        finally:
            subprocess.run([*ctl, "-m", "immediate", "-w", "stop"], check=True, capture_output=True)


def patch(url, body: dict, start=1):
    async def go():
        engine = create_async_engine(f"postgresql+asyncpg://{url}", poolclass=sa.pool.NullPool)
        try:
            md = sa.MetaData()
            t = TagLink.__table__.to_metadata(md)
            t.constraints = {c for c in t.constraints if not isinstance(c, sa.ForeignKeyConstraint)}
            t.foreign_keys.clear()
            for col in t.columns:
                col.foreign_keys.clear()
            async with engine.begin() as conn:
                await conn.run_sync(md.drop_all)
                await conn.run_sync(md.create_all)
                row = {"id": 1, "tag_id": 1, "platform": "site", "url": "https://example.org/a", "uncensored": start}
                for col in t.columns:
                    if col.name not in row and not col.nullable and col.server_default is None and not col.primary_key:
                        row[col.name] = datetime(2026, 1, 1) if isinstance(col.type, sa.DateTime) else (
                            False if isinstance(col.type, sa.Boolean) else 0 if isinstance(col.type, sa.Integer) else "x")
                await conn.execute(t.insert(), row)
            async with async_sessionmaker(engine, expire_on_commit=False)() as s:
                out = await TR.update_tag_link(1, TR.TagLinkUpdate(**body), db=s, auth_user=None)
            return out["uncensored"]
        finally:
            await engine.dispose()
    return asyncio.run(go())


def test_unknown_again_by_an_explicit_null(url):
    assert patch(url, {"uncensored": None}) is None


def test_unknown_again_by_clear_uncensored(url):
    assert patch(url, {"clear_uncensored": True}) is None


def test_a_request_that_does_not_mention_it_leaves_it(url):
    assert patch(url, {"note": "hello"}) == 1


def test_a_value_is_still_set(url):
    assert patch(url, {"uncensored": 0}) == 0


def test_the_artists_page_sends_clear_for_unknown():
    page = (REPO / "web/src/routes/artists/+page.svelte").read_text()
    assert "value === '' ? { clear_uncensored: true }" in page
