#!/usr/bin/env python3
"""kura-tagger worker loop.

Claim pending posts via the internal tagging API, run the PixAI +
ml-danbooru (+ WD14 character fallback) pipeline, post the result back
with /complete. Drains the queue then exits 0 so the systemd timer can
re-fire the unit from a clean slate on the next tick.

Environment variables:
    KURA_URL        base URL to kura-api (e.g. https://archive.example.invalid)
    KURA_USER       kura username for Token auth
    KURA_TOKEN      kura user token
    BATCH_LIMIT     posts to claim per round (default 10)
    LEASE_SECONDS   lease duration per claim (default 600)
    MAX_ROUNDS      safety cap on rounds (default 200)
    KURA_MODELS_DIR override ML model cache dir (default ~/.cache/kura-tagger/models)
"""

from __future__ import annotations

from tagger.credentials import token_headers
import glob
import json
import os
import socket
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

from .infer import load_all_models, model_source_id, reap_domain, tag_image
from .pixai_chars import RequiredCharacterComponentError
from .content_fetch import DEFER_SECONDS, ContentUnavailable, classify_urllib_error

HOSTNAME = socket.gethostname()
# Provenance identifier (task 4328fb0e): set once in main() after models
# finish loading (model_source_id() hashes the on-disk model files, so it
# needs load_all_models()/ensure_*() to have run first). None until then --
# claim()/complete() simply omit the field rather than crash.
TAGGER_SOURCE: str | None = None
# post_tag_audit.release_id (0072): the wd14-fixed tagger_release this run
# loaded, when the registry-backed model was available (None if it wasn't --
# load_wd14_fixed() degrades gracefully, see its docstring). Distinct from
# TAGGER_SOURCE, which hashes ALL loaded models (pixai/mldanbooru/wd14/...);
# only wd14-fixed maps 1:1 to a tagger_release row.
TAGGER_RELEASE_ID: int | None = None
# Claim guard identity (plan:kura-tagger-lifecycle-redesign Step 11): this
# is primary's full-ensemble pipeline (PixAI+ml-danbooru+wd14+content-heads),
# which is what tagger_expected_worker.profile='full' means for host=primary.
# A worker reporting the wrong profile for its host is refused the claim
# (409) -- see api/services/tagging.py::enforce_claim_guard.
WORKER_PROFILE = "full"
# 2026-08-04: 2 -> 3。**publish より先に上げて配り切る**のが唯一の安全な順序。
# tagger_governed_publish_release は active generation を必ず +1 するので、
# worker が 2 のまま publish すると active=3 > worker=2 で全ノードが 409 を返し
# fleet が止まる (2026-08-02 に実際に 14.5 時間・503,486 件を滞留させた)。
# worker >= floor なら通るので、worker=3 / active=2 の窓は安全。
WORKER_GENERATION = 5  # 2026-08-08: 4 -> 5, phimosis v2 retirement (chance AUC, serving P≈0.199) を載せる release の先行 bump — publish は active generation を必ず +1 するので、先に配り切るのが唯一の安全な順序 (2026-08-02 の 14.5h 停止の教訓)
WORKER_ID = f"{HOSTNAME}-loop"
TAGGER_PROVIDER: str | None = None
# veto-authority reap (plan:kura-tagger-self-improvement-loop, user 2026-07-22):
# the gate tags this worker's gated-veto applies. Reported on every completion
# so the server can DELETE an over-applied gate tag the veto suppressed (not
# just skip re-adding it). Empty until models load / if no veto heads present.
VETO_GATES: list[str] = []


def _on_ac_power() -> bool:
    """Return True if any AC adapter reports online, or no AC info exists.

    Desktops without a battery have no /sys/class/power_supply/AC*/online entries;
    we assume always-on for those. Laptops report 1=plugged, 0=on battery.
    """
    paths = (
        glob.glob("/sys/class/power_supply/AC*/online")
        + glob.glob("/sys/class/power_supply/ADP*/online")
    )
    if not paths:
        return True
    for p in paths:
        try:
            with open(p) as f:
                if f.read().strip() == "1":
                    return True
        except OSError:
            continue
    return False

from tagger.api_target import api_origin
KURA_URL = api_origin(required=False)
KURA_USER = os.environ.get("KURA_USER", "")
KURA_TOKEN = os.environ.get("KURA_TOKEN", "")
BATCH_LIMIT = int(os.environ.get("BATCH_LIMIT", "10"))
LEASE_SECONDS = int(os.environ.get("LEASE_SECONDS", "600"))
MAX_ROUNDS = int(os.environ.get("MAX_ROUNDS", "200"))
# Idle polling: stay alive (models loaded) while queue is empty.
# Sleep IDLE_POLL_INTERVAL seconds between empty-queue checks, exit after
# IDLE_MAX_WAIT total seconds of idleness. Default 5 min window (10×30s).
IDLE_POLL_INTERVAL = int(os.environ.get("KURA_TAGGER_IDLE_POLL", "30"))
IDLE_MAX_WAIT = int(os.environ.get("KURA_TAGGER_IDLE_MAX_WAIT", "300"))

_ctx = ssl.create_default_context()
_ctx.check_hostname = False
_ctx.verify_mode = ssl.CERT_NONE


def _auth_header() -> str:
    return token_headers(KURA_USER, KURA_TOKEN)['Authorization']


def _req(method: str, path_or_url: str, body: dict | None = None, timeout: int = 60) -> dict:
    url = path_or_url if path_or_url.startswith("http") else f"{KURA_URL}{path_or_url}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers={"Authorization": _auth_header(), "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, context=_ctx, timeout=timeout) as r:
        payload = r.read()
    return json.loads(payload) if payload else {}


def _fetch_bytes(url: str, timeout: int = 60) -> bytes:
    full = url if url.startswith("http") else f"{KURA_URL}{url}"
    req = urllib.request.Request(full, headers={"Authorization": _auth_header()})
    try:
        with urllib.request.urlopen(req, context=_ctx, timeout=timeout) as r:
            return r.read()   # a body shorter than Content-Length raises IncompleteRead
    except Exception as exc:  # noqa: BLE001
        deferred = classify_urllib_error(exc)
        if deferred is not None:
            raise deferred from exc
        raise


def claim(kind: str, limit: int) -> list[dict]:
    params = {"kind": kind, "limit": limit, "lease_seconds": LEASE_SECONDS, "host": HOSTNAME}
    if TAGGER_SOURCE:
        params["source"] = TAGGER_SOURCE
    if kind == "auto_tag":
        # Claim guard identity (Step 11) — required for auto_tag or the
        # claim is refused with 409. See WORKER_PROFILE's comment above.
        params["worker_id"] = WORKER_ID
        params["profile"] = WORKER_PROFILE
        params["generation"] = WORKER_GENERATION
        if TAGGER_RELEASE_ID is not None:
            params["release_id"] = TAGGER_RELEASE_ID
        if TAGGER_PROVIDER:
            params["provider"] = TAGGER_PROVIDER
        if TAGGER_SOURCE:
            params["model_sha256"] = TAGGER_SOURCE
    resp = _req("POST", f"/api/internal/tagging/claim?{urllib.parse.urlencode(params)}")
    return resp.get("claimed", [])


def complete(
    kind: str, item_id: int, status: str, tags: list[dict] | None = None, error: str | None = None
) -> None:
    body = {
        "id": item_id,
        "kind": kind,
        "status": status,
        "tags": tags or [],
        "error": error,
        "source": TAGGER_SOURCE,
        "host": HOSTNAME,
        "release_id": TAGGER_RELEASE_ID,
        # veto-authority reap: gates this worker's veto applies + its code
        # generation (non-repudiation). Only meaningful on status=ok; harmless
        # on failed completions (server ignores an empty output there).
        "veto_gates": VETO_GATES,
        "generation": WORKER_GENERATION,
    }
    _req("POST", "/api/internal/tagging/complete", body)


#: The 404 the complete route answers when the post was deleted after its claim
#: (api/routers/tagging.py). The item is dropped, not reported failed.
POST_GONE = "tagging_post_gone"


def _post_gone(e: urllib.error.HTTPError) -> bool:
    if e.code != 404:
        return False
    try:
        detail = json.loads(e.read() or b"{}").get("detail")
    except ValueError:
        return False
    return isinstance(detail, dict) and detail.get("error") == POST_GONE


def process_one(item: dict, models: dict) -> tuple[bool, str]:
    item_id = item["id"]
    content_url = item.get("content_url")
    if not content_url:
        return False, "no content_url"
    image_bytes = _fetch_bytes(content_url)
    notes: dict = {}
    tags = tag_image(image_bytes, item.get("mime_type") or "", models, notes=notes)
    note = f"{len(tags)} tags"
    cc_review = notes.get("chastity_cage_review")
    if cc_review:
        # chastity_cage Apply Gate の不確実域 [0.35, 0.50): タグは保持しつつ
        # marker tag で人手集約プールへ route する。pool API を直接叩かないのは
        # (a) 稼働中 production API (1.74.0) に pool-membership mutation
        # endpoint が無い (PUT /pools/{id} は 404)、(b) この loop は remote
        # tagger node (trainer 等) でも走り API しか到達できない、ため。
        # marker general tag は既存の complete() 経路にそのまま乗り、API
        # version 非依存・replication 安全。人手は search q=chastity_cage_review
        # で集約し、primary 側 script が後からタグ→pool を実体化できる。
        tags.append({"name": "chastity_cage_review", "category": "general"})
        note += f" (chastity_cage_review prob={cc_review['prob']})"
    cb_review = notes.get("chastity_belt_review")
    if cb_review:
        # chastity_belt Apply Gate の不確実域 — cage と同じ marker-tag 経路
        # (API version 非依存・replication 安全、search q=chastity_belt_review で集約)
        tags.append({"name": "chastity_belt_review", "category": "general"})
        note += f" (chastity_belt_review prob={cb_review['prob']})"
    complete("auto_tag", item_id, "ok", tags=tags)
    return True, note


def main() -> int:
    if not KURA_URL:
        print("ERROR: set KURA_URL or KURA_API before starting the worker", file=sys.stderr)
        return 2
    if not KURA_USER or not KURA_TOKEN:
        print("ERROR: KURA_USER / KURA_TOKEN env required", file=sys.stderr)
        return 2

    if not _on_ac_power():
        print("[kura-tagger] on battery — exiting before model load", flush=True)
        return 0

    print(
        f"[kura-tagger] start url={KURA_URL} user={KURA_USER} "
        f"batch={BATCH_LIMIT} lease={LEASE_SECONDS}s",
        flush=True,
    )

    print("[kura-tagger] loading models (download on first run may take minutes)...", flush=True)
    t0 = time.time()
    models = load_all_models(api_url=KURA_URL, headers={"Authorization": _auth_header()})
    print(f"[kura-tagger] models ready ({time.time() - t0:.1f}s)", flush=True)

    global TAGGER_SOURCE, TAGGER_RELEASE_ID, TAGGER_PROVIDER, VETO_GATES
    TAGGER_SOURCE = model_source_id(models)
    wd14_fixed = models.get("wd14_fixed")
    TAGGER_RELEASE_ID = wd14_fixed["release_id"] if wd14_fixed else None
    TAGGER_PROVIDER = models["wd14_sess"].get_providers()[0]
    # correction-authoritative reap domain: the release's patched rows ∪ veto
    # heads (NOT just the anilingus veto head — user 2026-07-22「最終的に一般化」).
    # Reported per completion so the server can reap a stale corrected tag this
    # run leaves out of its output. Data-driven, grows with the release.
    VETO_GATES = reap_domain(wd14_fixed, models.get("veto_heads"), models.get("character_heads"))
    print(f"[kura-tagger] source={TAGGER_SOURCE} release_id={TAGGER_RELEASE_ID} "
          f"provider={TAGGER_PROVIDER} reap_gates={VETO_GATES}", flush=True)

    total_ok = 0
    total_fail = 0
    round_num = 0
    idle_waited = 0
    for round_num in range(1, MAX_ROUNDS + 1):
        if not _on_ac_power():
            print(f"[kura-tagger] on battery at round {round_num} — graceful exit", flush=True)
            break
        # Cooperative yield (plan:gpu-batch-admission-policy): an admission gate
        # supervising this worker touches the flag file when the GPU should be
        # handed back (user returned / pressure); we exit at the round head.
        # Local file only — no broker coupling. Inert unless the env var is set.
        yield_flag = os.environ.get("KURA_TAGGER_YIELD_FLAG", "")
        if yield_flag and os.path.exists(yield_flag):
            print(f"[kura-tagger] yield flag present ({yield_flag}) — graceful exit", flush=True)
            break
        items = claim("auto_tag", BATCH_LIMIT)
        if not items:
            if idle_waited >= IDLE_MAX_WAIT:
                break
            time.sleep(IDLE_POLL_INTERVAL)
            idle_waited += IDLE_POLL_INTERVAL
            round_num -= 1  # don't count idle rounds against MAX_ROUNDS
            continue
        idle_waited = 0
        print(f"[round {round_num}] claimed {len(items)} auto_tag", flush=True)
        for item in items:
            try:
                ok, note = process_one(item, models)
                if ok:
                    total_ok += 1
                    print(f"  [OK]   post {item['id']} — {note}", flush=True)
                else:
                    total_fail += 1
                    complete("auto_tag", item["id"], "failed", error=note)
                    print(f"  [FAIL] post {item['id']} — {note}", flush=True)
            except ContentUnavailable as exc:
                # Infrastructure, not this post (tagger/content_fetch.py): no completion for
                # it or the rest of the batch; their leases expire uncounted. Then wait.
                print(f"[DEFER] post {item['id']}: content unavailable ({exc}); batch stopped, "
                      f"no completion sent; leases expire within {LEASE_SECONDS}s; "
                      f"retry in {DEFER_SECONDS}s", flush=True)
                time.sleep(DEFER_SECONDS)
                break
            except RequiredCharacterComponentError as exc:
                print(f"[DEFER] post {item['id']}: {exc}; batch stopped, no completion sent; "
                      f"leases expire within {LEASE_SECONDS}s; reload components on next worker start", flush=True)
                return 1
            except urllib.error.HTTPError as e:
                if _post_gone(e):
                    # Deleted after the claim: a failed completion would meet the same 404.
                    print(f"  [GONE] post {item['id']} — deleted after claim, dropped", flush=True)
                    continue
                total_fail += 1
                err = f"HTTP {e.code}: {e.reason}"
                try:
                    complete("auto_tag", item["id"], "failed", error=err[:500])
                except Exception:
                    pass
                print(f"  [FAIL] post {item['id']} — {err}", flush=True)
            except Exception as e:
                total_fail += 1
                err = f"{type(e).__name__}: {e}"
                try:
                    complete("auto_tag", item["id"], "failed", error=err[:500])
                except Exception:
                    pass
                print(f"  [FAIL] post {item['id']} — {err}", flush=True)

    print(
        f"[kura-tagger] done. rounds={round_num} ok={total_ok} fail={total_fail}",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
