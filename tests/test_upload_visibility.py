"""Regression test for the /api/upload visibility contract (broker
task 3faa322a, superseding 5d2b6360): a 2xx from POST /api/upload MUST mean
the post is ALREADY visible to GET /api/posts?query=id:<id> by the time the
response is returned. Silent "200 but invisible for N minutes" (the
860714-860719 incident, 2026-07-13) is the specific failure mode this guards.

Hits the REAL running kura-api service over HTTP — no mocking, no direct DB
access — so it exercises the actual synchronous-materialization code path in
api/routers/upload.py (_synchronous_materialize / materialize_image_bytes),
not a stand-in. Requires kura-api.service to be up (this project's
convention: the production API runs continuously via systemd) and
KURA_USER/KURA_TOKEN credentials (read from api/env, the same credentials
an importer uses for its own uploads).

This repo has no fastapi/sqlalchemy/httpx in the pytest environment (only
system python3's pytest — see tests/test_lora_ops.py, the only prior test
file, which is pure-function unit tests with zero I/O deps) — so this test
deliberately talks plain HTTP via stdlib urllib rather than importing the
app in-process or adding a new test dependency.

Run:  python3 -m pytest tests/test_upload_visibility.py -v
"""

from __future__ import annotations

import base64
import io
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent


def _load_env() -> dict:
    """api/env is systemd EnvironmentFile syntax (KEY=VALUE lines), not
    auto-sourced by a plain `pytest` invocation — parse the bits we need."""
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


def _tiny_png() -> bytes:
    """A small, unique-per-run PNG — synthesized so the test needs no
    external fixture file and never md5-collides with a prior test post
    (which would take the md5-dup branch instead of a fresh insert)."""
    from PIL import Image
    buf = io.BytesIO()
    px = (int(time.time() * 1000) % 256, (int(time.time()) * 7) % 256, 180)
    im = Image.new("RGB", (32, 32), px)
    # a few distinct pixels so it isn't a flat-color degenerate image
    for i in range(8):
        im.putpixel((i, i), (255 - px[0], i * 10 % 256, px[2]))
    im.save(buf, format="PNG")
    return buf.getvalue()


def _multipart_body(boundary: str, fields: dict, filename: str, file_bytes: bytes) -> bytes:
    body = io.BytesIO()
    for name, value in fields.items():
        body.write(
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"\r\n\r\n{value}\r\n".encode()
        )
    body.write(
        (f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; "
         f"filename=\"{filename}\"\r\nContent-Type: image/png\r\n\r\n").encode()
    )
    body.write(file_bytes)
    body.write(f"\r\n--{boundary}--\r\n".encode())
    return body.getvalue()


def _upload(file_bytes: bytes, tags: str = "api-contract-test", collection_url: str | None = None) -> tuple[int, dict]:
    boundary = "----kuraUploadVisibilityTest"
    fields = {"tags": tags, "safety": "safe"}
    if collection_url:
        fields["collection_url"] = collection_url
    body = _multipart_body(
        boundary, fields, "visibility_test.png", file_bytes
    )
    req = urllib.request.Request(
        f"{API_BASE}/api/upload",
        data=body,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}", **_auth_header()},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as e:
        pytest.fail(f"upload failed: HTTP {e.code} {e.read()!r}")


def _query_by_id(post_id: int) -> dict:
    req = urllib.request.Request(
        f"{API_BASE}/api/posts?query=id:{post_id}", headers=_auth_header()
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read())


def _delete_post(post_id: int) -> None:
    req = urllib.request.Request(
        f"{API_BASE}/api/post/{post_id}", headers=_auth_header(), method="DELETE"
    )
    try:
        urllib.request.urlopen(req, timeout=15)
    except Exception:
        pass  # cleanup is best-effort; don't fail the test over it


def _pool_id_by_source(url: str) -> int:
    req = urllib.request.Request(
        f"{API_BASE}/api/pools/by-source?url={urllib.parse.quote(url, safe='')}",
        headers=_auth_header(),
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read())["id"]


def _dedup_scan(pool_id: int, dry_run: bool = False) -> dict:
    flag = "true" if dry_run else "false"
    req = urllib.request.Request(
        f"{API_BASE}/api/pools/{pool_id}/dedup-scan?dry_run={flag}",
        headers=_auth_header(), method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read())


pytestmark = [
    pytest.mark.skipif(not _api_up(), reason=f"kura-api not reachable at {API_BASE}"),
    pytest.mark.skipif(not (KURA_USER and KURA_TOKEN),
                       reason="KURA_USER/KURA_TOKEN not configured (api/env missing?)"),
]


def test_upload_is_immediately_visible():
    """POST /api/upload -> 2xx MUST mean GET /api/posts?query=id:<id> already
    finds it, with NO sleep/retry/poll in between — that IS the contract."""
    status, payload = _upload(_tiny_png())
    post_id = payload["id"]
    try:
        if status == 202:
            # documented escape hatch: the endpoint declared it could NOT
            # materialize synchronously. A tiny in-memory PNG should always
            # take the fast (image) path, so seeing 202 here means the fast
            # path itself regressed — fail loudly rather than silently
            # accept a "sometimes 202" upload endpoint.
            pytest.fail(
                f"upload of a tiny PNG returned 202 (status={payload.get('status')!r}) "
                f"instead of synchronous 200 — the fast path regressed: {payload}"
            )
        assert status == 200, f"unexpected status {status}: {payload}"
        assert payload.get("status") == "ok", f"200 response but post.status != 'ok': {payload}"

        # THE assertion — no sleep, no retry, checked immediately:
        result = _query_by_id(post_id)
        assert result.get("total", 0) >= 1, (
            f"post {post_id} got a 2xx from /api/upload but is NOT visible to "
            f"search immediately after — visibility contract violated: {result}"
        )
        found_ids = {p["id"] for p in result.get("results", [])}
        assert post_id in found_ids, f"post {post_id} not among results: {result}"
    finally:
        _delete_post(post_id)


def test_duplicate_reupload_of_a_stuck_post_is_also_immediately_visible():
    """A re-upload that hits the md5-dup branch must ALSO honor the
    contract: if the original row was somehow still not 'ok', the dup path
    must finish materializing it before returning 2xx, not just early-return
    on the stale metadata. Uploads the SAME bytes twice; the second call
    exercises the dup branch's own _synchronous_materialize call."""
    data = _tiny_png()
    status1, payload1 = _upload(data)
    post_id = payload1["id"]
    try:
        assert status1 == 200 and payload1.get("status") == "ok"

        status2, payload2 = _upload(data)
        assert payload2["id"] == post_id  # same md5 -> same post, dup branch
        assert payload2.get("duplicate") is True
        assert status2 == 200, f"dup re-upload of an already-ok post returned {status2}: {payload2}"
        assert payload2.get("status") == "ok"

        result = _query_by_id(post_id)
        assert result.get("total", 0) >= 1
    finally:
        _delete_post(post_id)


@pytest.mark.skip(reason="fixture path removed: dedup-scan is REPORT-ONLY since "
                         "plan kura-phash-dedup-safety (perceptual auto-demote "
                         "deleted; sha256/md5 is the only auto-dedup key) — the "
                         "test's setup demoted a near_dup VIA dedup-scan, which "
                         "no longer writes. The guarded invariant (md5 re-upload "
                         "must not resurrect a demoted near_dup) needs a new "
                         "fixture path (e.g. human demote API) to be testable.")
def test_md5_dup_of_a_demoted_near_dup_stays_near_dup():
    """Incident b4ba5528 (2026-07-14): the md5-dup branch's materialize guard
    was `existing.status != "ok"`, which is also true for a dedup-scan-demoted
    near_dup row — so a re-crawl re-uploading byte-identical content for an
    already-curated near_dup silently resurrected it to 'ok', undoing pool
    curation on every re-walk. The visibility contract (3faa322a) is about
    rescuing rows that never finished materializing ('pending'), not about
    overriding curation state — the guard must be `== "pending"` instead.

    Builds two distinct-md5, same-perceptual-hash images (same pixels, a PNG
    text chunk added to the second so its bytes/md5 differ), uploads both
    into one fresh pool, dedup-scans that pool to demote one to real
    near_dup via the actual production code path (no direct DB writes), then
    re-uploads the demoted post's exact bytes and asserts status is still
    'near_dup' — not resurrected to 'ok'.
    """
    from PIL import Image, PngImagePlugin

    px = (17, 201, 133)
    im = Image.new("RGB", (32, 32), px)
    for i in range(8):
        im.putpixel((i, i), (255 - px[0], i * 10 % 256, px[2]))

    buf_a = io.BytesIO()
    im.save(buf_a, format="PNG")
    bytes_a = buf_a.getvalue()

    buf_b = io.BytesIO()
    info = PngImagePlugin.PngInfo()
    info.add_text("test-marker", "b4ba5528-regression")
    im.save(buf_b, format="PNG", pnginfo=info)  # same pixels, different bytes/md5
    bytes_b = buf_b.getvalue()
    assert bytes_a != bytes_b, "test fixture bug: the two variants must not md5-collide"

    pool_url = f"manual:pool/test-near-dup-regression-{int(time.time() * 1000)}"
    status_a, payload_a = _upload(bytes_a, collection_url=pool_url)
    post_id_a = payload_a["id"]
    status_b, payload_b = _upload(bytes_b, collection_url=pool_url)
    post_id_b = payload_b["id"]
    try:
        assert status_a == 200 and status_b == 200
        assert post_id_a != post_id_b, "fixtures collided into one post — can't form a dedup-scan cluster"

        pool_id = _pool_id_by_source(pool_url)
        # GET /api/post/{id} doesn't expose the internal `status` field, so
        # identify the demoted id from dedup-scan's own dry-run plan instead
        # of probing status afterward.
        plan = _dedup_scan(pool_id, dry_run=True)
        demoted_ids = {d for entry in plan.get("plan", []) if entry.get("action") == "demote"
                       for d in entry.get("demoted", [])}
        assert demoted_ids & {post_id_a, post_id_b}, f"dry-run plan doesn't cover our fixtures: {plan}"
        demoted_id = post_id_a if post_id_a in demoted_ids else post_id_b
        demoted_bytes = bytes_a if demoted_id == post_id_a else bytes_b

        scan = _dedup_scan(pool_id, dry_run=False)
        assert scan.get("demoted", 0) >= 1, f"dedup-scan did not demote either fixture: {scan}"

        # sanity: search's displayable set is status=='ok' only (list_posts /
        # get_post_index base queries), so a demoted near_dup must have
        # dropped out of `query=id:<id>` search visibility.
        assert _query_by_id(demoted_id).get("total", 0) == 0, (
            f"post {demoted_id} still search-visible right after dedup-scan demoted it"
        )

        # THE regression check: re-upload the demoted post's exact bytes.
        status_redup, payload_redup = _upload(demoted_bytes, collection_url=pool_url)
        assert payload_redup["id"] == demoted_id, "re-upload of identical bytes took a different post_id"
        assert payload_redup.get("duplicate") is True
        assert payload_redup.get("status") == "near_dup", (
            f"REGRESSION (incident b4ba5528): md5-dup re-upload resurrected a "
            f"curated near_dup back to {payload_redup.get('status')!r}: {payload_redup}"
        )

        assert _query_by_id(demoted_id).get("total", 0) == 0, (
            f"REGRESSION (incident b4ba5528): post {demoted_id} became search-visible "
            f"again after the md5-dup re-upload — it was resurrected out of near_dup"
        )
    finally:
        _delete_post(post_id_a)
        _delete_post(post_id_b)
