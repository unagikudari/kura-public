#!/usr/bin/env python3
# Description: Resolve a psycopg2-usable kura DSN from a flag or the environment, normalising the SQLAlchemy driver suffix.
# Kind: library
# Effects: read-only
# Requires: none
# Runs-on: any
# Version: 1.0
"""One place that answers "which DSN, and in whose dialect".

`api/env` holds `DATABASE_URL` in SQLAlchemy's form
(`postgresql+asyncpg://...`). psycopg2 and `psql` both reject that: the driver
suffix is part of the SQLAlchemy URL grammar, not of libpq's. The failure is
not a connection error but a *lookup* error -- libpq reads everything after the
last `/` as a database name, so the message is

    FATAL: database "postgresql+asyncpg://kura:...@localhost:5432/kura" does not exist

which reads like a missing database rather than a wrong dialect. Measured on
primary 2026-09-22 against production kura.

That is why this is a module and not four lines inlined per script: every
`EnvironmentFile=%h/kura/api/env` unit that drives a psycopg2 script inherits
the same trap, and `kura-mega-key-join.service` was already sitting in it --
the unit sets no `KURA_DB_DSN`, so the joiner it runs exits 2 with "no DSN"
every time it fires.

Precedence is flag, then the psycopg2-native variable, then the SQLAlchemy one.
An explicit `--dsn` wins so a one-off run can point at the proving DB without
editing anything.
"""
from __future__ import annotations

import os
from typing import Optional

#: SQLAlchemy driver suffixes seen in kura's own env files.
_DRIVER_SUFFIXES = ("+asyncpg", "+psycopg2", "+psycopg")


def normalise_dsn(dsn: str) -> str:
    """Strip the SQLAlchemy driver suffix, leaving a libpq-usable URL."""
    out = (dsn or "").strip().strip('"').strip("'")
    for suffix in _DRIVER_SUFFIXES:
        out = out.replace(suffix, "", 1)
    return out


def resolve_dsn(explicit: Optional[str] = None,
                env: Optional[dict] = None) -> Optional[str]:
    """The DSN to connect with, or None when nothing is set.

    None rather than a default: a script that silently falls back to
    `localhost/kura` would connect to *a* database on any node and report
    findings about the wrong one.
    """
    src = os.environ if env is None else env
    for value in (explicit, src.get("KURA_DB_DSN"), src.get("DATABASE_URL")):
        if value and value.strip():
            return normalise_dsn(value)
    return None


NO_DSN_MESSAGE = (
    "no DSN: pass --dsn, or set KURA_DB_DSN / DATABASE_URL "
    "(EnvironmentFile=%h/kura/api/env supplies DATABASE_URL)"
)


def without_password(dsn: str) -> tuple[str, str]:
    """A URL DSN split into a keyword DSN without the password, and the password: the first
    may go into argv (`psql <dsn>`), the second only into PGPASSWORD."""
    from urllib.parse import unquote, urlsplit
    u = urlsplit(normalise_dsn(dsn))
    keywords = (f"host={u.hostname or 'localhost'} port={u.port or 5432} "
                f"user={unquote(u.username or '')} dbname={u.path.lstrip('/')}")
    return keywords, unquote(u.password or "")
