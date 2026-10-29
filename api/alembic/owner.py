"""Owner-only DDL in a migration (docs/notes/fresh-install-baseline.md, "The privileged upgrade").

Some objects belong to `kura_reconciler` (the governed functions); replacing them needs that
owner, which `kura` is not and is never made a member of. Under ops/install/database.py upgrade
the connection was authenticated as a superuser and runs as `kura` (api/alembic/env.py); there
`as_owner` switches the session to the owner for the statement and back. On a plain startup
(authenticated as `kura`) the first SET is refused and the upgrade fails closed with the remedy.
"""
from __future__ import annotations

import re

from sqlalchemy import text

RUNTIME_ROLE = "kura"


def as_owner(op, owner: str, sql: str) -> None:
    if not re.fullmatch(r"[a-z_][a-z0-9_]*", owner):
        raise ValueError(f"not a role name: {owner!r}")
    bind = op.get_bind()
    try:
        bind.execute(text(f'SET SESSION AUTHORIZATION "{owner}"'))
    except Exception as exc:
        raise RuntimeError(
            f"this migration replaces objects owned by {owner}: run ops/install/database.py upgrade "
            f"(a superuser-authenticated upgrade), not a plain startup") from exc
    # A failure in `sql` aborts the migration transaction; its rollback undoes this SET too
    # (SET is transactional), and env.py's connection is discarded with the engine.
    bind.execute(text(sql))
    bind.execute(text(f'SET SESSION AUTHORIZATION "{RUNTIME_ROLE}"'))
    who = tuple(bind.execute(text("SELECT session_user, current_user")).one())
    if who != (RUNTIME_ROLE, RUNTIME_ROLE):
        raise RuntimeError(f"after as_owner the session is {who}, expected {RUNTIME_ROLE} for both")
