"""kura#102: the operator settings page reads back what the running API uses.

Every environment variable the core API reads is declared (so a new one cannot stay
invisible), secrets never leave as values, and only administrators see the page.
Stand-in database only; no production connection.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO = Path(__file__).resolve().parent.parent
READ = re.compile(r"""(?:os\.environ(?:\.get)?\(\s*|os\.environ\[\s*|getenv\(\s*)["']([A-Z][A-Z0-9_]+)["']""")


def _core_api_files():
    out = subprocess.run(["git", "ls-files", "api"], cwd=REPO, capture_output=True, text=True, check=True).stdout
    for name in out.split():
        if name.endswith(".py") and "/alembic/versions/" not in name:
            yield REPO / name


def test_every_variable_the_core_api_reads_is_declared():
    from api.routers.operator_settings import SETTINGS
    read = {m.group(1) for f in _core_api_files() for m in READ.finditer(f.read_text())}
    read |= {"KURA_API_KEY", "KURA_API_KEY_ENFORCE"}   # api/api_key.py reads them through constants
    assert read - set(SETTINGS) == set(), "declare these in api/routers/operator_settings.py SETTINGS"


def test_secrets_are_reported_as_set_or_unset_only():
    from api.routers.operator_settings import settings_now
    rows = {r["name"]: r for r in settings_now({"KURA_SECRET": "s3cret", "DATA_DIR": "/srv/kura", "OLLAMA_URL": ""})}
    assert rows["KURA_SECRET"] == {"name": "KURA_SECRET", "source": "env", "secret": True, "withheld": False, "value": None}
    assert rows["DATA_DIR"]["value"] == "/srv/kura" and rows["DATA_DIR"]["source"] == "env"
    assert rows["OLLAMA_URL"] == {"name": "OLLAMA_URL", "source": "empty", "secret": False, "withheld": False, "value": None}
    assert rows["DATABASE_URL"] == {"name": "DATABASE_URL", "source": "unset", "secret": True, "withheld": False, "value": None}
    assert "s3cret" not in repr(rows)


@pytest.mark.parametrize("raw", [
    "http://user:SYNTHETIC@ollama.local:11434/api",
    "http://ollama.local:11434/api?token=SYNTHETIC",
    "//user:SYNTHETIC@host/data",
    "https://user:SYNTHETIC,TAIL@host/data",
    "https://a.example, https://u:SYNTHETIC@b.example",
    "user:SYNTHETIC@host:11434",
])
def test_a_value_that_could_hold_url_credentials_is_withheld_whole(raw):
    from api.routers.operator_settings import settings_now
    for name in ("OLLAMA_URL", "DATA_URL", "CORS_ALLOW_ORIGINS"):
        row = next(r for r in settings_now({name: raw}) if r["name"] == name)
        assert row["withheld"] is True and row["value"] is None and "SYNTHETIC" not in repr(row)


def test_a_plain_value_is_shown():
    from api.routers.operator_settings import settings_now
    row = next(r for r in settings_now({"CORS_ALLOW_ORIGINS": "https://a.example, https://b.example"}) if r["name"] == "CORS_ALLOW_ORIGINS")
    assert row["withheld"] is False and row["value"] == "https://a.example, https://b.example"


class _Db:
    def __init__(self, users):
        self.users = users

    async def scalar(self, stmt):
        return next((u for u in self.users if u.user_id == 1), None)

    async def scalars(self, stmt):
        admins = [u for u in self.users if u.rank == "administrator"]
        return SimpleNamespace(all=lambda: admins)


def _client(user, users):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from api.auth import get_current_user
    from api.db.session import get_db
    from api.routers.operator_settings import router
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_db] = lambda: _Db(users)
    return TestClient(app)


OWNER = SimpleNamespace(user_id=1, name="owner", rank="administrator")
SECOND = SimpleNamespace(user_id=2, name="owner-mail", rank="administrator")
READER = SimpleNamespace(user_id=3, name="reader", rank="regular")


@pytest.mark.parametrize("who", [READER])
def test_only_administrators_see_it(who):
    assert _client(who, [OWNER, SECOND, READER]).get("/api/settings/operator").status_code == 403


def test_the_owner_and_every_administrator_are_shown(monkeypatch):
    monkeypatch.delenv("KURA_OWNER_USER_ID", raising=False)
    body = _client(SECOND, [OWNER, SECOND, READER]).get("/api/settings/operator").json()
    assert body["owner"] == {"id": 1, "source": "unset", "user": {"id": 1, "name": "owner", "rank": "administrator"}}
    assert [a["id"] for a in body["administrators"]] == [1, 2]
    assert any(r["name"] == "KURA_OWNER_USER_ID" for r in body["settings"])


def test_an_owner_id_with_no_user_says_so(monkeypatch):
    body = _client(SECOND, [SECOND]).get("/api/settings/operator").json()
    assert body["owner"]["user"] is None
