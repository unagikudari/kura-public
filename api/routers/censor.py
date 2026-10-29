"""Censor annotation router (issue #2 — human-annotation minimal slice).

CRUD over `censor_annotation` (1:0..N per post). Each annotation is bound to the
image version it was drawn on (post.sha256 + post.version) and goes `stale` when
the post's image is replaced. Geometry is normalized [0,1] in the served
canonical image space (post.data WebP, canvas_width×canvas_height); the server
rasterizes an immutable PNG `mask` from it at write time (the reproduction form;
geometry is the editable form). Concurrency: POST is idempotent on
clientMutationUuid; PATCH/DELETE require the current regionVersion (409 on
mismatch); DELETE is a soft-delete tombstone.

Review-state + coverage + export live in sibling endpoints (later commits).
"""

import csv
import json
from datetime import datetime, timezone
from io import BytesIO, StringIO
from uuid import UUID

from collections import defaultdict

from fastapi import APIRouter, Body, Depends, HTTPException, Response
from sqlalchemy import select, func, text, bindparam
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from PIL import Image, ImageDraw

from api.db.models import CensorAnnotation, PostCensorReview, Post, User
from api.db.session import get_db
from api.auth import get_current_user

router = APIRouter(prefix="/api", tags=["censor"])

CENSOR_TYPES = {"mosaic", "white_bar", "black_bar", "slash", "other"}
SHAPE_TYPES = {"rect", "circle", "ellipse", "polygon", "pen_path"}
REVIEW_STATES = {"partial", "complete"}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _validate_geometry(shape_type: str, geom: dict) -> None:
    """Cheap structural check — values must be normalized [0,1]."""
    if not isinstance(geom, dict):
        raise HTTPException(422, "geometry must be an object")
    req = {
        "rect": ("x", "y", "w", "h"),
        "circle": ("cx", "cy", "r"),
        "ellipse": ("cx", "cy", "rx", "ry"),
        "polygon": ("points",),
        "pen_path": ("points",),
    }[shape_type]
    for k in req:
        if k not in geom:
            raise HTTPException(422, f"geometry.{k} required for {shape_type}")
    if shape_type in ("polygon", "pen_path"):
        pts = geom["points"]
        if not isinstance(pts, list) or len(pts) < (3 if shape_type == "polygon" else 2):
            raise HTTPException(422, f"{shape_type} needs >= "
                                f"{3 if shape_type == 'polygon' else 2} points")


def _rasterize(shape_type: str, geom: dict, w: int, h: int) -> bytes | None:
    """Render the normalized geometry into an immutable 8-bit PNG mask (255=censor)."""
    if not w or not h:
        return None
    img = Image.new("L", (w, h), 0)
    d = ImageDraw.Draw(img)
    sx, sy = float(w), float(h)
    if shape_type == "rect":
        x, y, gw, gh = geom["x"], geom["y"], geom["w"], geom["h"]
        d.rectangle([x * sx, y * sy, (x + gw) * sx, (y + gh) * sy], fill=255)
    elif shape_type in ("circle", "ellipse"):
        cx, cy = geom["cx"] * sx, geom["cy"] * sy
        rx = geom.get("rx", geom.get("r")) * sx
        ry = geom.get("ry", geom.get("r")) * sx   # r normalized to width
        d.ellipse([cx - rx, cy - ry, cx + rx, cy + ry], fill=255)
    elif shape_type == "polygon":
        d.polygon([(p[0] * sx, p[1] * sy) for p in geom["points"]], fill=255)
    elif shape_type == "pen_path":
        # variable-width stroke: a thick pen swipe covers a diagonal bar/slash
        # in one go (stroke_width normalized to image width). Use polygon to
        # enclose an area instead.
        pts = [(p[0] * sx, p[1] * sy) for p in geom["points"]]
        sw = max(1, round(float(geom.get("stroke_width", 0.02)) * sx))
        if len(pts) >= 2:
            d.line(pts, fill=255, width=sw, joint="curve")
        r = sw // 2                                # round caps
        for px_, py_ in pts:
            d.ellipse([px_ - r, py_ - r, px_ + r, py_ + r], fill=255)
    buf = BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


def _serialize(a: CensorAnnotation, post: Post) -> dict:
    """camelCase view. `stale` = drawn on a now-replaced image version."""
    stale = bool(a.image_sha256) and bool(post.sha256) and a.image_sha256 != post.sha256
    return {
        "id": a.id,
        "postId": a.post_id,
        "censorType": a.censor_type,
        "shapeType": a.shape_type,
        "negative": bool(a.negative),          # eraser: subtracts from the unioned mask
        "geometry": a.geometry,
        "regionVersion": a.region_version,     # ETag for PATCH/DELETE
        "createdBy": a.created_by,
        "createdAt": a.created_at,
        "updatedAt": a.updated_at,
        "supersedesId": a.supersedes_id,
        "stale": stale,
    }


async def _get_post(db: AsyncSession, post_id: int) -> Post:
    post = (await db.execute(select(Post).where(Post.post_id == post_id))).scalar_one_or_none()
    if post is None:
        raise HTTPException(404, "Post not found")
    return post


@router.get("/post/{post_id}/censor-annotations")
async def list_annotations(
    post_id: int,
    db: AsyncSession = Depends(get_db),
) -> dict:
    post = await _get_post(db, post_id)
    rows = (await db.execute(
        select(CensorAnnotation)
        .where(CensorAnnotation.post_id == post_id,
               CensorAnnotation.deleted_at.is_(None))
        .order_by(CensorAnnotation.id)
    )).scalars().all()
    return {"results": [_serialize(a, post) for a in rows]}


@router.post("/post/{post_id}/censor-annotation", status_code=201)
async def create_annotation(
    post_id: int,
    body: dict = Body(...),
    db: AsyncSession = Depends(get_db),
    auth_user: User = Depends(get_current_user),
) -> dict:
    """Body: {censorType, shapeType, geometry, [negative], [clientMutationUuid]}."""
    censor_type = body.get("censorType")
    shape_type = body.get("shapeType")
    geometry = body.get("geometry")
    negative = bool(body.get("negative", False))   # eraser stroke = subtractive region
    if censor_type not in CENSOR_TYPES:
        raise HTTPException(422, f"censorType must be one of {sorted(CENSOR_TYPES)}")
    if shape_type not in SHAPE_TYPES:
        raise HTTPException(422, f"shapeType must be one of {sorted(SHAPE_TYPES)}")
    _validate_geometry(shape_type, geometry)

    mut = body.get("clientMutationUuid")
    mut_uuid = None
    if mut:
        try:
            mut_uuid = UUID(str(mut))
        except ValueError:
            raise HTTPException(422, "clientMutationUuid must be a UUID")
        # idempotency: a re-sent POST returns the existing row unchanged
        existing = (await db.execute(select(CensorAnnotation).where(
            CensorAnnotation.client_mutation_uuid == mut_uuid))).scalar_one_or_none()
        if existing is not None:
            return _serialize(existing, await _get_post(db, existing.post_id))

    post = await _get_post(db, post_id)
    now = _now()
    ann = CensorAnnotation(
        post_id=post_id,
        image_sha256=post.sha256,
        post_version_seen=post.version,
        censor_type=censor_type,
        shape_type=shape_type,
        negative=negative,
        geometry=geometry,
        mask=_rasterize(shape_type, geometry, post.canvas_width, post.canvas_height),
        created_by=auth_user.user_id,
        created_at=now,
        updated_at=now,
        region_version=1,
        client_mutation_uuid=mut_uuid,
    )
    db.add(ann)
    await db.commit()
    await db.refresh(ann)
    return _serialize(ann, post)


async def _get_live(db: AsyncSession, post_id: int, aid: int) -> CensorAnnotation:
    ann = (await db.execute(select(CensorAnnotation).where(
        CensorAnnotation.id == aid,
        CensorAnnotation.post_id == post_id,
        CensorAnnotation.deleted_at.is_(None)))).scalar_one_or_none()
    if ann is None:
        raise HTTPException(404, "Annotation not found")
    return ann


def _check_version(ann: CensorAnnotation, body: dict) -> None:
    expected = body.get("regionVersion")
    if expected is None:
        raise HTTPException(428, "regionVersion required (precondition)")
    if int(expected) != ann.region_version:
        raise HTTPException(409, "Region version conflict — reload and retry")


@router.patch("/post/{post_id}/censor-annotation/{aid}")
async def update_annotation(
    post_id: int,
    aid: int,
    body: dict = Body(...),
    db: AsyncSession = Depends(get_db),
    auth_user: User = Depends(get_current_user),
) -> dict:
    """Body: {regionVersion, [censorType], [shapeType], [geometry]}."""
    ann = await _get_live(db, post_id, aid)
    _check_version(ann, body)
    post = await _get_post(db, post_id)

    shape_type = body.get("shapeType", ann.shape_type)
    geometry = body.get("geometry", ann.geometry)
    censor_type = body.get("censorType", ann.censor_type)
    if censor_type not in CENSOR_TYPES:
        raise HTTPException(422, f"censorType must be one of {sorted(CENSOR_TYPES)}")
    if shape_type not in SHAPE_TYPES:
        raise HTTPException(422, f"shapeType must be one of {sorted(SHAPE_TYPES)}")
    if "geometry" in body or "shapeType" in body:
        _validate_geometry(shape_type, geometry)
        ann.mask = _rasterize(shape_type, geometry, post.canvas_width, post.canvas_height)

    ann.censor_type = censor_type
    ann.shape_type = shape_type
    if "negative" in body:
        ann.negative = bool(body["negative"])
    ann.geometry = geometry
    ann.region_version += 1
    ann.updated_at = _now()
    await db.commit()
    await db.refresh(ann)
    return _serialize(ann, post)


@router.delete("/post/{post_id}/censor-annotation/{aid}", status_code=200)
async def delete_annotation(
    post_id: int,
    aid: int,
    body: dict = Body(default={}),
    db: AsyncSession = Depends(get_db),
    auth_user: User = Depends(get_current_user),
) -> dict:
    """Soft-delete (tombstone). Body: {regionVersion}."""
    ann = await _get_live(db, post_id, aid)
    _check_version(ann, body)
    ann.deleted_at = _now()
    ann.region_version += 1
    await db.commit()
    return {"id": aid, "deleted": True}


# ── Review state ────────────────────────────────────────────────────────
# Post-level review: distinguishes 'unreviewed' (no row) from a human having
# looked. `hasNoCensor` is the explicit "looked, it's clean" signal — the
# source of training NEGATIVES and the denominator for any future statistics.

def _serialize_review(r: PostCensorReview | None, post: Post) -> dict:
    if r is None:
        return {"postId": post.post_id, "state": "unreviewed",
                "hasNoCensor": False, "reviewedBy": None, "reviewedAt": None,
                "stale": False}
    stale = bool(r.image_sha256) and bool(post.sha256) and r.image_sha256 != post.sha256
    return {"postId": r.post_id, "state": r.state, "hasNoCensor": r.has_no_censor,
            "reviewedBy": r.reviewed_by, "reviewedAt": r.reviewed_at, "stale": stale}


@router.get("/post/{post_id}/censor-review")
async def get_review(
    post_id: int,
    db: AsyncSession = Depends(get_db),
) -> dict:
    post = await _get_post(db, post_id)
    r = (await db.execute(select(PostCensorReview).where(
        PostCensorReview.post_id == post_id))).scalar_one_or_none()
    return _serialize_review(r, post)


@router.put("/post/{post_id}/censor-review")
async def put_review(
    post_id: int,
    body: dict = Body(...),
    db: AsyncSession = Depends(get_db),
    auth_user: User = Depends(get_current_user),
) -> dict:
    """Body: {state: partial|complete, [hasNoCensor]}. Idempotent upsert."""
    state = body.get("state", "complete")
    if state not in REVIEW_STATES:
        raise HTTPException(422, f"state must be one of {sorted(REVIEW_STATES)}")
    has_no_censor = bool(body.get("hasNoCensor", False))
    post = await _get_post(db, post_id)
    now = _now()
    vals = dict(post_id=post_id, image_sha256=post.sha256,
                post_version_seen=post.version, state=state,
                has_no_censor=has_no_censor, reviewed_by=auth_user.user_id,
                reviewed_at=now)
    # race-safe upsert on the UNIQUE(post_id) constraint
    stmt = pg_insert(PostCensorReview.__table__).values(**vals).on_conflict_do_update(
        index_elements=["post_id"],
        set_={k: vals[k] for k in
              ("image_sha256", "post_version_seen", "state",
               "has_no_censor", "reviewed_by", "reviewed_at")},
    )
    await db.execute(stmt)
    await db.commit()
    r = (await db.execute(select(PostCensorReview).where(
        PostCensorReview.post_id == post_id))).scalar_one()
    return _serialize_review(r, post)


# ── censor-pair: censored↔uncensored twin lookup (lightbox two-up view) ──
# censor_pair rows are written by the pair-assignment pipeline
# (kura/data/censor-pairs/assign_pairs.py); this endpoint only reads them.

@router.get("/post/{post_id}/censor-pair")
async def get_censor_pair(
    post_id: int,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Bidirectional censor_pair lookup — 404 when the post has no twin.

    Returns the pair row plus the PARTNER post serialized in the standard
    Post shape, so the lightbox renders censored|uncensored two-up without
    a second round-trip.
    """
    row = (await db.execute(text(
        """select censored_post_id, uncensored_post_id, method,
                  off_censor_pct, confirmed
           from censor_pair
           where censored_post_id = :p or uncensored_post_id = :p
           limit 1"""), {"p": post_id})).first()
    if row is None:
        raise HTTPException(status_code=404, detail="No censor pair for this post")
    cen, unc, method, off, confirmed = row
    partner_id = unc if post_id == cen else cen
    from sqlalchemy.orm import selectinload
    from api.db.models import Tag, PoolPost, Pool
    from api.routers.posts import _serialize_post
    stmt = (
        select(Post)
        .where(Post.post_id == partner_id)
        .options(
            selectinload(Post.tags).selectinload(Tag.names),
            selectinload(Post.tags).selectinload(Tag.category),
            selectinload(Post.user),
            selectinload(Post.favorited_by),
            selectinload(Post._pools).selectinload(PoolPost.pool).selectinload(Pool.names),
            selectinload(Post._pools).selectinload(PoolPost.pool).selectinload(Pool.category),
        )
    )
    partner = (await db.execute(stmt)).scalar_one_or_none()
    if partner is None:
        raise HTTPException(status_code=404, detail="Partner post missing")
    return {
        "role": "censored" if post_id == cen else "uncensored",
        "censoredPostId": cen,
        "uncensoredPostId": unc,
        "method": method,
        "offCensorPct": off,
        "confirmed": confirmed,
        "partner": _serialize_post(partner),
    }


# ── pair-search status: 未ペア post の探索状態 (schema: censor_pair_search /
# censor_pair_search_scope — raw 管理、探索プロセスは kura/data/censor-pairs/
# pair_search.py)。human は UI から post 単位 (pending/excluded) と pool 単位
# (enabled) のフラグを立てる。found/not_found/review は探索プロセスが書く。

PAIR_SEARCH_HUMAN_STATES = {"pending", "excluded"}


@router.get("/post/{post_id}/pair-search")
async def get_pair_search(
    post_id: int,
    db: AsyncSession = Depends(get_db),
) -> dict:
    row = (await db.execute(text(
        """select status, attempts, candidates_seen, best_candidate, best_score,
                  rejected_candidates, note, updated_at
           from censor_pair_search where post_id = :p"""), {"p": post_id})).first()
    if row is None:
        return {"postId": post_id, "status": None}
    st, att, seen, bc, bs, rej, note, upd = row
    return {"postId": post_id, "status": st, "attempts": att,
            "candidatesSeen": seen, "bestCandidate": bc, "bestScore": bs,
            "rejectedCandidates": list(rej or []), "note": note,
            "updatedAt": upd.isoformat() if upd else None}


@router.put("/post/{post_id}/pair-search")
async def put_pair_search(
    post_id: int,
    body: dict = Body(...),
    db: AsyncSession = Depends(get_db),
    auth_user: User = Depends(get_current_user),
) -> dict:
    """Body: {status: pending|excluded}. 人間の探索フラグ。upsert。"""
    st = body.get("status")
    if st not in PAIR_SEARCH_HUMAN_STATES:
        raise HTTPException(422, f"status must be one of {sorted(PAIR_SEARCH_HUMAN_STATES)}")
    await _get_post(db, post_id)
    await db.execute(text(
        """insert into censor_pair_search (post_id, status, updated_at)
           values (:p, :s, now())
           on conflict (post_id) do update set status = :s, updated_at = now()"""),
        {"p": post_id, "s": st})
    await db.commit()
    return await get_pair_search(post_id, db)


@router.get("/pool/{pool_id}/pair-search-scope")
async def get_pair_search_scope(
    pool_id: int,
    db: AsyncSession = Depends(get_db),
) -> dict:
    row = (await db.execute(text(
        "select enabled from censor_pair_search_scope where pool_id = :p"),
        {"p": pool_id})).first()
    return {"poolId": pool_id, "enabled": row[0] if row else None}


@router.put("/pool/{pool_id}/pair-search-scope")
async def put_pair_search_scope(
    pool_id: int,
    body: dict = Body(...),
    db: AsyncSession = Depends(get_db),
    auth_user: User = Depends(get_current_user),
) -> dict:
    enabled = bool(body.get("enabled", True))
    await db.execute(text(
        """insert into censor_pair_search_scope (pool_id, enabled, updated_at)
           values (:p, :e, now())
           on conflict (pool_id) do update set enabled = :e, updated_at = now()"""),
        {"p": pool_id, "e": enabled})
    await db.commit()
    return {"poolId": pool_id, "enabled": enabled}


# ── Coverage ────────────────────────────────────────────────────────────
# Raw counts only. Per-artist/source ranking is intentionally NOT exposed: with
# voluntary annotation, censor-rate-by-group is selection-biased until coverage
# is high. Stats land in a later phase once the reviewed corpus is large enough.

@router.get("/censor/coverage")
async def coverage(db: AsyncSession = Depends(get_db)) -> dict:
    total_posts = await db.scalar(select(func.count()).select_from(Post))
    total_images = await db.scalar(
        select(func.count()).select_from(Post).where(Post.type == "image"))
    reviewed_complete = await db.scalar(
        select(func.count()).select_from(PostCensorReview).where(
            PostCensorReview.state == "complete"))
    reviewed_partial = await db.scalar(
        select(func.count()).select_from(PostCensorReview).where(
            PostCensorReview.state == "partial"))
    has_no_censor = await db.scalar(
        select(func.count()).select_from(PostCensorReview).where(
            PostCensorReview.has_no_censor.is_(True)))
    annotated_posts = await db.scalar(
        select(func.count(func.distinct(CensorAnnotation.post_id))).where(
            CensorAnnotation.deleted_at.is_(None)))
    return {
        "totalPosts": total_posts,
        "totalImages": total_images,
        "reviewedComplete": reviewed_complete,
        "reviewedPartial": reviewed_partial,
        "hasNoCensor": has_no_censor,
        "annotatedPosts": annotated_posts,
        "note": ("raw coverage counts; per-artist/source ranking omitted until "
                 "coverage is sufficient (voluntary-annotation selection bias)"),
    }


# ── Status (batch) ──────────────────────────────────────────────────────
# Lightweight per-post censor status for the viewer/grid, so work progress is
# visible at a glance: how many annotations, review state, has-no-censor.

@router.get("/censor/status")
async def status_batch(
    post_ids: str = "",
    db: AsyncSession = Depends(get_db),
) -> dict:
    ids = [int(x) for x in post_ids.split(",") if x.strip().lstrip("-").isdigit()][:300]
    if not ids:
        return {"results": {}}
    counts = dict((await db.execute(
        select(CensorAnnotation.post_id, func.count())
        .where(CensorAnnotation.post_id.in_(ids), CensorAnnotation.deleted_at.is_(None))
        .group_by(CensorAnnotation.post_id))).all())
    reviews = {r.post_id: r for r in (await db.execute(
        select(PostCensorReview).where(PostCensorReview.post_id.in_(ids)))).scalars().all()}
    # censor_pair の有無 (グリッドの ⿻ バッジ用) — どちらの側でも返す
    pair = {}
    for c, u, cf in (await db.execute(text(
            """select censored_post_id, uncensored_post_id, confirmed from censor_pair
               where censored_post_id = any(:ids) or uncensored_post_id = any(:ids)"""),
            {"ids": ids})).all():
        if c in ids:
            pair[c] = {"role": "censored", "partner": u, "confirmed": cf}
        if u in ids:
            pair[u] = {"role": "uncensored", "partner": c, "confirmed": cf}
    out = {}
    for pid in ids:
        r = reviews.get(pid)
        out[str(pid)] = {
            "annotationCount": int(counts.get(pid, 0)),
            "reviewState": r.state if r else "unreviewed",
            "hasNoCensor": bool(r.has_no_censor) if r else False,
            "pair": pair.get(pid),
        }
    return {"results": out}


# ── Teacher selection ───────────────────────────────────────────────────
# The auto-import seeded censor_annotation from a noisy autodiff detector. This
# surfaces the likely-bad regions (heuristic score) worst-first so a reviewer can
# fix them in /post/<id>/censor or exclude the whole post from the training set
# (post_censor_review.teacher_excluded → export_human_labels.py skips it).

def _ann_metrics(shape_type: str, g: dict):
    """(cx, cy, area, aspect) of an annotation's bbox in normalized space, or None."""
    try:
        if shape_type == "rect":
            x0, y0, x1, y1 = g["x"], g["y"], g["x"] + g["w"], g["y"] + g["h"]
        elif shape_type in ("polygon", "pen_path"):
            xs = [p[0] for p in g["points"]]; ys = [p[1] for p in g["points"]]
            x0, y0, x1, y1 = min(xs), min(ys), max(xs), max(ys)
        elif shape_type in ("circle", "ellipse"):
            cx, cy = g["cx"], g["cy"]
            rx = g.get("rx", g.get("r")); ry = g.get("ry", g.get("r"))
            x0, y0, x1, y1 = cx - rx, cy - ry, cx + rx, cy + ry
        else:
            return None
    except (KeyError, TypeError, ValueError, IndexError):
        return None
    bw, bh = max(0.0, x1 - x0), max(0.0, y1 - y0)
    aspect = (max(bw, bh) / min(bw, bh)) if min(bw, bh) > 1e-6 else 99.0
    return ((x0 + x1) / 2, (y0 + y1) / 2, bw * bh, aspect)


def _suspect_score(metrics: list):
    """(score, reasons) for a post — higher = more likely a bad auto-label."""
    score = 0.0; reasons: set[str] = set()
    for m in metrics:
        if m is None:
            continue
        _cx, cy, area, aspect = m
        if cy < 0.33: score += 3; reasons.add("顔FP?(上1/3)")
        if area < 0.004: score += 2; reasons.add("極小")
        if area > 0.30: score += 2; reasons.add("極大")
        if aspect > 6: score += 1; reasons.add("細長")
    return score, sorted(reasons)


@router.get("/censor/teacher-candidates")
async def teacher_candidates(
    offset: int = 0, limit: int = 60, only: str = "suspect",
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Auto-imported (created_by NULL) annotations scored worst-first.
    only ∈ {suspect (score≥3 & kept), all, excluded, kept}."""
    limit = max(1, min(limit, 200))
    rows = (await db.execute(
        select(CensorAnnotation.post_id, CensorAnnotation.shape_type, CensorAnnotation.geometry)
        .where(CensorAnnotation.created_by.is_(None), CensorAnnotation.deleted_at.is_(None)))).all()
    by_post: dict[int, list] = defaultdict(list)
    for pid, st, geom in rows:
        by_post[pid].append((st, geom))
    excluded = set((await db.execute(
        select(PostCensorReview.post_id).where(
            PostCensorReview.teacher_excluded.is_(True)))).scalars().all())

    scored = []
    for pid, anns in by_post.items():
        s, reasons = _suspect_score([_ann_metrics(st, g) for st, g in anns])
        scored.append({"postId": pid, "score": round(s, 1), "reasons": reasons,
                       "annotationCount": len(anns), "excluded": pid in excluded})
    if only == "suspect":
        scored = [x for x in scored if x["score"] >= 3 and not x["excluded"]]
    elif only == "excluded":
        scored = [x for x in scored if x["excluded"]]
    elif only == "kept":
        scored = [x for x in scored if not x["excluded"]]
    scored.sort(key=lambda x: (-x["score"], -x["postId"]))
    total = len(scored)
    page = scored[offset:offset + limit]

    pids = [x["postId"] for x in page]
    detail: dict[int, tuple] = {}
    if pids:
        dstmt = text("select id, version, image_width, image_height from post where id in :ids"
                     ).bindparams(bindparam("ids", expanding=True))
        for pid, ver, w, h in (await db.execute(dstmt, {"ids": pids})).all():
            detail[pid] = (ver, w, h)
    for x in page:
        ver, w, h = detail.get(x["postId"], (0, None, None))
        x["thumbnailUrl"] = f"/api/post/{x['postId']}/thumbnail?v={ver}"
        x["canvasWidth"] = w; x["canvasHeight"] = h
    return {"results": page, "total": total, "offset": offset, "limit": limit, "only": only}


@router.put("/post/{post_id}/teacher-exclusion")
async def set_teacher_exclusion(
    post_id: int,
    body: dict = Body(...),
    db: AsyncSession = Depends(get_db),
    auth_user: User = Depends(get_current_user),
) -> dict:
    """Body: {excluded: bool}. Upsert post_censor_review.teacher_excluded."""
    excluded = bool(body.get("excluded", True))
    post = await _get_post(db, post_id)
    now = _now()
    vals = dict(post_id=post_id, image_sha256=post.sha256, post_version_seen=post.version,
                state="partial", teacher_excluded=excluded,
                reviewed_by=auth_user.user_id, reviewed_at=now)
    stmt = pg_insert(PostCensorReview.__table__).values(**vals).on_conflict_do_update(
        index_elements=["post_id"],
        set_={"teacher_excluded": excluded, "reviewed_by": auth_user.user_id, "reviewed_at": now})
    await db.execute(stmt)
    await db.commit()
    return {"postId": post_id, "excluded": excluded}


# ── Export ──────────────────────────────────────────────────────────────
# Training-data egress. Positives = human annotations (bbox + segmentation in
# pixels). Negatives = posts reviewed `complete` with `has_no_censor` (and no
# annotations). Each image carries its fingerprint (sha256 + canvas dims) so a
# trainer can pin the exact image version. COCO for detectors, CSV for analysis.

def _bbox_seg(shape_type: str, g: dict, w: int, h: int):
    """Return (bbox[x,y,w,h] px, segmentation[[poly px]]) from normalized geometry."""
    if shape_type == "rect":
        x, y, gw, gh = g["x"] * w, g["y"] * h, g["w"] * w, g["h"] * h
        return ([round(x, 1), round(y, 1), round(gw, 1), round(gh, 1)],
                [[round(x, 1), round(y, 1), round(x + gw, 1), round(y, 1),
                  round(x + gw, 1), round(y + gh, 1), round(x, 1), round(y + gh, 1)]])
    if shape_type in ("polygon", "pen_path"):
        xs = [p[0] * w for p in g["points"]]
        ys = [p[1] * h for p in g["points"]]
        x0, y0, x1, y1 = min(xs), min(ys), max(xs), max(ys)
        bbox = [round(x0, 1), round(y0, 1), round(x1 - x0, 1), round(y1 - y0, 1)]
        seg = ([[round(c, 1) for p in g["points"] for c in (p[0] * w, p[1] * h)]]
               if shape_type == "polygon" else [])  # pen is an open stroke: bbox only
        return bbox, seg
    return [0, 0, 0, 0], []


@router.get("/censor/export")
async def export(
    format: str = "coco",
    since: str | None = None,
    db: AsyncSession = Depends(get_db),
) -> Response:
    if format not in ("coco", "csv"):
        raise HTTPException(422, "format must be 'coco' or 'csv'")
    since_dt = None
    if since:
        try:
            since_dt = datetime.fromisoformat(since)
        except ValueError:
            raise HTTPException(422, "since must be ISO-8601")

    # erase (negative) regions can't be expressed as additive COCO/CSV rows; this
    # geometry-based egress emits positives only. The mask-based training export
    # (export_human_labels.py) is the one that composites positives AND NOT negatives.
    q = (select(CensorAnnotation, Post)
         .join(Post, Post.post_id == CensorAnnotation.post_id)
         .where(CensorAnnotation.deleted_at.is_(None),
                CensorAnnotation.negative.is_(False)))
    if since_dt:
        q = q.where(CensorAnnotation.created_at >= since_dt)
    rows = (await db.execute(q.order_by(CensorAnnotation.post_id, CensorAnnotation.id))).all()

    reviews = {r.post_id: r for r in
               (await db.execute(select(PostCensorReview))).scalars().all()}

    # posts that are reviewed but have no positive annotation = negatives;
    # fetch their dims/sha for the image entry.
    annotated = {p.post_id for _, p in rows}
    neg_ids = [pid for pid in reviews if pid not in annotated]
    neg_posts = {}
    if neg_ids:
        neg_posts = {p.post_id: p for p in (await db.execute(
            select(Post).where(Post.post_id.in_(neg_ids)))).scalars().all()}

    if format == "csv":
        buf = StringIO()
        w = csv.writer(buf)
        w.writerow(["post_id", "sha256", "canvas_w", "canvas_h", "review_state",
                    "has_no_censor", "annotation_id", "censor_type", "shape_type",
                    "nx", "ny", "nw", "nh", "created_at"])
        for ann, post in rows:
            r = reviews.get(post.post_id)
            bb, _ = _bbox_seg(ann.shape_type, ann.geometry,
                              post.canvas_width or 0, post.canvas_height or 0)
            cw, ch = (post.canvas_width or 1), (post.canvas_height or 1)
            w.writerow([post.post_id, post.sha256.hex() if post.sha256 else "",
                        post.canvas_width, post.canvas_height,
                        r.state if r else "", r.has_no_censor if r else "",
                        ann.id, ann.censor_type, ann.shape_type,
                        round(bb[0] / cw, 4), round(bb[1] / ch, 4),
                        round(bb[2] / cw, 4), round(bb[3] / ch, 4),
                        ann.created_at.isoformat() if ann.created_at else ""])
        for pid, post in neg_posts.items():       # reviewed-clean negatives
            r = reviews[pid]
            w.writerow([pid, post.sha256.hex() if post.sha256 else "",
                        post.canvas_width, post.canvas_height, r.state,
                        r.has_no_censor, "", "", "", "", "", "", "", ""])
        return Response(buf.getvalue(), media_type="text/csv",
                        headers={"Content-Disposition": "attachment; filename=censor_export.csv"})

    # COCO
    cats = sorted(CENSOR_TYPES)
    cat_id = {c: i + 1 for i, c in enumerate(cats)}
    images: dict[int, dict] = {}
    annotations = []

    def _img(post: Post) -> dict:
        if post.post_id not in images:
            r = reviews.get(post.post_id)
            images[post.post_id] = {
                "id": post.post_id,
                "file_name": f"post/{post.post_id}/content",
                "width": post.canvas_width or 0, "height": post.canvas_height or 0,
                "sha256": post.sha256.hex() if post.sha256 else None,
                "review": {"state": r.state, "hasNoCensor": r.has_no_censor} if r else None,
            }
        return images[post.post_id]

    for ann, post in rows:
        _img(post)
        bbox, seg = _bbox_seg(ann.shape_type, ann.geometry,
                              post.canvas_width or 0, post.canvas_height or 0)
        annotations.append({
            "id": ann.id, "image_id": post.post_id,
            "category_id": cat_id.get(ann.censor_type, len(cats) + 1),
            "bbox": bbox, "area": round(bbox[2] * bbox[3], 1), "iscrowd": 0,
            "segmentation": seg, "shape_type": ann.shape_type,
        })
    for pid, post in neg_posts.items():
        _img(post)

    coco = {
        "info": {"description": "kura censor annotations (issue #2)",
                 "note": "positives = human annotations; images with review.hasNoCensor "
                         "and no annotations are reviewed-clean negatives"},
        "images": list(images.values()),
        "annotations": annotations,
        "categories": [{"id": cat_id[c], "name": c} for c in cats],
    }
    return Response(json.dumps(coco), media_type="application/json",
                    headers={"Content-Disposition": "attachment; filename=censor_coco.json"})
