"""Post variant router (issue #2 — non-destructive AI reconstruction).

Serves and manages `post_variant` rows: an ALTERNATE rendering of a post's image
(e.g. an AI "decensored" pass) that NEVER touches the original. The original
Post.data/thumbnail/sha256 stay byte-identical; the variant has its own blobs
and its own content/thumbnail routes.

Honesty (design rev2 §3): kind is `ai_reconstruction` — fabricated, not
recovered. A variant is created `unreviewed` and the viewer only offers a
toggle for variants a human has set `approved`. `source_sha256`/
`source_version_seen` bind the original image version, so a later image replace
flags the variant `stale` at read time (post.sha256 != source_sha256).

Concurrency: PATCH (review-state) requires the current regionVersion (409 on
mismatch). One current variant per (post_id, kind) — POST upserts/replaces.
"""

import hashlib
import json
from datetime import datetime, timezone
from io import BytesIO

from fastapi import APIRouter, Depends, File, Form, HTTPException, Response, UploadFile
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from PIL import Image

from api.db.models import Post, PostVariant, User
from api.db.session import get_db
from api.auth import get_current_user

router = APIRouter(prefix="/api", tags=["variant"])

VARIANT_KINDS = {"ai_reconstruction"}
REVIEW_STATES = {"unreviewed", "approved", "rejected"}

_MAX_EDGE = 1600   # match post.data cap (migration 0025)
_THUMB = 320       # match post.thumbnail (~320px WebP)


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def _get_post(db: AsyncSession, post_id: int) -> Post:
    post = (await db.execute(select(Post).where(Post.post_id == post_id))).scalar_one_or_none()
    if post is None:
        raise HTTPException(404, "Post not found")
    return post


def _encode_webp(raw: bytes) -> tuple[bytes, bytes, int, int]:
    """raw image bytes -> (webp_full, webp_thumb, width, height). Full is capped
    to _MAX_EDGE long-edge (matches post.data); both are WebP."""
    im = Image.open(BytesIO(raw))
    if im.mode not in ("RGB", "RGBA"):
        im = im.convert("RGB")
    if im.mode == "RGBA":
        im = im.convert("RGB")
    w, h = im.size
    if max(w, h) > _MAX_EDGE:
        scale = _MAX_EDGE / max(w, h)
        im = im.resize((round(w * scale), round(h * scale)), Image.LANCZOS)
        w, h = im.size
    full = BytesIO()
    im.save(full, format="WEBP", quality=90, method=4)
    thumb_im = im.copy()
    thumb_im.thumbnail((_THUMB, _THUMB), Image.LANCZOS)
    thumb = BytesIO()
    thumb_im.save(thumb, format="WEBP", quality=80, method=4)
    return full.getvalue(), thumb.getvalue(), w, h


def _variant_content_url(v: PostVariant) -> str:
    # region_version busts the cache when a variant is regenerated/approved
    return f"/api/post/{v.post_id}/variant/{v.kind}/content?v={v.region_version}"


def _variant_thumbnail_url(v: PostVariant) -> str:
    return f"/api/post/{v.post_id}/variant/{v.kind}/thumbnail?v={v.region_version}"


def _serialize(v: PostVariant, post: Post) -> dict:
    stale = (
        v.source_sha256 is not None
        and post.sha256 is not None
        and bytes(v.source_sha256) != bytes(post.sha256)
    )
    return {
        "id": v.id,
        "postId": v.post_id,
        "kind": v.kind,
        "reviewState": v.review_state,
        "stale": stale,
        "model": v.model,
        "params": v.params,
        "canvasWidth": v.canvas_width,
        "canvasHeight": v.canvas_height,
        "mimeType": v.mime_type,
        "regionVersion": v.region_version,
        "createdAt": v.created_at.isoformat() if v.created_at else None,
        "updatedAt": v.updated_at.isoformat() if v.updated_at else None,
        "contentUrl": _variant_content_url(v),
        "thumbnailUrl": _variant_thumbnail_url(v),
    }


# ── reads (public, mirrors post content/thumbnail routes) ───────────────

@router.get("/post/{post_id}/variants")
async def list_variants(post_id: int, db: AsyncSession = Depends(get_db)):
    post = await _get_post(db, post_id)
    rows = (await db.execute(
        select(PostVariant).where(PostVariant.post_id == post_id)
        .order_by(PostVariant.kind)
    )).scalars().all()
    return {"variants": [_serialize(v, post) for v in rows]}


@router.get("/variant/status")
async def variant_status_batch(post_ids: str = "", db: AsyncSession = Depends(get_db)):
    """Batch status for the viewer/grid (mirrors /censor/status). For each
    requested post, return its ai_reconstruction variant summary (approved
    preferred) so the stream can offer a per-page toggle WITHOUT N fetches.
    Blobs are deferred — this never materialises image bytes."""
    ids = [int(x) for x in post_ids.split(",") if x.strip().lstrip("-").isdigit()][:300]
    if not ids:
        return {"results": {}}
    rows = (await db.execute(
        select(PostVariant).where(PostVariant.post_id.in_(ids))
    )).scalars().all()
    posts = {p.post_id: p for p in (await db.execute(
        select(Post).where(Post.post_id.in_(ids)))).scalars().all()}
    out: dict[int, dict] = {}
    for v in rows:
        post = posts.get(v.post_id)
        if post is None:
            continue
        cand = _serialize(v, post)
        cur = out.get(v.post_id)
        # one summary per post; an approved variant wins over a non-approved one
        if cur is None or (cand["reviewState"] == "approved"
                           and cur["reviewState"] != "approved"):
            out[v.post_id] = cand
    return {"results": {str(k): val for k, val in out.items()}}


async def _load_variant(db: AsyncSession, post_id: int, kind: str) -> PostVariant:
    v = (await db.execute(
        select(PostVariant).where(
            PostVariant.post_id == post_id, PostVariant.kind == kind)
    )).scalar_one_or_none()
    if v is None:
        raise HTTPException(404, "Variant not found")
    return v


@router.get("/post/{post_id}/variant/{kind}/content")
async def variant_content(post_id: int, kind: str, db: AsyncSession = Depends(get_db)):
    # blob is deferred; fetch only this row's body
    data = (await db.execute(
        select(PostVariant.data).where(
            PostVariant.post_id == post_id, PostVariant.kind == kind)
    )).scalar_one_or_none()
    if data is None:
        raise HTTPException(404, "Variant content not found")
    return Response(bytes(data), media_type="image/webp",
                    headers={"Cache-Control": "public, max-age=86400"})


@router.get("/post/{post_id}/variant/{kind}/thumbnail")
async def variant_thumbnail(post_id: int, kind: str, db: AsyncSession = Depends(get_db)):
    thumb = (await db.execute(
        select(PostVariant.thumbnail).where(
            PostVariant.post_id == post_id, PostVariant.kind == kind)
    )).scalar_one_or_none()
    if thumb is None:
        raise HTTPException(404, "Variant thumbnail not found")
    return Response(bytes(thumb), media_type="image/webp",
                    headers={"Cache-Control": "public, max-age=86400"})


# ── writes (auth required) ──────────────────────────────────────────────

@router.post("/post/{post_id}/variant", status_code=201)
async def create_variant(
    post_id: int,
    file: UploadFile = File(...),
    kind: str = Form("ai_reconstruction"),
    model: str = Form(None),
    params: str = Form(None),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    if kind not in VARIANT_KINDS:
        raise HTTPException(422, f"kind must be one of {sorted(VARIANT_KINDS)}")
    parsed_params = None
    if params:
        try:
            parsed_params = json.loads(params)
        except ValueError:
            raise HTTPException(422, "params must be valid JSON")
    post = await _get_post(db, post_id)
    raw = await file.read()
    if not raw:
        raise HTTPException(422, "empty file")
    try:
        full, thumb, w, h = _encode_webp(raw)
    except Exception as e:  # PIL decode failure
        raise HTTPException(422, f"cannot decode image: {e}")
    vsha = hashlib.sha256(full).digest()

    existing = (await db.execute(
        select(PostVariant).where(
            PostVariant.post_id == post_id, PostVariant.kind == kind)
    )).scalar_one_or_none()
    if existing is not None:
        # replace in place; bump region_version (cache-bust) and reset review
        existing.data = full
        existing.thumbnail = thumb
        existing.canvas_width = w
        existing.canvas_height = h
        existing.file_size = len(full)
        existing.variant_sha256 = vsha
        existing.mime_type = "image/webp"
        existing.model = model
        existing.params = parsed_params
        existing.source_sha256 = post.sha256
        existing.source_version_seen = post.version
        existing.review_state = "unreviewed"
        existing.region_version = existing.region_version + 1
        existing.updated_at = _now()
        v = existing
    else:
        v = PostVariant(
            post_id=post_id, kind=kind,
            source_sha256=post.sha256, source_version_seen=post.version,
            mime_type="image/webp", canvas_width=w, canvas_height=h,
            file_size=len(full), variant_sha256=vsha, data=full, thumbnail=thumb,
            model=model, params=parsed_params,
            review_state="unreviewed", region_version=1,
            created_by=user.id, created_at=_now(),
        )
        db.add(v)
    await db.commit()
    await db.refresh(v)
    return _serialize(v, post)


@router.patch("/post/{post_id}/variant/{kind}")
async def update_variant(
    post_id: int, kind: str,
    review_state: str = Form(...),
    region_version: int = Form(...),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    if review_state not in REVIEW_STATES:
        raise HTTPException(422, f"review_state must be one of {sorted(REVIEW_STATES)}")
    v = await _load_variant(db, post_id, kind)
    if v.region_version != region_version:
        raise HTTPException(409, f"regionVersion mismatch (current {v.region_version})")
    v.review_state = review_state
    v.region_version = v.region_version + 1
    v.updated_at = _now()
    await db.commit()
    await db.refresh(v)
    post = await _get_post(db, post_id)
    return _serialize(v, post)


@router.delete("/post/{post_id}/variant/{kind}", status_code=200)
async def delete_variant(
    post_id: int, kind: str,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    v = await _load_variant(db, post_id, kind)
    await db.delete(v)
    await db.commit()
    return {"deleted": True, "postId": post_id, "kind": kind}
