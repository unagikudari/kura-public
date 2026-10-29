"""Display renditions as immutable, content-addressed files (docs/notes/rendition-store-ideal.md).

Bytes are named by their own SHA-256 and written once. The database holds only
facts about them: `rendition_object` (what the bytes are) and `post_rendition`
(which post uses which bytes for what). Nothing here re-encodes: callers pass the
exact bytes to keep, so moving the legacy `post.data`/`post.thumbnail` columns
out is a copy, not a transcode.

Order of durability: the file is published (fsync + rename + dir fsync) BEFORE
the rows that point at it are committed. A crash in between leaves an orphan
file that no row names — harmless, re-written byte-identically on retry, and
collectable by `orphans()`. The opposite order could commit a row that names
bytes which do not exist, and that would serve a 404 for a post that has data.
"""
from __future__ import annotations

import hashlib
import os
import re
import uuid
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path

from api.config import DATA_DIR

ROOT = Path(os.environ.get("KURA_RENDITION_ROOT") or DATA_DIR / "renditions")

KINDS = ("view", "thumb")
# The recipe says how the bytes were made, so a later policy (other cap/quality)
# is a different recipe instead of a silent overwrite. Legacy bytes keep the
# recipe they were produced with (api/services/media_materialize.py).
LEGACY_RECIPE = {"view": "webp-cap1600-q82", "thumb": "webp-cap320-q75"}

_EXT = {"image/webp": "webp", "image/jpeg": "jpg", "image/png": "png", "image/gif": "gif"}


@dataclass(frozen=True)
class Stored:
    sha256: bytes
    mime: str
    byte_size: int
    width: int
    height: int
    path: Path


def path_for(sha256: bytes, mime: str, root: Path | None = None) -> Path:
    h = sha256.hex()
    return (root or ROOT) / h[:2] / f"{h}.{_EXT[mime]}"


def probe(data: bytes) -> tuple[str, int, int]:
    """(mime, width, height) of image bytes, read from the header only."""
    from PIL import Image
    with Image.open(BytesIO(data)) as im:
        mime = Image.MIME.get(im.format or "")
        if mime not in _EXT:
            raise ValueError(f"unsupported rendition format: {im.format!r}")
        return mime, im.width, im.height


def store(data: bytes, root: Path | None = None, sync: bool = True) -> Stored:
    """Publish `data` under its content address. Idempotent: same bytes → same file.

    sync=False skips the per-file fsync; the caller must then call `flush()`
    before committing any row that names the file (a bulk copy syncs once per
    batch instead of once per file — the ordering guarantee is the same).
    """
    if not data:
        raise ValueError("empty rendition")
    sha = hashlib.sha256(data).digest()
    mime, width, height = probe(data)
    path = path_for(sha, mime, root)
    # An existing file is reused only if its bytes hash to its name. Size alone
    # would accept a truncated-then-padded or bit-rotted file (review 2026-10-24);
    # a bad one is replaced atomically below, like a missing one.
    if path.exists() and verify(path, sha):
        return Stored(sha, mime, len(data), width, height, path)
    path.parent.mkdir(parents=True, exist_ok=True)
    # Unique per writer: two threads of one process storing the same bytes must
    # not share a temp name (one would rename the other's file away).
    tmp = path.with_name(f".{path.name}.tmp.{os.getpid()}.{uuid.uuid4().hex}")
    try:
        with open(tmp, "wb") as f:
            f.write(data)
            if sync:
                f.flush()
                os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.lexists(tmp):
            os.unlink(tmp)
    if sync:
        _fsync_dir(path.parent)
    return Stored(sha, mime, len(data), width, height, path)


def _fsync_dir(d: Path) -> None:
    dfd = os.open(d, os.O_RDONLY)
    try:
        os.fsync(dfd)
    finally:
        os.close(dfd)


def flush() -> None:
    """Make every file written with sync=False durable (data and renames)."""
    os.sync()


def verify(path: Path, sha256: bytes) -> bool:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.digest() == sha256


# ── rows (plain SQL so the API, the backfill and tests share one definition) ──
# Written with SQLAlchemy `:name` binds; `psycopg()` gives the psycopg2 spelling.


def psycopg(sql: str) -> str:
    return re.sub(r"(?<![:\w]):(\w+)", r"%(\1)s", sql)


UPSERT_OBJECT = """
INSERT INTO rendition_object (sha256, mime, byte_size, width, height)
VALUES (:sha256, :mime, :byte_size, :width, :height)
ON CONFLICT (sha256) DO NOTHING
"""

# Re-attaching the same bytes is a no-op; attaching different bytes for the same
# (post, kind) replaces the reference — the old object stays until GC finds it
# unreferenced, so a reader mid-request never loses its file.
ATTACH = """
INSERT INTO post_rendition (post_id, kind, sha256, recipe)
VALUES (:post_id, :kind, :sha256, :recipe)
ON CONFLICT (post_id, kind) DO UPDATE
   SET sha256 = EXCLUDED.sha256, recipe = EXCLUDED.recipe, attached_at = now()
 WHERE (post_rendition.sha256, post_rendition.recipe) IS DISTINCT FROM (EXCLUDED.sha256, EXCLUDED.recipe)
"""

LOOKUP = """
SELECT o.sha256, o.mime, o.byte_size, o.width, o.height
  FROM post_rendition r JOIN rendition_object o USING (sha256)
 WHERE r.post_id = :post_id AND r.kind = :kind
"""

# Objects no post references. GC deletes the file first, then the row, so a
# concurrent attach that re-references the bytes recreates the file via store().
UNREFERENCED = """
SELECT o.sha256, o.mime FROM rendition_object o
 WHERE NOT EXISTS (SELECT 1 FROM post_rendition r WHERE r.sha256 = o.sha256)
"""


def attach_params(post_id: int, kind: str, stored: Stored, recipe: str) -> tuple[dict, dict]:
    if kind not in KINDS:
        raise ValueError(f"unknown rendition kind {kind!r}")
    obj = {"sha256": stored.sha256, "mime": stored.mime, "byte_size": stored.byte_size,
           "width": stored.width, "height": stored.height}
    ref = {"post_id": post_id, "kind": kind, "sha256": stored.sha256, "recipe": recipe}
    return obj, ref


def orphans(root: Path | None = None, known: set[bytes] | None = None):
    """Files under the root that no `rendition_object` row names (crash leftovers)."""
    root = root or ROOT
    for sub in sorted(root.glob("??")):
        for p in sub.iterdir():
            if p.name.startswith("."):
                yield p  # an interrupted tmp
                continue
            try:
                sha = bytes.fromhex(p.stem)
            except ValueError:
                continue
            if known is not None and sha not in known:
                yield p


async def attach_bytes(db, post_id: int, blobs: dict[str, bytes | None], recipes: dict[str, str]) -> None:
    """Publish each kind's bytes and point the post at them, in the caller's transaction.

    Files first (fsync'd), rows after: the caller's commit is what makes them
    visible. If that commit never happens, the files are unreferenced orphans.
    """
    import asyncio
    from sqlalchemy import text
    loop = asyncio.get_running_loop()
    for kind, blob in blobs.items():
        if not blob:
            continue
        stored = await loop.run_in_executor(None, store, blob)
        obj, ref = attach_params(post_id, kind, stored, recipes[kind])
        await db.execute(text(UPSERT_OBJECT), obj)
        await db.execute(text(ATTACH), ref)


# ── serving ───────────────────────────────────────────────────────────────────

LOOKUP_SA = """
SELECT o.sha256, o.mime FROM post_rendition r JOIN rendition_object o USING (sha256)
 WHERE r.post_id = :post_id AND r.kind = :kind
"""


async def response_for(db, request, post_id: int, kind: str):
    """A file response for the post's rendition, or None to fall back to the legacy column.

    ETag is the content address, so a client holding the same bytes gets 304
    without the file being opened. Range is handled by Starlette's FileResponse.
    A row whose file is missing falls back (and is logged) rather than 404ing a
    post that still has its legacy bytes.
    """
    from sqlalchemy import text
    from starlette.responses import FileResponse, Response
    row = (await db.execute(text(LOOKUP_SA), {"post_id": post_id, "kind": kind})).first()
    if row is None:
        return None
    sha, mime = bytes(row[0]), row[1]
    etag = f'"{sha.hex()}"'
    headers = {"ETag": etag, "Cache-Control": "public, max-age=86400"}
    if etag in (request.headers.get("if-none-match") or ""):
        return Response(status_code=304, headers=headers)
    path = path_for(sha, mime)
    if not path.exists():
        import logging
        logging.getLogger(__name__).error(
            "rendition file missing for post %s %s: %s", post_id, kind, path)
        return None
    return FileResponse(path, media_type=mime, headers=headers)
