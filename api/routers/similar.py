"""Similar-images router — browse near-duplicate clusters by perceptual hash.

Groups posts that share an identical 64-bit ``perceptual_hash`` (the most-similar
bucket, Hamming-0). Surfaces censored/uncensored pairs (an artist's R18 +
mosaic'd public version land in the same bucket) and plain reposts. Paginated,
newest cluster first.

Perf note: the GROUP BY scans the whole ``post`` table per page (~0.5 M rows).
Fine for a low-QPS browse page; if it grows hot, back it with a materialized
view refreshed on ingest.
"""
import asyncio
import io

from fastapi import APIRouter, Depends, Query
from PIL import Image, ImageChops, ImageStat
from sqlalchemy import bindparam, text
from sqlalchemy.ext.asyncio import AsyncSession

from api.db.session import get_db


def _change_pct(ta: bytes, tb: bytes):
    """Lightweight visual change-rate between two posts: mean abs pixel diff of their
    32px grayscale thumbnails, as a percent. ~0% = identical repost, a few % = a
    localized edit (censor/text). Cheap enough to compute per page render."""
    try:
        a = Image.open(io.BytesIO(ta)).convert("L").resize((32, 32))
        b = Image.open(io.BytesIO(tb)).convert("L").resize((32, 32))
        return round(ImageStat.Stat(ImageChops.difference(a, b)).mean[0] / 255 * 100, 1)
    except Exception:
        return None

router = APIRouter(prefix="/api", tags=["similar"])

# WD14 page tags that mark a post as censored (same set as the censor pipeline).
CENSOR_TAGS = ("mosaic_censoring", "bar_censor", "censored", "censored_nipples",
               "convenient_censoring", "nipple_bar")


def _thumb(post_id: int, version: int) -> str:
    return f"/api/post/{post_id}/thumbnail?v={version}"


@router.get("/similar")
async def list_similar(
    offset: int = Query(0, ge=0),
    limit: int = Query(24, ge=1, le=100),
    pairs_only: bool = Query(False, alias="pairs"),
    untrained_only: bool = Query(False, alias="untrained"),
    db: AsyncSession = Depends(get_db),
):
    # status='ok' = the displayable curated set (matches list_posts; hides
    # near_dup/corrupt/lost rows that have no content). `count(*) over()` returns
    # the full cluster total from the SAME grouped scan, so we don't pay a second
    # full GROUP BY for pagination (was ~2x the page latency).
    if pairs_only:
        # Only clusters holding BOTH a censored and an uncensored member — the
        # censored↔uncensored pairs used as decensor / detector training teachers.
        # untrained=1 → drop clusters that already fed the detector training set
        # (any member in censor_trained_post), so curation shows only fresh teachers.
        untrained_having = ("and not bool_or(tr.post_id is not null)" if untrained_only else "")
        sql = text(f"""
            with cens as (
                select distinct pt.post_id from post_tag pt
                join tag_name tn on tn.tag_id = pt.tag_id where tn.name in :ctags
            ), g as (
                select encode(p.perceptual_hash, 'hex') as h,
                       array_agg(p.id order by p.id) as ids,
                       count(*) as c, max(p.id) as mx
                from post p
                left join cens c on c.post_id = p.id
                left join censor_trained_post tr on tr.post_id = p.id
                where p.perceptual_hash is not null and p.status = 'ok'
                group by p.perceptual_hash
                having count(*) >= 2
                   and bool_or(c.post_id is not null) and bool_or(c.post_id is null)
                   {untrained_having}
            )
            select h, ids, c, mx, count(*) over() as total
            from g order by mx desc limit :lim offset :off
        """).bindparams(bindparam("ctags", expanding=True))
        rows = (await db.execute(sql, {"lim": limit, "off": offset, "ctags": list(CENSOR_TAGS)})).all()
    else:
        sql = text("""
            with g as (
                select encode(perceptual_hash, 'hex') as h,
                       array_agg(id order by id) as ids,
                       count(*) as c, max(id) as mx
                from post
                where perceptual_hash is not null and status = 'ok'
                group by perceptual_hash having count(*) >= 2
            )
            select h, ids, c, mx, count(*) over() as total
            from g order by mx desc limit :lim offset :off
        """)
        rows = (await db.execute(sql, {"lim": limit, "off": offset})).all()

    total = rows[0].total if rows else 0

    ids = [pid for r in rows for pid in r.ids]
    detail: dict[int, tuple] = {}
    censored: set[int] = set()
    if ids:
        dstmt = text("select id, version, image_width, image_height "
                     "from post where id in :ids").bindparams(
            bindparam("ids", expanding=True))
        for pid, ver, w, h in (await db.execute(dstmt, {"ids": ids})).all():
            detail[pid] = (ver, w, h)
        cstmt = text("select distinct pt.post_id from post_tag pt "
                     "join tag_name tn on tn.tag_id = pt.tag_id "
                     "where pt.post_id in :ids and tn.name in :ctags").bindparams(
            bindparam("ids", expanding=True), bindparam("ctags", expanding=True))
        for (pid,) in (await db.execute(cstmt, {"ids": ids, "ctags": list(CENSOR_TAGS)})).all():
            censored.add(pid)

    # lightweight per-cluster change-rate: diff the first two members' thumbnails
    pair_ids = [pid for r in rows for pid in r.ids[:2]]
    thumbs: dict[int, bytes] = {}
    if pair_ids:
        tstmt = text("select id, thumbnail from post where id in :ids").bindparams(
            bindparam("ids", expanding=True))
        for pid, th in (await db.execute(tstmt, {"ids": pair_ids})).all():
            if th is not None:
                thumbs[pid] = bytes(th)

    def _calc_changes():
        out = {}
        for r in rows:
            two = r.ids[:2]
            out[r.h] = (_change_pct(thumbs[two[0]], thumbs[two[1]])
                        if len(two) >= 2 and two[0] in thumbs and two[1] in thumbs else None)
        return out
    changes = await asyncio.get_event_loop().run_in_executor(None, _calc_changes)

    clusters = []
    for r in rows:
        posts = []
        for pid in r.ids:
            ver, w, h = detail.get(pid, (0, None, None))
            posts.append({"id": pid, "thumbnailUrl": _thumb(pid, ver),
                          "censored": pid in censored, "width": w, "height": h})
        clusters.append({"hash": r.h, "count": r.c, "change": changes.get(r.h), "posts": posts})
    return {"results": clusters, "total": total, "offset": offset, "limit": limit,
            "pairsOnly": pairs_only, "untrainedOnly": untrained_only}
