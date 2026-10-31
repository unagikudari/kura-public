"""Upload router — POST /api/upload

シンプルな multipart アップロードエンドポイント。
kura DB に書き込み、ファイルを /data/posts/ に保存する。
UI と取り込みスクリプトから呼ばれる。拡張は extension_api.upload で同じ処理を使う。

POST /api/upload
  file:           binary (required)
  tags:           カンマ区切りタグ文字列 (optional)
  source:         ソース URL (optional)
  safety:         safe | sketchy | unsafe (default: unsafe)
  pool:           pool 名 (optional) — 存在しなければ新規作成、あれば末尾に追記
  pool_source_url: pool の取得元 URL (optional) — pool 名が変わっても同一 URL なら既存 pool を使う
  collection_url: この post の主 pool の URL (optional) — 無ければ作り、primary_pool_id にする
  artist_tags:    tags のうち作家名であるもの (optional, 空白区切り)
  それ以外の field は 422 unknown_upload_fields (file を読む前に断る)。

Response: { "id": int, "md5": str, "duplicate": bool }
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import mimetypes
import os
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated, Optional

import aiofiles
from fastapi import APIRouter, Form, HTTPException, Request, UploadFile, Depends
from fastapi.responses import JSONResponse
from PIL import Image, UnidentifiedImageError
import sqlalchemy as sa
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from api.config import DATA_DIR, POSTS_DIR, THUMBNAILS_DIR
from api.errors import empty_upload, unknown_upload_fields, zero_byte_content
from api.services import files, renditions
from api.services.hashing import get_security_hash
from api.auth import get_current_user
from api.services.pool_naming import derive_pool_name_from_url
from api.db.models import (
    Pool, PoolCategory, PoolName, PoolPost,
    Post, PostTag, Tag, TagCategory, TagName, User,
)
from api.db.session import get_db
from api.services.media_materialize import (
    materialize_animation_bytes,
    materialize_image_bytes,
    materialize_other_bytes,
)
from api.services.tags import expand_implications, create_tag_named, tag_by_name

router = APIRouter(prefix="/api", tags=["upload"])

from api.config import OWNER_USER_ID as _OWNER_USER_ID  # kura#43 item 5, read back by kura#102

from api.services.files import MIME_TO_EXT as MIME_EXT  # the one table (kura#61 PR-A)

THUMBNAIL_SIZE = (300, 300)


def _security_hash(post_id: int) -> str:
    """Delegates to the one implementation (kura#43-2).

    This was a byte-identical copy of api.services.hashing.get_security_hash —
    one of four. They agreed for years, and would have stopped agreeing the
    moment the filename salt was split out of KURA_SECRET, because only the
    canonical one was changed."""
    return get_security_hash(post_id)


def _detect_mime(data: bytes, filename: str) -> str:
    # ファイル先頭バイトで判定
    if data[:4] == b"\x00\x00\x00\x18" or data[4:8] == b"ftyp" or data[:4] == b"\x00\x00\x00\x1c":
        return "video/mp4"
    if data[:4] in (b"\x1aE\xdf\xa3", b"\x1a\x45\xdf\xa3"):
        return "video/webm"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data[:4] == b"%PDF":
        return "application/pdf"
    # フォールバック: 拡張子
    ext = Path(filename).suffix.lower()
    fallback = {".mp4": "video/mp4", ".webm": "video/webm", ".gif": "image/gif",
                ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
                ".webp": "image/webp", ".pdf": "application/pdf"}
    return fallback.get(ext, "application/octet-stream")


def _dhash_and_bands(data: bytes) -> tuple[bytes, list[int]] | None:
    """64-bit dHash + 4x16-bit position-encoded bands from in-memory image bytes.

    SAME algorithm as scripts/backfill_fingerprints.py:dhash_and_bands (kept
    byte-for-byte identical so a hash computed here and one computed later by
    the async backfill are interchangeable/comparable) — this just avoids a
    second disk read since upload_post already holds `data` in memory.
    Returns None on any decode failure (caller leaves perceptual_hash NULL;
    the async backfill_fingerprints.py sweep picks up NULL+status='pending'
    rows as a safety net, so a stamping failure here is not permanent).
    """
    try:
        with Image.open(io.BytesIO(data)) as im:
            g = im.convert("L").resize((9, 8), Image.LANCZOS)
            px = g.tobytes()
    except Exception:
        return None
    h = 0
    for row in range(8):
        base = row * 9
        for col in range(8):
            h = (h << 1) | (1 if px[base + col] > px[base + col + 1] else 0)
    phash = h.to_bytes(8, "big")
    bands = [((i << 16) | ((h >> (16 * i)) & 0xFFFF)) for i in range(4)]
    return phash, bands


def _post_type(mime: str) -> str:
    if mime.startswith("video/"):
        return Post.TYPE_VIDEO
    if mime == "image/gif":
        return Post.TYPE_ANIMATION
    if mime == "application/pdf":
        return Post.TYPE_DOCUMENT
    return Post.TYPE_IMAGE


def _probe_video_duration(src: Path) -> float:
    """Return video duration in seconds; 0.0 if ffprobe fails."""
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "csv=p=0", str(src)],
            check=True, capture_output=True, timeout=10,
        )
        return float(r.stdout.decode().strip() or "0")
    except Exception:
        return 0.0


def _probe_video_dimensions(src: Path) -> tuple[int | None, int | None]:
    """Return (width, height) of the first video stream via ffprobe.

    Populated at upload time for video posts so the frontend can size the
    player to its real aspect (instead of falling back to 4/3 and showing
    letterboxing/stretch). Returns (None, None) on failure.
    """
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=width,height",
             "-of", "csv=p=0", str(src)],
            check=True, capture_output=True, timeout=10,
        )
        parts = r.stdout.decode().strip().split(",")
        if len(parts) >= 2 and parts[0] and parts[1]:
            return int(parts[0]), int(parts[1])
    except Exception:
        pass
    return None, None


def _video_seek_candidates(duration: float) -> list[float]:
    """Frame-extract timestamps, MIDPOINT first.

    Blackouts, fade-ins and title cards cluster at the head and tail, so the
    exact middle of the video is the safest single frame. The rest are retries
    spread around the middle (caller stops at the first non-dark frame).
    """
    if duration <= 0:                       # unknown duration → first frame
        return [0.0]
    return [duration * 0.5, duration * 0.35, duration * 0.65, duration * 0.2]


def _video_thumb_at_seek(src: Path, dest: Path, seek_seconds: float) -> None:
    """Extract a single 300px-bounded frame at `seek_seconds`.

    `-ss` before `-i` = keyframe seek (fast, rough; matches user's "approx" need)."""
    from api.services.posters import video_render_slot
    with video_render_slot:
        subprocess.run(
            ["ffmpeg", "-y",
             "-ss", str(seek_seconds), "-i", str(src),
             "-frames:v", "1",
             "-vf", "scale='min(300,iw)':'min(300,ih)':force_original_aspect_ratio=decrease",
             "-update", "1",
             str(dest)],
            check=True, capture_output=True, timeout=30,
        )


def _is_dark_jpeg(path: Path, threshold: float = 15.0) -> bool:
    """True if the JPEG's mean luminance < threshold (out of 255).

    Used to detect blackout / fade-in frames and trigger a later-seek retry.
    """
    try:
        with Image.open(path) as img:
            from PIL import ImageStat
            return ImageStat.Stat(img.convert("L")).mean[0] < threshold
    except Exception:
        return False  # can't read → assume not dark, leave it


def _generate_thumbnail(src: Path, dest: Path, mime: str) -> bool:
    """Publish a complete real poster, preserving any good poster on failure."""
    from api.services.posters import publish_poster
    try:
        return publish_poster(dest, lambda candidate: _render_thumbnail(src, candidate, mime))
    except OSError as exc:
        # Decoder OSErrors have no errno; actual storage failures stay loud.
        if exc.errno is not None:
            raise
        return False
    except (ValueError, SyntaxError, Image.DecompressionBombError, subprocess.SubprocessError):
        return False


def _regenerate_video_thumbnail(post_id: int, src: Path, names: list[Path], seek: float) -> None:
    """Blocking explicit-seek render; publish and relink only complete output."""
    from api.services.posters import publish_poster
    with files.content_lock(post_id):
        if not publish_poster(names[0], lambda candidate: _video_thumb_at_seek(src, candidate, seek)):
            raise ValueError("ffmpeg produced no thumbnail")
        for other in names[1:]:
            files.link_secondary(names[0], other)


def _render_thumbnail(src: Path, dest: Path, mime: str) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    if mime.startswith("video/"):
        duration = _probe_video_duration(src)
        extracted = False
        for seek in _video_seek_candidates(duration):
            if duration and seek >= duration * 0.95:
                continue  # would land past the video end
            try:
                _video_thumb_at_seek(src, dest, seek)
            except Exception:
                continue  # this seek failed (ffmpeg error) — try the next
            extracted = True
            if not _is_dark_jpeg(dest):
                return  # bright, real frame — done
            # dark frame: keep it as a fallback, but try a brighter seek
        if extracted:
            return  # every candidate was dark, but `dest` holds a real frame
                    # (the midpoint) — far better than a synthetic black pixel
        # No seek produced ANY frame (ffmpeg missing / unreadable video). Do NOT
        # mask it with a 1x1 black placeholder — leave no poster so the post is
        # visibly un-thumbnailed and can be regenerated once the cause is fixed.
        if dest.exists():
            dest.unlink()
        return
    if mime == "application/pdf":
        stem = dest.with_suffix("")
        subprocess.run(
            ["pdftoppm", "-jpeg", "-scale-to", str(max(THUMBNAIL_SIZE)),
             "-f", "1", "-l", "1", "-singlefile", str(src), str(stem)],
            check=True, capture_output=True, timeout=60,
        )
        generated = stem.with_suffix(".jpg")
        if generated != dest:
            generated.replace(dest)
        with Image.open(dest) as img:
            img.thumbnail(THUMBNAIL_SIZE)
            img.convert("RGB").save(dest,"JPEG")
    else:
        with Image.open(src) as img:
            # thumbnail can request JPEG decoder downsampling before full load.
            img.thumbnail(THUMBNAIL_SIZE)
            img.convert("RGB").save(dest,"JPEG")


async def _get_or_create_tag(db: AsyncSession, name: str,
                             category_hint: str | None = None) -> Tag:
    """タグ名で Tag を取得、なければ default カテゴリで作成。

    lowercase に正規化して services.tags.get_or_create_tags / posts._get_or_create_tag
    と揃える(tag_name.name は case-sensitive UNIQUE のため大文字小文字の差で
    同義タグが別行に分裂する)。

    category_hint (artist-category propagation plan Phase 1): 呼び出し元は
    「このタグは作家名」と知っているのに平タグとして捨てていた — 呼び出し元が
    hint を渡した場合のみ、(a) 新規タグはその category で作る、(b) 既存タグが
    default (general) 止まりなら hint の category へ昇格する。hint 無しの呼び出し
    は従来と完全に同一挙動なので、手動 curation 済みタグや一般タグには触れない
    (昇格は default→hint の一方向のみ — 人が別 category に直した既存タグは
    default でないため対象外)。
    """
    name = name.replace(" ", "_").strip().lower()
    if not name:
        raise ValueError("empty tag name")
    # default カテゴリ取得 (hint の有無に関わらず判定基準として必要)
    rc = await db.execute(select(TagCategory).where(TagCategory.default == True))
    cat = rc.scalar_one_or_none()
    if cat is None:
        rc2 = await db.execute(select(TagCategory).limit(1))
        cat = rc2.scalar_one()
    hint_cat: TagCategory | None = None
    if category_hint:
        rh = await db.execute(
            select(TagCategory).where(TagCategory.name == category_hint))
        hint_cat = rh.scalar_one_or_none()  # 未定義 category は黙って default 扱い
    # TagName で検索
    r = await db.execute(select(TagName).where(TagName.name == name))
    tn = r.scalar_one_or_none()
    if tn:
        r2 = await db.execute(select(Tag).where(Tag.tag_id == tn.tag_id))
        tag = r2.unique().scalar_one()
        if hint_cat is not None and tag.category_id == cat.id and tag.category_id != hint_cat.id:
            tag.category_id = hint_cat.id
        return tag
    if hint_cat is not None:
        cat = hint_cat
    # **作成は services.tags に寄せる。** ここで直に INSERT していた版は、
    # 並行 upload で tag_name_name_key の UniqueViolation を出して session を汚し、
    # 後続 query を PendingRollbackError にしてリクエストを 500 にしていた
    # (2026-07-30 実測: 3 枚同時 upload で 2 枚が 500)。
    tag = await create_tag_named(db, name, cat)
    if tag is None:                       # 競合相手が先に作った → 読み直す
        tag = await tag_by_name(db, name)
    if tag is None:
        raise ValueError(f"tag {name!r} を作成も取得もできませんでした")
    return tag


def _derive_pool_name_from_url(url: str) -> str:
    # kura#42: see api/services/pool_naming.py — the single spelling.
    return derive_pool_name_from_url(url)


async def _get_or_create_pool_by_source_url(
    db: AsyncSession, source_url: str, display_name: str | None = None,
) -> Pool:
    """Phase C canonical pool lookup via pool_url table."""
    from api.db.models import PoolUrl, PoolState
    r = await db.execute(
        select(Pool)
        .join(PoolUrl, PoolUrl.pool_id == Pool.pool_id)
        .where(PoolUrl.url == source_url)
    )
    existing = r.unique().scalar_one_or_none()
    if existing:
        return existing
    # D2: canonical-variant match (trailing slash / ?p=N) → reuse the existing
    # pool rather than create a duplicate sibling (matches migration 0046's index).
    from api.pool_identity import CANONICAL_POOL_LOOKUP_SQL
    canon_pid = (await db.execute(sa.text(CANONICAL_POOL_LOOKUP_SQL),
                                  {"u": source_url})).scalar()
    if canon_pid is not None:
        return (await db.execute(select(Pool).where(Pool.pool_id == canon_pid))).unique().scalar_one()
    # The same thing imported from ANOTHER source (a creator re-sharing an
    # archive in a new folder): join that pool as a second url instead of
    # creating a twin. Only a site adapter can tell; none present = create.
    from api import site_hooks
    sib_pid = await site_hooks.find_sibling_source_pool(db, source_url)
    if sib_pid is not None:
        db.add(PoolUrl(pool_id=sib_pid, url=source_url, is_primary=False))
        await db.flush()
        return (await db.execute(select(Pool).where(Pool.pool_id == sib_pid))).unique().scalar_one()
    rc = await db.execute(select(PoolCategory).where(PoolCategory.default == True))
    pcat = rc.scalar_one_or_none()
    if pcat is None:
        rc2 = await db.execute(select(PoolCategory).limit(1))
        pcat = rc2.scalar_one()
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    pool = Pool(
        category_id=pcat.id, version=1, creation_time=now, last_edit_time=now,
    )
    db.add(pool)
    await db.flush()
    # Create primary PoolUrl
    from api.routers.pools import _infer_scope, adopt_posts_by_source
    db.add(PoolUrl(pool_id=pool.pool_id, url=source_url, is_primary=True))
    db.add(PoolState(pool_id=pool.pool_id, scope=_infer_scope(source_url)))
    await db.flush()
    # Pool created AFTER its posts (batch→per_post transition, re-dispatch):
    # adopt existing source-matching posts — the post-side attach trigger
    # cannot fire for a pool that did not exist yet.
    await adopt_posts_by_source(db, pool.pool_id, source_url)

    # name candidate: explicit display_name, else derive from URL. (The legacy
    # per-import gallery_title lookup was dropped in Phase 6 migration 0037.)
    candidate = display_name or _derive_pool_name_from_url(source_url)

    fallback = _derive_pool_name_from_url(source_url)

    # Pool_name must ALWAYS be inserted (otherwise the pool list shows the
    # bare URL — 2026-05-25 incident: pool 3774 had 323 posts + no pool_name).
    # Try candidate; on UNIQUE conflict (concurrent / pre-existing name), try
    # suffixed; finally guaranteed-unique form with pool_id suffix.
    insert_sql = sa.text(
        "INSERT INTO pool_name (pool_id, name, ord) "
        "VALUES (:pid, :name, 0) "
        "ON CONFLICT (name) DO NOTHING RETURNING pool_name_id"
    )
    for attempt_name in [
        candidate,
        f"{candidate} ({fallback})",
        f"{candidate} (pool-{pool.pool_id})",
        f"{fallback} (pool-{pool.pool_id})",
    ]:
        if not attempt_name:
            continue
        r_ins = await db.execute(insert_sql, {"pid": pool.pool_id, "name": attempt_name[:128]})
        if r_ins.first() is not None:
            return pool
    # All four attempts conflicted (extremely unlikely). Fall back to a
    # collision-impossible form using current timestamp.
    from datetime import datetime as _dt
    await db.execute(insert_sql, {
        "pid": pool.pool_id,
        "name": f"pool-{pool.pool_id}-{_dt.utcnow().strftime('%H%M%S')}",
    })
    return pool


async def _get_or_create_pool(
    db: AsyncSession, pool_name: str, source_url: str | None = None
) -> Pool:
    """Pool lookup keyed by source_url first (canonical), then name.

    URL is the canonical pool identity (pool_url.url is UNIQUE NOT NULL since
    migration 0013). The display name is normalization-fragile — the pool-ensure
    path stores it with spaces while the upload path forces underscores — so a
    name-first lookup misses the existing url-keyed pool and forks a `manual:pool/N`
    sibling (duplicate-pool bug). Resolve by source_url first so uploads converge
    onto the pool that kura_ensure_pool already created for that URL.
    """
    from api.db.models import PoolUrl, PoolState
    if source_url:
        r3 = await db.execute(
            select(Pool)
            .join(PoolUrl, PoolUrl.pool_id == Pool.pool_id)
            .where(PoolUrl.url == source_url)
        )
        existing = r3.unique().scalar_one_or_none()
        if existing:
            return existing
    r = await db.execute(select(PoolName).where(PoolName.name == pool_name))
    pn = r.scalar_one_or_none()
    if pn:
        r2 = await db.execute(select(Pool).where(Pool.pool_id == pn.pool_id))
        return r2.unique().scalar_one()
    rc = await db.execute(select(PoolCategory).where(PoolCategory.default == True))
    pcat = rc.scalar_one_or_none()
    if pcat is None:
        rc2 = await db.execute(select(PoolCategory).limit(1))
        pcat = rc2.scalar_one()
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    pool = Pool(
        category_id=pcat.id, version=1, creation_time=now, last_edit_time=now,
    )
    db.add(pool)
    await db.flush()
    # Invariant: every pool has a primary pool_url. Use the real source_url
    # when present (and not already owned), else a `manual:pool/<id>` sentinel,
    # so a pool is never url-less (url-less → observed_count 0 / "0" display).
    from api.routers.pools import _infer_scope
    url_to_use = source_url or None
    if url_to_use:
        taken = (await db.execute(
            select(PoolUrl.pool_id).where(PoolUrl.url == url_to_use)
        )).scalar_one_or_none()
        if taken is not None:
            url_to_use = None
    if not url_to_use:
        url_to_use = f"manual:pool/{pool.pool_id}"
    db.add(PoolUrl(pool_id=pool.pool_id, url=url_to_use, is_primary=True))
    db.add(PoolState(pool_id=pool.pool_id, scope=_infer_scope(url_to_use)))
    db.add(PoolName(pool_id=pool.pool_id, name=pool_name, order=0))
    return pool


def _read_poster(thumb_dest: Path) -> bytes | None:
    """Best-effort read of an already-generated poster JPEG (blocking file
    I/O — call from an executor). Missing/unreadable poster is not fatal:
    materialize_other_bytes() tolerates poster_bytes=None (thumbnail stays
    NULL; the content/thumbnail routes fall back to the on-disk file)."""
    try:
        return thumb_dest.read_bytes()
    except OSError:
        return None


def _is_sha256_conflict(exc: IntegrityError) -> bool:
    """True only for a unique violation on post.sha256.

    Narrow on purpose. Swallowing every IntegrityError here would turn any
    future constraint — a bad foreign key, a NOT NULL someone adds — into a
    silent "duplicate", which is the failure mode this endpoint keeps being
    bitten by: an error that reads as a normal outcome.

    Judged by SQLSTATE 23505 (unique_violation) and the violated constraint's
    name, never by the message: production's lc_messages is ja_JP.UTF-8, so the
    message reads 「重複したキー値は一意性制約…」 and the English words this used to
    look for ("duplicate key", "already exists") never appear — the race path
    re-raised as a 500 there (found 2026-10-25; the test DB is Japanese too).
    The violation must be on post, by a constraint that names the digest
    (ux_post_sha256_ok): other tables have sha256 constraints too
    (uq_lora_asset_sha256, blob_sha256_key), and those are not this race.
    """
    orig = getattr(exc, "orig", None)
    if getattr(orig, "sqlstate", None) != "23505":
        return False
    driver = getattr(orig, "driver_exception", None) or getattr(orig, "__cause__", None)
    constraint = getattr(driver, "constraint_name", None)
    return (getattr(driver, "table_name", None) == "post"
            and bool(constraint) and "sha256" in constraint)


async def _synchronous_materialize(db: AsyncSession, post: Post, data: bytes, thumb_dest: Path) -> bool:
    """Run the Pass-2 equivalent (fingerprint already done by the caller;
    this does cull-materialize: WebP transcode + sha256 + status='ok')
    INLINE, mutating `post` in place. Returns True iff `post.status` is now
    'ok'; on any failure `post` is left untouched (status stays 'pending')
    so the periodic backstop (scripts/backfill_media.py) can retry it later.

    This is the synchronous half of the visibility contract (broker
    task 3faa322a, superseding 5d2b6360): POST /api/upload must not return
    2xx while the post is still invisible to GET /api/posts. The caller
    downgrades the HTTP response to 202 when this returns False — see
    upload_post()."""
    loop = asyncio.get_event_loop()
    try:
        if post.type == Post.TYPE_ANIMATION:
            # 2026-07-21 fix: was routed through materialize_image_bytes,
            # which silently collapses every GIF/animated-WebP upload to its
            # first frame (Pillow's plain .save() only ever writes the
            # currently-seeked frame) — "GIFが動かない" (user report).
            res = await loop.run_in_executor(None, materialize_animation_bytes, data)
        elif post.type == Post.TYPE_IMAGE:
            res = await loop.run_in_executor(None, materialize_image_bytes, data)
        else:
            poster_bytes = await loop.run_in_executor(None, _read_poster, thumb_dest)
            res = await loop.run_in_executor(None, materialize_other_bytes, data, poster_bytes)
        # Display bytes are rendition files, not post columns
        # (docs/notes/rendition-store-ideal.md). Same transaction as the status flip.
        # A savepoint so a failed attach leaves the outer transaction usable and
        # the post 'pending' for the backstop, exactly like a failed transcode.
        async with db.begin_nested():
            await renditions.attach_bytes(
                db, post.post_id, {"view": res["data"], "thumb": res["thumbnail"]},
                renditions.LEGACY_RECIPE)
    except Exception:
        return False
    post.sha256 = res["sha256"]
    post.media_location = res["media_location"]
    if res["mime"]:
        post.mime_type = res["mime"]
    post.status = "ok"
    return True


def _legacy_original_sha256(post: Post) -> bytes | None:
    """Hash retained FS originals, never the serving WebP in post.data.

    mime_type may describe that WebP rather than the retained original. Probe
    the bounded set of canonical content names (including migration aliases).
    Missing, unreadable, or disagreeing originals leave identity unverified.
    """
    paths = dict.fromkeys(
        path for mime in [post.mime_type, *MIME_EXT]
        for path in files.names("content", post.post_id, mime)
    )
    digests = set()
    for path in paths:
        try:
            with path.open("rb") as stream:
                digests.add(hashlib.file_digest(stream, "sha256").digest())
        except FileNotFoundError:
            continue
        except OSError:
            return None
    return next(iter(digests)) if len(digests) == 1 else None


async def _find_upload_duplicate(db: AsyncSession, md5: str, sha256: bytes) -> Post | None:
    # Serialize identical originals across API workers, including requests whose
    # materialization fails. This lock is released by commit/rollback; a 64-bit
    # lock-key collision only serializes unrelated content, never identifies it.
    await db.execute(sa.text("SELECT pg_advisory_xact_lock(:key)"),
                     {"key": int.from_bytes(sha256[:8], "big", signed=True)})
    existing = (await db.execute(
        select(Post).where(Post.sha256 == sha256)
        .order_by((Post.status == "ok").desc().nullslast(), Post.post_id).limit(1)
    )).unique().scalar_one_or_none()
    if existing is not None:
        return existing

    # MD5 narrows legacy candidates only. Do not stop at the oldest collision.
    candidates = (await db.execute(
        select(Post).where(Post.checksum_md5 == md5).order_by(Post.post_id)
    )).unique().scalars().all()
    unresolved = False
    for candidate in candidates:
        digest = candidate.sha256
        if digest is None:
            digest = await asyncio.get_running_loop().run_in_executor(
                None, _legacy_original_sha256, candidate)
            if digest is None:
                unresolved = True
                continue
        if digest == sha256:
            candidate.sha256 = sha256
            return candidate
    if unresolved:
        raise HTTPException(status_code=409, detail={
            "code": "original_identity_unverified",
            "message": "Cannot verify a legacy MD5 candidate against its retained original; repair the original/SHA256 and retry.",
        })
    return None


@dataclass
class Ingested:
    """What `ingest` stored, before anyone commits."""
    post: Post
    md5: str
    sha256: bytes
    duplicate: bool
    materialized: bool
    # Files written for a new post; a rolled-back insert leaves them unreferenced.
    written: tuple[Path, ...] = ()


async def _finish_duplicate_upload(
    db: AsyncSession, existing: Post, data: bytes, md5: str, sha256: bytes, *,
    tags: str, source: str, pool: str, pool_src: str | None,
    collection_url: str, filename: str | None, artist_tags: str,
) -> Ingested:
    # Original identity has been verified before any metadata changes.
    # 重複: tags/pool だけ更新して返す. Tags are applied here too, so a re-upload
    # of an already-stored image still earns its artist tag (the md5-dup path is
    # how most of a re-imported set arrives).
    await _apply_tags(db, existing, tags, artist_names=_artist_name_set(artist_tags))
    # IMPORTANT: do NOT overwrite a non-NULL existing.source on md5 dup.
    # 2026-05-25 incident: a re-upload under a synthetic source URL
    # sent 2000+ files that md5-matched existing posts. The previous
    # `if source and existing.source != source: existing.source = source`
    # clobbered each existing post's original source URL with the
    # synthetic one, breaking all source-based lookups.
    # ALLOWED: NULL → value (recovery from corruption / first-time
    # source attribution); the first non-NULL value wins permanently.
    if source and not existing.source:
        existing.source = source
        existing.version += 1
    # original_filename: same NULL->value-only rule. Measured 2026-09-27:
    # 11 posts first imported from a source that carries no filename were
    # later md5-matched by the creator's archives, which DO name each file.
    # The dup path dropped that name, so the per-set split
    # (scripts/split_pool_by_filename_set.py) saw two pools with posts it
    # could not key and refused them on every run.
    if filename and not existing.original_filename:
        existing.original_filename = Path(filename).name
        existing.version += 1
    if pool:
        await _append_to_pool(db, existing, pool, source_url=pool_src)
    if collection_url:
        # Resolve once, used by both backfill (primary IS NULL) and
        # M:N attachment (primary already points elsewhere).
        pl = await _get_or_create_pool_by_source_url(db, collection_url)
        if existing.primary_pool_id is None:
            # Backfill: dup post originally had no primary pool — adopt this one.
            existing.primary_pool_id = pl.pool_id
            existing.version += 1
            # primary_pool_id trigger fills pool_post(pl.pool_id, existing.id)
        elif existing.primary_pool_id != pl.pool_id:
            # Existing post is already anchored to a different pool (e.g.
            # an md5-dup that originally landed in the creator pool).
            # The post should ALSO appear in this collection's pool —
            # pool_post is M:N and the kolietry Vol.N merge workflow
            # explicitly needs siblings to coexist. Insert pool_post
            # without disturbing primary_pool_id (preserves the post's
            # canonical home). ON CONFLICT keeps the call idempotent.
            next_ord = (await db.execute(sa.text(
                "SELECT COALESCE(MAX(ord), -1) + 1 FROM pool_post WHERE pool_id=:p"
            ), {"p": pl.pool_id})).scalar()
            await db.execute(sa.text(
                "INSERT INTO pool_post (pool_id, post_id, ord) "
                "VALUES (:p, :i, :o) ON CONFLICT (pool_id, post_id) DO NOTHING"
            ), {"p": pl.pool_id, "i": existing.post_id, "o": next_ord})
    # Visibility contract (broker task 3faa322a): an md5-dup
    # re-upload is still a 2xx response and must still mean "visible
    # now". If the ORIGINAL post never finished materializing (a legacy
    # stuck row, or the sync path failed on its first upload), this
    # request holds byte-identical `data` in memory — use it to finish
    # materializing `existing` instead of silently returning 200 for a
    # post that is still invisible to search.
    #
    # Scope this to status == "pending" ONLY (incident b4ba5528): the
    # contract is about rescuing rows that never finished materializing,
    # not about curation states. Rows a dedup-scan already demoted to
    # near_dup/corrupt/lost must stay demoted — `!= "ok"` was also true
    # for those, so a re-encountered md5-dup was silently resurrecting
    # them back to "ok" on every re-crawl (post_status_transition_guard
    # allows near_dup->ok for legitimate rollback, so it didn't block this).
    dup_materialized = True
    if existing.status == "pending":
        existing_thumb_names = files.names("thumbnail", existing.post_id)
        existing_thumb = existing_thumb_names[0]
        dup_materialized = await _synchronous_materialize(db, existing, data, existing_thumb)
        if dup_materialized and existing_thumb.is_file():
            for other in existing_thumb_names[1:]:   # kura#61
                files.link_secondary(existing_thumb, other)
            existing.version += 1
    return Ingested(existing, md5, sha256, True, dup_materialized)


async def ingest(
    db: AsyncSession, data, *,
    filename: str | None = None,
    tags: str = "",
    source: str = "",
    safety: str = "unsafe",
    pool: str = "",
    pool_source_url: str = "",
    collection_url: str = "",
    artist_tags: str = "",
    existing_only: bool = False,
) -> Ingested | None:
    """Store one original — a new post, or the existing post it duplicates — and never commit.

    `collection_url` is the canonical pool entry: the server finds-or-creates the
    pool keyed by that URL and sets post.primary_pool_id (a trigger keeps pool_post
    in sync). `pool` / `pool_source_url` name a secondary pool. `artist_tags` names
    which of `tags` are artists. With `existing_only`, a non-duplicate is not
    inserted and the result is None (the retry after losing an insert race).
    """
    if safety not in ("safe", "sketchy", "unsafe"):
        safety = "unsafe"
    if not data:
        raise empty_upload()
    if not any(data[:16]):
        raise zero_byte_content(len(data))

    pool_src = pool_source_url.strip() or None
    collection_url = collection_url.strip() or ""

    # Videos can be 2GiB: keep every full-buffer hash off the request loop.
    from api.services.upload_input import content_identity
    md5, sha1, sha256 = await asyncio.to_thread(content_identity, data)
    existing = await _find_upload_duplicate(db, md5, sha256)
    if existing:
        return await _finish_duplicate_upload(
            db, existing, data, md5, sha256, tags=tags, source=source, pool=pool,
            pool_src=pool_src, collection_url=collection_url, filename=filename,
            artist_tags=artist_tags)
    if existing_only:
        return None

    # MIME 検出
    mime = _detect_mime(data, filename or "")
    ext = MIME_EXT.get(mime, "dat")
    post_type = _post_type(mime)

    # 画像サイズ (PDF / video は計算しない)
    width = height = None
    perceptual_hash = None
    perceptual_hash_bands = None
    if post_type in (Post.TYPE_IMAGE, Post.TYPE_ANIMATION):
        try:
            def _get_image_size(d: bytes) -> tuple[int, int]:
                with Image.open(io.BytesIO(d)) as img:
                    return img.size
            width, height = await asyncio.get_event_loop().run_in_executor(None, _get_image_size, data)
        except Exception:
            pass
        # Stamp perceptual_hash SYNCHRONOUSLY at upload time (archive
        # provenance / near-dup plan rev5 Phase 1) — dedup-scan
        # (POST /api/pools/{id}/dedup-scan) reads it, and only relying on the
        # periodic scripts/backfill_fingerprints.py sweep would leave a
        # freshly-imported pool's own-pool near-dups invisible for up to a
        # sweep cycle. video/pdf are NOT hashed here (never PIL-decodable;
        # matches backfill_fingerprints.py's NEEDS_FP image/animation-only
        # predicate, so that sweep and this stamp never race the same row).
        fp = await asyncio.get_event_loop().run_in_executor(None, _dhash_and_bands, data)
        if fp is not None:
            perceptual_hash, perceptual_hash_bands = fp

    # Resolve primary pool from collection_url (Phase A invariant). The DB
    # trigger inserts the pool_post row automatically when primary_pool_id
    # is set, so no separate pool_post call is needed for primary membership.
    primary_pool_id = None
    if collection_url:
        primary_pool = await _get_or_create_pool_by_source_url(db, collection_url)
        primary_pool_id = primary_pool.pool_id

    now = datetime.now(timezone.utc).replace(tzinfo=None)
    post = Post(
        user_id=_OWNER_USER_ID,
        version=1,
        creation_time=now,
        last_edit_time=now,
        safety=safety,
        source=source or None,
        original_filename=Path(filename).name if filename else None,
        type=post_type,
        checksum=sha1,
        checksum_md5=md5,
        file_size=len(data),
        canvas_width=width,
        canvas_height=height,
        perceptual_hash=perceptual_hash,
        perceptual_hash_bands=perceptual_hash_bands,
        mime_type=mime,
        # status MUST be 'pending', not NULL: the cull pass
        # (backfill_fingerprints -> backfill_media) is what materializes
        # post.data/sha256/status='ok', and it only reaches 'pending' rows.
        # A NULL here deadlocks the pipeline fleet-wide — backfill_fingerprints
        # skips the row (its predicate needs perceptual_hash IS NULL, but upload
        # stamps the hash synchronously) and backfill_media then aborts on
        # "posts still status NULL", so nothing is ever materialized.
        # 385 rows were wedged this way (2026-07-12); uploads stayed invisible.
        status="pending",
        flags_string="",
        primary_pool_id=primary_pool_id,
    )
    db.add(post)
    await db.flush()  # post_id 確定

    # ファイル保存 — 名前は files.names() が決め、移行期間中は両名で書く (kura#61)
    content_names = files.names("content", post.post_id, mime)
    dest = await asyncio.get_event_loop().run_in_executor(None, files.write_all_names, content_names, data)

    # サムネイル生成 (ffmpeg/PIL は blocking — スレッドプールへオフロード)
    thumb_names = files.names("thumbnail", post.post_id)
    thumb_dest = thumb_names[0]
    # Image/animation materialization already creates the canonical PG poster.
    # Only filesystem media needs a separate decoder/render pass here.
    poster_ready = False
    if post_type not in (Post.TYPE_IMAGE,Post.TYPE_ANIMATION):
        poster_ready = await asyncio.get_event_loop().run_in_executor(None, _generate_thumbnail, dest, thumb_dest, mime)
    if poster_ready:
        for other in thumb_names[1:]:
            await asyncio.get_event_loop().run_in_executor(None, files.link_secondary, thumb_dest, other)

    # 動画は upload 時点では canvas dims がない (PIL.Image.open(bytes) は
    # 動画を読めない)。 file 保存後に ffprobe して埋める。
    if post_type == Post.TYPE_VIDEO:
        vw, vh = await asyncio.get_event_loop().run_in_executor(None, _probe_video_dimensions, dest)
        if vw and vh:
            post.canvas_width = vw
            post.canvas_height = vh

    # タグ適用 — the post is born tagged instead of waiting for a sweep.
    await _apply_tags(db, post, tags, artist_names=_artist_name_set(artist_tags))

    # pool 適用
    if pool:
        await _append_to_pool(db, post, pool, source_url=pool_src)

    # Visibility contract (broker task 3faa322a): a 2xx response MUST
    # mean this post is already visible to GET /api/posts?query=id:<id> — do
    # the cull-materialize work (normally scripts/backfill_media.py Pass 2,
    # run on a 10min timer) INLINE so there is no "200 but invisible for up
    # to 10 minutes" window (the 2026-07-13 860714-860719 incident this task
    # closes out). Near-dup CLUSTERING (Pass 1) is deliberately NOT run here
    # — it needs the whole corpus to cluster against, is DB-only/cheap but
    # not "single request" shaped, and pool-member posts (the vast majority
    # of pipeline volume — every collection_url upload sets primary_pool_id)
    # are already exempted from cross-pool near-dup culling by design (see
    # backfill_media.py's pool_members exemption). Only pool-less manual
    # uploads lose near-dup detection by skipping Pass 1 here — a narrow,
    # accepted gap, not a fleet-wide one. On sync-materialize failure (rare:
    # corrupt-in-a-way _dhash_and_bands missed, encode error, etc.) post
    # stays 'pending' — the periodic backstop cull retries it — and the
    # response degrades to 202 so callers can distinguish instead of getting
    # a silently-lying 200.
    materialized = await _synchronous_materialize(db, post, data, thumb_dest)
    # Preserve verified source identity even when conversion must be retried.
    post.sha256 = sha256
    return Ingested(post, md5, sha256, False, materialized, (dest, thumb_dest))


async def store(db: AsyncSession, data, *, record=None, **fields) -> Ingested:
    """`ingest`, then `record(db, ingested)` in the same transaction, then one commit.

    `record` is how a caller adds its own facts about the upload (an extension's
    receipt) so that they commit, or fail, together with the post.
    """
    result = await ingest(db, data, **fields)
    try:
        if record is not None:
            await record(db, result)
    except BaseException:
        # Nothing is committed yet: the new post's files will never be referenced.
        _discard(result.written)
        raise
    try:
        await db.commit()
    except IntegrityError as exc:
        # The flush was refused, so the transaction did not commit and the files
        # are orphans. (Any other commit error leaves the outcome unknown — the
        # server may have committed before the connection dropped — so the files
        # stay: deleting them could take the original from a stored post.)
        _discard(result.written)
        # Non-upload writers need not take our advisory lock. If one commits
        # the same original first, roll back and apply THIS request's metadata
        # to the SHA256-verified winner as well (#146); do not just return its ID.
        if not _is_sha256_conflict(exc):
            raise
        await db.rollback()
        winner = await ingest(db, data, existing_only=True, **fields)
        if winner is None:
            # The conflicting row is gone again (deleted between the failed
            # commit and this read). Say so instead of inventing an id.
            raise
        result = winner
        if record is not None:
            await record(db, result)
        await db.commit()
    return result


def _discard(paths) -> None:
    for orphan in paths:
        try:
            orphan.unlink(missing_ok=True)
        except OSError:
            pass


def upload_response(result: Ingested):
    body = {"id": result.post.post_id, "md5": result.md5,
            "duplicate": result.duplicate, "status": result.post.status}
    if result.materialized:
        return body
    return JSONResponse(status_code=202, content=body)


async def receive(file: UploadFile, handle):
    """Run `handle(data)` over the upload's bytes, to completion even if the client leaves."""
    from api.services.upload_input import mapped_upload
    from api.services.owned_work import await_owned
    async with mapped_upload(file) as data:
        return await await_owned(asyncio.create_task(handle(data)))


# The form fields /api/upload takes. Anything else is refused before the file is
# read: a caller still sending fields this route used to record (an extension's
# own protocol) fails loudly instead of losing them.
UPLOAD_FIELDS = frozenset({"file", "tags", "source", "safety", "pool", "pool_source_url",
                           "collection_url", "artist_tags"})


@router.post("/upload")
async def upload_post(
    file: UploadFile,
    tags: str = Form(""),
    source: str = Form(""),
    safety: str = Form("unsafe"),
    pool: str = Form(""),
    pool_source_url: str = Form(""),
    collection_url: str = Form(""),
    # artist-category propagation plan Phase 1: 呼び出し側が知っている
    # 「作家名タグ」の宣言 (空白区切り)。tags と同じ正規化で照合される。
    artist_tags: str = Form(""),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
    request: Request = None,
):
    if request is not None:
        unknown = sorted(set((await request.form()).keys()) - UPLOAD_FIELDS)
        if unknown:
            raise unknown_upload_fields(unknown, sorted(UPLOAD_FIELDS))
    return await receive(file, lambda data: _upload_post(
        file=file, data=data, tags=tags, source=source, safety=safety,
        pool=pool, pool_source_url=pool_source_url, collection_url=collection_url,
        artist_tags=artist_tags, db=db, user=user))


async def _upload_post(
    file: UploadFile,
    data,
    tags: str = "",
    source: str = "",
    safety: str = "unsafe",
    pool: str = "",
    pool_source_url: str = "",
    collection_url: str = "",
    artist_tags: str = "",
    db: AsyncSession = None,
    user: User = None,
):
    """ファイルをアップロードして kura DB + /data/posts/ に保存する。"""
    result = await store(
        db, data, filename=file.filename, tags=tags, source=source, safety=safety,
        pool=pool, pool_source_url=pool_source_url, collection_url=collection_url,
        artist_tags=artist_tags)
    return upload_response(result)


def _artist_name_set(artist_tags: str) -> frozenset[str]:
    """caller 宣言 (artist_tags) の作家名を、_get_or_create_tag と同じ正規化で照合集合にする。"""
    names = [t.replace(" ", "_").strip().lower()
             for t in artist_tags.replace(",", " ").split() if t.strip()]
    return frozenset(n for n in names if n)


async def _apply_tags(db: AsyncSession, post: Post, tags_str: str,
                      artist_names: frozenset[str] = frozenset()) -> None:
    """upload/import 経由でタグを付与する唯一の経路。

    plan kura-tagger-audit-and-remediation rev6: ここで書き込む行は無条件に
    locked=True にする — カテゴリや名前パターンでの判定はしない。この関数は
    構造的に upload 元 (source) の metadata 由来のタグのみを受け取る(WD14 推論は
    完全に別経路の complete_auto_tag を通る、ここは呼ばれない)。locked=True
    が「upload 起源だから reap 対象外」を保証する唯一の signal。
    """
    if not tags_str.strip():
        return
    tag_names = [t.strip() for t in tags_str.replace(",", " ").split() if t.strip()]
    # 既存タグ ID を取得
    r = await db.execute(select(PostTag).where(PostTag.post_id == post.post_id))
    existing_by_id = {pt.tag_id: pt for pt in r.scalars()}
    seen_this_call: set[int] = set()
    for name in tag_names:
        try:
            # artist_names に含まれる名は呼び出し元が「作家名」と宣言したもの —
            # category hint 付きで get-or-create する (Phase 1)。
            norm = name.replace(" ", "_").strip().lower()
            hint = "artist" if norm in artist_names else None
            tag = await _get_or_create_tag(db, name, category_hint=hint)
            if tag.tag_id in seen_this_call:
                continue  # tags_str 内の重複名 (既にこの呼び出しで処理済み)
            seen_this_call.add(tag.tag_id)
            existing = existing_by_id.get(tag.tag_id)
            if existing is None:
                db.add(PostTag(post_id=post.post_id, tag_id=tag.tag_id, locked=True))
            elif not existing.locked:
                # 再 upload で既存タグが unlocked のままなら昇格 (upload 起源と再確認)
                existing.locked = True
        except Exception:
            pass  # タグ名不正はスキップ
    # implication 展開 (例: character → copyright)。raw SQL から見えるよう先に flush。
    # upload 経由の implication も locked=True (親タグが upload 起源なので子も同様)。
    await db.flush()
    await expand_implications(db, post.post_id, locked=True)


async def _append_to_pool(
    db: AsyncSession, post: Post, pool_name: str, source_url: str | None = None
) -> None:
    """2026-07-21 fix: was a check-then-act race (SELECT "already in pool?"
    → SELECT max(order) → plain INSERT) — under concurrent uploads to the
    SAME pool (e.g. a burst upload of many pages from one gallery, or this
    call racing the primary_pool_id trigger's own pool_post insert,
    migration 0034's attach trigger) two requests could both
    pass the "not yet in pool" check before either committed, then both
    try to INSERT the same (pool_id, post_id) row — asyncpg
    UniqueViolationError on pool_post_pkey, occasionally escalating to a
    genuine DeadlockDetectedError between the two racing transactions (user
    report 2026-07-21, live in kura-api's journal at the time). Collapsed
    to one atomic statement: ON CONFLICT DO NOTHING makes a duplicate a
    silent no-op instead of a race, matching the already-safe pattern this
    same file uses elsewhere (the collection_url M:N-attach branch above)."""
    pool = await _get_or_create_pool(db, pool_name, source_url=source_url)
    await db.execute(sa.text(
        "INSERT INTO pool_post (pool_id, post_id, ord) "
        "SELECT :p, :i, COALESCE(MAX(ord), -1) + 1 FROM pool_post WHERE pool_id = :p "
        "ON CONFLICT (pool_id, post_id) DO NOTHING"
    ), {"p": pool.pool_id, "i": post.post_id})
