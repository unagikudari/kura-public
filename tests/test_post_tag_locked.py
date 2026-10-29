"""post_tag.locked / tagger replace-semantics regression tests (plan
kura-tagger-audit-and-remediation rev6, fact 8a24ac93, migration 0069).

Hits the REAL running kura-api over HTTP (matching tests/test_invm_human_pin.py's
convention), plus a direct DB read for `post_tag.locked` and
`post_tag_audit.removed_tags` — neither is exposed over HTTP today (out of
scope for this revision; a future UI-provenance step may add it), so this
file is the one exception to the HTTP-only convention, matching the
precedent of direct-DB test helpers elsewhere in this repo's scripts.

Run:  python3 -m pytest tests/test_post_tag_locked.py -v
"""

from __future__ import annotations

import base64
import io
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

WORKER_SOURCE = "rev6-test-worker"


def _load_env() -> dict:
    env: dict[str, str] = {}
    env_path = REPO / "api" / "env"
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip()
    return env


_ENV = _load_env()
API_BASE = os.environ.get("KURA_API_BASE", "http://127.0.0.1:8001").rstrip("/")
KURA_USER = os.environ.get("KURA_USER") or _ENV.get("KURA_USER", "")
KURA_TOKEN = os.environ.get("KURA_TOKEN") or _ENV.get("KURA_TOKEN", "")


def _auth_header() -> dict:
    if not (KURA_USER and KURA_TOKEN):
        return {}
    b64 = base64.b64encode(f"{KURA_USER}:{KURA_TOKEN}".encode()).decode()
    return {"Authorization": f"Token {b64}"}


def _api_up() -> bool:
    try:
        urllib.request.urlopen(f"{API_BASE}/api/build", timeout=5)
        return True
    except Exception:
        return False


def _req(method: str, path: str, body: dict | None = None, timeout: int = 30) -> tuple[int, dict]:
    data = json.dumps(body).encode() if body is not None else None
    headers = {**_auth_header()}
    if data is not None:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(f"{API_BASE}{path}", data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            return resp.status, (json.loads(raw) if raw else {})
    except urllib.error.HTTPError as e:
        # A 4xx/5xx is a valid response for a test to assert on (e.g. schema
        # validation rejecting an invalid reap_scope) — surface it as a
        # normal (status, body) pair instead of raising, matching urlopen's
        # success-path shape.
        raw = e.read()
        return e.code, (json.loads(raw) if raw else {})


def _tiny_png() -> bytes:
    from PIL import Image
    buf = io.BytesIO()
    px = (int(time.time() * 1000) % 256, (int(time.time()) * 7) % 256, 90)
    im = Image.new("RGB", (32, 32), px)
    for i in range(8):
        im.putpixel((i, i), (255 - px[0], i * 10 % 256, px[2]))
    im.save(buf, format="PNG")
    return buf.getvalue()


def _upload_post(tags: str) -> int:
    """Uploads via /api/upload with a `tags` field — the upload path
    (_apply_tags), exactly like a real importer. Under rev6 every row
    this writes is locked=true."""
    boundary = "----kuraRev6LockTest"
    file_bytes = _tiny_png()
    body = io.BytesIO()
    for name, value in {"tags": tags, "safety": "safe"}.items():
        body.write(
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"\r\n\r\n{value}\r\n".encode()
        )
    body.write(
        (f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; "
         f"filename=\"rev6_lock_test.png\"\r\nContent-Type: image/png\r\n\r\n").encode()
    )
    body.write(file_bytes)
    body.write(f"\r\n--{boundary}--\r\n".encode())
    req = urllib.request.Request(
        f"{API_BASE}/api/upload",
        data=body.getvalue(),
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}", **_auth_header()},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        payload = json.loads(resp.read())
    assert not payload.get("duplicate"), f"unexpected md5 duplicate: {payload}"
    return payload["id"]


def _post_tag_names(post_id: int) -> set[str]:
    _, post = _req("GET", f"/api/post/{post_id}")
    return {name for t in post.get("tags", []) for name in t.get("names", [])}


def _machine_complete(post_id: int, tags: list[str], reap_scope: str = "off",
                       source: str = WORKER_SOURCE,
                       host: str | None = None, release_id: int | None = None) -> dict:
    """A worker completion, exactly as tagger/loop.py sends it. Empty
    `tags` with status='ok' exercises the R7 empty-run guard.

    `reap_scope` defaults "off" (rev7, plan kura-tagger-audit-and-remediation
    fact 00702a48): production runs many independent per-head specialist
    workers that each complete with only their own single tag — retraction
    must stay opt-in, not implied by every completion. Tests exercising
    self-retraction pass reap_scope="own" explicitly.

    `source` defaults to the single shared WORKER_SOURCE — tests that need
    to simulate MULTIPLE independent actors on the same post (the exact
    shape self-retraction must stay safe under) pass distinct `source`
    values per call; the claim table (0073) and self-scoped reap both key
    on this.

    `host`/`release_id` default None: most tests don't care about provenance
    columns (0072) and omitting them exercises the same "older worker" path
    as before this column existed."""
    status, resp = _req(
        "POST", "/api/internal/tagging/complete",
        {"id": post_id, "kind": "auto_tag", "status": "ok",
         "tags": [{"name": t} for t in tags], "source": source, "reap_scope": reap_scope,
         "host": host, "release_id": release_id},
    )
    assert status == 200
    return resp


async def _actor_claims(post_id: int, actor_id: str) -> set[str]:
    """Current post_tag_actor_claim tag NAMES for one actor on one post (0073)."""
    conn = await _db_conn()
    try:
        rows = await conn.fetch(
            "SELECT tn.name FROM post_tag_actor_claim c "
            "JOIN tag_name tn ON tn.tag_id = c.tag_id AND tn.ord = 0 "
            "WHERE c.post_id = $1 AND c.actor_id = $2",
            post_id, actor_id,
        )
        return {r["name"] for r in rows}
    finally:
        await conn.close()


async def _db_conn():
    import asyncpg
    from api import config
    return await asyncpg.connect(config.DATABASE_URL_SYNC)


async def _tag_locked(post_id: int, tag_name: str) -> bool | None:
    """None if the post doesn't currently have this tag."""
    conn = await _db_conn()
    try:
        row = await conn.fetchrow(
            "SELECT pt.locked FROM post_tag pt "
            "JOIN tag_name tn ON tn.tag_id = pt.tag_id AND tn.ord = 0 "
            "WHERE pt.post_id = $1 AND tn.name = $2",
            post_id, tag_name,
        )
        return row["locked"] if row else None
    finally:
        await conn.close()


async def _latest_removed_tags(post_id: int) -> list[str]:
    conn = await _db_conn()
    try:
        row = await conn.fetchrow(
            "SELECT removed_tags FROM post_tag_audit WHERE post_id=$1 AND source='auto_tag' "
            "ORDER BY created_at DESC, id DESC LIMIT 1",
            post_id,
        )
        return list(row["removed_tags"]) if row else []
    finally:
        await conn.close()


async def _latest_audit_provenance(post_id: int) -> tuple[str | None, int | None] | None:
    """(host, release_id) of the most recent post_tag_audit row, or None if
    no row exists yet (0072)."""
    conn = await _db_conn()
    try:
        row = await conn.fetchrow(
            "SELECT host, release_id FROM post_tag_audit WHERE post_id=$1 AND source='auto_tag' "
            "ORDER BY created_at DESC, id DESC LIMIT 1",
            post_id,
        )
        return (row["host"], row["release_id"]) if row else None
    finally:
        await conn.close()


@pytest.fixture()
def post_id():
    if not _api_up():
        pytest.skip(f"kura-api not reachable at {API_BASE}")
    if not (KURA_USER and KURA_TOKEN):
        pytest.skip("KURA_USER/KURA_TOKEN not configured")
    pid = _upload_post("rev6-upload-tag")
    yield pid
    status, _ = _req("DELETE", f"/api/post/{pid}")
    assert status == 204, f"DELETE /post/{pid} failed (teardown)"


def test_upload_tag_is_locked(post_id):
    """_apply_tags (the upload write path) sets locked=true unconditionally."""
    import asyncio
    assert "rev6-upload-tag" in _post_tag_names(post_id)
    locked = asyncio.run(_tag_locked(post_id, "rev6-upload-tag"))
    assert locked is True, "a tag written via /api/upload must be locked=true"


def test_locked_upload_tag_survives_self_retraction(post_id):
    """A locked (upload-origin) tag is never removed by self-scoped retraction
    — INV-L (locked-row immunity) carries over unchanged from rev6."""
    assert "rev6-upload-tag" in _post_tag_names(post_id)
    resp = _machine_complete(post_id, ["rev7-wd14-tag"], reap_scope="own")
    assert resp["final_status"] == "done"
    names = _post_tag_names(post_id)
    assert "rev6-upload-tag" in names, "locked upload tag must survive a reap_scope='own' run that omits it"
    assert "rev7-wd14-tag" in names, "the tagger's own detected tag must still be added"


def test_self_retraction_removes_own_stale_tag(post_id):
    """The core Mode 1 mechanism (rev7, fact 00702a48): an actor's own tag
    it no longer proposes is retracted on its NEXT non-empty completion —
    the first completion only records the claim, the second detects and
    acts on the drop."""
    import asyncio
    resp = _machine_complete(post_id, ["rev7-stale-tag"], reap_scope="own")  # claims rev7-stale-tag
    assert resp["final_status"] == "done"
    assert "rev7-stale-tag" in _post_tag_names(post_id)
    locked = asyncio.run(_tag_locked(post_id, "rev7-stale-tag"))
    assert locked is False, "a tag written by complete_auto_tag must be locked=false"

    resp2 = _machine_complete(post_id, ["rev7-fresh-tag"], reap_scope="own")  # omits rev7-stale-tag
    assert resp2["final_status"] == "done"
    names = _post_tag_names(post_id)
    assert "rev7-stale-tag" not in names, "actor's own dropped tag must be self-retracted"
    assert "rev7-fresh-tag" in names
    assert "rev6-upload-tag" in names, "locked upload tag must be unaffected"

    removed = asyncio.run(_latest_removed_tags(post_id))
    assert "rev7-stale-tag" in removed, (
        "post_tag_audit.removed_tags must reflect the ACTUAL deleted row (DELETE ... RETURNING)"
    )


def test_self_retraction_never_touches_other_actor(post_id):
    """Two independent actors on the same post: actor A retracting its own
    dropped tag must NEVER delete actor B's still-claimed tag, even though
    B never re-completes in this test — the exact safety property rev6's
    full-replacement reap lacked (the ~7-minute production incident)."""
    actor_a, actor_b = "rev7-actor-a", "rev7-actor-b"
    resp_a1 = _machine_complete(post_id, ["rev7-a-tag"], reap_scope="own", source=actor_a)
    assert resp_a1["final_status"] == "done"
    resp_b1 = _machine_complete(post_id, ["rev7-b-tag"], reap_scope="own", source=actor_b)
    assert resp_b1["final_status"] == "done"
    names = _post_tag_names(post_id)
    assert "rev7-a-tag" in names and "rev7-b-tag" in names

    resp_a2 = _machine_complete(post_id, ["rev7-a-fresh"], reap_scope="own", source=actor_a)
    assert resp_a2["final_status"] == "done"
    names = _post_tag_names(post_id)
    assert "rev7-a-tag" not in names, "A's own dropped tag must be retracted"
    assert "rev7-b-tag" in names, "A's retraction must NEVER touch B's still-claimed tag"
    assert "rev7-a-fresh" in names


def test_reproposed_present_tag_survives_retraction(post_id):
    """round-1 BLOCKER regression (codex task 3f9532fe): actor A first-adds
    a tag; actor B ALSO proposes the same (already-present) tag every run
    — B earns no post_tag_audit credit for it (on_conflict_do_nothing skips
    already-present rows), but B's CLAIM row is written regardless. When A
    later drops the tag, it must survive because B still claims it — this
    is exactly what an audit-only reap design would have gotten wrong."""
    import asyncio
    actor_a, actor_b = "rev7-actor-a2", "rev7-actor-b2"
    resp_a1 = _machine_complete(post_id, ["rev7-shared-tag"], reap_scope="own", source=actor_a)
    assert resp_a1["final_status"] == "done"
    assert "rev7-shared-tag" in _post_tag_names(post_id)

    resp_b1 = _machine_complete(post_id, ["rev7-shared-tag"], reap_scope="own", source=actor_b)
    assert resp_b1["final_status"] == "done"
    b_claims = asyncio.run(_actor_claims(post_id, actor_b))
    assert "rev7-shared-tag" in b_claims, "B must record a claim even for an already-present tag"

    resp_a2 = _machine_complete(post_id, ["rev7-a-other"], reap_scope="own", source=actor_a)
    assert resp_a2["final_status"] == "done"
    names = _post_tag_names(post_id)
    assert "rev7-shared-tag" in names, "tag still claimed by B must survive A's retraction"


def test_actor_dropping_claim_unblocks_reap(post_id):
    """round-2 regression (codex task 19f71efe): once B ALSO drops the
    shared tag (via a later non-empty completion), and A (which no longer
    proposes it either) completes again, the tag becomes actually
    reapable — neither actor's stale claim blocks it forever. Would FAIL
    under a pure append/UPSERT claim table."""
    actor_a, actor_b = "rev7-actor-a3", "rev7-actor-b3"
    resp_a1 = _machine_complete(post_id, ["rev7-shared2"], reap_scope="own", source=actor_a)
    assert resp_a1["final_status"] == "done"
    resp_b1 = _machine_complete(post_id, ["rev7-shared2"], reap_scope="own", source=actor_b)
    assert resp_b1["final_status"] == "done"
    assert "rev7-shared2" in _post_tag_names(post_id)

    resp_b2 = _machine_complete(post_id, ["rev7-b-other"], reap_scope="own", source=actor_b)
    assert resp_b2["final_status"] == "done"
    assert "rev7-shared2" in _post_tag_names(post_id), "still claimed by A, so not yet reaped"

    resp_a2 = _machine_complete(post_id, ["rev7-a-other2"], reap_scope="own", source=actor_a)
    assert resp_a2["final_status"] == "done"
    names = _post_tag_names(post_id)
    assert "rev7-shared2" not in names, "a tag no actor claims anymore must actually be reaped"


def test_empty_run_does_not_retract(post_id):
    """R7 (carried from rev6): a status='ok' completion with an EMPTY tags
    list must not retract anything even with reap_scope='own'."""
    resp = _machine_complete(post_id, ["rev7-keep-me"], reap_scope="own")
    assert resp["final_status"] == "done"
    assert "rev7-keep-me" in _post_tag_names(post_id)

    resp2 = _machine_complete(post_id, [], reap_scope="own")  # empty, status still "ok"
    assert resp2["final_status"] == "done"
    names = _post_tag_names(post_id)
    assert "rev7-keep-me" in names, "an empty-tags 'ok' completion must not retract existing tags"
    assert "rev6-upload-tag" in names


def test_empty_run_preserves_claim_snapshot(post_id):
    """round-3 regression (codex task 296b6709): an empty-tags completion
    must leave the actor's PRIOR claim snapshot untouched — not just skip
    reaping post_tag, but also not silently clear post_tag_actor_claim
    (a wrongful clear could enable a wrong future reap by another actor)."""
    import asyncio
    resp = _machine_complete(post_id, ["rev7-snapshot-tag"], reap_scope="own")
    assert resp["final_status"] == "done"
    claims_before = asyncio.run(_actor_claims(post_id, WORKER_SOURCE))
    assert "rev7-snapshot-tag" in claims_before

    resp2 = _machine_complete(post_id, [], reap_scope="own")
    assert resp2["final_status"] == "done"
    claims_after = asyncio.run(_actor_claims(post_id, WORKER_SOURCE))
    assert "rev7-snapshot-tag" in claims_after, (
        "an empty-tags completion must NOT clear the actor's prior claim snapshot"
    )
    assert "rev7-snapshot-tag" in _post_tag_names(post_id)


def test_default_off_never_reaps(post_id):
    """default reap_scope='off': multiple independent per-head completions
    (matching the real trainer-head-gN architecture) must never retract
    anything — this is the exact production incident rev6's emergency fix
    prevented, carried forward under the new field name."""
    resp1 = _machine_complete(post_id, ["rev7-head-a-tag"])  # reap_scope default "off"
    assert resp1["final_status"] == "done"
    resp2 = _machine_complete(post_id, ["rev7-head-b-tag"])  # separate "head", default "off"
    assert resp2["final_status"] == "done"
    names = _post_tag_names(post_id)
    assert "rev7-head-a-tag" in names, (
        "default (reap_scope='off') completion must NOT delete an earlier unrelated head's tag"
    )
    assert "rev7-head-b-tag" in names
    assert "rev6-upload-tag" in names


def test_full_replacement_not_expressible(post_id):
    """rev6's full-replacement reap is removed at the TYPE level, not just
    defaulted off: any reap_scope outside {'off','own'} must be rejected
    by request validation, never silently accepted as some other scope."""
    status, resp = _req(
        "POST", "/api/internal/tagging/complete",
        {"id": post_id, "kind": "auto_tag", "status": "ok",
         "tags": [{"name": "rev7-x"}], "source": WORKER_SOURCE, "reap_scope": "full"},
    )
    assert status == 422, f"reap_scope='full' must be rejected by schema validation, got {status}: {resp}"


def test_legacy_reap_field_ignored(post_id):
    """R5: a legacy payload sending the removed `reap: true` field (an
    older/un-upgraded worker) must not 4xx — pydantic silently ignores the
    unknown field and reap_scope stays at its 'off' default, so no
    retraction occurs despite the stale flag."""
    resp1 = _machine_complete(post_id, ["rev7-legacy-a"])
    assert resp1["final_status"] == "done"
    status, resp2 = _req(
        "POST", "/api/internal/tagging/complete",
        {"id": post_id, "kind": "auto_tag", "status": "ok",
         "tags": [{"name": "rev7-legacy-b"}], "source": WORKER_SOURCE, "reap": True},
    )
    assert status == 200, f"legacy `reap` field must be silently ignored, not rejected: {resp2}"
    names = _post_tag_names(post_id)
    assert "rev7-legacy-a" in names, "legacy `reap: true` must NOT trigger retraction (ignored, not honored)"
    assert "rev7-legacy-b" in names


def test_provenance_host_release_id_recorded(post_id):
    """0072: a completion that reports host/release_id must persist them on
    the post_tag_audit row it writes, and they must be queryable/joinable
    (plain columns, not buried in the opaque `source` string) — this is the
    fix for 'どこの・どのバージョンのタガーが・いつ入れたタグなのか' being
    unanswerable from actor_id alone.

    Uses a real tagger_release id, not an arbitrary int — release_id is a
    genuine FK (0072), so a fabricated id would 500 rather than silently
    accept unlinkable provenance."""
    import asyncio

    async def _any_release_id():
        conn = await _db_conn()
        try:
            return await conn.fetchval("SELECT id FROM tagger_release ORDER BY id LIMIT 1")
        finally:
            await conn.close()

    real_release_id = asyncio.run(_any_release_id())
    assert real_release_id is not None, "test requires at least one tagger_release row to exist"

    resp = _machine_complete(post_id, ["prov-tag"], host="test-host", release_id=real_release_id)
    assert resp["final_status"] == "done"
    assert "prov-tag" in _post_tag_names(post_id)
    prov = asyncio.run(_latest_audit_provenance(post_id))
    assert prov == ("test-host", real_release_id), (
        f"post_tag_audit.host/release_id must reflect the reported values, got {prov}"
    )


def test_provenance_omitted_stays_null(post_id):
    """An older-style completion that omits host/release_id must still write
    an audit row (source alone is enough to trigger the INSERT) with those
    two columns NULL — additive, not required."""
    import asyncio
    resp = _machine_complete(post_id, ["prov-omitted-tag"])
    assert resp["final_status"] == "done"
    prov = asyncio.run(_latest_audit_provenance(post_id))
    assert prov == (None, None), f"omitted host/release_id must persist as NULL, got {prov}"


