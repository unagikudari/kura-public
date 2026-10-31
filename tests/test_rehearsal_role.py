"""KURA_DB_ROLE=rehearsal: a writable, isolated copy someone views before a move
(docs/notes/kura-on-nodeb-plan.md). It refuses writes like a replica, except signing in and
out, and it skips migration and the sequence heal like a replica.

Each case runs api.main in a fresh interpreter, because the role is read at import.
No database: the requests below are answered before any query runs.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]

_PROBE = r"""
import json, sys
from fastapi.testclient import TestClient
from api.main import app
client = TestClient(app)
key = {"X-Api-Key": "rehearsal-test-key"}
out = {
    "login": client.post("/api/auth/login", json={}, headers=key).status_code,
    "post": client.post("/api/posts", json={}, headers=key).status_code,
    "post_detail": client.post("/api/posts", json={}, headers=key).json(),
}
print(json.dumps(out))
"""


def _run(role: str) -> dict:
    env = dict(os.environ, KURA_DB_ROLE=role, KURA_API_KEY="rehearsal-test-key",
               KURA_API_KEY_ENFORCE="enforce")
    out = subprocess.run([sys.executable, "-c", _PROBE], cwd=REPO, env=env,
                         capture_output=True, text=True, timeout=120)
    assert out.returncode == 0, out.stderr[-2000:]
    return json.loads(out.stdout.strip().splitlines()[-1])


def test_rehearsal_lets_sign_in_through_and_refuses_other_writes():
    seen = _run("rehearsal")
    assert seen["login"] == 400, "sign-in must reach its handler (400: no email/key given)"
    assert seen["post"] == 403
    assert "rehearsal" in seen["post_detail"]["detail"]


def test_a_replica_still_refuses_sign_in():
    seen = _run("replica")
    assert seen["login"] == 403 and seen["post"] == 403


def test_rehearsal_skips_migration_and_sequence_heal_like_a_replica():
    src = (REPO / "api" / "main.py").read_text()
    assert '_READ_ONLY_ROLES = ("replica", "rehearsal")' in src
    assert src.count("_DB_ROLE in _READ_ONLY_ROLES") == 4   # heal guard, runtime-role check, lifespan, middleware


@pytest.mark.parametrize("role,allowed", [("canonical", True), ("rehearsal", True), ("replica", False)])
def test_human_login_boundary_by_role(monkeypatch, role, allowed):
    from api import human_login
    monkeypatch.setenv("KURA_DB_ROLE", role)
    if allowed:
        human_login.canonical_only()
    else:
        with pytest.raises(human_login.KeyRefused):
            human_login.canonical_only()


class _Result:
    def __init__(self, value):
        self.value, self.rowcount = value, 1

    def scalar(self):
        return self.value


class _Conn:
    def __init__(self, log, db_rev):
        self.log, self.db_rev = log, db_rev

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def execute(self, statement, params=None):
        self.log.append(str(statement))
        return _Result(self.db_rev)


class _Engine:
    def __init__(self, db_rev):
        self.log, self.db_rev = [], db_rev

    def connect(self):
        return _Conn(self.log, self.db_rev)

    def begin(self):
        self.log.append("BEGIN")
        return _Conn(self.log, self.db_rev)


@pytest.mark.parametrize("role,stamps", [("rehearsal", False), ("replica", True)])
def test_a_behind_ledger_is_stamped_only_on_a_replica(monkeypatch, role, stamps):
    """Codex 1.267.0 blocking: _assert_schema_current's self-stamp ran on rehearsal too."""
    import asyncio
    import api.main as main
    import api.db.session as session
    import api.replica_propagation as propagation
    engine = _Engine("0001")
    monkeypatch.setattr(main, "_DB_ROLE", role)
    monkeypatch.setattr(session, "engine", engine)
    monkeypatch.setattr(propagation, "collect_new_revisions", lambda a, b: ["0002"])
    monkeypatch.setattr(propagation, "all_publisher_only", lambda revs: True)
    asyncio.run(main._assert_schema_current())
    wrote = any("UPDATE kura_alembic_version" in s for s in engine.log) or "BEGIN" in engine.log
    assert wrote is stamps, engine.log


def test_rehearsal_does_not_run_boot_hooks():
    src = (REPO / "api" / "main.py").read_text()
    assert 'if _DB_ROLE != "rehearsal":   # boot hooks' in src
