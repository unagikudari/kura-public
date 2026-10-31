# Description: Phase 2b — perceptual cull + WebP transcode/thumbnail + sha256 into post.data (plan kura-pg-native-media-schema rev4)
# Kind: oneshot
# Effects: mutating (post.data/thumbnail/sha256/status/media_location/duplicate_of/perceptual_distance)
# Requires: Phase 2a complete (perceptual_hash populated; status set), migrations <=0029
# Runs-on: primary
# OnChange: none
# Version: 0.1
"""
Phase 2b of the pg-native media migration: CULL + MATERIALIZE.

Prereq: Phase 2a finished (every post has a status; images carry perceptual_hash;
corrupt/lost are terminal). This pass touches only status='pending' posts.

Pass 1 — perceptual cull (in-memory multi-index hashing, DB-only, no file I/O):
  Cluster pending IMAGES by dHash near-duplicate. 4x16-bit bands; a near-dup at
  Hamming <= THRESHOLD shares >=1 exact band when THRESHOLD <= 3 (pigeonhole over
  4 bands), so default THRESHOLD=3 makes the band index COMPLETE (no missed pairs).
  Pairs also must match aspect ratio (within ASPECT_TOL) to avoid crop/letterbox
  false merges. Each cluster keeps the highest-resolution member (tiebreak: lowest
  id) as the representative; the rest become status='near_dup', duplicate_of=rep,
  perceptual_distance=Hamming(self,rep), data stays NULL. Degenerate band buckets
  larger than BUCKET_CAP (near-flat images sharing a trivial slice) are skipped and
  COUNTED (no silent truncation) — those images simply stay their own rep.

Pass 2 — materialize survivors (representatives + videos + non-image), multiprocess:
  - image: read original -> WebP (long-edge cap CAP, q WEBP_Q) into post.data, WebP
    thumbnail (THUMB px, q THUMB_Q) into post.thumbnail, sha256(original) ->
    status='ok', media_location='pg'.
  - video (mp4/webm): sha256(file). Thumbnail = the EXISTING HDD poster
    (generated-thumbnails/<id>_*, taken at a good timestamp — NOT re-extracted, to
    avoid black opening frames) transcoded to WebP. data=NULL, media_location='hdd',
    status='ok'. Body streamed from HDD by the serving layer.
  - pdf/other non-image: same as video (HDD poster -> WebP if present; data NULL;
    media_location='hdd'; sha256; status='ok').
  Originals on the HDD are never modified or deleted (the cull is reversible).

Idempotent/resumable: re-running re-clusters remaining pending and re-materializes;
already-ok/dup rows are untouched. Dry-run reports cluster stats and transcodes a
small sample without writing.

Usage (primary, AFTER 2a completes):
  python3 scripts/backfill_media.py                       # dry-run: cluster stats + sample transcode
  python3 scripts/backfill_media.py --apply --workers 8   # full run
  python3 scripts/backfill_media.py --apply --threshold 3 --cap 1600 --webp-q 82 --workers 8
"""

import argparse
import asyncio
import hashlib
import io
import os
import sys
import time
from concurrent.futures import ProcessPoolExecutor

from PIL import Image, ImageFile
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

ImageFile.LOAD_TRUNCATED_IMAGES = False

# api/services/media_materialize.py holds the SAME WebP-encode math the
# synchronous upload path (api/routers/upload.py) uses, so a post touched by
# either path is byte-identical. Script invocation (`python3
# scripts/backfill_media.py`) only puts scripts/ on sys.path, not the repo
# root — add it explicitly (same shim as scripts/backfill_ok_null_phash_*.py).
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)
from api.services.media_materialize import webp_bytes as _shared_webp_bytes  # noqa: E402
from api.services import renditions  # noqa: E402
from api.services.media_materialize import animated_webp_bytes as _shared_animated_webp_bytes  # noqa: E402

# Non-'ok' rows that are nonetheless DONE — the cull must never re-touch these
# (near_dup/exact_dup point at a representative; corrupt/lost have no usable
# source). Everything else non-'ok' is fair game for the backstop, regardless
# of which specific column-combination (status='pending', status IS NULL, or
# some future value) caused it to fall through — see kura-visibility-contract
# (broker task 3faa322a): a narrow status='pending'-only predicate is
# exactly what let the 2026-07-12 status=NULL bug wedge 385 rows fleet-wide.
TERMINAL_STATES = ("exact_dup", "near_dup", "corrupt", "lost")
NOT_OK_NOT_TERMINAL = (
    "status IS DISTINCT FROM 'ok' AND "
    "(status IS NULL OR status NOT IN ('exact_dup', 'near_dup', 'corrupt', 'lost'))"
)

DATABASE_URL = os.environ.get(
    "DATABASE_URL", "postgresql+asyncpg://kura:kura@localhost:5432/kura"
)
# Prefer LOCAL ext4 (fast, parallelizes) and fall back to the NTFS HDD backup.
# kura#43: 既定は「この checkout の data-images/」。退役ホスト desk の
# 旧 home 配下の kura/... を既定にしていたので、env を渡し忘れると存在しない
# path を見に行って「1 枚も見つからない」形で黙って空振りしていた。
LOCAL_POSTS = os.environ.get(
    "KURA_LOCAL_POSTS", os.path.join(_REPO_ROOT, "data-images", "posts"))
LOCAL_THUMBS = os.environ.get(
    "KURA_LOCAL_THUMBS", os.path.join(_REPO_ROOT, "data-images", "generated-thumbnails"))
HDD_POSTS = os.environ.get("KURA_HDD_POSTS", "/mnt/d/kura-image-backup/posts")
HDD_THUMBS = os.environ.get("KURA_HDD_THUMBS", "/mnt/d/kura-image-backup/generated-thumbnails")

DECODABLE_EXT = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".tif", ".tiff"}

# tuned via CLI; module globals so pool workers inherit them
CAP = 1600
WEBP_Q = 82
THUMB = 320
THUMB_Q = 75


# ── Pass 2 worker (pure CPU, no DB) ─────────────────────────────────────────
def materialize_one(task):
    """task = (id, kind, orig_path|None, thumb_path|None). Returns row dict or error."""
    pid, kind, orig, thumb_path = task
    try:
        if orig is None:
            return {"id": pid, "error": "no_original"}
        sha = hashlib.sha256()
        with open(orig, "rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                sha.update(chunk)
        sha256 = sha.digest()

        ext = os.path.splitext(orig)[1].lower()
        if ext in DECODABLE_EXT:
            # image representative: WebP full-view + WebP thumbnail, body in PG.
            # 2026-07-21 fix: a plain im.convert("RGB") + single-frame .save()
            # silently collapses an animated GIF/WebP to its first frame — this
            # loop independently reimplemented the same bug that
            # api/routers/upload.py had via materialize_image_bytes ("GIFが
            # 動かない", user report). is_animated (not the .gif extension
            # alone) also catches animated WebP uploads.
            with Image.open(orig) as im:
                if getattr(im, "is_animated", False):
                    full = _shared_animated_webp_bytes(im, CAP, WEBP_Q)
                    im.seek(0)
                    thumb = _webp(im.convert("RGB"), THUMB, THUMB_Q)
                else:
                    im = im.convert("RGB")
                    full = _webp(im, CAP, WEBP_Q)
                    thumb = _webp(im, THUMB, THUMB_Q)
            # data is now WebP — the mime-type column must say so (it drives the
            # download extension + API mimeType; the body served from PG is
            # already image/webp). Without this it stayed at the original jpeg/png.
            return {"id": pid, "status": "ok", "data": full, "thumbnail": thumb,
                    "sha256": sha256, "media_location": "pg", "mime": "image/webp"}
        else:
            # video / pdf: body stays on HDD; thumbnail from the existing HDD poster
            thumb = None
            if thumb_path and os.path.exists(thumb_path):
                try:
                    with Image.open(thumb_path) as tim:
                        thumb = _webp(tim.convert("RGB"), THUMB, THUMB_Q)
                except Exception:
                    thumb = None
            # video/pdf: body stays on HDD in its original format — keep its mime.
            return {"id": pid, "status": "ok", "data": None, "thumbnail": thumb,
                    "sha256": sha256, "media_location": "hdd", "mime": None}
    except Exception as e:
        return {"id": pid, "error": f"{type(e).__name__}: {e}"}


def _webp(im, cap, q) -> bytes:
    """Thin wrapper — the actual resize+encode lives in
    api/services/media_materialize.py so this periodic backstop and the
    synchronous upload path always produce byte-identical output."""
    return _shared_webp_bytes(im, cap, q)


# ── Pass 1 helpers ──────────────────────────────────────────────────────────
def _bands(h: int):
    return ((h >> 0) & 0xFFFF, (h >> 16) & 0xFFFF, (h >> 32) & 0xFFFF, (h >> 48) & 0xFFFF)


class UF:
    def __init__(self): self.p = {}
    def find(self, x):
        self.p.setdefault(x, x)
        while self.p[x] != x:
            self.p[x] = self.p[self.p[x]]; x = self.p[x]
        return x
    def union(self, a, b):
        ra, rb = self.find(a), self.find(b)
        if ra != rb: self.p[ra] = rb


def build_index(d):
    idx = {}
    # The HDD index is an optional fallback (KURA_HDD_POSTS); on native nodes the
    # WSL `/mnt/d` backup path doesn't exist. A missing dir must be a no-op, not a
    # crash — mirrors backfill_fingerprints' "hdd=0 fallback" tolerance.
    if not os.path.isdir(d):
        return idx
    with os.scandir(d) as it:
        for e in it:
            n = e.name; us = n.find("_")
            if us > 0 and n[:us].isdigit():
                idx[int(n[:us])] = e.path
    return idx


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--threshold", type=int, default=3, help="max Hamming for near-dup (<=3 keeps 4-band MIH complete)")
    ap.add_argument("--aspect-tol", type=float, default=0.02)
    ap.add_argument("--bucket-cap", type=int, default=2000, help="skip degenerate band buckets larger than this")
    ap.add_argument("--cap", type=int, default=1600)
    ap.add_argument("--webp-q", type=int, default=82)
    ap.add_argument("--thumb", type=int, default=320)
    ap.add_argument("--thumb-q", type=int, default=75)
    ap.add_argument("--workers", type=int, default=max(2, (os.cpu_count() or 4) - 1))
    ap.add_argument("--batch", type=int, default=500)
    ap.add_argument("--sample", type=int, default=200, help="dry-run: transcode this many to report sizes")
    args = ap.parse_args()
    global CAP, WEBP_Q, THUMB, THUMB_Q
    CAP, WEBP_Q, THUMB, THUMB_Q = args.cap, args.webp_q, args.thumb, args.thumb_q

    eng = create_async_engine(DATABASE_URL)
    async with eng.connect() as conn:
        todo = (await conn.execute(text("SELECT count(*) FROM post WHERE status IS NULL"))).scalar_one()
        if todo:
            # NOTE: this used to `return 2` (abort the ENTIRE pass, including
            # Pass 2 materialization of unrelated already-pending rows) when
            # --apply. That single guard is what let ONE post.status=NULL bug
            # wedge 385 unrelated rows fleet-wide for hours (2026-07-12
            # incident — see kura-visibility-contract, broker task
            # 3faa322a). A NULL/odd status is now just an incompletely-
            # processed row like any other: Pass 1 below skips it (no
            # perceptual_hash yet — self-heals once Phase 2a fingerprints it
            # on a later run) and Pass 2's broadened predicate materializes it
            # unconditionally instead of gating the whole run on it.
            print(f"NOTE: {todo:,} posts still status NULL — Phase 2a fingerprinting "
                  f"incomplete for them. Pass 1 near-dup clustering skips these this "
                  f"round (no perceptual_hash yet); Pass 2 below still materializes "
                  f"them (does not require a hash). Not aborting.")

        # ── Pass 1: cluster pending images ──────────────────────────────────
        rows = (await conn.execute(text(
            f"SELECT id, perceptual_hash, image_width, image_height FROM post "
            f"WHERE {NOT_OK_NOT_TERMINAL} AND perceptual_hash IS NOT NULL"))).all()
        print(f"pending images to cluster: {len(rows):,}")
        items = []
        buckets = [dict(), dict(), dict(), dict()]
        for r in rows:
            h = int.from_bytes(r.perceptual_hash, "big")
            w, ht = r.image_width or 0, r.image_height or 0
            asp = (w / ht) if ht else 0.0
            items.append((r.id, h, w * ht, asp))
            for i, bv in enumerate(_bands(h)):
                buckets[i].setdefault(bv, []).append(len(items) - 1)

        uf = UF()
        skipped_buckets = 0
        for i in range(4):
            for bv, members in buckets[i].items():
                if len(members) > args.bucket_cap:
                    skipped_buckets += 1
                    continue
                for a in range(len(members)):
                    ia = members[a]; _, ha, _, aspa = items[ia]
                    for b in range(a + 1, len(members)):
                        ib = members[b]; _, hb, _, aspb = items[ib]
                        if (ha ^ hb).bit_count() <= args.threshold and \
                           (aspa == 0 or aspb == 0 or abs(aspa - aspb) <= args.aspect_tol * max(aspa, aspb)):
                            uf.union(ia, ib)

        clusters = {}
        for idx_i in range(len(items)):
            clusters.setdefault(uf.find(idx_i), []).append(idx_i)
        dup_assignments = []  # (dup_id, rep_id, distance)
        multi = 0
        for members in clusters.values():
            if len(members) == 1:
                continue
            multi += 1
            rep = max(members, key=lambda m: (items[m][2], -items[m][0]))  # max pixels, then min id
            rep_id, rep_h = items[rep][0], items[rep][1]
            for m in members:
                if m == rep:
                    continue
                dup_assignments.append((items[m][0], rep_id, (items[m][1] ^ rep_h).bit_count()))

        # Gallery/pool pages are curated works — never cull a pool member as a
        # perceptual near_dup of a post in another work. ("The same work running
        # in Weekly Shōnen Jump AND in the collected tankōbon is not a
        # duplicate.") Keep every pool member; only standalone (pool-less) posts
        # dedup against each other. Without this, low-detail manga/text pages
        # dHash-collide across galleries and whole pools collapse to 1-2 pages.
        pool_members = {
            r.post_id for r in (await conn.execute(text(
                "SELECT DISTINCT post_id FROM pool_post"))).all()
        }
        before = len(dup_assignments)
        dup_assignments = [d for d in dup_assignments if d[0] not in pool_members]
        print(f"exempted {before - len(dup_assignments):,} pool-member pages from cross-pool cull")

        print(f"clusters with >1 member: {multi:,}   near_dup rows: {len(dup_assignments):,}   "
              f"degenerate buckets skipped(>{args.bucket_cap}): {skipped_buckets}")
        reps_img = len(rows) - len(dup_assignments)
        print(f"image representatives to keep: {reps_img:,}")

        if args.apply and dup_assignments:
            for i in range(0, len(dup_assignments), 1000):
                chunk = dup_assignments[i:i + 1000]
                await conn.execute(text(
                    "UPDATE post SET status='near_dup', duplicate_of=:r, perceptual_distance=:d, data=NULL "
                    "WHERE id=:i"),
                    [{"i": d, "r": r, "d": dist} for (d, r, dist) in chunk])
            await conn.commit()
            print(f"marked {len(dup_assignments):,} near_dup")

        # ── Pass 2: materialize remaining pending (reps + video + nonimg) ────
        # local ext4 first (fast), HDD backup as fallback.
        lposts, hposts = build_index(LOCAL_POSTS), build_index(HDD_POSTS)
        lthumbs, hthumbs = build_index(LOCAL_THUMBS), build_index(HDD_THUMBS)
        print(f"indexed posts local={len(lposts):,}/hdd={len(hposts):,}  "
              f"thumbs local={len(lthumbs):,}/hdd={len(hthumbs):,}")
        survivors = (await conn.execute(text(
            f"SELECT id, type FROM post WHERE {NOT_OK_NOT_TERMINAL} ORDER BY id"))).all()
        tasks = []
        for r in survivors:
            orig = lposts.get(r.id) or hposts.get(r.id)
            ext = os.path.splitext(orig)[1].lower() if orig else ""
            kind = "image" if ext in DECODABLE_EXT else "other"
            tasks.append((r.id, kind, orig, lthumbs.get(r.id) or hthumbs.get(r.id)))
        if not args.apply:
            tasks = tasks[:args.sample]
        print(f"\nPass 2 targets (status=ok survivors): {len(survivors):,}  "
              f"{'(sample '+str(len(tasks))+')' if not args.apply else ''}  workers={args.workers}")

        # sha256 identity set (load existing 'ok' first so resume is collision-safe).
        # A colliding sha256 = a byte-identical original the perceptual cull missed;
        # mark it exact_dup rather than violating ux_post_sha256_ok (WHERE status='ok'),
        # which is what crashed the prior run.
        #
        # Only load it when Pass 2 has something to materialize (2026-09-16, incident
        # friction/slow-query:primary). The node-local cull timer runs this every 10 min and the
        # steady state is "Pass 2 targets: 0" — so 144 times a day we were reading
        # 1,040,160 rows out of a 2.4GB heap (842ms, ~900MB resident) to build a map
        # the loop below never consults. It was the single biggest entry in
        # pg_stat_statements by total time (1,580 calls / 1,331s) and none of it was
        # work anyone was waiting on. When there ARE targets the load stays: its cost
        # is proportional to the identity set it must not lose.
        seen: dict[bytes, int] = {}
        if tasks:
            seen = {bytes(r.sha256): r.id for r in (await conn.execute(text(
                "SELECT id, sha256 FROM post WHERE status='ok' AND sha256 IS NOT NULL"))).all()}
            print(f"loaded {len(seen):,} existing ok sha256 (collision -> exact_dup)")

        t0 = time.time(); done = 0; errs = 0; pg = 0; hdd_loc = 0; exact = 0; bytes_pg = 0
        loop = asyncio.get_running_loop()
        with ProcessPoolExecutor(max_workers=args.workers) as ex:
            for i in range(0, len(tasks), args.batch):
                chunk = tasks[i:i + args.batch]
                results = await asyncio.gather(*[loop.run_in_executor(ex, materialize_one, t) for t in chunk])
                ok_writes = []; dup_writes = []
                for res in results:
                    done += 1
                    if "error" in res:
                        errs += 1; continue
                    sha = res["sha256"]
                    prior = seen.get(sha)
                    if prior is not None and prior != res["id"]:
                        exact += 1
                        dup_writes.append({"id": res["id"], "dup": prior, "sha256": sha})
                        continue
                    seen[sha] = res["id"]
                    if res["media_location"] == "pg":
                        pg += 1; bytes_pg += len(res["data"] or b"")
                    else:
                        hdd_loc += 1
                    ok_writes.append(res)
                if args.apply:
                    if ok_writes:
                        # Display bytes become rendition files in this same transaction
                        # (docs/notes/rendition-store-ideal.md); the columns stay NULL.
                        for w in ok_writes:
                            await renditions.attach_bytes(
                                conn, w["id"], {"view": w["data"], "thumb": w["thumbnail"]},
                                renditions.LEGACY_RECIPE)
                        await conn.execute(text(
                            "UPDATE post SET status='ok', "
                            "sha256=:sha256, media_location=:media_location, "
                            "\"mime-type\"=COALESCE(:mime, \"mime-type\") WHERE id=:id"), ok_writes)
                    if dup_writes:
                        await conn.execute(text(
                            "UPDATE post SET status='exact_dup', duplicate_of=:dup, sha256=:sha256, "
                            "data=NULL, thumbnail=NULL, media_location=NULL WHERE id=:id"), dup_writes)
                    await conn.commit()
                if done % 2000 < args.batch:
                    el = time.time() - t0
                    print(f"  materialized {done:,}/{len(tasks):,}  {done/max(el,1):.0f}/s  "
                          f"pg={pg:,} hdd={hdd_loc:,} exact_dup={exact:,} err={errs}  pg_bytes={bytes_pg/1e9:.1f}GB", flush=True)

        el = time.time() - t0
        print(f"\n{'APPLIED' if args.apply else 'DRY-RUN'} pass2: {done:,} in {el:,.0f}s  "
              f"pg(image)={pg:,} hdd(video/doc)={hdd_loc:,} exact_dup={exact:,} errors={errs}")
        if pg:
            print(f"  avg PG image bytes = {bytes_pg/pg/1024:.0f}KB  "
                  f"projected total for {reps_img:,} images = {bytes_pg/max(pg,1)*reps_img/1e9:.0f}GB")

        # ── Orphan detection (kura-visibility-contract, broker task
        # 3faa322a) ──────────────────────────────────────────────────────────
        # This timer (the node-local cull timer, every 10min) is now a BACKSTOP —
        # POST /api/upload materializes synchronously in the common case, so
        # anything still stuck here after a full Pass 1 + Pass 2 run AND
        # older than 30min is a genuine anomaly (a bug fell through both the
        # sync path and this backstop), not routine pipeline lag. Staleness
        # is measured off creation_time, not last_edit_time/an updated_at
        # column: neither this script's UPDATE nor Pass 1's near_dup UPDATE
        # touch last_edit_time, so it would never reflect "still stuck since
        # X" — creation_time is the only column that actually ages with the
        # problem. Runs on every invocation (apply and dry-run) so a stray
        # cron/manual dry-run still surfaces the count; only --apply's
        # ExecStart is on the timer, so in practice this fires every 10min.
        orphan_rows = (await conn.execute(text(
            f"SELECT id, creation_time FROM post WHERE {NOT_OK_NOT_TERMINAL} "
            f"AND creation_time < (now() AT TIME ZONE 'utc') - INTERVAL '30 minutes' "
            f"ORDER BY creation_time LIMIT 20"))).all()
        orphan_count = (await conn.execute(text(
            f"SELECT count(*) FROM post WHERE {NOT_OK_NOT_TERMINAL} "
            f"AND creation_time < (now() AT TIME ZONE 'utc') - INTERVAL '30 minutes'"))).scalar_one()
        if orphan_count:
            oldest_age_s = (await conn.execute(text(
                f"SELECT EXTRACT(EPOCH FROM (now() AT TIME ZONE 'utc') - MIN(creation_time)) "
                f"FROM post WHERE {NOT_OK_NOT_TERMINAL} "
                f"AND creation_time < (now() AT TIME ZONE 'utc') - INTERVAL '30 minutes'"))).scalar_one()
            print(f"\nORPHAN CHECK: {orphan_count:,} post(s) stuck non-ok/non-terminal for "
                  f">30min (oldest {oldest_age_s/60:.0f}min) — sample ids "
                  f"{[r.id for r in orphan_rows]}")
            _emit_orphan_signal(orphan_count, [r.id for r in orphan_rows], float(oldest_age_s or 0))
        else:
            print("\nORPHAN CHECK: 0 posts stuck non-ok/non-terminal for >30min")
            _resolve_orphan_signal()
    await eng.dispose()
    return 0


def _broker_headers() -> dict:
    key = os.environ.get("BROKER_API_KEY", "")
    return {"Content-Type": "application/json", **({"X-API-Key": key} if key else {})}


def _emit_orphan_signal(count: int, sample_ids: list[int], oldest_age_s: float) -> None:
    """Best-effort POST to the real broker's /signals/upsert.

    Mirrors the import worker's emit_shortfall_signal() pattern —
    kura's own in-process broker-task-client (scripts/kura_task_client.py) is
    a documented no-op for non-task payloads, so this talks directly to
    BROKER_URL like that worker does. A broker outage must NEVER
    break this pipeline: every failure mode here is swallowed. Uses stdlib
    urllib (not `requests`) because the kura-api venv this script runs under
    does not have `requests` installed (only scripts run under system
    python3 do).
    """
    broker_url = os.environ.get("BROKER_URL", "").rstrip("/")
    if not broker_url:
        return
    import json
    import urllib.error
    import urllib.request
    try:
        urgency = 1 if (count > 50 or oldest_age_s > 2 * 3600) else 2
        payload = {
            "kind": "friction",
            "signal_key": "kura-orphan-posts",
            "urgency": urgency,
            "delta": 1,
            "status": "active",
            "payload": {
                "orphan_count": count,
                "sample_ids": sample_ids,
                "oldest_age_minutes": round(oldest_age_s / 60, 1),
                "source": "kura-cull (scripts/backfill_media.py)",
                "note": ("posts with status not 'ok' and not a terminal state "
                         "(exact_dup/near_dup/corrupt/lost), unmaterialized for "
                         ">30min despite the sync-upload path and this 10min "
                         "backstop both having a chance to fix them"),
            },
        }
        req = urllib.request.Request(
            f"{broker_url}/signals/upsert",
            data=json.dumps(payload).encode("utf-8"),
            headers=_broker_headers(),
            method="POST",
        )
        urllib.request.urlopen(req, timeout=10).read()
    except Exception:
        pass


def _resolve_orphan_signal() -> None:
    """Best-effort PATCH status=resolved when the orphan count returns to 0.
    404 (no active signal to resolve) is expected and swallowed like any
    other failure — this is cleanup, not an assertion."""
    broker_url = os.environ.get("BROKER_URL", "").rstrip("/")
    if not broker_url:
        return
    import json
    import urllib.error
    import urllib.request
    try:
        req = urllib.request.Request(
            f"{broker_url}/signals/friction/kura-orphan-posts",
            data=json.dumps({"status": "resolved"}).encode("utf-8"),
            headers=_broker_headers(),
            method="PATCH",
        )
        urllib.request.urlopen(req, timeout=10).read()
    except Exception:
        pass


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
