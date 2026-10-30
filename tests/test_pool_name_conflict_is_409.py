"""POST /api/pools with a name another pool already has answers 409 and names that pool,
instead of letting the unique constraint surface as a 500. Found by the szurubooru
migration tool, which creates pools by name and is re-run after interruptions.

Same style as test_upload_visibility.py: plain urllib against the live API, auth from
api/env; skipped where the API is not reachable or the node is a read-only replica.
"""
from __future__ import annotations

import base64
import json
import os
import subprocess
import urllib.error
import urllib.request
import uuid
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
API_BASE = os.environ.get("KURA_API_BASE", "http://127.0.0.1:8001").rstrip("/")


def _load_env() -> dict:
    env: dict[str, str] = {}
    p = REPO / "api" / "env"
    if p.exists():
        for line in p.read_text().splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip()
    return env


_ENV = _load_env()
KURA_USER = os.environ.get("KURA_USER") or _ENV.get("KURA_USER", "")
KURA_TOKEN = os.environ.get("KURA_TOKEN") or _ENV.get("KURA_TOKEN", "")


def _api(method: str, path: str, body: dict | None = None):
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Content-Type": "application/json"} if body is not None else {}
    if KURA_USER and KURA_TOKEN:
        headers["Authorization"] = "Token " + base64.b64encode(f"{KURA_USER}:{KURA_TOKEN}".encode()).decode()
    req = urllib.request.Request(f"{API_BASE}{path}", data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def _api_up() -> bool:
    try:
        urllib.request.urlopen(f"{API_BASE}/api/build", timeout=5)
        return bool(KURA_USER and KURA_TOKEN)
    except Exception:
        return False


def _writable() -> bool:
    out = subprocess.run(["psql", "-h", "127.0.0.1", "-U", "kura", "-d", "kura", "-tAc", "SELECT count(*) FROM pg_subscription"],
                         env={**os.environ, "PGPASSWORD": "kura"}, capture_output=True, text=True).stdout.strip()
    return out in ("", "0")


pytestmark = pytest.mark.skipif(not _api_up() or not _writable(),
                                reason="kura API not reachable at 127.0.0.1:8001 with api/env auth, or this node is a replica")


def test_a_taken_pool_name_is_a_409_that_names_the_owner():
    name = f"test_pool_name_conflict_{uuid.uuid4().hex[:8]}"
    status, first = _api("POST", "/api/pools", {"names": [name]})
    assert status == 200 and "id" in first, first
    try:
        status, again = _api("POST", "/api/pools", {"names": [name]})
        assert status == 409, again
        assert again["detail"] == f"pool name {name!r} is already used by pool {first['id']}"
        # the refused request left nothing behind: the name still belongs to the first pool only
        status, pool = _api("GET", f"/api/pools/{name}")
        assert status == 200 and pool["id"] == first["id"]
    finally:
        _api("DELETE", f"/api/pool/{first['id']}")
