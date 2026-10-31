"""INV-M negative test (plan kura-tagger-audit-and-remediation fe46c5fa,
Step C / migration 0057): a machine completion must NOT re-add a tag a human
explicitly removed from a post.

This is the regression test for the 6348/6186 incident (b4ba5528): the user
hand-judged pool posts and removed false-positive tags, then an auto-tag
worker re-completed the same posts and silently re-added the removed tags —
destroying the human judgment. The plan's approval condition is that this
exact sequence is now mechanically impossible (DB trigger + server-side
pre-filter, not prompt warnings).

Hits the REAL running kura-api over HTTP (this repo's test convention — see
tests/test_upload_visibility.py: no fastapi/sqlalchemy in the pytest env, so
stdlib urllib against the live systemd service). Requires kura-api.service up
and KURA_USER/KURA_TOKEN (api/env).

Run:  python3 -m pytest tests/test_invm_human_pin.py -v
"""

from __future__ import annotations

import base64
import io
import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent

# Fixed name: reused across runs so the test doesn't accumulate one junk tag
# row per invocation (posts are deleted in teardown; the tag row remains with
# post_count 0 and is recycled by the next run).
TEST_TAG = "invm_negative_test_tag"
WORKER_SOURCE = "invm-test-worker"


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
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read()
        return resp.status, (json.loads(raw) if raw else {})


def _tiny_png() -> bytes:
    from PIL import Image
    buf = io.BytesIO()
    px = (int(time.time() * 1000) % 256, (int(time.time()) * 7) % 256, 60)
    im = Image.new("RGB", (32, 32), px)
    for i in range(8):
        im.putpixel((i, i), (255 - px[0], i * 10 % 256, px[2]))
    im.save(buf, format="PNG")
    return buf.getvalue()


def _upload_post() -> int:
    boundary = "----kuraInvmPinTest"
    file_bytes = _tiny_png()
    body = io.BytesIO()
    for name, value in {"tags": "api-contract-test", "safety": "safe"}.items():
        body.write(
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"\r\n\r\n{value}\r\n".encode()
        )
    body.write(
        (f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; "
         f"filename=\"invm_pin_test.png\"\r\nContent-Type: image/png\r\n\r\n").encode()
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


def _post_version(post_id: int) -> int:
    _, post = _req("GET", f"/api/post/{post_id}")
    return post["version"]


def _human_set_tags(post_id: int, tags: list[str]) -> None:
    """The human tag-edit path (PUT /post/{id}) — full replacement + diff audit."""
    status, _ = _req(
        "PUT", f"/api/post/{post_id}",
        {"version": _post_version(post_id), "tags": tags},
    )
    assert status == 200


def _machine_complete(post_id: int, tag: str) -> dict:
    """A worker completion, exactly as tagger/loop.py sends it."""
    status, resp = _req(
        "POST", "/api/internal/tagging/complete",
        {"id": post_id, "kind": "auto_tag", "status": "ok",
         "tags": [{"name": tag}], "source": WORKER_SOURCE},
    )
    assert status == 200
    return resp


@pytest.fixture()
def post_id():
    if not _api_up():
        pytest.skip(f"kura-api not reachable at {API_BASE}")
    if not (KURA_USER and KURA_TOKEN):
        pytest.skip("KURA_USER/KURA_TOKEN not configured")
    pid = _upload_post()
    yield pid
    # STRICT teardown, deliberately not try/except: both tests leave
    # state='present' pins on the post, so this DELETE is itself the
    # regression check that migration 0057's FK-cascade change keeps
    # DELETE /post/{id} working for pinned posts (gate-review finding:
    # a swallowed teardown would hide exactly that breakage and leak
    # undeletable posts into the production DB).
    status, _ = _req("DELETE", f"/api/post/{pid}")
    # delete_post is declared status_code=204 (No Content)
    assert status == 204, f"DELETE /post/{pid} failed on a pinned post (INV-M cascade regression)"


def test_machine_cannot_readd_human_removed_tag(post_id):
    """The 6348/6186 sequence, end to end."""
    # 1. machine tags the post
    resp = _machine_complete(post_id, TEST_TAG)
    assert resp["final_status"] == "done"
    assert TEST_TAG in _post_tag_names(post_id), "machine tag should apply on a fresh post"

    # 2. human removes the tag (judgment)
    remaining = sorted(_post_tag_names(post_id) - {TEST_TAG})
    _human_set_tags(post_id, remaining)
    assert TEST_TAG not in _post_tag_names(post_id)

    # 3. THE NEGATIVE TEST: worker re-completes the same tag -> must NOT be written
    resp = _machine_complete(post_id, TEST_TAG)
    assert resp["final_status"] == "done", "completion itself must still succeed"
    assert TEST_TAG not in _post_tag_names(post_id), (
        "INV-M violated: machine re-added a human-removed tag "
        "(the 6348/6186 incident would reproduce)"
    )
    # and the skip is visible, not silent
    assert resp.get("tags_skipped_pinned", 0) >= 1

    # 4. the human CAN reverse their own decision (pin must not block humans)
    _human_set_tags(post_id, sorted(_post_tag_names(post_id) | {TEST_TAG}))
    assert TEST_TAG in _post_tag_names(post_id), "human re-add must override the absent pin"

    # 5. and after the human re-added it, a machine completion stays a no-op
    #    but harmless (tag already present, pin now state=present)
    resp = _machine_complete(post_id, TEST_TAG)
    assert resp["final_status"] == "done"
    assert TEST_TAG in _post_tag_names(post_id)


def test_human_add_then_machine_complete_keeps_tag(post_id):
    """Human-added tag survives a later machine completion that omits it
    (machines only add tags today, so this documents the add-path half of
    INV-M: nothing in the completion flow may remove the human's tag)."""
    _human_set_tags(post_id, sorted(_post_tag_names(post_id) | {TEST_TAG}))
    assert TEST_TAG in _post_tag_names(post_id)

    resp = _machine_complete(post_id, "api-contract-test")  # unrelated tag
    assert resp["final_status"] == "done"
    assert TEST_TAG in _post_tag_names(post_id), "human-added tag must survive machine completion"
