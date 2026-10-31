# Description: Phase 2a — perceptual_hash/bands + corrupt/lost detection over HDD originals (plan kura-pg-native-media-schema rev4)
# Kind: oneshot
# Effects: mutating (writes post.perceptual_hash/perceptual_hash_bands/status only; NO image bytes)
# Requires: migration 0027 (perceptual_hash, perceptual_hash_bands, status)
# Runs-on: primary
# OnChange: none
# Version: 0.1
"""
Phase 2a of the pg-native media migration: FINGERPRINT + INTEGRITY pass.

For every existing post, read its original from the HDD backup and:
  - dHash (difference hash, 64-bit, pure PIL — scale-invariant near-dup id) ->
    post.perceptual_hash (8 raw bytes) + post.perceptual_hash_bands (INT[4],
    position-encoded per migration 0027: element = (band_index<<16) | value16).
  - integrity: missing file -> status='lost'; 0-byte or undecodable -> 'corrupt'.
  - decodable image -> status='pending' (awaits Phase 2b cull + data load).
  - video/pdf (mp4/webm/pdf): not PIL-decodable, so NO perceptual_hash; existence/
    size checked only; status='pending' (dedup handled by sha256 in 2b).

This pass deliberately does NOT load post.data and does NOT compute sha256 —
sha256 is the checksum of the STORED bytes (=hash(data)), finalized in Phase 2b.

Idempotent / resumable: only processes rows with status IS NULL. Re-running
continues where it left off. Writes nothing in dry-run.

Usage (primary):
  python3 scripts/backfill_fingerprints.py                 # dry-run, 300-post sample, report only
  python3 scripts/backfill_fingerprints.py --sample 2000   # bigger dry-run sample
  python3 scripts/backfill_fingerprints.py --apply         # full resumable run (writes)
  python3 scripts/backfill_fingerprints.py --apply --batch 500
"""

import argparse
import asyncio
import hashlib  # noqa: F401  (kept for 2b symmetry; unused here by design)
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor

from PIL import Image, ImageFile
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

# Truncated files must RAISE so we catch them as corrupt (do not silently pad).
ImageFile.LOAD_TRUNCATED_IMAGES = False

DATABASE_URL = os.environ.get(
    "DATABASE_URL", "postgresql+asyncpg://kura:kura@localhost:5432/kura"
)
# Read originals from LOCAL ext4 first (data-images/posts; ~950/s/thread, parallel
# ~2400/s) and fall back to the NTFS HDD backup (/mnt/d; ~60ms/file, poor
# concurrency) only when the local copy is missing or 0-byte — that also recovers
# the locally zero-byte-corrupt set from the backup when the backup has it.
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# kura#43: 既定は「この checkout の data-images/」。退役ホスト desk の
# 旧 home 配下の kura/... を既定にしていたので、env を渡し忘れると存在しない
# path を見に行って「1 枚も見つからない」形で黙って空振りしていた。
LOCAL_POSTS_DIR = os.environ.get(
    "KURA_LOCAL_POSTS", os.path.join(_REPO_ROOT, "data-images", "posts"))
HDD_POSTS_DIR = os.environ.get("KURA_HDD_POSTS", "/mnt/d/kura-image-backup/posts")

DECODABLE_EXT = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".tif", ".tiff"}


def dhash_and_bands(path: str):
    """Return (perceptual_hash_bytes(8), bands:list[int](4)). Raises on decode failure."""
    with Image.open(path) as im:
        # 9x8 grayscale; compare horizontally-adjacent pixels -> 64 bits.
        g = im.convert("L").resize((9, 8), Image.LANCZOS)
        px = g.tobytes()  # 72 grayscale bytes, row-major (8 rows x 9 cols)
    h = 0
    for row in range(8):
        base = row * 9
        for col in range(8):
            h = (h << 1) | (1 if px[base + col] > px[base + col + 1] else 0)
    phash = h.to_bytes(8, "big")
    # position-encoded bands (migration 0027 contract): 4 x 16-bit slices.
    bands = [((i << 16) | ((h >> (16 * i)) & 0xFFFF)) for i in range(4)]
    return phash, bands


def _fingerprint(pid: int, primary, fallback):
    """Per-post work (runs in a thread; file read + decode both release the GIL).
    Prefer the local copy; fall back to the HDD backup if local is missing/0-byte.
    Returns (pid, status, perceptual_hash|None, bands|None, category)."""
    path, found_any = None, False
    for cand in (primary, fallback):
        if not cand:
            continue
        try:
            sz = os.path.getsize(cand)
        except OSError:
            continue
        found_any = True
        if sz > 0:
            path = cand
            break
    if path is None:
        # no file anywhere -> lost; present but 0-byte everywhere -> corrupt
        return (pid, ("corrupt" if found_any else "lost"), None, None,
                ("corrupt" if found_any else "lost"))
    ext = os.path.splitext(path)[1].lower()
    if ext in DECODABLE_EXT:
        try:
            phash, bands = dhash_and_bands(path)
            return (pid, "pending", phash, bands, "pending_img")
        except Exception:
            return (pid, "corrupt", None, None, "corrupt")
    # video/pdf/etc — not PIL-decodable; integrity-only, no perceptual hash.
    return (pid, "pending", None, None, "pending_nonimg")


def build_index(dirpath: str) -> dict:
    """Map post_id (int, filename prefix before '_') -> absolute path. One scan."""
    idx = {}
    if not os.path.isdir(dirpath):
        return idx
    with os.scandir(dirpath) as it:
        for e in it:
            name = e.name
            us = name.find("_")
            if us <= 0:
                continue
            head = name[:us]
            if head.isdigit():
                idx[int(head)] = e.path
    return idx


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="write to DB (default dry-run)")
    ap.add_argument("--sample", type=int, default=None,
                    help="process only the first N unprocessed posts (default: 300 in dry-run, all with --apply)")
    ap.add_argument("--batch", type=int, default=1000, help="rows per concurrent chunk + commit")
    ap.add_argument("--workers", type=int, default=24,
                    help="concurrent file-read+dHash threads (NTFS read is latency-bound → high helps)")
    args = ap.parse_args()
    sample = args.sample if args.sample is not None else (None if args.apply else 300)

    t_idx = time.time()
    local = build_index(LOCAL_POSTS_DIR)
    hdd = build_index(HDD_POSTS_DIR)
    print(f"indexed local={len(local):,} ({LOCAL_POSTS_DIR}) + hdd={len(hdd):,} fallback "
          f"in {time.time()-t_idx:,.0f}s", flush=True)

    engine = create_async_engine(DATABASE_URL)
    counts = {"pending_img": 0, "pending_nonimg": 0, "corrupt": 0, "lost": 0, "mismatch": 0}
    lost_ids, corrupt_ids = [], []
    t0 = time.time()
    done = 0

    # "needs fingerprinting" = an un-fingerprinted image awaiting cull. The
    # original 411k corpus was status IS NULL; after migration 0034 a NEW import/
    # upload defaults to status='pending'. Match ANY non-'ok', non-terminal
    # status (not just the literal 'pending') — same broadening rationale as
    # backfill_media.py's NOT_OK_NOT_TERMINAL (kura-visibility-contract,
    # broker task 3faa322a): this pass should catch a stuck image
    # regardless of which specific status string caused it to fall through,
    # rather than encoding today's one known value into the query.
    # video/document never get a perceptual_hash (so are EXCLUDED here, else they
    # would re-select forever) — backfill_media Pass 2 materialises them straight
    # from 'pending'. Running this + backfill_media on a timer is the continuous
    # ingest that turns new pending uploads into status='ok' + post.data.
    NEEDS_FP = ("perceptual_hash IS NULL "
                "AND (status IS NULL OR status NOT IN ('ok', 'exact_dup', 'near_dup', 'corrupt', 'lost')) "
                "AND lower(type) IN ('image','animation')")
    # video/document with status IS NULL: these were inserted without the
    # 'pending' default (e.g. a direct upload path that left status NULL), so
    # the image-only NEEDS_FP predicate never touches them and they sit at NULL
    # forever. They get no perceptual_hash by design (not PIL-decodable), so
    # promote them straight to 'pending'; backfill_media Pass 2 then
    # materialises them from HDD via sha256. Idempotent: once 'pending' they
    # are excluded by NEEDS_FP (type filter), so this never re-selects. (Prior
    # to this fix, a NULL status here also tripped backfill_media's
    # "status IS NULL → abort the whole pass" guard — that guard is gone now
    # [2026-07-12 incident, kura-visibility-contract], so this promotion is
    # purely cosmetic/for-clarity, not deadlock-prevention, but still correct
    # to keep: it documents these rows as "seen, no hash coming".
    PROMOTE_NONIMG = ("status IS NULL AND lower(type) NOT IN ('image','animation')")
    async with engine.connect() as conn:
        if args.apply:
            promoted = (await conn.execute(text(
                f"UPDATE post SET status='pending' WHERE {PROMOTE_NONIMG}"))).rowcount
            await conn.commit()
            if promoted:
                print(f"promoted {promoted:,} NULL-status non-image post(s) → 'pending' "
                      f"(video/document; materialised by backfill_media)", flush=True)
        total_todo = (await conn.execute(
            text(f"SELECT count(*) FROM post WHERE {NEEDS_FP}"))).scalar_one()
        limit_sql = f"LIMIT {sample}" if sample else ""
        rows = (await conn.execute(text(
            f"SELECT id, type, lower(\"mime-type\") AS mime, image_width, image_height "
            f"FROM post WHERE {NEEDS_FP} ORDER BY id {limit_sql}"))).all()
        print(f"to-process (needs fingerprinting)={total_todo:,}  this run={len(rows):,}  "
              f"{'APPLY' if args.apply else 'DRY-RUN'}", flush=True)

        # Concurrent: the NTFS read (61ms) + decode (26ms) both release the GIL, so
        # a thread pool overlaps the per-file latency across many cores. The main
        # coroutine batches the DB writes per chunk.
        loop = asyncio.get_running_loop()
        print(f"workers={args.workers} batch={args.batch}", flush=True)
        with ThreadPoolExecutor(max_workers=args.workers) as ex:
            for i in range(0, len(rows), args.batch):
                chunk = rows[i:i + args.batch]
                results = await asyncio.gather(*[
                    loop.run_in_executor(ex, _fingerprint, r.id, local.get(r.id), hdd.get(r.id))
                    for r in chunk
                ])
                for (pid, status, phash, bands, cat) in results:
                    counts[cat] += 1; done += 1
                    if cat == "lost": lost_ids.append(pid)
                    elif cat == "corrupt": corrupt_ids.append(pid)
                if args.apply:
                    await _flush(conn, [(p, s, ph, b) for (p, s, ph, b, _c) in results])
                    await conn.commit()
                el = time.time() - t0
                print(f"  {done:,}/{len(rows):,}  {done/max(el,1):,.0f} posts/s  "
                      f"img={counts['pending_img']:,} nonimg={counts['pending_nonimg']:,} "
                      f"corrupt={counts['corrupt']:,} lost={counts['lost']:,}", flush=True)

    el = time.time() - t0
    print(f"\n{'APPLIED' if args.apply else 'DRY-RUN'} {done:,} posts in {el:,.0f}s "
          f"({done/max(el,1):,.0f}/s)")
    print(f"  pending(image w/ dHash) = {counts['pending_img']:,}")
    print(f"  pending(non-image, no dHash) = {counts['pending_nonimg']:,}")
    print(f"  corrupt = {counts['corrupt']:,}   lost(no HDD original) = {counts['lost']:,}")
    if lost_ids[:20]:
        print(f"  sample lost ids: {lost_ids[:20]}")
    if corrupt_ids[:20]:
        print(f"  sample corrupt ids: {corrupt_ids[:20]}")
    await engine.dispose()
    return 0


async def _flush(conn, updates) -> None:
    await conn.execute(
        text("UPDATE post SET status=:s, perceptual_hash=:p, perceptual_hash_bands=:b "
             "WHERE id=:i"),
        [{"i": i, "s": s, "p": p, "b": b} for (i, s, p, b) in updates],
    )


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
