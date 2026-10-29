"""artist-category propagation plan, Phase 1 — artist category hint.

取り込み側は作家名を知りながら平タグとして捨て、get-or-create が default (general)
で作るため、作家タグが全て general 止まりになっていた (pipitua 6,175 /
bokaba 14,058 post、artist 146 タグは手動 curation のみ — task 1d3ebefa 診断)。
Phase 1 は /api/upload の `artist_tags` Form ヒント → _get_or_create_tag の
category_hint 伝搬のみを検証する。fleet データの backfill は Phase 2 (別承認)。

Hits the REAL running kura-api over HTTP (this repo's test convention — see
tests/test_invm_human_pin.py: no fastapi/sqlalchemy in the pytest env).
Requires kura-api.service up and KURA_USER/KURA_TOKEN (api/env).

Run:  python3 -m pytest tests/test_artist_category_hint.py -v
"""

from __future__ import annotations

import base64
import io
import json
import os
import struct
import urllib.error
import urllib.request
import zlib
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
BASE = os.environ.get("KURA_TEST_BASE", "http://127.0.0.1:8001")

# Fixed names: reused across runs so the test doesn't accumulate junk tag rows
# (posts are deleted in teardown; tag rows remain with post_count 0 and are
# recycled by the next run).
NEW_ARTIST_TAG = "artist_hint_test_creator"
PLAIN_TAG = "artist_hint_test_plain"


def _read_api_env() -> dict:
    """api/env if it is there. It is untracked, so a fresh worktree has none —
    and reading it unconditionally turned that into FileNotFoundError, an error
    naming a missing FILE when the real shortage is CREDENTIALS (measured
    2026-09-04 on primary: three tests here errored in a clean worktree). It also
    ran before the KURA_USER/KURA_TOKEN environment path this function already
    supports, so the env vars could not stand in for the file."""
    env = {}
    try:
        text = (REPO / "api" / "env").read_text()
    except OSError:
        return env
    for line in text.splitlines():
        if "=" in line and not line.startswith("#"):
            k, _, v = line.partition("=")
            env[k.strip()] = v.strip()
    return env


def _api_up() -> bool:
    """Same probe the other live-API suites use (upload_visibility,
    invm_human_pin, post_tag_locked). This file was the one that skipped it.

    kura-checkout-pull.timer fast-forwards ~/kura and restarts kura-api whenever the
    running process is older than HEAD, so the service is briefly down exactly after a
    push — which is exactly when the suite gets run before a deploy. Measured
    2026-09-04: kura-api restarted at 22:17:58 by that unit, and runs of this file
    inside the window died with ConnectionRefusedError while runs outside it passed,
    3 of 5 and then 0 of 8. A test that needs a service it does not own must say so
    when the service is absent, rather than reporting a defect in the code under test."""
    try:
        urllib.request.urlopen(f"{BASE}/api/build", timeout=5)
        return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(
    not _api_up(), reason=f"kura-api not reachable at {BASE}")


def _auth_header() -> dict:
    env = _read_api_env()
    user = os.environ.get("KURA_USER", env.get("KURA_USER", "owner"))
    token = os.environ.get("KURA_TOKEN", env.get("KURA_TOKEN", ""))
    if not token:
        pytest.skip("no kura credentials: set KURA_TOKEN (or provide api/env). "
                    "These tests write to a running kura-api and cannot be faked.")
    return {"Authorization": "Token " + base64.b64encode(f"{user}:{token}".encode()).decode()}


def _png_bytes(seed: int) -> bytes:
    """Tiny unique 1x1 PNG (seed varies pixel so md5 dedup can't collide)."""
    def chunk(typ: bytes, data: bytes) -> bytes:
        c = struct.pack(">I", len(data)) + typ + data
        return c + struct.pack(">I", zlib.crc32(typ + data) & 0xFFFFFFFF)
    ihdr = struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)
    raw = b"\x00" + bytes([seed & 0xFF, (seed >> 8) & 0xFF, (seed >> 16) & 0xFF])
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr)
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


def _multipart(fields: dict[str, str], file_bytes: bytes) -> tuple[bytes, str]:
    boundary = "----arthinttest"
    out = io.BytesIO()
    for k, v in fields.items():
        out.write(f"--{boundary}\r\nContent-Disposition: form-data; name=\"{k}\"\r\n\r\n{v}\r\n".encode())
    out.write(f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"t.png\"\r\n"
              f"Content-Type: image/png\r\n\r\n".encode())
    out.write(file_bytes)
    out.write(f"\r\n--{boundary}--\r\n".encode())
    return out.getvalue(), boundary


def _req(method: str, path: str, body: bytes | None = None,
         headers: dict | None = None) -> dict:
    req = urllib.request.Request(BASE + path, data=body, method=method,
                                 headers={**_auth_header(), **(headers or {})})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode() or "{}")


def _upload(tags: str, artist_tags: str, seed: int) -> int:
    fields = {"tags": tags, "safety": "unsafe", "source": "artist-hint-test"}
    if artist_tags:
        fields["artist_tags"] = artist_tags
    body, boundary = _multipart(fields, _png_bytes(seed))
    res = _req("POST", "/api/upload", body,
               {"Content-Type": f"multipart/form-data; boundary={boundary}"})
    assert not res.get("duplicate"), f"unexpected md5 dup: {res}"
    return res["id"]


def _tag_category(name: str) -> str | None:
    res = _req("GET", f"/api/tags?query={name}&limit=50")
    rows = res if isinstance(res, list) else res.get("results", res.get("tags", []))
    for t in rows:
        if name in (t.get("names") or []):
            return t.get("category")
    return None


@pytest.fixture()
def cleanup_posts():
    ids: list[int] = []
    yield ids
    for pid in ids:
        try:
            _req("DELETE", f"/api/post/{pid}")
        except Exception:
            pass


def test_new_tag_with_hint_becomes_artist(cleanup_posts):
    """合成の取り込み 1 作家: artist_tags ヒント付きの新規タグは category=artist。"""
    import time
    seed = int(time.time()) & 0xFFFFFF
    pid = _upload(f"{NEW_ARTIST_TAG} {PLAIN_TAG}", NEW_ARTIST_TAG, seed)
    cleanup_posts.append(pid)
    assert _tag_category(NEW_ARTIST_TAG) == "artist"
    # 同じ upload に含まれた一般タグ (ヒント外) は default のまま — 誤爆しない
    assert _tag_category(PLAIN_TAG) in ("default", "general")


def test_existing_manual_artist_tag_unchanged(cleanup_posts):
    """既存の手動 artist タグ (yucchris) はヒント付き再 upload でも不変。"""
    before = _tag_category("yucchris")
    assert before == "artist", f"precondition: yucchris should be artist, got {before}"
    import time
    seed = (int(time.time()) & 0xFFFFFF) ^ 0x5A5A5A
    pid = _upload("yucchris", "yucchris", seed)
    cleanup_posts.append(pid)
    assert _tag_category("yucchris") == "artist"


def test_no_promotion_without_hint(cleanup_posts):
    """作家でない一般 general タグ: ヒント無し upload では昇格しない。"""
    import time
    seed = (int(time.time()) & 0xFFFFFF) ^ 0xA0A0A0
    pid = _upload(PLAIN_TAG, "", seed)
    cleanup_posts.append(pid)
    assert _tag_category(PLAIN_TAG) in ("default", "general")
