"""A disposable PostgreSQL 18 cluster holding the schema baseline, loaded the way
ops/install/database.py create loads it (roles, extensions, the newest baseline).

Never the application database: the cluster lives in a temporary directory, on a
private socket, and is removed afterwards.
"""
from __future__ import annotations

import contextlib
import shutil
import socket
import subprocess
import tempfile
import uuid
from pathlib import Path

import pytest

BIN = Path("/usr/lib/postgresql/18/bin")
ROOT = Path(__file__).resolve().parents[1]


@contextlib.contextmanager
def cluster():
    if not (BIN / "initdb").exists():
        pytest.fail("PG18 required: a full-schema test may not silently skip")
    home = Path(tempfile.mkdtemp(prefix="kura-schema-pg-"))
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]
    subprocess.run([str(BIN / "initdb"), "-D", str(home / "db"), "-A", "trust", "--no-locale",
                    "--encoding=UTF8", "-U", "postgres"], check=True, capture_output=True)
    subprocess.run([str(BIN / "pg_ctl"), "-D", str(home / "db"), "-l", str(home / "server.log"),
                    "-o", f"-k {home} -p {port} -c listen_addresses= -c fsync=off", "-w", "start"],
                   check=True, capture_output=True)
    try:
        yield home, port
    finally:
        subprocess.run([str(BIN / "pg_ctl"), "-D", str(home / "db"), "-m", "immediate", "-w", "stop"],
                       check=True, capture_output=True)
        shutil.rmtree(home)


def load(home: Path, port: int, baseline: Path | None = None) -> str:
    """A new database with `baseline` (default: the newest) loaded; its asyncpg URL."""
    baseline = baseline or sorted((ROOT / "api/alembic/baseline").glob("*.sql"))[-1]
    name = "k_" + uuid.uuid4().hex[:12]
    psql = [str(BIN / "psql"), "-h", str(home), "-p", str(port), "-U", "postgres", "-q", "-v", "ON_ERROR_STOP=1"]
    roles = "".join(f"DO $$BEGIN CREATE ROLE {r} NOLOGIN; EXCEPTION WHEN duplicate_object THEN NULL; END$$;"
                    for r in ("kura", "kura_reconciler", "kura_audit"))
    subprocess.run([*psql, "-d", "postgres", "-c", roles, "-c", f'CREATE DATABASE "{name}"'],
                   check=True, capture_output=True)
    subprocess.run([*psql, "-d", name, "-c", "CREATE EXTENSION vector; CREATE EXTENSION pg_trgm; "
                    "CREATE EXTENSION fuzzystrmatch;", "-f", str(baseline)], check=True, capture_output=True)
    return f"postgresql+asyncpg://postgres@/{name}?host={home}&port={port}"
