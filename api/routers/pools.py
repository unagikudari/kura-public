"""Pools router — GET + write endpoints (Phase C: pool_url unified table)."""

import re
import os
from datetime import datetime

from fastapi import APIRouter, Body, Depends, Form, HTTPException, Query
from api.routers.posts import safe_mode
from pydantic import BaseModel, Field
from api.tagger_core.paired_review import is_paired_review
from sqlalchemy import Text, and_, bindparam, func, literal_column, or_, select, text
from sqlalchemy.exc import IntegrityError, DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload, raiseload, load_only

from api.auth import get_current_user
from api.config import DATA_URL
from api.services.pool_naming import derive_pool_name_from_url
from api.services import tagging_queue
from api.services.pool_visibility import pool_covers
from api.db.models import CensorAnnotation, Pool, PoolCategory, PoolName, PoolPost, PoolState, PoolUrl, Post, PostTag, Tag, TagCategory, TagImplication, TagName, User
from api import site_rules
from api.site_rules import detect_domain
from api.db.session import get_db
from api.schemas.common import PagedResult, PoolSummary

router = APIRouter(prefix="/api", tags=["pools"])
dashboard_router = APIRouter(prefix="/api", tags=["pools"])


class ReselectReviews(BaseModel):
    references: list[str] = Field(min_length=1, max_length=5)


@dashboard_router.post('/tagger-dashboard/reselect-reviews')
async def reselect_reviews(body: ReselectReviews, db: AsyncSession = Depends(get_db),
                           auth_user: User = Depends(get_current_user)):
    from api.errors import KuraError
    from api.services.review_reselection import replace

    if os.environ.get('KURA_DB_ROLE', 'canonical').lower() != 'canonical':
        raise KuraError(503, "human_review_canonical_required")
    if len(set(body.references)) != len(body.references) or any(
            re.fullmatch(r"[a-f0-9]{64}", ref) is None for ref in body.references):
        raise KuraError(422, "review_reselection_invalid")
    try:
        result = await replace(db, body.references, auth_user.user_id)
        await db.commit()
    except Exception:
        await db.rollback()
        raise
    return result


@dashboard_router.get('/tagger-dashboard/review-queues/compact', include_in_schema=False)
async def compact_review_queues(all_pending: bool = False, db: AsyncSession = Depends(get_db),
                                auth_user: User = Depends(get_current_user)):
    from api.services.dashboard_review_catalog import first_pending, population, project
    from api.services.review_set_registry import available

    if not await available(db):
        return {"state": "not_configured", "queues": [], "pending_total": 0}
    if all_pending:
        queues, completed = project(await population(db))
        return {"state": "available", "queues": queues, "pending_total": len(queues), "completed": completed}
    rows = await first_pending(db)
    queues, _ = project(rows)
    return {"state": "available", "queues": queues, "pending_total": rows[0]["pending_total"] if rows else 0}


@dashboard_router.get('/tagger-dashboard/review-queues/history', include_in_schema=False)
async def review_queue_history(offset: int = Query(0, ge=0), limit: int = Query(30, ge=1, le=100),
                               db: AsyncSession = Depends(get_db), auth_user: User = Depends(get_current_user)):
    from api.services.dashboard_review_catalog import history
    from api.services.review_set_registry import available

    if not await available(db):
        return {"results": [], "total": 0, "offset": offset, "limit": limit}
    page = await history(db, offset=offset, limit=limit)
    return {"results": page["results"], "total": page["total"], "offset": offset, "limit": limit}


@dashboard_router.get('/tagger-dashboard/review-queues/{ref}/open', include_in_schema=False)
async def open_legacy_review(ref: str, db: AsyncSession = Depends(get_db),
                             auth_user: User = Depends(get_current_user)):
    from fastapi.responses import RedirectResponse
    from api.services.review_set_registry import catalog_sets

    if not re.fullmatch(r"[a-f0-9]{64}", ref):
        raise HTTPException(404, "Review set not found")
    queues = await catalog_sets(db, ref)
    if len(queues) != 1 or queues[0]["schema"] != "human-review-set/1":
        raise HTTPException(404, "Review set not found")
    return RedirectResponse(queues[0]["unjudged_url"], status_code=303)


@dashboard_router.get('/tagger-dashboard/review-queues')
async def review_queues(db: AsyncSession = Depends(get_db), auth_user: User = Depends(get_current_user)):
    import asyncio
    from api.errors import KuraError
    from api.services.review_queues import ReviewQueueRefused, read_legacy_lora_refs, review_lanes
    try:
        from api.services.review_set_registry import candidates_for_release_review, legacy_lora_refs, load_configured
        queues = await load_configured(db)
    except ReviewQueueRefused as exc:
        raise KuraError(503, 'review_sets_unavailable', context={'reason': str(exc)},
                        fix='固定セットの配置とdigestを確認してください。空の成功として扱わないでください。') from exc
    if queues is None:
        return {'state': 'not_configured', 'queues': []}
    origin_metadata_unavailable = False
    try:
        legacy_refs = await legacy_lora_refs(db)
        if legacy_refs is None:
            legacy_refs = await asyncio.to_thread(read_legacy_lora_refs)
    except (OSError, ValueError):
        legacy_refs = set()
        origin_metadata_unavailable = True
    review_lanes(queues, legacy_refs)
    from api.services.review_requests import load_requests
    try:
        requests = await asyncio.to_thread(load_requests, queues)
        request_unavailable = None
        requests_state = 'not_configured' if requests is None else 'available'
    except Exception:
        # Optional operator metadata must not hide existing purpose-based requests.
        # Do not expose arbitrary local exception text.
        requests, requests_state = {}, 'unavailable'
        request_unavailable = {'reason': 'request_metadata_unavailable'}
    for queue in queues:
        queue['request'] = (requests or {}).get(queue['queue_sha256'])
        queue['request_unavailable'] = bool(request_unavailable and queue['purpose'] == 'training_support')
    from api.services.review_comparison import load_scores, compare
    try:
        scores = await asyncio.to_thread(load_scores, queues)
        score_error = None
    except ReviewQueueRefused as exc:
        scores, score_error = {}, str(exc)
    except Exception:
        # Optional comparison must not hide working review requests. Never
        # expose arbitrary exception text (paths or other local context).
        scores, score_error = {}, 'score_read_failed'
    from api.services.review_adjudication import awaiting_resolution, is_adjudication
    from api.services.review_reselection import retired_refs
    retired = await retired_refs(db)
    current_queues = [
        queue for queue in queues
        if not queue.get('superseded_by') and queue['queue_sha256'] not in (retired or ())
    ]
    # Resolve each frozen name once, maintaining the database's exact case
    # comparison and the canonical-name requirement.
    resolved_by_tag = {}
    names = sorted({queue['tag'] for queue in current_queues})
    if names:
        rows = (await db.execute(text(
            'SELECT requested.name, n.tag_id, canonical.name '
            'FROM unnest(CAST(:names AS text[])) AS requested(name) '
            'JOIN tag_name n ON lower(n.name)=lower(requested.name) '
            'JOIN tag_name canonical ON canonical.tag_id=n.tag_id AND canonical.ord=0'
        ), {'names': names})).all()
        for requested, tag_id, canonical in rows:
            resolved_by_tag.setdefault(requested, []).append((tag_id, canonical))
    available, unavailable = [], []
    for queue in current_queues:
        resolved = resolved_by_tag.get(queue['tag'], ())
        if len(resolved) != 1 or resolved[0][1] != queue['tag']:
            raise KuraError(503, 'review_sets_unavailable', context={'reason': 'tag_unresolved'},
                            fix='固定セットの正式なタグ名を確認してください。')
        tag_id = resolved[0][0]
        if queue['schema'] in ('human-review-set/2', 'human-review-set/3'):
            reason = None
            paired_review = is_paired_review(queue)
            events = []
            if os.environ.get('KURA_DB_ROLE', 'canonical').lower() != 'canonical':
                reason = 'human_review_canonical_required'
            else:
                from api.services.human_review import bounded_transaction, reviewable_post_ids
                try:
                    nested = await db.begin_nested()
                    try:
                        await bounded_transaction(db)
                        queue['post_ids'] = await reviewable_post_ids(db, queue)
                        events = (await db.execute(text(
                            'SELECT DISTINCT ON(post_id) post_id,decision FROM tagger_human_review_event '
                            'WHERE queue_sha256=:q AND post_id=ANY(:ids) ORDER BY post_id,id DESC'),
                            {'q':queue['queue_sha256'],'ids':queue['post_ids']})).all()
                    finally:
                        await nested.rollback()  # Also restore SET LOCAL on a successful read.
                except DBAPIError as exc:
                    if getattr(exc.orig, 'sqlstate', None) not in ('42P01','55P03','57014'):
                        raise
                    reason = 'human_review_storage_unavailable'
            if reason:
                unavailable.append({'queue_sha256':queue['queue_sha256'], 'tag':queue['tag'],
                                    'total':len(queue['post_ids']), 'reason':reason})
                continue
            if not queue['post_ids']:
                continue
            queue['judged'] = {state:sum(s==state for _,s in events) for state in ('present','absent','uncertain')}
            queue['adjudication'] = is_adjudication(queue)
            queue['reselectable'] = (retired is not None and not queue['adjudication']
                                     and queue['purpose'] == 'historical_label_recheck'
                                     and not paired_review)
            resolved_dispute = (
                queue['adjudication'] and not (queue['judged']['present'] or queue['judged']['absent'])
                and not await awaiting_resolution(db, queue)
            )
            queue['total'] = len(queue.pop('post_ids'))
            queue['remaining'] = queue['total']-len(events)
            if queue['adjudication']:
                queue['remaining'] = 0 if resolved_dispute else queue['remaining'] + queue['judged']['uncertain']
            queue['comparison'] = {'state':'explicit_recheck_not_quality','quality_granted':False}
            queue['responses'] = len(events)
            queue['decisive'] = queue['judged']['present'] + queue['judged']['absent']
            queue['review_state'] = ('awaiting_responses' if queue['remaining'] else
                'review_completed_quality_unresolved' if queue['judged']['uncertain'] else 'review_completed_not_quality_approval')
            available.append(queue)
            continue
        pins = (await db.execute(text(
            'SELECT pin.post_id, pin.state FROM post_tag_pin pin '
            "WHERE pin.tag_id=:tag_id AND pin.post_id=ANY(:ids) AND pin.state IN ('present','absent') ORDER BY pin.post_id"
        ), {'tag_id': tag_id, 'ids': queue['post_ids']})).all()
        queue['judged'] = {state: sum(s == state for _, s in pins)
                           for state in ('present', 'absent') if any(s == state for _, s in pins)}
        if score_error:
            queue['comparison'] = {'state': 'unavailable', 'reason': score_error}
        elif queue['queue_sha256'] in scores:
            queue['comparison'] = compare(queue, scores[queue['queue_sha256']], pins)
        else:
            queue['comparison'] = {'state': 'not_supplied'}
        queue['total'] = len(queue.pop('post_ids'))
        queue['remaining'] = queue['total'] - sum(queue['judged'].values())
        available.append(queue)
    release_reviews = await candidates_for_release_review(db, [
        queue['queue_sha256'] for queue in available
        if queue['schema'] == 'human-review-set/3' and queue['remaining'] == 0
        and queue['decisive'] == queue['total']
    ])
    from api.services.dashboard_review_accuracy import read_review_accuracy
    accuracy = await asyncio.to_thread(read_review_accuracy)
    # The dashboard lists sets; it never reads their per-image digests (2026-11-27: media_sha256
    # was 1.29 MB of a 1.87 MB response). The review screen gets them per set from its own API.
    for queue in available:
        queue.pop('media_sha256', None)
    return {'state': 'available', 'queues': available, 'unavailable_queues': unavailable,
            'requests_state': requests_state, 'request_unavailable': request_unavailable,
            'release_reviews': release_reviews, 'origin_metadata_unavailable': origin_metadata_unavailable,
            'review_accuracy': accuracy}

METADATA_LOAD_OPTS = [
    selectinload(Pool.names),
    selectinload(Pool.category),
    selectinload(Pool.state),
    selectinload(Pool.urls),
]

LOAD_OPTS = [*METADATA_LOAD_OPTS, selectinload(Pool._posts)]

# The JSON fields POST/PUT /api/pools take. Anything else is refused (422), so a
# caller that still sends fields these routes used to store learns it.
CREATE_FIELDS = frozenset({"names", "category", "description", "verify_status", "tagger_source",
                           "primarySourceUrl", "posts"})
UPDATE_FIELDS = frozenset({"version", "names", "posts", "description", "primarySourceUrl", "scope",
                           "category"})


def _refuse_unknown_fields(body: dict, accepted: frozenset) -> None:
    unknown = sorted(set(body) - accepted)
    if unknown:
        from api.errors import KuraError
        raise KuraError(422, "unknown_pool_fields", context={"fields": unknown, "accepted": sorted(accepted)},
                        fix="send only the accepted fields; an extension's own pool state goes to that extension's route.")


def _serialize_pool(pool: Pool, *, include_posts: bool = True) -> dict:
    names = [n.name for n in sorted(pool.names, key=lambda n: n.order)]
    posts = sorted(pool._posts, key=lambda pp: pp.order) if include_posts else []
    pu = pool.primary_url
    st = pool.state
    return {
        "version": pool.version,
        "id": pool.pool_id,
        "names": names,
        "category": pool.category.name if pool.category else "default",
        "description": pool.description,
        **({"postCount": len(posts), "posts": [pp.post_id for pp in posts]} if include_posts else {}),
        "creationTime": pool.creation_time,
        "lastEditTime": pool.last_edit_time,
        "verifyStatus": pool.verify_status,
        "taggerSource": pool.tagger_source,
        "primarySourceUrl": pu.url if pu else None,
        "scope": st.scope if st else None,
        "urlAliases": [u.url for u in (pool.urls or []) if not u.is_primary],
        # Full per-source view (primary first): a pool can be fed by N sources,
        # each carrying its own import lineage (pool_url per-url state). The web
        # surfaces this as a collapsible "+N sources" list.
        "sources": [
            {
                "url": u.url,
                "isPrimary": bool(u.is_primary),
                "isExternal": bool(u.url) and u.url.startswith(("http://", "https://")),
            }
            for u in sorted(pool.urls or [], key=lambda u: (not u.is_primary, u.url or ""))
        ],
    }




def _thumbnail_url_by_id(post_id: int) -> str:
    # Serve the pool cover from PG (post.thumbnail blob) via the API, not the
    # legacy FS poster. The generated-thumbnails/*.jpg files were reclaimed for
    # PG-backed posts (Phase 6, 409GB→45GB), so the FS path 404s for ~all pools.
    return f"/api/post/{post_id}/thumbnail"


def _parse_pool_query(q: str) -> tuple[list[str], list[str], list[str]]:
    """(free_terms, name_terms, tag_terms)。

    裸語 (free) と `name:` は**別物**。裸語は「pool 名 または メンバーのタグ」に
    当たり、`name:` は名前だけに絞る (2026-09-05: 最初この 2 つを同じ list に入れて
    しまい、`name:` まで OR に飲み込まれて絞り込みが効かなくなった)。
    """
    free_terms: list[str] = []
    name_terms: list[str] = []
    tag_terms: list[str] = []
    for raw in q.split():
        if not raw:
            continue
        if raw.startswith("tag:"):
            value = raw[4:].strip()
            if value:
                tag_terms.append(value)
        elif raw.startswith("name:"):
            value = raw[5:].strip()
            if value:
                name_terms.append(value)
        elif ":" in raw:
            continue
        else:
            free_terms.append(raw)
    return free_terms, name_terms, tag_terms


def _canonical_gallery_url(url: str) -> str:
    """Canonicalize a source's gallery URL spellings to one pool key.

    The rule lives in the source's adapter (site_rules.canonical_url); typical
    normalizations, keyed on whatever identifies the gallery:
      - strip ?p=N pagination
      - fold a mirror domain onto its canonical domain (same gallery, so this
        never loses access)
    Without the mirror fold the same gallery imported under both domains spawned
    two pools. A one-off repair script healed the existing duplicates.
    """
    return site_rules.canonical_url(url) or url


def _derive_pool_name_from_url(url: str) -> str:
    # kura#42: 実装は pool_naming に一本化 (この copy と upload.py の copy が drift
    # し、同じ archive set が経路により 'set.zip' / 'set' に割れていた)。
    return derive_pool_name_from_url(url)


def _pool_untitled(pool, ensured_url: str) -> bool:
    """True when a pool has no REAL title yet — its ord-0 name is a URL-derived
    fallback for one of its own urls (or it has no name at all).

    A pool that already carries a real title (a per-artist name, or a
    curated/merged-pool name) must NOT be auto-renamed by a later re-import of
    any of its sources: for a merge pool (many external pool_urls → many
    sources) each imported gallery would otherwise re-promote its own title
    and churn the whole pool's name. Mirrors name-from-title's
    "already titled → no-op" guard. Requires pool.names + pool.urls loaded."""
    names = sorted(pool.names, key=lambda n: n.order)
    if not names:
        return True
    fallbacks = {_derive_pool_name_from_url(pu.url) for pu in pool.urls}
    fallbacks.add(_derive_pool_name_from_url(ensured_url))
    return names[0].name in fallbacks


def _infer_scope(url: str) -> str | None:
    """Infer pool scope from URL pattern.

    `kura-merge:` は kura 自身の合成 scheme なので core が知っている。取得元ごとの
    形 (creator feed か 1 post か) は adapter が答える (kura#66 G6)。
    """
    if url.startswith("kura-merge:"):
        return "collection"
    return site_rules.scope(url)


async def _find_pool_by_source(source_url: str, db: AsyncSession) -> Pool | None:
    """Find pool by pool_url.url (primary or alias). Returns None if not found.

    Paged gallery URLs (?p=N) and mirror spellings are normalized to the
    canonical gallery URL before lookup.
    """
    canonical = _canonical_gallery_url(source_url)
    for lookup_url in dict.fromkeys([source_url, canonical]):
        r = await db.execute(
            select(Pool)
            .join(PoolUrl, PoolUrl.pool_id == Pool.pool_id)
            .where(PoolUrl.url == lookup_url)
            .options(*LOAD_OPTS)
        )
        pool = r.scalar_one_or_none()
        if pool is not None:
            return pool
    return None


async def _insert_pool_urls(db: AsyncSession, pool_id: int, alternates: list[str]) -> None:
    """Insert non-primary pool_url entries, ignoring conflicts."""
    for url in alternates:
        if url and url.strip():
            await db.execute(
                text(
                    "INSERT INTO pool_url (pool_id, url, is_primary) "
                    "VALUES (:pid, :url, false) "
                    "ON CONFLICT (url) DO NOTHING"
                ),
                {"pid": pool_id, "url": url.strip()},
            )


def _source_url_variants(url: str) -> list[str]:
    """The post.source spellings that identify `url`'s gallery/post: as-given,
    canonical (?p=N stripped + mirror-domain fold), the adapter's mirror URLs, and
    each without the trailing slash. Small enumerable set so the match stays on
    ix_post_source (equality) instead of a regexp scan over the post table."""
    variants = {url, url.rstrip("/")}
    canon = _canonical_gallery_url(url)
    variants |= {canon, canon.rstrip("/")}
    # mirror 等の「同じ対象を指す別 URL」は取得元の知識 (kura#66 G6)。
    variants |= set(site_rules.url_variants(canon))
    return [v for v in variants if v]


_BULK_POOL_POST_SQL = text("""
    INSERT INTO pool_post (pool_id, post_id, ord)
    SELECT :pid, x.post_id, x.ord
    FROM unnest(CAST(:post_ids AS int[]), CAST(:ords AS int[])) AS x(post_id, ord)
    ON CONFLICT (pool_id, post_id) DO NOTHING
""")


async def _bulk_insert_pool_posts(db: AsyncSession, pool_id: int, post_ids: list[int]) -> None:
    """Safe replacement for `for i, pid in enumerate(post_ids): db.add(PoolPost(...))`
    (2026-07-21 fix — both call sites in this file used that pattern, a plain
    per-row ORM insert with no conflict handling). A caller passing a
    `post_ids` list with an accidental duplicate entry, or a double-submitted
    request racing itself, hit a bare asyncpg UniqueViolationError on
    pool_post_pkey — occasionally escalating to a genuine
    DeadlockDetectedError between two overlapping transactions (user report
    2026-07-21, confirmed live in kura-api's journal at the time). One
    unnest-based bulk statement with ON CONFLICT DO NOTHING makes a
    duplicate a silent no-op instead of a race, and is a single round trip
    instead of N ORM inserts."""
    if not post_ids:
        return
    ords = list(range(len(post_ids)))
    await db.execute(_BULK_POOL_POST_SQL, {"pid": pool_id, "post_ids": post_ids, "ords": ords})


_ADOPT_POSTS_SQL = (
    text("""
        INSERT INTO pool_post (pool_id, post_id, ord)
        SELECT :pid,
               p.id,
               (SELECT COALESCE(MAX(ord), -1) FROM pool_post WHERE pool_id = :pid)
                 + row_number() OVER (ORDER BY p.id)
        FROM post p
        WHERE p.source IN :variants
          AND NOT EXISTS (SELECT 1 FROM pool_post pp
                          WHERE pp.pool_id = :pid AND pp.post_id = p.id)
        ON CONFLICT (pool_id, post_id) DO NOTHING
        RETURNING post_id
    """)
    .bindparams(bindparam("variants", expanding=True))
)

_ADOPT_PRIMARY_SQL = (
    text("""
        UPDATE post SET primary_pool_id = :pid, version = version + 1
        WHERE source IN :variants AND primary_pool_id IS NULL
    """)
    .bindparams(bindparam("variants", expanding=True))
)


async def adopt_posts_by_source(db, pool_id: int, url: str) -> int:
    """Pool-side counterpart of the attach_source_pool trigger (migration 0010).

    The trigger attaches a POST to any pool whose pool_url matches its source —
    but only fires on post INSERT/UPDATE, so a pool created AFTER its posts
    already exist stayed empty forever (batch→per_post transition pools, an
    importer's pre-creates whose import then skip-exits on "already in kura",
    reconciler re-dispatches). Called at pool creation to close that gap: adopt
    existing posts whose post.source matches the pool's URL variants. Posts with
    no primary pool are anchored here; posts anchored elsewhere only gain M:N
    membership (mirrors the /api/upload md5-dup path). `db` is any async
    executor (AsyncSession or AsyncConnection); the caller owns the commit."""
    variants = _source_url_variants(url)
    if not variants:
        return 0
    rows = await db.execute(_ADOPT_POSTS_SQL, {"pid": pool_id, "variants": variants})
    adopted = len(rows.fetchall())
    await db.execute(_ADOPT_PRIMARY_SQL, {"pid": pool_id, "variants": variants})
    return adopted


@router.get("/pools/by-source")
async def get_pool_by_source(
    url: str = Query(..., min_length=1, max_length=2048),
    db: AsyncSession = Depends(get_db),
) -> dict:
    pool = await _find_pool_by_source(url, db)
    if pool is None or pool.hidden:
        raise HTTPException(status_code=404, detail="Pool not found")
    data = _serialize_pool(pool)
    covers = await pool_covers(db, [pool.pool_id])
    cover_id, first_post_source = covers.get(pool.pool_id, (None, None))
    sorted_names = sorted(pool.names, key=lambda n: n.order)
    data["name"] = sorted_names[0].name if sorted_names else f"pool#{pool.pool_id}"
    data["thumbnailUrl"] = _thumbnail_url_by_id(cover_id) if cover_id else None
    data["firstPostSource"] = first_post_source
    data["extensions"] = await _pool_annotations(db, pool.pool_id)
    return data


# 検証/correction 作業 pool の名前パターン — client バッジ(+page.svelte VERIFY_POOL_RE)と同期。
# HEADVERIFY は自動タガー更新ループ(trainer ~/tagger-heads)が毎サイクル生成する検証 pool。
VERIFY_POOL_NAME_RE = r"HEADVERIFY|検証|判定|seed|SWEEP|audit|要人手|教師|版比較|FP-audit|pigment|wrinkled"


async def _resolve_pool_query_tags(db: AsyncSession, terms: list[str]) -> dict[str, list[int]]:
    resolved = {term.lower(): [] for term in terms}
    if resolved:
        rows = (await db.execute(select(func.lower(TagName.name), TagName.tag_id)
                                 .where(func.lower(TagName.name).in_(resolved)))).all()
        for name, tag_id in rows:
            resolved[name].append(tag_id)
    return resolved


def _pool_has_tag(term: str, resolved: dict[str, list[int]] | None = None):
    """EXISTS per pool, not `pool.id IN (all pools of all posts with the tag)`.

    The IN form materialized every pool_post×post_tag row of the tag and, inside
    the OR with the name match, was rescanned per pool: `pools?query=1girl` ran
    for minutes on 2026-10-24 (39 s on the replica, >2 min on primary). Probing each
    pool's posts through post_tag_pkey takes 288 ms for 1girl, 132 ms for glasses,
    126 ms for a 75-post tag (same counts).
    """
    if resolved is not None:
        # Literal tag IDs (not a bound list) so the planner sees the real distribution.
        ids = [literal_column(str(int(i))) for i in resolved[term.lower()]] or [literal_column("NULL")]
        return (select(PoolPost.pool_id)
                .join(PostTag, PostTag.post_id == PoolPost.post_id)
                .where(PoolPost.pool_id == Pool.pool_id, PostTag.tag_id.in_(ids))
                .correlate(Pool).exists())
    return (
        select(PoolPost.pool_id)
        .join(PostTag, PostTag.post_id == PoolPost.post_id)
        .join(TagName, TagName.tag_id == PostTag.tag_id)
        .where(PoolPost.pool_id == Pool.pool_id, func.lower(TagName.name) == term.lower())
        .correlate(Pool).exists()
    )


def _apply_pool_filters(stmt, free_terms: list[str], name_terms: list[str],
                        tag_terms: list[str], hide_verify: bool = False,
                        resolved: dict[str, list[int]] | None = None):
    if hide_verify:
        # hide_verify is retained as the legacy-history filter; no review work is pending.
        name_sub = select(PoolName.pool_id).where(PoolName.name.op("~*")(VERIFY_POOL_NAME_RE))
        # All verification metadata is historical, including former pending pools.
        visible = and_(Pool.verify_status.is_(None), Pool.pool_id.not_in(name_sub))
        stmt = stmt.where(visible)
    # 裸の語は「pool 名 **または** メンバーのタグ」に当てる (2026-09-05)。
    #
    # なぜ OR か: ヘッダーの検索候補は `/api/tags` 由来 — つまり **タグ**を出す。
    # 利用者はその候補 (`soba647`, usages 5,431) を選んで pool mode で検索する。
    # 裸語を pool 名だけに当てていた頃、この経路は必ず 0 件になった: soba647 の
    # pool は `2025 Dec.1 JPEG unprocessed` のような set 名で、名前に語を含まない。
    # UI が「5,431 件ある」と示した語が 0 件を返すのは、検索が壊れているのと同じ。
    for term in free_terms:
        by_name = select(PoolName.pool_id).where(PoolName.name.ilike(f"%{term}%"))
        stmt = stmt.where(or_(Pool.pool_id.in_(by_name), _pool_has_tag(term, resolved)))
    # `name:` は名前だけ — 絞り込みの手段として残す (OR に混ぜない)。
    for term in name_terms:
        sub = select(PoolName.pool_id).where(PoolName.name.ilike(f"%{term}%"))
        stmt = stmt.where(Pool.pool_id.in_(sub))
    for term in tag_terms:
        stmt = stmt.where(_pool_has_tag(term, resolved))
    return stmt


# Request-local only: bound both Python memory and SQL bind count. Broad searches
# keep the relational path instead of expanding the whole library into IDs.
POOL_SEARCH_CANDIDATE_LIMIT = 4096


async def _pool_search_candidates(db: AsyncSession, base_stmt) -> list[int] | None:
    ids = (await db.execute(base_stmt.with_only_columns(Pool.pool_id)
                           .limit(POOL_SEARCH_CANDIDATE_LIMIT + 1))).scalars().all()
    return list(ids) if len(ids) <= POOL_SEARCH_CANDIDATE_LIMIT else None


async def _load_pool(pool_id_or_name: str, db: AsyncSession, *, include_posts: bool = True) -> Pool:
    options = LOAD_OPTS if include_posts else [*METADATA_LOAD_OPTS, raiseload(Pool._posts)]
    try:
        pid = int(pool_id_or_name)
        stmt = select(Pool).where(Pool.pool_id == pid).options(*options)
    except ValueError:
        stmt = (
            select(Pool)
            .join(PoolName, PoolName.pool_id == Pool.pool_id)
            .where(PoolName.name == pool_id_or_name)
            .options(*options)
        )
    result = await db.execute(stmt)
    pool = result.scalar_one_or_none()
    if pool is None:
        raise HTTPException(status_code=404, detail="Pool not found")
    return pool


async def _get_default_category(db: AsyncSession) -> PoolCategory:
    result = await db.execute(
        select(PoolCategory).where(PoolCategory.default.is_(True)).limit(1)
    )
    cat = result.scalar_one_or_none()
    if cat is None:
        result = await db.execute(select(PoolCategory).limit(1))
        cat = result.scalar_one_or_none()
    return cat


async def _apply_pool_posts(pool: Pool, post_ids: list[int], db: AsyncSession) -> None:
    for pp in list(pool._posts):
        await db.delete(pp)
    await db.flush()
    await _bulk_insert_pool_posts(db, pool.pool_id, post_ids)


# ── GET ──────────────────────────────────────────────────────────────

@router.post("/pools/repair-urlless")
async def repair_urlless_pools(dry_run: bool = False, db: AsyncSession = Depends(get_db)) -> dict:
    """Catch-all guard: ensure every pool has a primary pool_url.

    A pool with no pool_url row resolves observed_count to 0 ("0" display bug)
    and breaks importer dedup. These arise from create paths that insert pool then
    `INSERT INTO pool_url ... ON CONFLICT (url) DO NOTHING` when the url was
    already owned (race / pre-existing) — i.e. a duplicate of an existing
    url-bearing pool.

    Disposition per url-less pool P:
      • consolidated → if every post of P also lives in a single url-bearing
        twin T (P ⊆ T, a pure duplicate), delete P. Its posts already exist in
        the canonical T, so nothing is lost — this "整流s" the duplicate into
        the real pool. Empty url-less pools (no posts, no cover) are also
        deleted as stray stubs.
      • sentinelled → otherwise (genuine standalone, or partial overlap that
        needs human review) assign a `manual:pool/<id>` primary url so the
        invariant holds and it displays its real count instead of 0.

    Idempotent; wired into the reconcile timer. dry_run=true previews only.
    """
    rows = (await db.execute(text("""
        SELECT p.id AS pid,
               (SELECT count(*) FROM pool_post pp WHERE pp.pool_id = p.id) AS posts,
               (SELECT b.pool_id
                  FROM pool_post a JOIN pool_post b ON a.post_id = b.post_id
                  JOIN pool_url pu ON pu.pool_id = b.pool_id AND pu.is_primary
                  WHERE a.pool_id = p.id AND b.pool_id <> p.id
                  GROUP BY b.pool_id ORDER BY count(*) DESC LIMIT 1) AS twin,
               (SELECT count(*)
                  FROM pool_post a JOIN pool_post b ON a.post_id = b.post_id
                  JOIN pool_url pu ON pu.pool_id = b.pool_id AND pu.is_primary
                  WHERE a.pool_id = p.id AND b.pool_id <> p.id
                  GROUP BY b.pool_id ORDER BY count(*) DESC LIMIT 1) AS overlap
        FROM pool p
        WHERE NOT EXISTS (SELECT 1 FROM pool_url pu WHERE pu.pool_id = p.id)
    """))).mappings().all()

    consolidate, sentinel, partial = [], [], []
    for r in rows:
        pid, posts, twin, overlap = r["pid"], r["posts"], r["twin"], r["overlap"]
        if posts == 0:
            consolidate.append(pid)            # empty stray stub → delete
        elif twin is not None and overlap == posts:
            consolidate.append(pid)            # P ⊆ T pure duplicate → delete
        else:
            sentinel.append(pid)
            if twin is not None:
                partial.append({"pool_id": pid, "twin": twin, "overlap": overlap, "posts": posts})

    if dry_run:
        return {"dry_run": True, "consolidate": len(consolidate),
                "sentinel": len(sentinel), "partial_review": partial}

    for pid in consolidate:
        await db.execute(text("DELETE FROM pool_name WHERE pool_id = :pid"), {"pid": pid})
        await db.execute(text("DELETE FROM pool WHERE id = :pid"), {"pid": pid})
    for pid in sentinel:
        await db.execute(text("""
            INSERT INTO pool_url (pool_id, url, is_primary, observed_count)
            VALUES (:pid, :u, true,
                    (SELECT count(*) FROM pool_post WHERE pool_id = :pid))
            ON CONFLICT (url) DO NOTHING
        """), {"pid": pid, "u": f"manual:pool/{pid}"})
        await db.execute(text("""
            INSERT INTO pool_state (pool_id, scope) VALUES (:pid, NULL)
            ON CONFLICT (pool_id) DO NOTHING
        """), {"pid": pid})
    await db.commit()
    return {"consolidated": len(consolidate), "sentinelled": len(sentinel),
            "partial_review": partial}


@router.post("/pools/reconcile-counts")
async def reconcile_pool_counts(dry_run: bool = False, db: AsyncSession = Depends(get_db)) -> dict:
    """Resync primary pool_url.observed_count to the real pool_post count.

    The observed_count column is trigger-maintained (+1/-1 on pool_post
    INSERT/DELETE) but merges that bulk-move pool_post rows — or inserts applied
    with session_replication_role=replica — skip the trigger, leaving
    observed_count stale (often 0 while the pool holds 100+ posts → "0"
    display + broken importer skip logic). This sets observed_count to the actual
    member count and bumps expected_count to at least that (monotonic
    invariant) so the badge reads N/N. Idempotent.

    G4 (plan archive-provenance-and-near-dup rev5): from THIS endpoint's
    perspective pool_url.expected_count is DISPLAY-ONLY cosmetics — a hint for
    dashboards/badges. The pg-native import pipeline's own completeness
    decision (is a source fully imported?) is per-item and count-INDEPENDENT
    (INV-1, its per-source "done" view) plus the G2 files_expected/
    files_uploaded shortfall check — NEITHER reads this column.

    HOWEVER a repo-wide grep (G4 audit, 2026-07-12) found THREE surviving
    control-flow (not display) readers outside the pg-native path — flagged
    here per the plan's "do not silently change" instruction, left AS-IS
    (none migrated to G2 accounting) because all three also cover the legacy
    non-pg-native task-queue path, which the pg-native per-item table does not
    observe at all:
      1. the importer's pool reconciler (derive_status): actual>=expected_count
         -> 'complete' else 'broken', and dispatches a repair task on
         'broken'. The ACTUAL periodic timer target (see reconcile-into-
         dedup-scan wiring below) — this is the real decision consumer this
         docstring previously implied was reconcile-counts/repair-urlless
         themselves (neither of those two endpoints is wired to any timer;
         see the dedup-scan endpoint's docstring for that correction).
      2. the importer's task router, POST .../cancel-ingested: cancels a
         pending task when `pu.observed_count > 0 AND (pu.expected_count
         IS NULL OR pu.observed_count >= pu.expected_count)` — a genuine
         skip/cancel decision.
      3. the legacy per-task import worker: a "physically_complete"
         short-circuit — `observed_count >= expected_count > 0` skips a
         re-import entirely (dispatch-time no-op) to avoid burning source quota
         on already-complete pools (bug fix 2026-05-29).
    """
    sel = text("""
        SELECT pu.pool_id,
               (SELECT count(*) FROM pool_post pp WHERE pp.pool_id = pu.pool_id) AS actual
        FROM pool_url pu
        WHERE pu.is_primary
          AND COALESCE(pu.observed_count, 0)
              <> (SELECT count(*) FROM pool_post pp WHERE pp.pool_id = pu.pool_id)
    """)
    rows = (await db.execute(sel)).mappings().all()
    if dry_run:
        return {"dry_run": True, "divergent": len(rows)}
    for r in rows:
        await db.execute(text("""
            UPDATE pool_url
            SET observed_count = :actual,
                expected_count = GREATEST(COALESCE(expected_count, 0), :actual)
            WHERE pool_id = :pid AND is_primary
        """), {"actual": r["actual"], "pid": r["pool_id"]})
    await db.commit()
    return {"reconciled": len(rows)}


@router.post("/pools/{pool_id}/dedup-scan")
async def dedup_scan_pool(
    pool_id: int, dry_run: bool = Query(True), db: AsyncSession = Depends(get_db),
) -> dict:
    """Demote within-pool exact perceptual-hash duplicates to near_dup.

    Plan archive-provenance-and-near-dup rev5, Phase 2 (rev4 carryover).
    Root cause: /api/upload's md5-only dedup lets a
    perceptual re-encode (same page, re-compressed by a re-import/re-archive)
    ingest as a SEPARATE visible post — pool 5426's near-dup inflation. Since
    migration 0050/this-plan's upload.py change, perceptual_hash is stamped
    SYNCHRONOUSLY at upload time, so a same-pool exact collision is detectable
    immediately (no async lag to wait out).

    Scope is deliberately the OPPOSITE of scripts/backfill_media.py's global
    cull: that pass explicitly EXEMPTS every pool member from cross-pool
    perceptual dedup ("the same work in Weekly Shonen Jump AND the collected
    tankobon is not a duplicate") — it only dedups pool-LESS standalone posts.
    This endpoint is pool-scoped (WITHIN one pool's own members) and does not
    touch anything outside pool_id, so the two mechanisms never compete for
    the same rows.

    Clustering: EXACT perceptual_hash match (Hamming-0) among this pool's
    status='ok' members — same bucket definition as GET /api/similar's
    `pairsOnly` clusters (no pixel-difference threshold: the plan's Rejected
    Alternatives found re-encode max-block (1.20%) and genuine-mosaic-pair
    (1.42%) pixel-diff distributions inseparable, so pixel diff is NOT the
    discriminator — hash-exact-match + tag-state IS).

    INV-2 (never violate): a cluster containing BOTH a censored-tag member
    (CENSOR_TAGS, the same list GET /api/similar uses) AND an uncensored
    member is a decensor/censor-detector training TEACHER PAIR, not a
    duplicate — left completely untouched (not even partially demoted).
    Only a cluster that is ALL-censored or ALL-uncensored gets demoted.

    Demotion (dry_run=false only): keep the LOWEST pool `ord` member as
    representative (INV-5: natural reading order — the earliest page in
    archive-contiguous order is the "canonical" copy); every other member of
    the cluster -> status='near_dup', duplicate_of=representative,
    perceptual_distance=0. INV-1: post.data/thumbnail are NOT nulled (unlike
    the legacy backfill_media cull, whose targets were still un-materialized
    'pending' rows) — an 'ok' pool member already has its content in place, so
    keeping it means a demoted post is reversible with a single UPDATE
    (status='near_dup'->'ok', duplicate_of=NULL — the same pattern
    a gallery-page restore script already used), with ZERO re-fetch/
    re-encode needed. INV-4 idempotent: the base query only selects
    status='ok' rows, so an already-demoted post drops out of clustering on
    the next scan — re-running a fully-scanned pool finds 0 clusters. INV-G:
    mutates ONLY post.status/duplicate_of/perceptual_distance — never touches
    importer per-item accounting or pool_post membership (the demoted post STAYS a
    pool member row; only its displayable-status flips), so this cannot
    feed back into G2's shortfall counters or trigger a re-fetch loop.

    dry_run (default true, plan's HARD CHECKPOINT RULE): report the plan only,
    no writes. Call with dry_run=false to apply (still additive/reversible;
    the plan's checkpoint gate is about the BACKFILL SCRIPT driving this over
    all pools, not this endpoint's mechanism itself).
    """
    from api.routers.similar import CENSOR_TAGS  # single source of truth (INV-2)

    exists = await db.scalar(select(Pool.pool_id).where(Pool.pool_id == pool_id))
    if exists is None:
        raise HTTPException(404, "pool not found")

    rows = (await db.execute(text("""
        SELECT pp.post_id AS id, pp.ord AS ord, encode(p.perceptual_hash, 'hex') AS h
        FROM pool_post pp
        JOIN post p ON p.id = pp.post_id
        WHERE pp.pool_id = :pid AND p.status = 'ok' AND p.perceptual_hash IS NOT NULL
        ORDER BY pp.ord
    """), {"pid": pool_id})).all()

    by_hash: dict[str, list[tuple[int, int]]] = {}
    for r in rows:
        by_hash.setdefault(r.h, []).append((r.ord, r.id))
    clusters = {h: members for h, members in by_hash.items() if len(members) >= 2}

    ids = [pid for members in clusters.values() for _, pid in members]
    censored: set[int] = set()
    if ids:
        cstmt = text(
            "select distinct pt.post_id from post_tag pt "
            "join tag_name tn on tn.tag_id = pt.tag_id "
            "where pt.post_id in :ids and tn.name in :ctags"
        ).bindparams(bindparam("ids", expanding=True), bindparam("ctags", expanding=True))
        for (pid,) in (await db.execute(cstmt, {"ids": ids, "ctags": list(CENSOR_TAGS)})).all():
            censored.add(pid)

    plan: list[dict] = []
    would_demote = 0
    skipped_divergent = 0
    for h, members in clusters.items():
        states = {pid in censored for _, pid in members}
        if len(states) > 1:
            # INV-2: mixed censored/uncensored within one exact-hash cluster —
            # a decensor teacher pair. Leave the WHOLE cluster untouched.
            skipped_divergent += 1
            plan.append({"hash": h, "action": "skip_divergent",
                        "members": sorted(pid for _, pid in members)})
            continue
        members_sorted = sorted(members)  # (ord, post_id) ascending -> ord order
        rep_id = members_sorted[0][1]
        dupes = [pid for _, pid in members_sorted[1:]]
        would_demote += len(dupes)
        plan.append({"hash": h, "action": "demote", "representative": rep_id,
                    "demoted": dupes})

    # REPORT-ONLY (plan kura-phash-dedup-safety): perceptual-hash auto-demotion
    # is REMOVED. A perceptual hash is a SIMILARITY signal (it powers GET
    # /api/similar), NOT an identity proof. Hamming-0 phash equality does not
    # mean "same image": a censor-swap pair (a mosaic/bar changed in one region,
    # the rest identical) and a plain re-encode produce the SAME small overall
    # diff, and template-reuse AI art (same composition, different CHARACTER —
    # Android 18 vs Tsunade) collides at Hamming-0. Auto-demoting on it silently
    # discarded 28,304 posts including decensor teacher pairs and distinct-
    # character assets. sha256/md5 (DB-enforced by ux_post_sha256_ok) is the ONLY
    # auto-dedup key. This endpoint now ONLY reports candidate clusters for human
    # review (and feeds no status write); the `dry_run` param is retained for
    # caller back-compat but both values behave identically (report, never write).
    return {"report_only": True, "pool_id": pool_id, "ok_members_scanned": len(rows),
            "clusters": len(clusters), "would_demote": would_demote, "demoted": 0,
            "skipped_divergent": skipped_divergent, "plan": plan,
            "note": "perceptual auto-demote removed; phash is search-only, sha256/md5 is the dedup key"}


async def _dashboard_nodes(db: AsyncSession):
    nodes = (await db.execute(text(
        "SELECT w.host, n.release_id, n.verified, n.detail, n.reported_at, "
        "CASE WHEN n.reported_at IS NULL THEN 'missing' "
        "     WHEN now() - n.reported_at < interval '5 minutes' THEN 'active' "
        "     WHEN now() - n.reported_at < interval '1 hour' THEN 'stale' "
        "     ELSE 'dead' END AS liveness, "
        "w.description "
        "FROM tagger_expected_worker w "
        "LEFT JOIN tagger_node_state n ON n.host = w.host "
        "ORDER BY (n.reported_at IS NULL), n.reported_at DESC"
    ))).mappings().all()
    # 登録忘れの worker (expected に無いが報告はしている) も別途拾う — 逆方向の
    # ドリフトも沈黙させない。
    unexpected_nodes = (await db.execute(text(
        "SELECT n.host, n.release_id, n.reported_at FROM tagger_node_state n "
        "WHERE NOT EXISTS (SELECT 1 FROM tagger_expected_worker w WHERE w.host = n.host)"
    ))).mappings().all()
    return nodes, unexpected_nodes


@dashboard_router.get("/tagger-dashboard/summary/compact", include_in_schema=False)
async def tagger_dashboard_summary_compact(auth_user: User = Depends(get_current_user)):
    import asyncio
    from fastapi.responses import JSONResponse
    from api.services.dashboard_runtime_cache import fresh_snapshot

    return JSONResponse(await asyncio.to_thread(fresh_snapshot), headers={"Cache-Control": "private, no-store"})


@dashboard_router.get("/tagger-dashboard/summary", include_in_schema=False)
async def tagger_dashboard_summary(auth_user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    import asyncio
    from fastapi.responses import JSONResponse
    from api.services.dashboard_summary import snapshot
    from api.services.web_release_observation import dashboard_status
    from api.services.dashboard_human_support import read_human_support
    summary = await snapshot(db)
    summary["human_support"] = await read_human_support(db)
    from api.services.dashboard_base_comparison import read_comparison
    summary["base_comparison"] = await read_comparison(db)
    from api.services.dashboard_base_vocabulary import read_vocabulary
    summary["base_vocabulary"] = await read_vocabulary(db)
    summary["web_release"] = await asyncio.to_thread(dashboard_status)
    return JSONResponse(summary, headers={"Cache-Control": "private, no-store"})


@dashboard_router.get("/tagger-dashboard/lifecycle-health", include_in_schema=False)
async def tagger_dashboard_lifecycle_health(auth_user: User = Depends(get_current_user),
                                            db: AsyncSession = Depends(get_db)):
    from fastapi.encoders import jsonable_encoder
    from fastapi.responses import JSONResponse
    from api.services.lifecycle_health import snapshot
    return JSONResponse(jsonable_encoder(await snapshot(db)), headers={"Cache-Control": "private, no-store"})


@dashboard_router.get("/tagger-dashboard/experiment", include_in_schema=False)
async def tagger_dashboard_experiment():
    """Bounded local observation only; no aggregate query or worker contact."""
    from fastapi.responses import HTMLResponse
    from api.tagger_core.experiment_status import read
    from api.tagger_core.dashboard_experiment import render
    return HTMLResponse(render(read()), headers={"Cache-Control": "no-store"})


@dashboard_router.get("/tagger-dashboard/nodes", include_in_schema=False)
async def tagger_dashboard_nodes(db: AsyncSession = Depends(get_db)):
    """Heartbeat snapshot only: never wait for dashboard aggregates or contact workers."""
    from fastapi.encoders import jsonable_encoder
    from fastapi.responses import JSONResponse
    nodes, unexpected = await _dashboard_nodes(db)
    return JSONResponse(jsonable_encoder({
        "nodes": [dict(n) for n in nodes],
        "unexpected": [dict(n) for n in unexpected],
    }), headers={"Cache-Control": "no-store"})


@dashboard_router.get("/tagger-dashboard", include_in_schema=False)
async def tagger_dashboard(raw: bool = Query(False), db: AsyncSession = Depends(get_db),
                           include_nodes: bool = Query(True)):
    """tagger ダッシュボード — **スキーマ直結の動的レンダー** (2026-07-20、静的HTML
    push を廃止)。毎リクエストで DB(tagger_release/active・tagger_node_state・
    tagger_expected_worker・post.tagging_status) と patches/*.json(head 詳細) を読み、
    メインタガーと全 head の状況を同時表示する。kura Web の header からリンク (モバイル含む)。

    worker の稼働状況 (0071 tagger_expected_worker、同日追加): 「一度も報告した
    ことがない worker」を沈黙のまま見落とさないための明示登録テーブルを
    LEFT JOIN する — primary の kura-tagger.service が1ヶ月 enable されずに誰も
    気付かなかった事故の直接対策。鮮度 (active/stale/dead) は SQL の now() で
    毎回導出 (アプリ側の時計計算に頼らない、フリート共通の "beat"/"stale" 慣習)。"""
    from fastapi.responses import HTMLResponse, RedirectResponse
    import json as _json, html as _h
    from urllib.parse import quote as _q
    # ブラウザ直アクセスは共通 Header 付き route へ誘導、iframe だけ ?raw=1 で生 HTML。
    if not raw:
        return RedirectResponse("/tagger-dashboard", status_code=307)

    # ── tagger_config (0098): ML判断の閾値がダッシュボードから編集可能 (user
    # 指示「マジックナンバーにせず、変数としてダッシュボードからコントロール
    # できるようにしてくれ」/「その他の値についてもだ」)。移行前(テーブル
    # 未作成)でも壊れないよう to_regclass で存在確認してから読む。
    cfg_table_exists = (await db.execute(text(
        "SELECT to_regclass('public.tagger_config') IS NOT NULL"
    ))).scalar_one()
    cfg_rows = []
    if cfg_table_exists:
        cfg_rows = (await db.execute(text(
            "SELECT key, value, description, updated_at, updated_by FROM tagger_config ORDER BY key"
        ))).mappings().all()
    # 進捗バー計算用 (user指示 2026-07-20「headタグ検証の実行量と更新まで
    # あとどれぐらい必要かをバーで」)。api/tagger_core/config.py::DEFAULTS と同じ値
    # をfallbackに(未migration環境でも壊れない)。
    cfg_map = {r["key"]: float(r["value"]) for r in cfg_rows}
    cfg_map.setdefault("min_val_pos_support", 100)
    cfg_map.setdefault("min_gt_labels", 10)

    # ── schema 直結: 運用状態を DB から (BLOB 列 model_data/selected_tags_csv は触らない) ──
    rel = (await db.execute(text(
        "SELECT r.id, r.base_model_repo, r.n_stock, r.n_out, r.created_at, r.created_by, "
        "r.patched FROM tagger_release r JOIN tagger_release_active a ON a.release_id = r.id LIMIT 1"
    ))).mappings().first()
    live_heads: dict = {}
    if rel and rel["patched"]:
        pv = rel["patched"]
        live_heads = pv if isinstance(pv, dict) else _json.loads(pv)
    # ── content-head 配備 (tagger_head_policy) — release patched とは**別の配備台帳**。
    # 2026-09-03 incident: dark_labia_minora は policy 経由で全 post に発火し
    # アーカイブの 45% に付いていたのに、このダッシュボードの「live」は
    # release.patched しか見ておらず、配備中である事実がどこにも表示されなかった。
    # 片方の台帳だけ読む画面は、もう片方の台帳の事故を構造的に映せない。
    # 効力のある版は tagger_current_head_policy (0186) が唯一持つ。
    policy_by_tag: dict[str, dict] = {}
    if (await db.execute(text(
            "SELECT to_regclass('public.tagger_current_head_policy') IS NOT NULL"))).scalar():
        for _pr in (await db.execute(text(
            "SELECT p.tag, p.patch_version, p.threshold, p.apply_mode, p.gate_tags "
            "FROM tagger_current_head_policy p"
        ))).mappings().all():
            policy_by_tag[_pr["tag"]] = {
                "ver": _pr["patch_version"], "thr": float(_pr["threshold"]),
                "mode": _pr["apply_mode"], "gates": sorted(_pr["gate_tags"] or [])}
    # ── curated domain 宣言 (tagger_tag_rule → tagger_tag_condition) — 昇格が
    # gate_tags を導出する側の台帳 (v1.175.0 head_policy.domain_gate_tags)。
    # 宣言と配備の食い違い (= 今回の incident の signature) をここで検出して出す。
    # tag -> sorted all-of gates。any/none を含む条件は gate_tags で表現できない
    # ので None (比較不能として表示)。**キーが無い = 未宣言** — それを「無条件で
    # よい」と読まないのが v1.175.0 の教訓。
    declared_by_tag: dict[str, list | None] = {}
    if (await db.execute(text(
            "SELECT to_regclass('public.tagger_tag_rule') IS NOT NULL"))).scalar():
        for _dr in (await db.execute(text(
            "SELECT r.tag, c.all_tags, c.any_tags, c.none_tags FROM tagger_tag_rule r "
            "JOIN tagger_tag_condition c ON c.name = r.condition"
        ))).mappings().all():
            _dt_ = _dr["tag"]
            if (_dr["any_tags"] or []) or (_dr["none_tags"] or []):
                declared_by_tag[_dt_] = None
                continue
            if declared_by_tag.get(_dt_, []) is None:
                continue
            declared_by_tag[_dt_] = sorted(
                set(declared_by_tag.get(_dt_) or []) | set(_dr["all_tags"] or []))

    # gated-veto 稼働タグ(plan:kura-tagger-self-improvement-loop 「打ち消し続ける」)。
    # release ckpt に bundle され全 worker が推論時に適用する veto head の gate 一覧。
    # primary 上の VETO_HEADS_DIR ファイルから取得(dashboard は primary でのみ動く)。これが
    # あると「そのタグの誤検出を推論時に自動で打ち消している」= veto 稼働の可視化。
    import os as _os
    from pathlib import Path as _Path
    _veto_dir = _Path(_os.environ.get("KURA_MODELS_DIR")
                      or (_Path.home() / ".cache" / "kura-tagger" / "models")) / "veto-heads"
    veto_gates = {p.stem for p in _veto_dir.glob("*.safetensors")} if _veto_dir.is_dir() else set()
    # ── evidence-gated reap authority (plan:kura-tagger-self-improvement-loop,
    # user 2026-07-22「現状の実装を反映」)。tagger/reap_authorized.json = サーバの
    # reap ゲートの唯一の入力(api/services/tagging.py::_reap_authorized)。worker が
    # reap_domain(patched∪veto)を提案しても、GT-positive recall が min_recall 以上と
    # 実測されたタグだけが実際に reap される。ここを可視化して「どのタグが実際に打ち
    # 消されているか / なぜ他は保留か(recall 不足)」をダッシュボードに反映する。──
    import json as _json
    from api.state_paths import reap_authority_path
    _reap_path = reap_authority_path()
    reap_authorized: set[str] = set()
    reap_measured: dict[str, float | None] = {}
    reap_promotable: set[str] = set()
    reap_auc: dict[str, float | None] = {}
    reap_min = 0.85
    try:
        _rd = _json.loads(_reap_path.read_text())
        reap_authorized = set(_rd.get("authorized") or [])
        reap_min = float(_rd.get("min_recall", 0.85))
        for _t, _m in (_rd.get("measured") or {}).items():
            _m = _m or {}
            reap_measured[_t] = _m.get("gt_pos_recall")
            reap_auc[_t] = _m.get("fresh_auc")  # fresh linear probe ROC-AUC = head-fixability
            if _m.get("promotable"):
                reap_promotable.add(_t)  # denied now, but a fresh probe separates it (retrain graduates)
    except Exception:  # noqa: BLE001
        pass

    # reconciler の自動処理 disposition(tagger/reconciler_disposition.json、
    # reconciler が毎 tick 出力する唯一の判定 SoT)。ダッシュボードはこれを表示する
    # だけで、ゲートを再計算しない(drift 防止)。これが無いと「＋正例 あとN」が、
    # corpus 正例で自動 precision 再学習される established 過検出タグにも出て
    # 「正例が要る=blocked」と誤読させる(user 2026-07-23、機構配線後の食い違い)。
    from api.services import tagger_progress as _progress
    progress = await _progress.progress(db)
    disposition = progress["tags"] if progress["current"] else {}
    disp_min_neg = progress["min_gt_negative"]
    disp_generated_at = progress["generated_at"]
    # ── 自動化の稼働状況(user 2026-07-22「全部はい」)。7日窓の増減列では sweep の
    # 短期活動が希釈されて見えないため、(1) sweep 進捗(稼働 release で処理済み /
    # backlog 残)、(2) 直近1h/24h の稼働・reap 数、(3) authorized reap タグの絶対数と
    # 24h reap(「打ち消し」の実進捗)を別枠で可視化する。──
    _rel_id = rel["id"] if rel else 0
    # 2026-09-05: この 3 値は `api/services/tagger_situation.py` に移した。分子
    # (release の audit 履歴) を分母 (post.tagging_status='done' の現在数) で割って
    # いたため **1308.9%** と表示され、対になる max(0, done-processed) が
    # **「残 backlog 0」** を出していた — 941,116 件が queue に居る最中に。
    # sweep が済んだ post を再 queue するので、post は分子に残ったまま分母から
    # 出る。母集団違いを clamp が隠す形だった。分母はアーカイブ全体にした。
    from api.services import tagger_situation as _situation
    _sweep_cov = await _situation.sweep_coverage(db, _rel_id)
    _queue = await _situation.queue_state(db)
    _act = (await db.execute(text(
        "SELECT "
        "count(*) FILTER (WHERE created_at > now()-interval '1 hour') AS c1h, "
        "coalesce(sum(cardinality(added_tags)) FILTER (WHERE created_at > now()-interval '1 hour'),0) AS a1h, "
        "coalesce(sum(cardinality(removed_tags)) FILTER (WHERE created_at > now()-interval '1 hour'),0) AS r1h, "
        "count(*) AS c24h, "
        "coalesce(sum(cardinality(added_tags)),0) AS a24h, "
        "coalesce(sum(cardinality(removed_tags)),0) AS r24h "
        "FROM post_tag_audit WHERE source='auto_tag' AND created_at > now()-interval '24 hours'"
    ))).mappings().one()
    _reap_tag_rows = []
    if reap_authorized:
        _tags = sorted(reap_authorized)
        # 現在数(tag_id index で高速)
        _cur = {r["name"]: int(r["cur"]) for r in (await db.execute(text(
            "SELECT tn.name, count(*) AS cur FROM post_tag pt JOIN tag_name tn ON tn.tag_id = pt.tag_id "
            "WHERE tn.name = ANY(:tags) GROUP BY tn.name"
        ), {"tags": _tags})).mappings().all()}
        # 24h reap は unnest を1回だけ(タグごとの相関サブクエリだと 99k 行を毎回 seq scan
        # して ~1.7s かかっていた — user 2026-07-22「読み込みに時間がかかる」の主因)。
        _r24 = {r["tag"]: int(r["n"]) for r in (await db.execute(text(
            "SELECT rt AS tag, count(*) AS n FROM post_tag_audit a, unnest(a.removed_tags) rt "
            "WHERE a.source='auto_tag' AND a.created_at > now()-interval '24 hours' AND rt = ANY(:tags) "
            "GROUP BY rt"
        ), {"tags": _tags})).mappings().all()}
        _reap_tag_rows = [{"tag": t, "cur": _cur.get(t, 0), "reaped_24h": _r24.get(t, 0)} for t in _tags]
    # ── expected worker registry (0071) LEFT JOIN node_state: 一度も報告しない
    # worker を沈黙のまま見落とさない(primary の kura-tagger.service が1ヶ月
    # enable されず誰も気付かなかった事故の直接対策)。鮮度は SQL の now() で
    # 判定 (アプリ側時計に頼らない、フリート共通の "beat"/"stale" 慣習に合わせる)。
    nodes, unexpected_nodes = await _dashboard_nodes(db) if include_nodes else ([], [])
    queue = await tagging_queue.by_status(db)

    # ── throughput: 「いつどのタガーがどれだけ仕事したか」(post_tag_audit.host/
    # release_id, 0072 — この同日追加の provenance 列を直読)。actor_id を主キーに
    # するのは host/release_id が trainer head 群でまだ未報告(追従中)のため —
    # host 待ちにすると現状表示が空になってしまう。上位30 actor まで(全体は
    # 26-30程度で収まる規模、live 実測)。
    #
    # 時間窓は **WHERE** に置く (2026-09-16, incident friction/slow-query:primary)。
    # 元は窓を全部 FILTER 側に持ち、`HAVING count(*) FILTER (7d) > 0` で捨てていた
    # ので、捨てる行まで全部読んでいた: post_tag_audit 2,993,156 行の Parallel Seq
    # Scan で 1022ms、実際に要るのは 7d 内の 325,572 行だけだった。
    #
    # WHERE に下ろしても **結果は一致する**:
    #   ・n24h / n7d / added_7d / removed_7d はどれも 7d 以内の窓 (24h ⊂ 7d)。
    #   ・`last_seen` も変わらない。HAVING が残す group は 7d 内に 1 行以上持つ group
    #     だけで、窓の外の行は定義上それより古いので max は窓の中にある。
    #   ・HAVING は「7d 内に 1 行以上」— WHERE で切った時点で同じ集合になるので落とす。
    # 実測 1022ms → 144ms (ix_post_tag_audit_created_at の Parallel Index Scan)。──
    throughput = (await db.execute(text(
        "SELECT actor_id, host, release_id, "
        "count(*) FILTER (WHERE created_at > now() - interval '24 hours') AS n24h, "
        "count(*) AS n7d, "
        "coalesce(sum(cardinality(added_tags)), 0) AS added_7d, "
        "coalesce(sum(cardinality(removed_tags)), 0) AS removed_7d, "
        "max(created_at) AS last_seen "
        "FROM post_tag_audit "
        "WHERE source = 'auto_tag' AND created_at > now() - interval '7 days' "
        "GROUP BY actor_id, host, release_id "
        "ORDER BY n7d DESC LIMIT 30"
    ))).mappings().all()
    # タグ別内訳(全 actor 横断・直近7日): どのタグが最も追加/削除されているか。
    # 人間の操作は actor_type で判定。一括修正など source 名の追加に依存しない。
    # source を auto_tag に限定すると 削除(=ほぼ全て人間の補正 source='manual')が
    # 抜け落ち 減カラムが常に~0 になる(user 2026-07-22 指摘)。auto_tag と manual を
    # 別カラムに分割(user 2026-07-22:「autotag とマニュアルのカラムは分割しろ」)。
    # 自動増=タガーが付与 / 手動減=人間の補正削除、の対比が veto 候補の信号
    # (例: anal_hair はタガーが付与し続け人間が大量削除 = veto すべきタグ)。
    # unnest+集計は post_tag_audit の行数に比例するため直近7日限定で軽量に保つ。
    tag_churn = (await db.execute(text(
        "WITH ev AS ("
        "  SELECT source, actor_type, unnest(added_tags) AS tag, 1 AS is_add, 0 AS is_rm FROM post_tag_audit "
        "    WHERE (source='auto_tag' OR actor_type='human') AND created_at > now() - interval '7 days' "
        "  UNION ALL "
        "  SELECT source, actor_type, unnest(removed_tags) AS tag, 0 AS is_add, 1 AS is_rm FROM post_tag_audit "
        "    WHERE (source='auto_tag' OR actor_type='human') AND created_at > now() - interval '7 days'"
        ") "
        "SELECT tag, "
        "  coalesce(sum(is_add) FILTER (WHERE source='auto_tag'), 0) AS auto_add, "
        "  coalesce(sum(is_rm)  FILTER (WHERE source='auto_tag'), 0) AS auto_rm, "
        "  coalesce(sum(is_add) FILTER (WHERE actor_type='human'),   0) AS man_add, "
        "  coalesce(sum(is_rm)  FILTER (WHERE actor_type='human'),   0) AS man_rm "
        "FROM ev GROUP BY tag"
        # LIMIT なし: 総churn降順で切ると 1girl 等の汎用タグが上位を占め、補正対象の
        # 稀少タグ(anilingus 等、総churn~250)が圏外に落ちて churn 全列が0表示になる
        # (user 2026-07-22 指摘)。集計は既に7日境界で bounded、出力は distinct タグ
        # (数千)で軽量。テーブルには「既に載っているタグだけ」を enrich する(下記)。
    ))).mappings().all()

    # ── 反復補正の推移: user指示 2026-07-21「正答率が低いanilingusの学習を
    # 繰り返して補正をしているんだけど、それを掌握するバーもほしい」。
    # tagger_head_candidateはtag×poolで1行(検証プールを回すたび追加)なので
    # 2ラウンド以上あるタグ=実際に反復補正が進行中のタグ。各ラウンドの補正率
    # はreasonテキストに埋め込み済み(pool #N 検証時、人間がX/Y件(Z%)を補正)
    # -- そのZ%を正規表現で取り出し、ラウンド順に並べて推移をバー化する。
    # 生pool再集計ではなくreason埋め込み値を使う理由: あの数字は「その時点で
    # 何%訂正されたか」の凍結された記録であり、後からpoolの中身が変わっても
    # (通常は変わらないが)過去の推移としては歴史的な値の方が正しい。
    correction_rounds = (await db.execute(text(
        "SELECT tag, source_pool_id, added_at, "
        "(regexp_match(reason, '\\((\\d+)%\\)'))[1]::int AS pct "
        "FROM tagger_head_candidate "
        "WHERE reason ~ '\\(\\d+%\\)' "
        "ORDER BY tag, added_at"
    ))).mappings().all()
    _rounds_by_tag: dict[str, list] = {}
    for r in correction_rounds:
        _rounds_by_tag.setdefault(r["tag"], []).append(r)
    iterative_tags = {t: rs for t, rs in _rounds_by_tag.items() if len(rs) >= 2}

    # ── head candidate (0075): verify-status切替に相乗りして自動登録される
    # 「head化する価値がありそうな概念」の一覧。既にtagger_patchに行がある
    # ものはpromoted扱いとして除外表示(二重管理しない)。──
    head_candidates = (await db.execute(text(
        "SELECT hc.id, hc.tag, hc.source_pool_id, hc.reason, hc.added_at, "
        "EXISTS(SELECT 1 FROM tagger_patch tp WHERE tp.tag = hc.tag) AS has_patch, "
        "(SELECT count(*) FILTER (WHERE g.verdict = 'positive') FROM tagger_gt_label g WHERE g.tag = hc.tag) AS gt_pos, "
        "(SELECT count(*) FILTER (WHERE g.verdict = 'negative') FROM tagger_gt_label g WHERE g.tag = hc.tag) AS gt_neg "
        "FROM tagger_head_candidate hc WHERE hc.status = 'candidate' "
        "ORDER BY hc.tag, hc.added_at DESC"
    ))).mappings().all()
    # user指示 2026-07-21「head candidateとのカード統合だ」-- tag単位に集約
    # (同じtagが複数poolラウンドで何行も出るため)、head状況テーブルへ直接
    # 合流させる。GT/has_patchはtag単位で既に同一値なので先頭行を代表に、
    # reason/pool/added_atは最新ラウンドを採用。
    candidates_by_tag: dict[str, dict] = {}
    for c in head_candidates:
        t = c["tag"]
        if t not in candidates_by_tag:
            candidates_by_tag[t] = {
                "reason": c["reason"], "source_pool_id": c["source_pool_id"],
                "added_at": c["added_at"], "gt_pos": c["gt_pos"] or 0, "gt_neg": c["gt_neg"] or 0,
                "rounds": 0,
            }
        candidates_by_tag[t]["rounds"] += 1

    # ── quarantine: 0068 tag_quarantine が SoT (infer.py の frozenset でなく schema) ──
    from api.tagger_core.quality_hold import held_names
    quar = set(await held_names(db))

    # ── lifecycle_stage: tagger_lifecycle view (0078) 直読 -- 「approved patchは
    # あるが、今まさにv-next再学習が走っている」を可視化するため(user指示
    # 2026-07-21「段階が伝わるようにして」)。tagger_patch.statusだけだと既存
    # approved patchの状態しか見えず、reconciler.pyが自動でv-next dispatchする
    # ようになった今、「approved固定」と「approved+裏でtraining中」が同じ表示
    # になってしまい区別がつかなかった。──
    lifecycle_by_tag = {
        r["tag"]: r["lifecycle_stage"]
        for r in (await db.execute(text(
            "SELECT tag, lifecycle_stage FROM tagger_lifecycle"
        ))).mappings().all()
    }

    # Human evidence is independent of archived review pools and model accuracy.
    from api.tagger_core.dashboard_evidence import EVIDENCE_SQL, evidence_counts
    evidence_by_tag = {r["tag"]: dict(r) for r in
                       (await db.execute(text(EVIDENCE_SQL))).mappings().all()}

    # ── head 詳細: tagger_patch を直読 (0070 で gt/head_type を専用列に昇格、band-aid 除去)。
    # 最新 version を DISTINCT ON で採る。head_type = 役割、gt = 教師、metrics = P/R/F1。──
    patch_rows = (await db.execute(text(
        "SELECT DISTINCT ON (tag) tag, version, status, head_type, logit_index, metrics, gt, "
        "why, created_by, updated_at FROM tagger_patch ORDER BY tag, version DESC"
    ))).mappings().all()
    seen = set()
    rows = []
    for d in patch_rows:
        met = d["metrics"] or {}
        if isinstance(met, str):
            met = _json.loads(met)
        g = d["gt"] or {}
        if isinstance(g, str):
            g = _json.loads(g)
        t = d["tag"]
        seen.add(t)
        rows.append({"tag": t, "type": d["head_type"], "logit": d["logit_index"], "ver": d["version"],
                     "status": d["status"], "p": met.get("precision"), "r": met.get("recall"),
                     "f1": met.get("f1"), "gt": g.get("n_train"),
                     "by": d["created_by"], "why": d["why"] or "", "updated_at": d["updated_at"],
                     "live": t in live_heads, "quar": t in quar})
    for t in sorted(live_heads):  # live だが registry に行が無い head も落とさない
        if t not in seen:
            rows.append({"tag": t, "type": "wd14_logit_patch", "logit": live_heads[t], "ver": None,
                         "status": "(registry 未登録)", "p": None, "r": None, "f1": None, "gt": None,
                         "by": None, "why": "", "updated_at": None, "live": True, "quar": t in quar})
            seen.add(t)
    # head candidateとの統合(user指示「head candidateとのカード統合だ」):
    # 既存head行にはcandidate情報を添え、まだtagger_patchが無い純粋な候補は
    # 新規行として追加する -- どちらも同じ1テーブルで見られるようにする。
    for r in rows:
        r["candidate"] = candidates_by_tag.get(r["tag"])
        r["lifecycle_stage"] = lifecycle_by_tag.get(r["tag"])
        r["perf"] = evidence_by_tag.get(r["tag"])
    for t, cand in candidates_by_tag.items():
        if t not in seen:
            rows.append({"tag": t, "type": None, "logit": None, "ver": None,
                         "status": "(候補のみ・未approved)", "p": None, "r": None, "f1": None, "gt": None,
                         "by": None, "why": "", "updated_at": None, "live": False, "quar": t in quar,
                         "candidate": cand, "lifecycle_stage": lifecycle_by_tag.get(t),
                         "perf": evidence_by_tag.get(t)})
            seen.add(t)
    rows.sort(key=lambda r: (not r["live"], r["candidate"] is None, r["tag"]))

    def e(x):
        return _h.escape(str(x)) if x is not None else "—"

    def pct(x):
        return f"{x:.0%}" if isinstance(x, (int, float)) else "—"

    n_live = len(live_heads)
    n_ok = sum(1 for r in rows if r["status"] == "approved")
    n_quar = sum(1 for r in rows if r["quar"])
    _LIVENESS = {"active": ("🟢", "active"), "stale": ("🟡", "stale"),
                 "dead": ("🔴", "dead (>1h)"), "missing": ("⚫", "MISSING — 未登録の稼働確認")}
    if nodes:
        _node_rows = []
        for n in nodes:
            icon, label = _LIVENESS[n["liveness"]]
            row_cls = "live" if n["liveness"] == "active" else "off"
            rel_txt = ("rel " + e(n["release_id"])) if n["release_id"] is not None else "—"
            ver_txt = ("✅" if n["verified"] else "❌") if n["reported_at"] is not None else "—"
            _node_rows.append(
                f"<tr class='{row_cls}'>"
                f"<td>{e(n['host'])}</td><td class=mut>{e(n['description'])}</td>"
                f"<td>{icon} {label}</td>"
                f"<td>{rel_txt}</td>"
                f"<td>{ver_txt}</td>"
                f"<td>{e(n['reported_at'])}</td>"
                f"<td class=mut>{e((n['detail'] or '')[:60])}</td></tr>"
            )
        node_html = "".join(_node_rows)
        if unexpected_nodes:
            node_html += "".join(
                f"<tr class=off><td>{e(n['host'])}</td>"
                f"<td class=mut>⚠ tagger_expected_worker 未登録(report はある)</td>"
                f"<td>⚠ unregistered</td><td>{('rel ' + e(n['release_id'])) if n['release_id'] is not None else '—'}</td>"
                f"<td>—</td><td>{e(n['reported_at'])}</td><td class=mut>—</td></tr>"
                for n in unexpected_nodes)
    else:
        node_html = "<tr><td colspan=7 class=mut>tagger_expected_worker が空 — 0071 未適用の可能性</td></tr>"
    node_panel = f"""<div data-tagger-nodes><h1 style='font-size:13px;margin:var(--control-padding-x) 0 var(--control-gap)'>worker node</h1>
<div class=wrap><table><tr><th>host<th>role<th>status<th>release<th>verified<th>last seen<th>detail</tr>{node_html}</table></div></div>""" if include_nodes else ""
    _ROLE = {"wd14_logit_patch": "logit-patch", "content_head": "content-head",
             "anatomy_multihead": "anatomy"}

    def _pbar(pct: float, title: str) -> str:
        pct = max(0.0, min(100.0, pct))
        color = "#3fb950" if pct >= 100 else "#e0a52e" if pct >= 40 else "#6e7681"
        return (f"<span class=pbar title='{e(title)}'>"
                f"<span class=pbar-fill style='width:{pct:.0f}%;background:{color}'></span></span>"
                f"<span class=mono>{pct:.0f}%</span>")

    # min_gt_positive: gated veto 学習に必要な正例数の閾値(_uni_row の ＋正例 導線・
    # 凡例で使う)。旧「過検出ホットスポット」panel はこの統合テーブルの 誤検出降順+導線
    # 列に吸収済み。
    _min_pos = cfg_map.get("min_gt_positive", 20)

    # ── 統合タグテーブル(user 2026-07-22:「タグ別増減・過検出ホットスポット・head状況
    # は tag が最左列の同一要素の側面。フィルタ/ソートで表現できる。文字が多すぎる、
    # 数値/割合バーだけでいい」)。3 panel を 1 テーブルに統合、client-side sort/filter。──
    _churn = {r["tag"]: (int(r["auto_add"]), int(r["auto_rm"]), int(r["man_add"]), int(r["man_rm"]))
              for r in tag_churn}
    # ── 登録判定 (4 クラス) ────────────────────────────────────────────
    # 判定器は 不要 / 配備可 / 掃除可 / 保留 の 4 つを出すが、2026-08-05 まで
    # auto_review が not-needed と hold しか扱わず、残り 2 つは旧 floor gate へ
    # 素通しして「検証内訳が無い」で却下されていた。**配備可 2 件と、
    # smegma の「誤検出を 163,583 件減らす」判定がそこに埋もれていた。**
    #
    # 理由文に `hold:` / `cleanup-capable:` 前置を入れて機械判別可能にしたが、
    # **owner が見る画面に出していなければ「一覧できる」ことにならない** —
    # 記録に残すことと人が見られることは別。ここがその画面。
    reg_rows = []
    if (await db.execute(text(
            "SELECT to_regclass('public.tagger_registration_request') IS NOT NULL"))).scalar():
        reg_rows = (await db.execute(text(
            "SELECT DISTINCT ON (tag) id, tag, status, requested_at, validated_at, "
            "       metrics->>'disposition' AS disposition, "
            "       coalesce(rejected_reason,'') AS reason, "
            "       gt->>'positive' AS gt_pos, gt->>'negative' AS gt_neg, "
            "       (metrics->'classification'->>'net_fp_removed_deploy')::float AS net, "
            "       (metrics->'classification'->>'lift')::float AS lift, "
            "       metrics->'classification'->>'at' AS op_at, "
            "       metrics->'arms' AS arms, "
            "       metrics->'support' AS support, metrics->>'reason' AS measurement_reason, "
            "       metrics->'serving_qualification' AS serving_qualification "
            "FROM tagger_registration_request "
            "ORDER BY tag, id DESC"
        ))).mappings().all()

    def _reg_bucket(r) -> str:
        """status と理由の前置から 4 クラスへ写す。**disposition が空の旧行**も
        取りこぼさない — 旧形式は理由文しか手掛かりが無い。"""
        if r.get("serving_qualification") is not None:
            if r["status"] == "pending":
                return "判定待ち"
            if (r["reason"] or "").startswith("awaiting_production_precision"):
                return "配備条件の確認待ち"
            return "不採用"
        d = (r["disposition"] or "").strip()
        reason = r["reason"] or ""
        if r["status"] == "validated":
            return "配備可"
        if d == "cleanup-capable" or reason.startswith("cleanup-capable:"):
            return "掃除可"
        if d == "hold" or reason.startswith("hold:"):
            return "測定不能"
        if d == "not-needed":
            return "不要"
        if "no val breakdown provided" in reason:
            # 旧 gate が新形式を読めずに落とした行。**性能不足ではない**ので
            # 「基準未達」に混ぜない — 混ぜると GT 待ちや owner 判断待ちが埋もれる。
            return "旧gate誤却下"
        return "基準未達"


    # tag -> 登録判定。**別パネルにしない** (user 2026-08-05「タグ状況カードに
    # 統合しろ」/ 既存の「head candidate とのカード統合だ」「特殊化するな」と同じ方針)。
    # 統合テーブルの 1 列として出す — タグの状態は 1 箇所で見える方がよい。
    from api.services.learning_execution import recent as read_learning_progress
    learning_rows = await read_learning_progress(db)
    learning_progress = learning_rows[0] if learning_rows else None
    learning_by_tag = {row["tag"]: row for row in learning_rows}
    from api.services.execution_receipts import published_candidate_coverage
    candidate_coverage = await published_candidate_coverage(db) if include_nodes else {}
    reg_by_tag: dict[str, dict] = {}
    for r in reg_rows:
        t = r["tag"]
        if t in reg_by_tag:          # 最新 (id DESC 順) を代表にする
            continue
        # 現タガー vs 候補の乖離 (user 2026-08-05「現タガーとどれだけ乖離して
        # いるかはどこで見える？」)。**A_deployed が比較の基準** — これが
        # 無いと「候補が良い」しか分からず、現タガーがどれだけ外しているかが
        # 見えない。0.5 は偶然、それ以下は逆相関 (付けるほど間違う)。
        _arms = r["arms"] or {}
        _a_node = _arms.get("A_deployed") or {}
        _cur = _a_node.get("auc")
        # **「基準が無い」には 2 種ある**。区別しないと「測れていない」と
        # 「そもそも比較対象が無い」が同じに見える:
        #   skipped = "no deployed row ..." → そのタグにまだ patch が無い新規候補。
        #             比較対象が存在しないのが正常 (実測 2026-08-05: 11 件中 10 件)。
        #   キー自体が無い → 測定が古く基準側を記録していない (2026-08-05 以前)。
        _cur_skip = _a_node.get("skipped")
        _best_k, _best_v = None, None
        for _k, _v in _arms.items():
            if _k in ("A_deployed", "E_stock_pixai") or not isinstance(_v, dict):
                continue
            _a = _v.get("auc")
            if _a is not None and (_best_v is None or _a > _best_v):
                _best_k, _best_v = _k, _a
        reg_by_tag[t] = {"bucket": _reg_bucket(r), "status": r["status"],
                         "net": r["net"], "lift": r["lift"], "at": r["validated_at"] or r["op_at"], "requested_at": r["requested_at"], "request_id": r["id"],
                         "reason": r["reason"], "gt_pos": r["gt_pos"], "gt_neg": r["gt_neg"],
                         "cur_auc": _cur, "best_auc": _best_v, "best_arm": _best_k,
                         # **描画側が参照するので必ず載せる** — 抽出しただけで
                         # 辞書に入れ忘れ、乖離バッジが 1 件も出なかった (2026-08-05)。
                         "cur_skip": _cur_skip, "serving_qualification": r.get("serving_qualification"),
                         "support": r.get("support"), "measurement_reason": r.get("measurement_reason")}

    # ── 再学習の進捗 (user 2026-08-05「再学習の進捗も見たい」) ──────────
    # candidate⇄training の往復と、いま training に入ってからの経過。
    # **別 panel にしない** — タグ状況カードの 1 列として出す (「特殊化するな」)。
    #
    # 往復回数が意味を持つのは、この系が「GT が育つ → 再測定 → 良くなる」を
    # 回す設計だから。回数だけ増えて結果が変わらないタグは、GT が育っていないか
    # 測っても floor に届いていないかのどちらかで、**どちらかは登録判定の列**で
    # 分かる (測定不能 = GT 待ち / 基準未達 = 性能)。
    retrain: dict[str, dict] = {}
    if (await db.execute(text(
            "SELECT to_regclass('public.tagger_lifecycle_event') IS NOT NULL"))).scalar():
        for r in (await db.execute(text(
            "SELECT tag, "
            "       count(*) FILTER (WHERE to_stage = 'training') AS rounds, "
            "       max(occurred_at) FILTER (WHERE to_stage = 'training') AS last_train, "
            "       max(occurred_at) AS last_any "
            "FROM tagger_lifecycle_event GROUP BY tag"
        ))).mappings().all():
            retrain[r["tag"]] = {"rounds": r["rounds"] or 0,
                                 "last_train": r["last_train"], "last_any": r["last_any"]}

    from datetime import datetime as _dt, timezone as _tz
    _now_utc = _dt.now(_tz.utc)
    _uni: dict[str, dict] = {}

    _reap_domain_seen = reap_authorized | set(reap_measured) | veto_gates

    def _ut(tag: str) -> dict:
        if tag not in _uni:
            _uni[tag] = {"tag": tag, "type": None, "live": False, "veto": tag in veto_gates,
                         "status": "", "stage": None, "perf": None,
                         "auto_add": 0, "auto_rm": 0, "man_add": 0, "man_rm": 0,
                         "cand": None, "quar": tag in quar, "rounds": None,
                         "reap_auth": tag in reap_authorized, "reap_dom": tag in _reap_domain_seen,
                         "reap_recall": reap_measured.get(tag),
                         "promotable": tag in reap_promotable, "auc": reap_auc.get(tag),
                         "disp": disposition.get(tag),
                         "reg": reg_by_tag.get(tag),
                         "retrain": retrain.get(tag),
                         # content-head 配備 (policy 台帳) と curated domain 宣言。
                         # has_decl と decl_gates を分けるのは「宣言が無い」と
                         # 「宣言はあるが gate_tags で表現できない」が別物だから。
                         "policy": policy_by_tag.get(tag),
                         "has_decl": tag in declared_by_tag,
                         "decl_gates": declared_by_tag.get(tag)}
        return _uni[tag]
    for r in rows:
        u = _ut(r["tag"]); u["type"] = r.get("type"); u["live"] = r["live"]
        u["status"] = r["status"] or ""; u["stage"] = r.get("lifecycle_stage")
        u["perf"] = r.get("perf"); u["cand"] = r.get("candidate")
    for row in learning_rows:
        _ut(row["tag"])
    for _t in reg_by_tag:
        _ut(_t)  # Rejected first candidates have no deployed patch row.
    for _t, _m in evidence_by_tag.items():
        _ut(_t)["perf"] = _m
    # Keep snapshot-only tags visible even when their decisions are historical.
    # The per-row badge below reports pause/staleness instead of an action claim.
    for _t in progress["tags"]:
        _ut(_t)
    for _t, _rs in iterative_tags.items():
        _ut(_t)["rounds"] = _rs
    # churn は最後に merge。既存タグ(head/gt/iterative)は必ず enrich し churn 全列を
    # 反映する(user 2026-07-22: anilingus の自動増/手動減が0表示だった — 原クエリの
    # LIMIT 80 総churn降順で汎用タグに押し出されていた)。churn だけに現れるタグは
    # 「人間が大量に手動削除=veto 候補」だけ追加し、1girl 等の汎用高 auto_add タグで
    # テーブルを膨張させない。閾値は min_gt_positive を流用(新マジックナンバーを作らない)。
    for _t, (_aa, _ar, _ma, _mr) in _churn.items():
        if _t not in _uni and _mr < int(_min_pos):
            continue
        u = _ut(_t); u["auto_add"] = _aa; u["auto_rm"] = _ar; u["man_add"] = _ma; u["man_rm"] = _mr

    uni = sorted(_uni.values(),
                 key=lambda x: (-evidence_counts(x["perf"])[1], -evidence_counts(x["perf"])[0],
                                -(x["auto_add"] + x["auto_rm"] + x["man_add"] + x["man_rm"]), x["tag"]))

    from api.tagger_core import dashboard_visual as visual

    def _uni_row(u: dict) -> str:
        corrections, violations, pos, neg = evidence_counts(u["perf"])
        tag = u["tag"]
        # 状態は多値(live+veto+training が同時に立つ)。各要素を独立バッジに分割し、
        # data-st トークンでフィルタのキーにする(user 2026-07-22:「状態カラムは
        # 要素を分割してくれ。フィルタリングのカギだ」)。表示とフィルタ token を対に。
        _pol = u.get("policy")
        _STATES = [
            (u["live"], "live", "🟢", "稼働 release に焼込済"),
            # policy 台帳の配備も live と同格の「稼働」— 2026-09-03 incident では
            # dark_labia_minora がこの経路で全 post に発火していたのに、release
            # 台帳しか見ないこの画面では稼働に見えなかった。
            (_pol is not None, "policy", "🧩",
             (f"content-head policy 設定 (v{_pol['ver']} thr {_pol['thr']:g} "
              f"{_pol['mode']}, gate={','.join(_pol['gates']) or '無'}) — worker の適用確認はノード欄"
              if _pol else "")),
            (u["veto"], "veto", "🛡", "gated-veto 稼働(推論で誤検出を打消)"),
            (u["reap_auth"], "reap", "🎯", "reap 権限あり: GT-positive recall が閾値以上と実測 → サーバが実際にこのタグの stale FP を削除する"),
            (u["promotable"], "promote", "⭐", "昇格候補: 現行タガーの recall は閾値未満だが、frozen 特徴の fresh linear probe が閾値に届く(head-fixable) → logit-patch を再学習すれば reap 権限に昇格できる"),
            (u["stage"] == "training", "training", "🔄", "reconciler が再学習を dispatch 済・登録待ち"),
            (u["cand"] is not None and not u["live"] and _pol is None,
             "candidate", "◔", "head 候補(未稼働)"),
            (u["quar"], "quar", "⚠", "quarantine(出力停止中)"),
        ]
        st_tokens = [tok for on, tok, _ic, _t in _STATES if on]
        # gate/宣言の異常はフィルタキーにもする (行属性 data-st が filter の入力)。
        if _pol is not None:
            _gts0 = _pol.get("gates") or []
            _dg0 = u.get("decl_gates")
            if not u.get("has_decl") and not _gts0:
                st_tokens.append("ungated")
            elif u.get("has_decl") and _dg0 is not None and sorted(_gts0) != _dg0:
                st_tokens.append("driftgate")
        state = "".join(
            f"<span class=sb data-st={tok} title='{e(tt)}'>{ic}</span>"
            for on, tok, ic, tt in _STATES if on) or "<span class=mut>⚪</span>"
        st_attr = " ".join(st_tokens)
        # No version-bound independent predictions are stored by this dashboard.
        # A past registration score or a corrected tag is not current accuracy.
        p_cell = "<span class=mut title='現行版の独立した予測と固定正解の比較結果は未登録。訂正履歴から精度は計算しない'>未測定</span>"
        # 増減は auto_tag(タガー)と manual(人間補正)を別カラムに分割
        # (user 2026-07-22:「autotag とマニュアルのカラムは分割しろ」)。
        aa, ar, ma, mr = u["auto_add"], u["auto_rm"], u["man_add"], u["man_rm"]

        def _num(v, cls):
            return f"<span class={cls}>{v}</span>" if v else "<span class=mut>—</span>"
        need = int(_min_pos) - pos
        # 自動処理 disposition(reconciler_disposition.json)。reconciler が実際に
        # どう判定したかを表示する — established 過検出タグは corpus 正例で自動
        # precision 再学習されるので「＋正例 あとN(=正例が要る/blocked)」は誤り。
        _d = u.get("disp") or {}
        _dd = _d.get("disposition")
        auto_badge = ""
        # **`reg_badge` を別に持つ。** この下の reconciler 判定は auto_badge に
        # `=` で代入するので、同じ変数に += で積むと **静かに上書きされて消える**
        # (2026-08-05 実測: 乖離・登録判定・再学習のバッジが 1 件も出なかった)。
        # 最後に連結する。
        reg_badge = ""
        # ── domain 宣言 vs 配備 gate の突合 (2026-09-03 incident の signature 検出) ──
        # 「宣言はあるのに配備が読んでいない」「宣言そのものが無いのに無条件配備」
        # は、起きたら即この列に赤で出る。緑 (一致) は小さく事実だけ出す。
        if _pol is not None:
            _gts = _pol.get("gates") or []
            _dg = u.get("decl_gates")
            if not u.get("has_decl") and not _gts:
                reg_badge += (
                    "<span class='sb perf-fp' data-st=ungated title='配備中 (policy) なのに gate 無し + domain 未宣言。"
                    "dark_labia_minora が校正域外の全 post に発火した 2026-09-03 incident と同じ形。"
                    "tagger_tag_rule に condition (always か in_*_domain) を evidence 付きで宣言すること "
                    "— 次の昇格は宣言が無いと BLOCK される (v1.175.0)'>⛔ 無gate・未宣言</span> ")
            elif not u.get("has_decl"):
                reg_badge += (
                    f"<span class=perf-warn title='配備 gate={','.join(_gts)} だが curated 宣言 (tagger_tag_rule) が無い。"
                    f"次の昇格は宣言が無いと BLOCK される (v1.175.0) — 現 gate を condition として宣言すること'>"
                    f"🚪{e(','.join(_gts))} (未宣言)</span> ")
            elif _dg is None:
                reg_badge += (
                    "<span class=perf-warn title='宣言条件が any/none を含み gate_tags で表現できない — "
                    "配備との突合不能。all_tags 条件へ書き直すか、この head の配備意図を確認'>⚠ 宣言比較不能</span> ")
            elif sorted(_gts) != _dg:
                reg_badge += (
                    f"<span class='sb perf-fp' data-st=driftgate title='宣言 (tagger_tag_rule: {e(','.join(_dg) or 'always')}) と"
                    f"配備 (policy gate_tags: {e(','.join(_gts) or '無')}) が食い違っている — 別帳簿 drift。"
                    f"昇格し直すか policy を宣言に合わせること'>⚠ 宣言≠配備</span> ")
            elif _gts:
                reg_badge += (
                    f"<span class=mut title='domain gate 一致: 宣言どおり {e(','.join(_gts))} が"
                    f"付いている画像でのみ発火'>🚪{e(','.join(_gts))}</span> ")
        # 配備可 (validated) なのに domain 未宣言 = 昇格がBLOCKされ続ける状態。
        # validated 表示だけ出すと「もうすぐ配備される」に見えて永久に待つ。
        _rg_pre = u.get("reg") or {}
        if _rg_pre.get("bucket") == "配備可" and not u.get("has_decl"):
            reg_badge += (
                "<span class=perf-warn title='測定は配備可 (validated) だが domain 未宣言のため"
                "昇格が毎サイクル BLOCK される (v1.175.0 fail-closed)。tagger_tag_rule に宣言を足せば"
                "次サイクルで自動昇格する'>🚧 宣言待ち(昇格BLOCK中)</span> ")
        # ── 登録判定 (4 クラス) ── **別 panel にしない** (user 2026-08-05
        # 「タグ状況カードに統合しろ」/ 既存の「特殊化するな」)。タグの状態は
        # 1 箇所で見えるほうがよいので、ここのバッジ列に合流させる。
        if coverage := candidate_coverage.get(u["tag"]):
            workers = coverage["workers"]
            checked = sum(worker["confirmed"] for worker in workers)
            label = "公開済み・推論確認済み" if coverage["complete"] else "公開済み・推論確認待ち"
            reg_badge += "<span class=mut>" + e(f"{label} {checked}/{len(workers)}") + "</span> "
        tag_learning = learning_by_tag.get(u["tag"])
        if tag_learning:
            reg_badge += "<span class=mut>" + e(tag_learning["label"]) + "</span> "
        _rg = u.get("reg") or {}
        if _rg:
            _b = _rg.get("bucket")
            # **`_num` という名前は使わない** — 同じスコープに _num(v, cls) 関数が
            # あり、文字列で潰すと後続の {_num(aa,'perf-ok')} が
            # "'str' object is not callable" で 500 になる (2026-08-05 実測)。
            _rg_num = ""
            if _rg.get("net") is not None:
                _rg_num = f" 差引{_rg['net']:,.0f}件"
                if _rg.get("lift") is not None:
                    _rg_num += f"/{_rg['lift']:.1f}x"
            elif _rg.get("gt_pos") is not None:
                _rg_num = f" GT正{_rg['gt_pos']}/負{_rg['gt_neg']}"
            _RG = {
                "配備可": ("auto-go", "✅", "絶対 floor (率 OR 規模) を満たし validated"),
                "掃除可": ("auto-go", "🧹", "差し引き改善するが率の floor 未達 — 2026-08-05 以降は配備可へ倒す"),
                "測定不能": ("mut", "⏳", "GT が足りず CV が組めない。**性能不足ではない** — 正例を足せば解ける"),
                "旧gate誤却下": ("mut", "⚠", "新形式を旧 gate が読めず落とした行 (2026-08-05 修正済・再測定で解消)"),
                "不要": ("mut", "🚫", "測った上で配備しない判断 (差し引き純減 or 改善が無いに等しい)"),
                "基準未達": ("mut", "📉", "測れていて性能が floor に届かない"),
            }
            _cls, _ico, _tip = _RG.get(_b, ("mut", "·", ""))
            reg_badge += (f"<span class={_cls} title='{e(_tip)}｜{e((_rg.get('reason') or '')[:200])}'>"
                           f"{_ico} {e(_b)}{e(_rg_num)}</span> ")
        if _rg.get("serving_qualification") is not None:
            from api.tagger_core.learning_evidence import serving_summary
            reg_badge += "<span class=mut>" + e(serving_summary(_rg["serving_qualification"])) + "</span> "
        # 再学習の往復。**回数だけ出しても意味が薄い**ので、最後に training へ
        # 入った時刻を添える — 「回っているのに進まない」と「そもそも回って
        # いない」を区別できるようにする。
        # 現タガーとの乖離。**基準 (現タガー) を必ず併記する** — 候補の AUC
        # だけ出すと「良い候補がある」で終わり、いま何が起きているかが見えない。
        if _rg.get("cur_auc") is not None or _rg.get("best_auc") is not None:
            _ca, _ba = _rg.get("cur_auc"), _rg.get("best_auc")
            if _ca is not None and _ba is not None:
                _gap = _ba - _ca
                _cls2 = "perf-fp" if _ca < 0.5 else ("perf-warn" if _gap >= 0.15 else "mut")
                _note = ("**現タガーは偶然以下 (逆相関: 付けるほど外す)**" if _ca < 0.5
                         else "現タガーと候補の判別力の差")
                reg_badge += (
                    f"<span class={_cls2} title='現タガー(A_deployed) AUC {_ca:.3f} / "
                    f"最良候補 {e(_rg.get("best_arm") or "")} AUC {_ba:.3f}。"
                    f"0.5 が偶然、下回れば逆相関。{_note}'>"
                    f"👥 乖離 {_ca:.2f}→{_ba:.2f} (+{_gap:.2f})</span> ")
            elif _ba is not None:
                _cs = _rg.get("cur_skip")
                if _cs and "no deployed row" in str(_cs):
                    _t2 = ("このタグにはまだ patch が無いので比較対象が存在しない"
                           "(新規候補として正常)。配備後は現行 patch との乖離が出る")
                    _l2 = f"👥 新規 (比較対象なし) · 候補 AUC {_ba:.2f}"
                else:
                    _t2 = ("測定に現タガーの腕が記録されていない。2026-08-05 以前の測定は "
                           "_arms_of が auc_in_gate_keep_vs_remove を拾えず基準側が落ちていた "
                           "— 再測定で入る")
                    _l2 = f"👥 基準未記録 · 候補 AUC {_ba:.2f}"
                reg_badge += f"<span class=mut title='{e(_t2)}'>{_l2}</span> "
        _rt = u.get("retrain") or {}
        if _rt.get("rounds"):
            _lt = _rt.get("last_train")
            _ago = ""
            if _lt is not None:
                _mins = (_now_utc - _lt).total_seconds() / 60 if _now_utc else None
                if _mins is not None:
                    _ago = (f"{_mins/60:.0f}h前" if _mins >= 120
                            else f"{_mins:.0f}分前" if _mins >= 1 else "たった今")
            reg_badge += (f"<span class=mut title='candidate⇄training の往復回数。"
                           f"GT が育つたび reconciler が再 dispatch する。最後に training へ入ったのは "
                           f"{e(_lt)}'>🔁 再学習{_rt['rounds']}回{(' · ' + _ago) if _ago else ''}</span> ")
        if _dd == "dispatch":
            _trig = _d.get("trigger", "") or ""
            _kind = ("recall+precision" if _trig.startswith("recall+precision")
                     else "precision" if _trig.startswith("precision")
                     else "recall" if _trig.startswith("recall") else "再")
            _cp = _d.get("corpus_pos")
            _cpt = f"、正例源=corpus {_cp}件" if _cp else ""
            auto_badge = (f"<span class=auto-go title='reconciler が {e(_trig)} を dispatch 済"
                          f"(gt_pos={_d.get('gt_pos')}/gt_neg={_d.get('gt_neg')}{_cpt})'>🔧 {_kind}再学習 dispatch済</span>")
        elif _dd == "in_flight":
            auto_badge = "<span class=auto-wait title='依頼後の段階を示す。学習プロセスの実行開始は未確認'>学習依頼済・実行未確認</span>"
        elif _dd == "hold" and _d.get("reason") == "no_growth" and _d.get("established"):
            _nn, _np = _d.get("need_neg", 0), _d.get("need_pos", 0)
            _parts = []
            if _nn:
                _parts.append(f"FP+{_nn}で精度再学習")
            if _np:
                _parts.append(f"正例+{_np}で recall 再学習")
            auto_badge = (f"<span class=auto-wait title='メインタガー組込済の過検出タグ。"
                          f"gt_neg={_d.get('gt_neg')}。補正が閾値分たまれば reconciler が自動再学習(手動プール不要)'>"
                          f"⏳ {' / '.join(_parts or ['補正待ち'])}</span>")
        elif _dd == "hold" and _d.get("reason") == "quarantined_output_discarded":
            # This is the reconciler's training restriction, not proof that every
            # inference path discards repaired-head output identically.
            auto_badge = ("<span class=auto-wait title='隔離の設定により自動学習の依頼を停止。"
                          "修復学習と配備の可否は別途確認が必要'>隔離中・自動学習停止</span>")
        if not progress["current"]:
            _freshness = {"missing": "未取得", "invalid": "読取失敗", "stale": "更新が古い"}
            _why = (f"自動処理停止: {progress['halt_reason'] or '理由未記録'}" if progress["paused"]
                    else f"自動学習の状態: {_freshness.get(progress['observation_state'], '未確認')}")
            auto_badge = f"<span class=auto-wait>{e(_why)}</span>"
        elif _dd and not auto_badge:
            auto_badge = f"<span class=auto-wait>{e(_progress.describe_decision(_d))}</span>"
        auto_badge = (auto_badge + (" " if auto_badge and reg_badge else "") + reg_badge)
        from api.tagger_core.dashboard_actions import task, task_html
        auto_badge = task_html(task(u, tag_learning, progress)) + "<details><summary>過去の候補・自動判定の記録</summary>" + auto_badge + "</details>"
        drive = (auto_badge + "<details><summary>画像で訂正する（任意）</summary>"
                 + f"<a href='/?q={_q(tag)}' target=_blank rel=noopener "
                   "title='通常の画像でタグを追加・削除・確認すると正解データに反映されます'>付与済み画像の誤タグを訂正</a>"
                 + f" · <a href='/?q={_q('-'+tag)}' target=_blank rel=noopener>未付与画像から不足を探す</a></details>")
        # 補正R: 検証プールを回した反復補正の推移(バーは古→新の補正率、右へ下がる=改善)。
        # 別 panel でなく1列に畳む(user 2026-07-22:「反復補正の推移も統合。特殊化するな」)。
        rounds = u.get("rounds")
        if rounds:
            n_r, first_pct, latest_pct = len(rounds), rounds[0]["pct"], rounds[-1]["pct"]
            _bar_cells = []
            for i, rr in enumerate(rounds):
                rp = rr["pct"]
                rcolor = "#f85149" if rp >= 70 else "#e0a52e" if rp >= 30 else "#3fb950"
                rtitle = f"#{i + 1} pool#{rr['source_pool_id']} {rr['added_at']}: 補正率{rp}%"
                _bar_cells.append(
                    f"<span class=rbar title='{e(rtitle)}' style='height:{max(15, rp)}%;background:{rcolor}'></span>")
            bars = "".join(_bar_cells)
            trend = ("perf-ok", "↓") if latest_pct < first_pct else (
                ("perf-bad", "↑") if latest_pct > first_pct else ("mut", "→"))
            rounds_cell = (
                f"<span class=mono>{n_r}R</span>"
                f"<span class=rbar-row style='height:16px;width:auto;display:inline-flex;vertical-align:middle;margin:0 4px'>{bars}</span>"
                f"<span class={trend[0]} title='初回{first_pct}%→直近{latest_pct}%'>{trend[1]}{latest_pct}%</span>")
            r_sort, rp_sort = n_r, latest_pct
        else:
            rounds_cell, r_sort, rp_sort = "<span class=mut>—</span>", 0, -1
        # reap 権限: サーバの証拠ゲート(reap_authorized.json)の状態を反映。
        # GT-positive recall が実測され閾値以上なら 🎯 = 実際に reap 稼働、未達なら
        # 保留(recall < 閾値)、reap ドメインだが未測定なら未測定、対象外なら —。
        rr_val = u.get("reap_recall")
        if rr_val is not None:
            if u["reap_auth"]:
                reap_cell = (f"<span class=perf-ok>🎯</span> "
                             + _pbar(rr_val * 100, f'GT-positive recall {rr_val:.0%} ≥ {reap_min:.0%} → reap 権限あり(サーバが stale FP を実削除)'))
            elif u["promotable"]:
                _auc = u.get("auc")
                _at = f" AUC{_auc:.2f}" if _auc is not None else ""
                reap_cell = (f"<span class=perf-warn>{rr_val:.0%}</span> "
                             f"<span class=promote title='現行 recall は閾値未満だが frozen 特徴の fresh linear probe が分離する(head-fixable)→ logit-patch 再学習で reap 権限に昇格できる'>⭐昇格候補{_at}</span>")
            else:
                _auc = u.get("auc")
                _at = f" · AUC{_auc:.2f}" if _auc is not None else ""
                reap_cell = (f"<span class=perf-warn>{rr_val:.0%}</span>"
                             f"<span class=mut title='recall 未達。fresh probe も分離しない=特徴が足りない(base 改善待ち=backbone-bound)'> &lt;{reap_min:.0%} 保留{_at}</span>")
            reap_sort = rr_val
        elif u["reap_dom"]:
            reap_cell, reap_sort = "<span class=mut title='reap ドメイン(patched∪veto)だが recall 未測定'>未測定</span>", -0.5
        else:
            reap_cell, reap_sort = "<span class=mut>—</span>", -1.0
        role = _ROLE.get(u.get("type"), u.get("type") or "")
        visual_group = visual.group(u, tag_learning, progress)
        visual_label = dict((k, label) for k, label, _, _ in visual.GROUPS)[visual_group]
        measured = visual.metrics(_rg.get("serving_qualification"))
        precision_sort = measured["precision"] if measured and measured["precision"] is not None else ""
        candidate_chart = visual.metric_cell(measured, _rg)
        p_cell = visual.comparison_chart(_rg.get("serving_qualification")) + "<details><summary>候補の評価根拠</summary>" + candidate_chart + "</details>" + "<details class=model-detail><summary>配備モデルの保存済み測定</summary>" + p_cell + "</details>"
        state = (visual.write_state(u) + f"<span class=group-label>{e(visual_label)}</span><span class=serving-label>配備設定: {'あり（実行確認はノード欄）' if visual.serving(u) else 'なし'}</span>"
                 + "<details><summary>構成</summary>" + state + "</details>")
        return (
            f"<tr data-group='{visual_group}' data-serving='{"yes" if visual.serving(u) else "no"}' data-precision='{precision_sort}' data-tag='{e(tag)}' data-st='{st_attr}' data-corrections='{corrections}' data-violations='{violations}' data-pos='{pos}' "
            f"data-aadd='{aa}' data-arm='{ar}' data-madd='{ma}' data-mrm='{mr}' "
            f"data-rounds='{r_sort}' data-roundpct='{rp_sort}' data-reap='{reap_sort:.4f}'>"
            f"<td data-label='タグ'><a href='/?q={_q(tag)}' target=_blank rel='noopener noreferrer'><b>{e(tag)}</b></a>"
            f"{('<br><span class=mut style=font-size:10px>' + e(role) + '</span>') if role else ''}</td>"
            f"<td data-label='次工程・配備設定' title='🟢live(release焼込) 🧩policy設定(content-head) 🛡veto設定 🎯reap権限 ⭐昇格候補 🔄学習依頼済 ◔候補 ⚠quar'>{state}</td>"
            f"<td>{reap_cell}</td>"
            f"<td data-label=評価>{p_cell}</td>"
            f"<td class=mono data-label=保存済み訂正>{corrections}</td>"
            f"<td class=mono data-label=現在タグとの不一致 title='人の負例なのにタグあり / 人の正例なのにタグなし'>{violations} "
            f"<span class=mut>({(u['perf'] or {}).get('unwanted_present', 0)} / {(u['perf'] or {}).get('wanted_missing', 0)})</span></td>"
            f"<td class=mono title='全GTと未同期pinを統合した正例 / 負例。専用プールの有無を問わない'>{pos} / {neg}</td>"
            f"<td class=mono>{_num(aa, 'perf-ok')}</td>"
            f"<td class=mono>{_num(ar, 'perf-fp')}</td>"
            f"<td class=mono>{_num(ma, 'perf-ok')}</td>"
            f"<td class=mono>{_num(mr, 'perf-fp')}</td>"
            f"<td>{rounds_cell}</td>"
            f"<td data-label=次にすること>{drive}</td></tr>")

    action_overview = visual.overview(uni, learning_progress, rel["id"] if rel else None, progress,
                                      learning_by_tag=learning_by_tag)
    from api.tagger_core.experiment_status import read as read_experiment_progress
    from api.tagger_core.dashboard_experiment import render as render_experiment_progress
    action_overview += render_experiment_progress(read_experiment_progress())
    unified_html = "".join(_uni_row(u) for u in uni)

    _tp_rows = []
    for t in throughput:
        host_txt = e(t["host"]) if t["host"] is not None else "<span class=mut>(host未報告)</span>"
        rel_txt = e(t["release_id"]) if t["release_id"] is not None else "—"
        _tp_rows.append(
            f"<tr><td class=mono>{e(t['actor_id'][:48])}</td><td>{host_txt}</td><td>{rel_txt}</td>"
            f"<td class=mono>{e(t['n24h'])}</td><td class=mono>{e(t['n7d'])}</td>"
            f"<td class=mono>+{e(t['added_7d'])}</td><td class=mono>-{e(t['removed_7d'])}</td>"
            f"<td>{e(t['last_seen'])}</td></tr>"
        )
    throughput_html = "".join(_tp_rows) if _tp_rows else (
        "<tr><td colspan=8 class=mut>直近7日の auto_tag 監査行なし</td></tr>"
    )
    # tagger_config: 各行に個別 <form> でPOST(このダッシュボードはiframe
    # 埋め込みで認証headerを持てないため、GET側と同じく無認証 -- 書込先は
    # tagger_governed_set_config 経由のみで既知keyのみ更新可、値は数値検証
    # 済み。ブラスト半径は小さい(ML閾値のみ、post/tagデータ本体には触れない)。
    _cfg_rows = []
    for c in cfg_rows:
        _cfg_rows.append(
            "<tr><td class=mono><b>" + e(c["key"]) + "</b></td>"
            "<td class=mut>" + e(c["description"] or "") + "</td>"
            "<td>"
            f"<form method=post action='/api/tagger-config' style='display:flex;gap:4px'>"
            f"<input type=hidden name=key value='{e(c['key'])}'>"
            f"<input type=number step=any name=value value='{e(c['value'])}' "
            "style='width:90px'>"
            "<button type=submit>更新</button></form></td>"
            "<td class=mut>" + e(c["updated_at"]) + "<br>" + e(c["updated_by"] or "") + "</td></tr>"
        )
    cfg_html = "".join(_cfg_rows) if _cfg_rows else (
        "<tr><td colspan=4 class=mut>tagger_config 未マイグレーション(0098 未適用)</td></tr>"
    )

    # ── character 判定の稼働状況 (2026-09-04, user「dashboardから状況はわかる？」) ──
    # pixai char 配線 (v1.179.0) の進捗は sweep % だけでは見えない: 見たいのは
    # 「キャラ付き post が増えているか」「primary 以外の host も付けているか」(30日間
    # primary のみだったのが incident)。coverage は実測 ~1.3s — オンデマンド描画なので許容。
    # 2026-09-05: これら 3 本は `api/services/tagger_situation.character_coverage()`
    # に移した。category_id を 40 と直書きしていた箇所が 3 つあり、採番が変われば
    # 例外ではなく **0 件** を返す — 「キャラタグが 1 つも無い」は実際に起こりうる
    # 文なので、そのまま信じられてしまう。カテゴリは名前で引く。
    _char = await _situation.character_coverage(db)
    _char_cov = {"covered": _char["covered"], "total": _char["total_posts"]}
    _char_adds = [{"host": h, "n": n} for h, n in _char["applied_24h"].items()]
    _pixai_hosts = _char["pixai_hosts"]
    _cov_pct = (100.0 * _char_cov["covered"] / _char_cov["total"]) if _char_cov["total"] else 0.0
    _adds_txt = " · ".join(f"{r['host']} {r['n']}" for r in _char_adds) or "0 (24h 付与なし)"
    _pix_txt = " ".join(("🧩" if r["has_pixai"] else "⚠") + e(r["host"]) for r in _pixai_hosts) \
        or "(6h 以内の completion なし)"
    char_status_html = (
        "<h1 style='font-size:13px;margin:var(--control-padding-x) 0 var(--control-gap)'>character 判定 "
        "<span class=mut>(pixai v0.9 全 worker 配線 2026-09-04 — カバレッジと「primary 以外も付けているか」)</span></h1>"
        "<div class=grid>"
        f"<div class=kv title='character タグを1つ以上持つ post / 全 post。sweep の全アーカイブ再タグ "
        f"(post_id 昇順) が進むほど上がる — 最近の upload (高 id) ほど後に付く'>"
        f"<b>{_cov_pct:.1f}%</b>キャラ付き post ({_char_cov['covered']:,}/{_char_cov['total']:,})</div>"
        f"<div class=kv title='直近24h に character タグを付与した completion の host 内訳。"
        f"primary 以外がゼロのままなら配線が死んでいる (2026-09 以前は 30 日間 primary のみ = incident)'>"
        f"<b>{sum(int(r['n']) for r in _char_adds)}</b>キャラ付与/24h ({e(_adds_txt)})</div>"
        f"<div class=kv title='直近6h の completion actor に pixai sha が刻まれている host。"
        f"⚠ = pixai 無しで走っている (fail-soft でキャラを付けない)'>"
        f"<b>{sum(1 for r in _pixai_hosts if r['has_pixai'])}/{len(_pixai_hosts)}</b>pixai 稼働 host ({_pix_txt})</div>"
        "</div>")

    # ── 自動化の稼働状況パネル(user 2026-07-22「全部はい」)──
    _sw_proc, _sw_total = _sweep_cov["processed"], _sweep_cov["total_posts"]
    _sw_pending = _queue["queued"]
    _sw_pct = _sweep_cov["pct"]
    _sw_remain = _sweep_cov["remaining"]
    _reap_tag_html = "".join(
        f"<div class=kv><b>{int(r['cur']):,}</b>{e(r['tag'])} "
        f"<span class=perf-fp>−{int(r['reaped_24h'])}/24h</span></div>"
        for r in _reap_tag_rows) or "<div class=mut>reap 権限タグなし</div>"
    # ライフサイクルの健康状態 (owner 裁定 2026-11-23: 警告は kura の画面に出す)。
    # 集計の失敗で dashboard 全体を落とさない — 失敗はそのまま表示する。
    try:
        from api.services import lifecycle_health
        async with db.begin_nested():
            lifecycle_html = lifecycle_health.render(await lifecycle_health.snapshot(db), e)
    except Exception as exc:  # noqa: BLE001 - shown, never hidden
        lifecycle_html = (f"<div class=panel data-tagger-category=overview><h1>ライフサイクルの健康状態</h1>"
                          f"<p class=perf-bad>集計に失敗しました: {e(type(exc).__name__)}</p></div>")
    automation_html = (
        "<div class=panel data-tagger-category=automation><h1>自動化の稼働状況 "
        "<span class=mut>(sweep 継続稼働 + 証拠ゲート reap · 7日窓に埋もれない短期指標)</span></h1>"
        "<div class=grid>"
        f"<div class=kv><b>{_sw_pct:.1f}%</b>backlog 再タグ済(rel {_rel_id})</div>"
        f"<div class=kv><b>{_sw_remain:,}</b>残 backlog(sweep 対象)</div>"
        f"<div class=kv><b>{_sw_pending:,}</b>今 pending(ワーカー処理待ち)</div>"
        f"<div class=kv><b>{int(_act['c1h'])}</b>完了/1h · <span class=perf-fp>reap {int(_act['r1h'])}</span></div>"
        f"<div class=kv><b>{int(_act['c24h'])}</b>完了/24h · <span class=perf-fp>reap {int(_act['r24h'])}</span></div>"
        "</div>"
        f"<div style='margin-top:8px'><span class=pbar style='width:220px'>"
        f"<span class=pbar-fill style='width:{min(100.0, _sw_pct):.1f}%;background:#3fb950'></span></span> "
        f"<span class=mono>{_sw_proc:,}/{_sw_total:,} 処理済</span></div>"
        "<h1 style='font-size:13px;margin:var(--control-padding-x) 0 var(--control-gap)'>reap 権限タグ — 現在数と直近24h の打ち消し(減っていくのが進捗)</h1>"
        f"<div class=grid>{_reap_tag_html}</div>"
        f"<div class=mut style='margin-top:8px'>sweep(kura-tagger-sweep.timer, 5000件/15分)が backlog を漸進再タグ → "
        f"veto が FP を抑制 + reap(recall≥{int(reap_min * 100)}% の {'・'.join(sorted(reap_authorized)) or '(なし)'})が "
        "stale FP を削除。<b>再タグは既存タグの再生成なので「自動増(付与)」はほぼ増えない</b> — "
        "稼働の実信号は<b>完了数</b>と<b>reap(自動減)</b>。上の reap タグ現在数が24hで減るのが「打ち消し続ける」の進捗。</div>"
        f"{char_status_html}"
        "</div>")

    # ── held (Phase 0 §held, plan kura-tagger-self-improvement-loop): reconciler が
    # 頭打ち(prevalence_ceiling 等)と判定して status='held' に落とした候補。candidate
    # 一覧(status='candidate')からは外れるので、消えたように見せず「なぜ held か」の
    # 根拠(hold_reason_class + hold_evidence の最新測定)をここで人にも LLM にも見せる。
    # tagger_head_candidate は tag×pool で複数行 -> tag 単位に集約(DISTINCT ON、最新 hold 行を代表)。
    # 同一 tag の held 行は reconciler が同一 evidence で一括 hold するので代表1行で足りる。
    held_rows = (await db.execute(text(
        "SELECT DISTINCT ON (hc.tag) hc.tag, hc.hold_reason_class AS rc, "
        "hc.hold_evidence->'history'->-1->>'rejected_reason' AS rej, "
        "hc.hold_evidence->'history'->-1->'metrics'->>'P_deploy' AS pdep, "
        "(hc.hold_evidence->'history'->-1->>'gt_pos') AS ehp, "
        "(hc.hold_evidence->'history'->-1->>'gt_neg') AS ehn, "
        "COALESCE(jsonb_array_length(hc.hold_evidence->'history'),0) AS nhist, "
        "(SELECT count(*) FILTER (WHERE g.verdict='positive') FROM tagger_gt_label g WHERE g.tag=hc.tag) AS gp_now, "
        "(SELECT count(*) FILTER (WHERE g.verdict='negative') FROM tagger_gt_label g WHERE g.tag=hc.tag) AS gn_now "
        "FROM tagger_head_candidate hc WHERE hc.status='held' "
        "ORDER BY hc.tag, hc.added_at DESC"
    ))).mappings().all()
    _rc_label = {
        "prevalence_ceiling": "頭打ち(prevalence ceiling)",
        "label_definition_conflict": "ラベル定義の矛盾",
        "inert_serving_path": "serving 到達せず",
        "vocab_absent_routing": "vocab 欠落",
        "insufficient_gt": "GT 不足",
        "migration_quarantine": "移行 quarantine",
    }
    # Auto-release is per-CLASS, not universal (api/tagger_core/transitions.py
    # GT_GROWTH_RELEASABLE, plan rev2 F3). Read the policy from that one constant
    # instead of restating it here: this panel used to tell every held row "GT が
    # 伸びれば自動 release", which is false for the INV-D classes -- a
    # vocab_absent_routing hold (cum_in_cup) waits for a human no matter how much
    # GT arrives, and showing "+GT" next to it promised a release that never comes.
    from api.tagger_core.transitions import GT_GROWTH_RELEASABLE
    if held_rows:
        _hh = []
        for h in held_rows:
            _ehp, _ehn = int(h["ehp"] or 0), int(h["ehn"] or 0)
            _auto = h["rc"] in GT_GROWTH_RELEASABLE
            _grow = (h["gp_now"] > _ehp or h["gn_now"] > _ehn) and _auto
            _rc_title = ("reconciler が自動 hold。GT が hold 時を超えたら自動 release" if _auto
                         else "reconciler が自動 hold。このクラスは GT では解けないため自動 release されない(人手 release/dismiss 待ち)")
            _hh.append(
                f"<tr><td class=mono>{e(h['tag'])}</td>"
                f"<td><span class=badge-training title='{_rc_title}'>{e(_rc_label.get(h['rc'], h['rc'] or '—'))}"
                f"{'' if _auto else ' <span class=mut>(人手)</span>'}</span></td>"
                f"<td class=mono>{e(h['pdep'] or '—')}</td>"
                f"<td class=mono>{_ehp}/{_ehn} → {h['gp_now']}/{h['gn_now']}"
                f"{' <span class=auto-go>+GT</span>' if _grow else ''}</td>"
                f"<td>{e(h['nhist'])}</td>"
                f"<td class=mut style='max-width:520px;white-space:normal'>{e((h['rej'] or '')[:180])}</td></tr>")
        held_html = (
            "<div class=panel data-tagger-category=held><h1>頭打ち held <span class=mut>("
            f"{len(held_rows)} タグ · reconciler が自動 dismiss した候補。GT が伸びれば自動 release)</span></h1>"
            "<div class=wrap><table><thead><tr><th>tag<th>理由(hold_reason_class)<th title='corpus prevalence 補正後の deploy 精度。floor 未達で held'>P_deploy"
            "<th title='hold 時 GT(pos/neg) → 現在 GT。現在が上回れば +GT で自動 release 対象'>GT hold→現在<th title='hold_evidence.history の測定回数(append-only)'>測定<th>reject 根拠</tr></thead>"
            f"<tbody>{''.join(_hh)}</tbody></table></div>"
            "<div class=mut style='margin-top:6px'>Phase 0 §held: floor が reject した候補は approved に至らず、旧来は stale-training GC で "
            "candidate⇄training を永久 respawn していた。reconciler が<b>却下された候補を自動 held</b> にして respawn を止める。"
            "<b>自動 release は GT で解けるクラスのみ</b>(prevalence_ceiling / insufficient_gt — GT が hold 時を超えたら再評価)。"
            "vocab_absent_routing・inert_serving_path 等 (人手) 付きのクラスは GT が増えても自動復帰せず、人手 release/dismiss 待ち "
            "— routing や serving 経路の欠落はラベルでは解けないため。</div></div>")
    else:
        held_html = ("<div class=panel data-tagger-category=held><h1>頭打ち held <span class=mut>(0 — 現在 held の候補なし)</span></h1>"
                     "<div class=mut>reconciler が prevalence ceiling 等で自動 held にした候補がhere に出る(Phase 0 §held)。"
                     "旧来は candidate⇄training を永久 respawn していたものを止める機構。</div></div>")


    html = f"""<!doctype html><html lang=ja><head><meta charset=utf-8>
<meta name=viewport content='width=device-width,initial-scale=1'>
<title>kura tagger — メイン & head 状況</title>
<link rel=stylesheet href="/control-tokens.css"><style>
{visual.CSS}
:root{{color-scheme:light dark}}body{{font:var(--control-font)/1.5 system-ui,sans-serif;overflow-wrap:anywhere;margin:0;padding:var(--control-padding-x);
background:#0d1117;color:#e6edf3}}@media(prefers-color-scheme:light){{body{{background:#fff;color:#1a1a1a}}}}
h1{{font-size:14px;margin:0 0 4px}}.mut{{opacity:.6;font-size:12px}}.mono{{font-family:ui-monospace,monospace}}
.panel{{background:#161b22;border:1px solid #30363d;border-radius:var(--control-radius);padding:var(--control-padding-x);margin:var(--control-gap) 0}}
@media(prefers-color-scheme:light){{.panel{{background:#f6f8fa;border-color:#d0d7de}}}}
.grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(130px,1fr));gap:var(--control-gap)}}
.kv{{font-size:12px}}.kv b{{display:block;font-size:18px;font-weight:700}}
table{{width:100%;border-collapse:collapse;font-size:var(--control-font);overflow-wrap:normal}}th{{white-space:nowrap}}th,td{{text-align:left;padding:2px var(--control-padding-x);border-bottom:1px solid #30363d}}
@media(prefers-color-scheme:light){{th,td{{border-color:#eaecef}}}}th{{font-size:11px;text-transform:uppercase;opacity:.6}}
tr.off{{opacity:.5}}tr.quar{{background:rgba(210,60,60,.12)}}
.st[data-s=approved]{{color:#3fb950}}.st[data-s=rejected]{{color:#f85149}}
.badge{{display:inline-block;padding:0 var(--control-gap);border-radius:var(--control-radius);font-size:var(--control-count-font);background:#238636;color:#fff}}
.badge-training{{display:inline-block;padding:0 var(--control-gap);border-radius:var(--control-radius);font-size:var(--control-count-font);background:#9e6a03;color:#fff}}
.badge-veto{{display:inline-block;padding:0 var(--control-gap);border-radius:var(--control-radius);font-size:var(--control-count-font);background:#1f6feb;color:#fff}}
.sb{{display:inline-block;margin-right:3px}}
.chip{{display:inline-flex;align-items:center;box-sizing:border-box;min-height:var(--control-compact-height);cursor:pointer;padding:0 var(--control-padding-x);border:1px solid #30363d;border-radius:var(--control-radius);font-size:var(--control-font);user-select:none;white-space:nowrap}}
.chip.on{{background:#1f6feb;color:#fff;border-color:#1f6feb}}
@media(prefers-color-scheme:light){{.chip{{border-color:#d0d7de}}}}
.perf-bad{{color:#f85149;font-weight:700}}.perf-warn{{color:#d29922;font-weight:600}}.perf-ok{{color:#3fb950}}.perf-fp{{color:#f85149;font-weight:600}}
.promote{{color:#000;background:#ffd33d;font-weight:700;padding:0 5px;border-radius:var(--control-radius)}}
.auto-go{{display:inline-block;color:#fff;background:#1f6feb;font-weight:600;font-size:11px;padding:1px 6px;border-radius:var(--control-radius);white-space:nowrap}}
.auto-wait{{display:inline-block;color:#8b949e;border:1px solid #30363d;font-size:11px;padding:1px 6px;border-radius:var(--control-radius);white-space:nowrap}}
.pbar{{display:inline-block;width:70px;height:8px;background:#30363d;border-radius:4px;overflow:hidden;vertical-align:middle;margin-right:6px}}
.pbar-fill{{display:block;height:100%}}
@media(prefers-color-scheme:light){{.pbar{{background:#d0d7de}}}}
.rbar-row{{display:flex;align-items:flex-end;gap:2px;height:28px;width:140px}}
.rbar{{display:inline-block;width:10px;border-radius:2px 2px 0 0;min-height:3px}}
td.cand{{border-left:2px solid #30363d;padding-left:10px;white-space:nowrap}}
@media(prefers-color-scheme:light){{td.cand{{border-color:#d0d7de}}}}
table a{{color:#58a6ff;text-decoration:none}}table a:hover{{text-decoration:underline}}
@media(prefers-color-scheme:light){{table a{{color:#0969da}}}}
.wrap{{overflow-x:auto}}
button,input:not([type=checkbox]):not([type=hidden]),select{{box-sizing:border-box;height:var(--control-height);padding:0 var(--control-padding-x);border:1px solid #30363d;border-radius:var(--control-radius);font:inherit;background:inherit;color:inherit}}
button{{cursor:pointer;white-space:nowrap}}button:hover{{border-color:#58a6ff}}
.filter-controls{{display:flex;gap:var(--control-gap);align-items:center;flex-wrap:wrap;margin-bottom:var(--control-gap)}}
@media(max-width:600px),(pointer:coarse){{input:not([type=checkbox]):not([type=hidden]),select{{font-size:16px}}}}
[hidden]{{display:none!important}}
body.embedded{{background:var(--embed-background);color:var(--embed-text);padding:16px 24px}}
.embedded .panel{{background:var(--embed-surface);border-color:var(--embed-line);padding:16px;margin:0 0 12px}}
.embedded th,.embedded td{{border-color:var(--embed-line)}}
.embedded table a{{color:var(--embed-accent)}}
.embedded button,.embedded input,.embedded select{{border-color:var(--embed-line)}}
@media(max-width:600px){{body.embedded{{padding:16px}}}}
</style></head><body>
<div data-tagger-chrome><h1>tagger</h1>
<div class=mut>rendered {e(datetime.now().isoformat(timespec='seconds'))} — 毎アクセスで DB + patches/*.json を直読</div></div>

{lifecycle_html}

{action_overview}
<details class=panel data-tagger-category=overview><summary>配備構成の内訳と処理件数</summary><p class=mut>構成ごとの数は重複を含みます。ノードでの実行成功・モデル精度を表す数ではありません。</p>
<div class=grid>
<div class=kv><b>{e(rel['base_model_repo'].split('/')[-1]) if rel else '—'}</b>base model</div>
<div class=kv><b>rel {e(rel['id']) if rel else '—'}</b>選択 release · {e(rel['created_at']) if rel else ''}</div>
<div class=kv><b>{e(rel['n_out']) if rel else '—'}</b>出力タグ (stock {e(rel['n_stock']) if rel else '—'})</div>
<div class=kv><b>{n_live}</b>releaseに組み込んだ補正</div>
<div class=kv title='content-head policy 配備 (release とは別の配備台帳・worker の適用確認はノード欄)。「未宣言無gate」= gate 無し配備なのに tagger_tag_rule の domain 宣言も無い head — 校正域外で発火し得る 2026-09-03 incident の形。0 が健全'><b>{len(policy_by_tag)}</b>別途配備を設定した補正 (未宣言無gate {sum(1 for _t, _p in policy_by_tag.items() if not _p["gates"] and _t not in declared_by_tag)})</div>
<div class=kv><b>{e(queue.get('done','0'))}</b>done</div>
<div class=kv><b>{e(queue.get('pending','0'))}</b>pending</div>
<div class=kv><b>{e(queue.get('failed','0'))}</b>failed</div>
</div>
{node_panel}
</details>

{automation_html}

{held_html}

<div class=panel data-tagger-category=tags><h1>タグ状況 <span class=mut>({len(uni)} タグ · 統合ビュー — 列見出しでソート / 絞込)</span></h1>
<p class=snapshot-note>表示生成: {e(datetime.now().astimezone().isoformat(timespec="seconds"))}。候補・タグ集計は順次取得した記録で、同時刻のDB snapshotではありません。最新にするには上部の「再読み込み」。実験観測とノードは30秒ごとに更新します。</p>
<div class=filter-controls>
<label for=tf>タグ名</label><input id=tf placeholder='タグ絞込…' oninput='ftag()' style='width:180px'>
<details><summary>構成でさらに絞る</summary><span class=mut>構成:</span>
<button type=button class=chip data-st=live onclick='tst(this)' title='稼働 release に焼込済'>🟢 live</button>
<button type=button class=chip data-st=policy onclick='tst(this)' title='content-head policy 設定 (release とは別の配備台帳)'>🧩 policy設定</button>
<button type=button class=chip data-st=ungated onclick='tst(this)' title='配備中なのに gate 無し + domain 未宣言 (2026-09-03 incident の形)'>⛔ 無gate</button>
<button type=button class=chip data-st=driftgate onclick='tst(this)' title='宣言 (tagger_tag_rule) と配備 (policy gate_tags) の食い違い'>⚠ 宣言≠配備</button>
<button type=button class=chip data-st=veto onclick='tst(this)' title='gated-veto 稼働'>🛡 veto</button>
<button type=button class=chip data-st=reap onclick='tst(this)' title='reap 権限あり(recall 実測が閾値以上、サーバが実削除)'>🎯 reap権限</button>
<button type=button class=chip data-st=promote onclick='tst(this)' title='昇格候補: recall 未達だが head-fixable(fresh probe が閾値到達)→ 再学習で reap 権限に昇格できる'>⭐ 昇格候補</button>
<button type=button class=chip data-st=training onclick='tst(this)' title='再学習 dispatch 済'>🔄 学習依頼済</button>
<button type=button class=chip data-st=candidate onclick='tst(this)' title='head 候補(未live)'>◔ 候補</button>
<button type=button class=chip data-st=quar onclick='tst(this)' title='quarantine'>⚠ quar</button>
</details></div>
{visual.controls()}
<div class=wrap><table id=tt>
<thead><tr><th><button type=button onclick="stag('tag')">tag ⇅</button><th title='🟢live 🛡veto設定 🎯reap権限 ⭐昇格候補 🔄学習依頼済 ◔候補 ⚠quar'>状態<th title='reap 権限: GT-positive recall 実測が閾値以上なら 🎯(サーバが stale FP を実削除)、未達は保留、reap ドメインだが未測定は未測定。証拠ゲート reap_authorized.json を反映'><button type=button onclick="stag('reap')">reap権限 ⇅</button><th title='版を固定したモデル予測と独立した正解の比較。未登録なら未測定'>候補の固定評価<th title='保存中の人手負例（post/tagの件数）。訂正済みを含み、モデルの現在の誤検出数ではない'><button type=button onclick="stag('corrections')">訂正記録 ⇅</button><th title='人の正解と現在タグの不一致。内訳は不要なタグあり / 必要なタグなし。モデル起因とは限らない'><button type=button onclick="stag('violations')">未解決の不一致 ⇅</button><th title='全GTと未同期pinの正例 / 負例。人のpin優先で重複なし'><button type=button onclick="stag('pos')">GT 正 / 負 ⇅</button><th title='直近7日 タガー(auto_tag)が付与した数'><button type=button onclick="stag('aadd')">自動増 ⇅</button><th title='直近7日 タガー(auto_tag/veto)が削除した数'><button type=button onclick="stag('arm')">自動減 ⇅</button><th title='直近7日 人間（一括操作を含む）が付与した数'><button type=button onclick="stag('madd')">手動増 ⇅</button><th title='直近7日 人間（一括操作を含む）が削除した補正数=タガーが間違えた実信号'><button type=button onclick="stag('mrm')">手動減 ⇅</button><th title='旧検証プールの過去の反復補正ラウンド数と補正率推移(左=初回,右=最新。右へ下がる=改善)'><button type=button onclick="stag('rounds')">旧補正履歴 ⇅</button><th>次にすること</tr></thead>
<tbody id=tb>{unified_html}</tbody></table></div>
<details><summary>指標・構成の詳しい説明</summary><div class=mut style='margin-top:6px'>状態: 🟢live(稼働release に焼込) · 🛡veto設定(推論で誤検出を打消) · 🎯reap権限 · 🔄学習依頼済 · ◔候補 · ⚠quarantine。<b>reap権限</b>=証拠ゲート(reap_authorized.json)の反映: worker は reap ドメイン(patched∪veto)を提案するが、サーバは <b>GT-positive recall が {int(reap_min*100)}% 以上と実測されたタグだけ</b>を実際に reap(stale FP を削除)。未達タグは保留(recall が低い=タガーが TP を見逃す→非再現が FP でなく TP の巻き込みになる=削除しない)。recall が上がれば自動評価器 reap_authorization_eval.py が再測定して権限拡大。<b>⭐昇格候補</b>=recall 未達だが frozen 特徴の fresh linear probe が閾値に届く(head-fixable)タグ — logit-patch を再学習すれば reap 権限に昇格できる(強調表示)。逆に fresh probe も届かないタグは backbone-bound で base 改善待ち。現状: reap 稼働={'・'.join(sorted(reap_authorized)) or '(なし)'}、昇格候補={'・'.join(sorted(reap_promotable)) or '(評価中)'}。<b>モデル精度</b>=現行版の予測と独立した固定正解の比較。記録がないときは未測定。<b>訂正記録</b>=保存中の人手負例数（同一画像・タグは1件、訂正済みも含む）。削除イベントの累計や現行モデルの誤検出数ではない。<b>未解決の不一致</b>=人が不要としたタグが残る数＋人が必要としたタグがない数。タグ編集・同期などの影響も含み、モデル精度ではない。<b>GT 正 / 負</b>=通常の訂正を含む全GTと未同期pinの件数（pin優先）。旧検証プールに限定しない。既定は不一致数、次に訂正記録数の降順。<b>自動増/自動減</b>=タガー(auto_tag)の付与/削除、<b>手動増/手動減</b>=人間（一括操作を含む）の付与/削除(直近7日, post_tag_audit)。<b>手動減</b>こそ人間の補正削除の実信号(自動増が高く手動減も高い=タガーが付け続け人間が消し続ける=veto 候補)。<b>補正R</b>=検証プールを反復した補正ラウンド、バーは補正率(緑≦30/黄30-70/赤≥70%)を古→新、右へ下がれば改善。<b>次にすることの詳細</b>=reconciler の自動処理 disposition(reconciler_disposition.json、tick 毎)を反映: <span class=auto-go>🔧 …再学習 dispatch済</span>=学習を投入済、<span class=auto-go>学習依頼済・実行未確認</span>=依頼後の段階（実行開始の証拠ではない）、<span class=auto-wait>⏳ FP+N で精度再学習</span>=established 過検出タグ(メインタガー組込済)で補正があと N 件たまれば自動で precision 再学習が発火(手動プール不要、正例源は corpus 全体)。<b>＋正例 あとN</b> はメインタガー未組込の候補=確認済みの正例が不足しているタグにだけ出る。通常の画像でタグを訂正し、正しいタグは固定して確認する。専用の検証プール運用は廃止済み。min_gt_negative={disp_min_neg if disp_min_neg is not None else int(_min_pos)}。SoT: tagger_patch/tagger_release/post_tag_audit/reconciler_disposition.json 直読{(' · disp ' + disp_generated_at[:19]) if disp_generated_at else ''}。</div></details>
</div>
<script>{visual.JS}</script>

<div class=panel data-tagger-category=activity><h1>タガー稼働量 <span class=mut>(post_tag_audit 直読 · actor 単位 · 直近7日で1件以上あるもののみ上位30)</span></h1>
<div class=wrap><table>
<tr><th>actor<th>host<th>release<th>24h<th>7d<th>付与(7d)<th>削除(7d)<th>last seen</tr>
{throughput_html}</table></div>
<div class=mut style='margin-top:8px'>host/release が「(host未報告)」の actor は provenance 列(0072)未対応のワーカー — actor_id はどのcompletionでも常に記録されるが、host/release はワーカー側の対応が必要(2026-07-20時点: primary/nodec 対応済、trainer は追従中)。付与/削除は post_tag_audit.added_tags/removed_tags(0072/rev6)の配列長合計 — completion 1件で複数タグを同時付与・削除しうるため 7d 件数(completion 数)とは一致しない。</div>
</div>

<div class=panel data-tagger-category=settings><h1>閾値設定 <span class=mut>(tagger_config · ML判断の閾値、旧マジックナンバー)</span></h1>
<div class=wrap><table>
<tr><th>key<th>説明<th>値<th>最終更新</tr>
{cfg_html}</table></div>
<div class=mut style='margin-top:8px'>reconciler.py/health.py/transitions.pyが起動ごとに読む(api/tagger_core/config.py::load_config)。書込は`tagger_governed_set_config`経由のみ、既知keyのみ更新可(新規key作成不可)。このpanelはiframe埋め込みのため無認証 — ML閾値のみを扱いpost/tagデータ本体には触れないため許容。</div>
</div>
</body></html>"""
    return HTMLResponse(html, headers={"Cache-Control": "no-store"})


@dashboard_router.post("/tagger-config", include_in_schema=False)
async def set_tagger_config(
    key: str = Form(...),
    value: float = Form(...),
    db: AsyncSession = Depends(get_db),
):
    """tagger-dashboard の閾値設定パネル(<form method=post>、application/
    x-www-form-urlencoded)からのPOST先 -- Form() で受ける、Body() だと
    JSONボディを期待してしまい素のHTMLフォーム送信を読めない。あのpanelと
    同じ理由で無認証(iframe埋め込み、Authorization header を持てない) --
    書込は tagger_governed_set_config(SECURITY DEFINER, 既知keyのみUPDATE
    可、新規key作成不可)を必ず経由するので、任意SQL/任意keyの注入経路には
    ならない。value は FastAPI の float 型検証を通るので非数値は既にここで
    弾かれる。
    """
    from fastapi.responses import RedirectResponse
    from api.tagger_core.config import set_config, DEFAULTS

    if key not in DEFAULTS:
        raise HTTPException(status_code=400, detail=f"unknown tagger_config key: {key}")
    try:
        await set_config(db, key, value, updated_by="tagger-dashboard")
    except Exception as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    return RedirectResponse("/api/tagger-dashboard?raw=1", status_code=303)


@dashboard_router.post("/tagger-dashboard/verify-pools/cleanup", include_in_schema=False)
async def cleanup_verify_pools(pool_id: list[int] = Form(default=[])):
    raise HTTPException(status_code=410, detail="Verification pool workflow retired; historical evidence is retained.")


@dashboard_router.post("/tagger-dashboard/head/{tag}/generate-pool", include_in_schema=False)
async def generate_head_review_pool(tag: str):
    raise HTTPException(status_code=410, detail="Verification pools are retired. Correct tags on ordinary posts instead.")


def pool_list_options():
    """Only fields consumed by PoolSummary; prevent eager backrefs reloading Pool."""
    return [
        load_only(Pool.pool_id, Pool.version, Pool.cover_post_id, Pool.verify_status, Pool.tagger_source, raiseload=True),
        raiseload(Pool.category), raiseload(Pool._posts),
        selectinload(Pool.names),
        selectinload(Pool.state).raiseload(PoolState.pool),
        selectinload(Pool.urls).raiseload(PoolUrl.pool),
    ]


@router.get("/pools")
async def list_pools(
    query: str = Query("", alias="query"),
    offset: int = Query(0),
    limit: int = Query(40),
    include_empty: bool = Query(False, description="include pools with no posts (import tracking stubs)"),
    hide_verify: bool = Query(False, description="exclude verification/correction work pools (HEADVERIFY etc.)"),
    sort: str = Query("id", pattern="^(id|updated)$"),
    dir: str = Query("desc", pattern="^(asc|desc)$"),
    db: AsyncSession = Depends(get_db),
    safe: bool = Depends(safe_mode),
) -> PagedResult[PoolSummary]:
    limit = min(limit, 200)
    free_terms, name_terms, tag_terms = _parse_pool_query(query)

    # Safe mode: a pool's thumbnail is its cover post, so a pool may only appear
    # if its cover is explicitly safe — otherwise the grid leaks a non-safe image.
    def _safe_pool(stmt):
        if not safe:
            return stmt
        return stmt.where(
            Pool.cover_post_id.in_(select(Post.post_id).where(Post.safety == "safe"))
        )

    # observed_count from primary pool_url row (trigger-maintained on pool_post
    # +1/-1, so it counts EVERY member row including status!='ok' pending/near_dup
    # ones). Kept only as telemetry; the displayed count is displayable_sq below.
    observed_sq = (
        select(PoolUrl.observed_count)
        .where(PoolUrl.pool_id == Pool.pool_id)
        .where(PoolUrl.is_primary.is_(True))
        .correlate(Pool)
        .scalar_subquery()
    )
    # The DISPLAYED count must match what opening the pool shows = displayable
    # members only (status='ok'). observed_count diverges from this between upload
    # and cull (pages uploaded as 'pending' bump it but aren't shown) and after
    # perceptual dedup ('near_dup' rows stay members but aren't displayable) —
    # that gap is the "green big count but empty pool" bug. Count real members.
    displayable_sq = (
        select(func.count())
        .select_from(PoolPost)
        .join(Post, Post.post_id == PoolPost.post_id)
        .where(PoolPost.pool_id == Pool.pool_id)
        .where(Post.status == "ok")
        .correlate(Pool)
        .scalar_subquery()
    )


    # 💢 pool-level censor flag — does the pool contain a human-ANNOTATED censor?
    # A short-circuiting EXISTS (no count: the user wants the symbol, not a tally).
    # Deliberately annotation-only, NOT the `censored` tag: the tag marks ~89% of
    # pools (most NSFW manga is tagged censored) → useless as a list discriminator,
    # whereas a live censor_annotation marks the few pools actually worked/flagged
    # for decensoring. Indexed (censor_annotation.post_id); correlated to Pool so
    # it's one scalar column per row, no extra round trip.
    has_censored_sq = (
        select(text("1"))
        .select_from(PoolPost)
        .join(CensorAnnotation, CensorAnnotation.post_id == PoolPost.post_id)
        .where(PoolPost.pool_id == Pool.pool_id)
        .where(CensorAnnotation.deleted_at.is_(None))
        .correlate(Pool)
        .exists()
    )
    # Separate, weaker signal: the pool has a `censored`-tagged post (or a post
    # whose tag directly implies one — the old tags_search meaning). Read from
    # post_tag by id; marks ~89% of pools, so it's a DISTINCT (fainter) mark, not
    # merged into has_censored — else the annotated few drown.
    # Ids are resolved once and inlined: a correlated id subquery re-ran per pool
    # (1.1 s for 200 pools vs 50 ms with literals; GIN arrays were ~100 ms).
    _censor_names = ("censored", "bar_censor", "mosaic_censoring", "censor")
    _censor_ids = sorted(set((await db.execute(
        select(TagName.tag_id).where(TagName.name.in_(_censor_names)).union(
            select(TagImplication.parent_id)
            .join(TagName, TagName.tag_id == TagImplication.child_id)
            .where(TagName.name.in_(_censor_names)))
    )).scalars().all()))
    has_censored_tag_sq = (
        select(text("1"))
        .select_from(PoolPost)
        .join(PostTag, PostTag.post_id == PoolPost.post_id)
        .where(PoolPost.pool_id == Pool.pool_id)
        .where(PostTag.tag_id.in_([literal_column(str(int(i))) for i in _censor_ids] or [literal_column("NULL")]))
        .correlate(Pool)
        .exists()
    )

    resolved = await _resolve_pool_query_tags(db, [*free_terms, *tag_terms])
    visible_pools = select(Pool).where(Pool.hidden.is_(False))
    base_stmt = _apply_pool_filters(visible_pools, free_terms, name_terms, tag_terms, hide_verify, resolved)
    if not include_empty:
        base_stmt = base_stmt.where(Pool.cover_post_id.isnot(None))
    base_stmt = _safe_pool(base_stmt)
    filtered = bool(free_terms or name_terms or tag_terms)
    candidate_ids = await _pool_search_candidates(db, base_stmt) if filtered else None
    if candidate_ids is not None:
        total = len(candidate_ids)
    else:
        count_stmt = select(func.count()).select_from(base_stmt.subquery())
        total = (await db.execute(count_stmt)).scalar() or 0

    # Total storage of member pages — only for a filtered query (絞り込み); a
    # full-library sum over every pool's members is an expensive scan we skip.
    total_bytes = 0
    if filtered:
        # A small resolved set exposes actual selectivity to the planner, rather
        # than re-running the tag/name subplans inside a full-post aggregate.
        if candidate_ids is not None:
            membership = PoolPost.pool_id.in_(candidate_ids)
        else:
            pid_sq = base_stmt.with_only_columns(Pool.pool_id.label("pid")).subquery()
            membership = PoolPost.pool_id.in_(select(pid_sq.c.pid))
        total_bytes = int((await db.execute(
            select(func.coalesce(func.sum(Post.file_size), 0))
            .select_from(PoolPost)
            .join(Post, Post.post_id == PoolPost.post_id)
            .where(membership)
            .where(Post.status == "ok")
        )).scalar() or 0)

    list_opts = pool_list_options()
    def _dir(col): return col.asc() if dir == "asc" else col.desc()
    if sort == "updated":
        order_exprs = [_dir(func.coalesce(Pool.last_edit_time, Pool.creation_time)), _dir(Pool.pool_id)]
    else:
        order_exprs = [_dir(Pool.pool_id)]
    stmt = (
        select(
            Pool,
            displayable_sq.label("post_count"),
            has_censored_sq.label("has_censored"),
            has_censored_tag_sq.label("has_censored_tag"),
        )
        .where(Pool.hidden.is_(False))
        .options(*list_opts)
        .order_by(*order_exprs)
        .offset(offset)
        .limit(limit)
    )
    if candidate_ids is not None:
        # Candidates already enforce safety, visibility and include_empty.
        stmt = stmt.where(Pool.pool_id.in_(candidate_ids))
    else:
        stmt = _apply_pool_filters(stmt, free_terms, name_terms, tag_terms, hide_verify, resolved)
        if not include_empty:
            stmt = stmt.where(Pool.cover_post_id.isnot(None))
        stmt = _safe_pool(stmt)
    rows = (await db.execute(stmt)).unique().all()

    from api import extension_hooks
    pool_ids = [row[0].pool_id for row in rows]
    covers = await pool_covers(db, pool_ids, safe=safe)
    annotations = await extension_hooks.annotate_pools(db, pool_ids)
    results: list[PoolSummary] = []
    for pool, post_count, has_censored, has_censored_tag in rows:
        sorted_names = sorted(pool.names, key=lambda n: n.order)
        name = sorted_names[0].name if sorted_names else f"pool#{pool.pool_id}"
        cover_id, first_post_source = covers.get(pool.pool_id, (None, None))
        thumb = _thumbnail_url_by_id(cover_id) if cover_id else None
        pu = pool.primary_url
        st = pool.state
        # An extension that knows where the pool came from names its domain (a set's
        # own key can be synthetic); otherwise the cover post's own source decides.
        pool_annotations = annotations.get(pool.pool_id, {})
        domain = extension_hooks.annotated_domain(pool_annotations) or detect_domain(first_post_source)
        results.append(PoolSummary(
            id=pool.pool_id,
            name=name,
            names=[n.name for n in sorted_names],
            version=pool.version or 0,
            postCount=post_count or 0,
            thumbnailUrl=thumb,
            firstPostSource=first_post_source,
            domain=domain,
            scope=st.scope if st else None,
            verifyStatus=pool.verify_status,
            taggerSource=pool.tagger_source,
            primarySourceUrl=pu.url if pu else None,
            extensions=pool_annotations,
            hasCensored=bool(has_censored),
            hasCensoredTag=bool(has_censored_tag),
        ))
    return PagedResult(query=query, offset=offset, limit=limit, total=total,
                       totalBytes=total_bytes, results=results)


async def _pool_focus_tag(db: AsyncSession, pool: Pool) -> str | None:
    """検証プールの focus タグ (このプールで検証すべき 1 タグ)。

    導出順: tagger_source の dashboard-manual:<tag> が最も確定的。無ければ
    tagger_head_candidate.source_pool_id 逆引き (trainer-head-gN / strict-*
    等の自動生成プールはここに紐づく)。どちらも無ければ null — 検証プールで
    ない通常プールでは出さない。UI 明示要望 (user 2026-08-02):「検証プール
    画面でフォーカスタグがきちんと明示されていない」。"""
    ts = pool.tagger_source or ""
    if ts.startswith("dashboard-manual:"):
        return ts.split(":", 1)[1] or None
    if not ts:
        return None
    return (await db.execute(text(
        "SELECT tag FROM tagger_head_candidate WHERE source_pool_id = :pid "
        "ORDER BY added_at DESC LIMIT 1"
    ), {"pid": pool.pool_id})).scalar_one_or_none()


async def _pool_artists(db: AsyncSession, pool_id: int, *, localize: bool = False) -> list[dict]:
    """Artist identities from all members, grouped by tag ID (aliases never duplicate)."""
    counts = (
        select(PostTag.tag_id, func.count(func.distinct(PostTag.post_id)).label("member_count"))
        .join(PoolPost, PoolPost.post_id == PostTag.post_id)
        .join(Tag, Tag.tag_id == PostTag.tag_id)
        .join(TagCategory, Tag.category_id == TagCategory.id)
        .where(PoolPost.pool_id == pool_id, TagCategory.name == "artist")
        .group_by(PostTag.tag_id).subquery()
    )
    result = await db.execute(
        select(Tag, counts.c.member_count)
        .join(counts, counts.c.tag_id == Tag.tag_id)
        .options(selectinload(Tag.names), selectinload(Tag.localizations))
        .order_by(counts.c.member_count.desc(), Tag.tag_id)
    )
    artists = []
    for tag, count in result.unique().all():
        item = {"id": tag.tag_id, "names": [n.name for n in tag.names],
                "category": "artist", "postCount": count}
        if localize:
            item["localizedNames"] = {row.locale: row.name for row in tag.localizations}
        artists.append(item)
    return artists


@router.get("/pools/{pool_id_or_name}")
@router.get("/pool/{pool_id_or_name}", include_in_schema=False)
async def get_pool(pool_id_or_name: str, db: AsyncSession = Depends(get_db),
                   localize: bool = Query(False), include_posts: bool = Query(True)) -> dict:
    pool = await _load_pool(pool_id_or_name, db, include_posts=include_posts)
    if pool.hidden:
        raise HTTPException(status_code=404, detail="Pool not found")
    data = _serialize_pool(pool, include_posts=include_posts)
    data["focusTag"] = await _pool_focus_tag(db, pool)
    data["artists"] = await _pool_artists(db, pool.pool_id, localize=localize)
    covers = await pool_covers(db, [pool.pool_id])
    cover_id, first_post_source = covers.get(pool.pool_id, (None, None))
    sorted_names = sorted(pool.names, key=lambda n: n.order)
    data["name"] = sorted_names[0].name if sorted_names else f"pool#{pool.pool_id}"
    data["thumbnailUrl"] = _thumbnail_url_by_id(cover_id) if cover_id else None
    data["firstPostSource"] = first_post_source
    data["extensions"] = await _pool_annotations(db, pool.pool_id)
    return data


async def _pool_annotations(db: AsyncSession, pool_id: int) -> dict:
    from api import extension_hooks
    return (await extension_hooks.annotate_pools(db, [pool_id])).get(pool_id, {})


# ── POST (ensure — idempotent create-or-return by source URL) ────────

async def ensure_pool_by_source(
    db: AsyncSession, url: str, *, name: str | None = None, alternates=(),
) -> tuple[Pool, bool]:
    """The pool keyed by a source URL, created if absent; (pool, created). Never commits.

    `name` is a title hint: it names a new pool, and promotes itself over a pool
    that still carries only its URL-derived fallback name. `alternates` are more
    URLs of the same source, recorded as aliases.
    """
    canonical_url = _canonical_gallery_url(url)
    alternates = [a.strip() for a in alternates if a and str(a).strip()]
    if canonical_url != url and url not in alternates:
        alternates = [url] + alternates
    url = canonical_url

    pool = await _find_pool_by_source(url, db)
    created = pool is None

    if pool is None:
        cat = await _get_default_category(db)
        now = datetime.utcnow()
        pool = Pool(
            category_id=cat.id if cat else 1,
            version=1,
            creation_time=now,
        )
        db.add(pool)
        await db.flush()

        # Create primary PoolUrl
        db.add(PoolUrl(pool_id=pool.pool_id, url=url, is_primary=True))

        # Set scope from URL pattern and create PoolState
        scope = _infer_scope(url)
        db.add(PoolState(pool_id=pool.pool_id, scope=scope))
        await db.flush()

        # name hint: explicit name, else derive from the URL.
        candidate = name or _derive_pool_name_from_url(url)
        fallback = _derive_pool_name_from_url(url)
        insert_name_sql = text(
            "INSERT INTO pool_name (pool_id, name, ord) "
            "VALUES (:pid, :name, 0) "
            "ON CONFLICT (name) DO NOTHING RETURNING pool_name_id"
        )
        inserted = False
        for attempt_name in [
            candidate,
            f"{candidate} ({fallback})" if candidate != fallback else None,
            f"{candidate} (pool-{pool.pool_id})",
            f"{fallback} (pool-{pool.pool_id})",
        ]:
            if not attempt_name:
                continue
            r_ins = await db.execute(insert_name_sql, {"pid": pool.pool_id, "name": attempt_name[:128]})
            if r_ins.first() is not None:
                inserted = True
                break
        if not inserted:
            await db.execute(insert_name_sql, {
                "pid": pool.pool_id,
                "name": f"pool-{pool.pool_id}-{datetime.utcnow().strftime('%H%M%S')}",
            })
    else:
        existing_names = {n.name for n in pool.names}
        if not pool.names:
            # Pool has no name at all: add URL-derived fallback or hint.
            backfill = name or _derive_pool_name_from_url(url)
            await db.execute(
                text(
                    "INSERT INTO pool_name (pool_id, name, ord) "
                    "VALUES (:pid, :name, 0) "
                    "ON CONFLICT (name) DO NOTHING"
                ),
                {"pid": pool.pool_id, "name": backfill[:128]},
            )
        elif name and name not in existing_names and _pool_untitled(pool, url):
            # Pool has only a URL-derived fallback name but a proper title is now
            # available → promote it (first titling). Guarded by _pool_untitled so
            # an ALREADY-titled pool (real artist/curated/merged-pool name) is never
            # overwritten by a later visit of one of its sources — the
            # merge-pool naming-churn fix (pool 58 report, 2026-07-18).
            # Try the hint and fallback variants; demote only when an insert lands.
            fallback_for_name = _derive_pool_name_from_url(url)
            inserted_name = None
            for attempt in [
                name,
                f"{name} ({fallback_for_name})" if name != fallback_for_name else None,
                f"{name} (pool-{pool.pool_id})",
            ]:
                if not attempt:
                    continue
                r_ins = await db.execute(
                    text(
                        "INSERT INTO pool_name (pool_id, name, ord) "
                        "VALUES (:pid, :name, 0) "
                        "ON CONFLICT (name) DO NOTHING RETURNING pool_name_id"
                    ),
                    {"pid": pool.pool_id, "name": attempt[:128]},
                )
                if r_ins.first() is not None:
                    inserted_name = attempt
                    break
            if inserted_name:
                # Demote existing names only now that the new name is confirmed inserted.
                await db.execute(
                    text("UPDATE pool_name SET ord = ord + 1 WHERE pool_id = :pid AND name != :n"),
                    {"pid": pool.pool_id, "n": inserted_name},
                )

    # Adopt pre-existing posts on a NEW pool, or an EMPTY existing one (a
    # pre-created pool whose source turned out to be already in kura — safe
    # because an empty pool cannot carry curated membership). See
    # adopt_posts_by_source.
    if created:
        await adopt_posts_by_source(db, pool.pool_id, url)
    else:
        empty = (await db.execute(
            text("SELECT NOT EXISTS (SELECT 1 FROM pool_post WHERE pool_id = :pid)"),
            {"pid": pool.pool_id})).scalar()
        if empty:
            await adopt_posts_by_source(db, pool.pool_id, url)

    await _insert_pool_urls(db, pool.pool_id, alternates)
    return pool, created


@router.post("/pools/ensure", status_code=200)
async def ensure_pool(
    body: dict = Body(...),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Idempotent create-or-return pool by source URL with goal metadata.

    Body: { "url": str, "count"?: int, "name"?: str, "alternates"?: [str] }
    Response: pool dict + created (bool) + existingPostCount (int)
              + goalMet (bool) + deltaNeeded (int)
    """
    url = (body.get("url") or "").strip()
    if not url:
        raise HTTPException(status_code=400, detail="url is required")
    try:
        count = int(body["count"]) if body.get("count") is not None else None
    except (ValueError, TypeError):
        count = None

    pool, created = await ensure_pool_by_source(
        db, url, name=(body.get("name") or "").strip() or None,
        alternates=body.get("alternates") or [])
    if count is not None and count > 0:
        # One statement: expected_count only grows, also against a concurrent
        # writer (another ensure, or the observed-count trigger).
        await db.execute(text("""
            UPDATE pool_url SET expected_count = GREATEST(COALESCE(expected_count, 0), :n)
            WHERE pool_id = :p AND is_primary
        """), {"n": count, "p": pool.pool_id})
    await db.commit()

    pool_id = pool.pool_id
    db.expire_all()  # the statement above and the triggers changed rows this session holds
    pool = await _load_pool(str(pool_id), db)
    pu = pool.primary_url
    existing_post_count = pu.observed_count or 0 if pu else 0
    expected = pu.expected_count if pu else None
    goal_met = expected is None or existing_post_count >= expected
    delta_needed = max(0, (expected or 0) - existing_post_count)

    data = _serialize_pool(pool)
    data["created"] = created
    data["existingPostCount"] = existing_post_count
    data["goalMet"] = goal_met
    data["deltaNeeded"] = delta_needed
    return data


@router.post("/pools/name-from-title", status_code=200)
async def set_pool_name_from_title(
    body: dict = Body(...),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Idempotently promote the source title to a pool's display name (ord 0),
    demoting the URL-derived fallback (`<site>-post-NNN` etc.) to an alias.

    An importer's pre-create path (/pools/ensure) only knows the URL at dispatch
    time, so it names pools `<site>-post-NNN`. Once the import fetches the real
    title it calls this to fix the name — without it meta-tagging has no title to
    derive tags from. Looks the pool up by source URL. No-op when the pool is
    already titled (ord-0 name differs from the URL-derived fallback).

    Body: { "url": str, "title": str }
    """
    url = (body.get("url") or "").strip()
    title = (body.get("title") or "").strip()[:128]
    force = bool(body.get("force"))
    if not url or not title:
        raise HTTPException(status_code=400, detail="url and title are required")

    pool = await _find_pool_by_source(url, db)
    if pool is None:
        raise HTTPException(status_code=404, detail="pool not found for url")

    names = sorted(pool.names, key=lambda n: n.order)
    url_name = _derive_pool_name_from_url(url)

    # Already titled (a real title sits at ord 0, not the URL fallback) → no-op.
    # force=true (backfill of a known-broken name) skips this guard.
    if not force and names and names[0].name != url_name:
        # ...unless that title IS the one we were asked to set but mis-ordered.
        if names[0].name == title:
            return {"poolId": pool.pool_id, "changed": False, "reason": "already-titled"}
        if not any(n.name == title for n in names):
            return {"poolId": pool.pool_id, "changed": False, "reason": "already-titled"}

    # Title already attached to this pool — just make it ord 0.
    if any(n.name == title for n in names):
        if names and names[0].name == title:
            return {"poolId": pool.pool_id, "changed": False, "reason": "noop"}
        await db.execute(
            text("UPDATE pool_name SET ord = ord + 1 WHERE pool_id = :pid AND name <> :t"),
            {"pid": pool.pool_id, "t": title},
        )
        await db.execute(
            text("UPDATE pool_name SET ord = 0 WHERE pool_id = :pid AND name = :t"),
            {"pid": pool.pool_id, "t": title},
        )
        await db.commit()
        return {"poolId": pool.pool_id, "changed": True, "name": title}

    # Insert the title at ord 0, shifting existing names down. pool_name.name is
    # globally UNIQUE, so on collision fall back to a pool-scoped suffix.
    await db.execute(
        text("UPDATE pool_name SET ord = ord + 1 WHERE pool_id = :pid"),
        {"pid": pool.pool_id},
    )
    for attempt in (title, f"{title} (pool-{pool.pool_id})"[:128]):
        r = await db.execute(
            text(
                "INSERT INTO pool_name (pool_id, name, ord) VALUES (:pid, :name, 0) "
                "ON CONFLICT (name) DO NOTHING RETURNING pool_name_id"
            ),
            {"pid": pool.pool_id, "name": attempt},
        )
        if r.first() is not None:
            await db.commit()
            return {"poolId": pool.pool_id, "changed": True, "name": attempt}

    await db.rollback()
    return {"poolId": pool.pool_id, "changed": False, "reason": "name-collision"}


# ── POST (create) ────────────────────────────────────────────────────

@router.patch("/pools/{pool_id_or_name}/verify-status")
async def set_pool_verify_status(pool_id_or_name: str, body: dict = Body(...),
                                 auth_user: User = Depends(get_current_user)):
    raise HTTPException(status_code=410, detail="Verification status is historical and read-only. Correct tags on ordinary posts instead.")


@router.post("/pools", status_code=200)
@router.post("/pool", status_code=200, include_in_schema=False)
async def create_pool(
    body: dict = Body(...),
    db: AsyncSession = Depends(get_db),
    auth_user: User = Depends(get_current_user),
) -> dict:
    _refuse_unknown_fields(body, CREATE_FIELDS)
    if body.get("verify_status") is not None or body.get("tagger_source") is not None:
        raise HTTPException(status_code=410, detail="Verification pools are retired; create an ordinary pool without verification metadata.")
    names = body.get("names", [])
    if not names:
        raise HTTPException(status_code=400, detail="names is required")

    cat_name = body.get("category", "default")
    result = await db.execute(select(PoolCategory).where(PoolCategory.name == cat_name))
    cat = result.scalar_one_or_none()
    if cat is None:
        cat = await _get_default_category(db)

    now = datetime.utcnow()
    pool = Pool(
        category_id=cat.id if cat else 1,
        version=1,
        creation_time=now,
        description=body.get("description"),
        # Legacy verification metadata is rejected above; new pools are ordinary groups.
        verify_status=body.get("verify_status"),
        tagger_source=body.get("tagger_source"),
    )
    db.add(pool)
    await db.flush()

    # Invariant: every pool has exactly one primary pool_url with a NOT-NULL
    # unique url. An imported pool carries its real source URL; a user-created
    # pool with no source gets a sentinel `manual:pool/<id>` so it is never
    # url-less (url-less pools resolve observed_count to 0 → "0" display bug
    # and confuse importer dedup). If the supplied real url is already owned by
    # another pool (UNIQUE(url)), fall back to the sentinel rather than 500 —
    # this should not happen when callers dedup by url first, but keeps the
    # invariant intact defensively.
    src_url = (body.get("primarySourceUrl") or "").strip() or None
    if src_url:
        # Normalize ?p=N paged / mirror gallery URLs to the canonical gallery
        # URL before it becomes the primary key. /pools/ensure already does this;
        # this path did not, which let paged URLs create duplicate pools that
        # never share posts with the canonical pool (cleaned by
        # a one-off repair script).
        src_url = _canonical_gallery_url(src_url)
        taken = (await db.execute(
            select(PoolUrl.pool_id).where(PoolUrl.url == src_url)
        )).scalar_one_or_none()
        if taken is not None:
            src_url = None  # already owned → use sentinel below
    if not src_url:
        src_url = f"manual:pool/{pool.pool_id}"
    db.add(PoolUrl(pool_id=pool.pool_id, url=src_url, is_primary=True))
    db.add(PoolState(pool_id=pool.pool_id, scope=_infer_scope(src_url)))

    for i, name in enumerate(names):
        db.add(PoolName(pool_id=pool.pool_id, name=name, order=i))

    post_ids = body.get("posts", [])
    await _flush_or_name_conflict(db, names)
    await _bulk_insert_pool_posts(db, pool.pool_id, post_ids)

    await db.commit()
    return _serialize_pool(await _load_pool(str(pool.pool_id), db))


POOL_NAME_UNIQUE = "pool_name_name_key"


async def _flush_or_name_conflict(db: AsyncSession, names: list[str]) -> None:
    """Flush the new pool's names, and answer a taken name with a 409 that says which pool
    has it. Without this the unique constraint speaks as a 500 — found by the szurubooru
    migration tool, which creates pools by name and re-runs. Only that constraint is
    folded, for the reason _commit_or_url_conflict gives."""
    try:
        await db.flush()
    except IntegrityError as e:
        if POOL_NAME_UNIQUE not in str(getattr(e, "orig", e)):
            raise
        await db.rollback()
        owner = (await db.execute(
            select(PoolName.name, PoolName.pool_id).where(PoolName.name.in_(names)).limit(1)
        )).first()
        taken, pool_id = (owner.name, owner.pool_id) if owner else (names[0], "another pool")
        raise HTTPException(status_code=409,
                            detail=f"pool name {taken!r} is already used by pool {pool_id}") from e


# ── PUT (update) ─────────────────────────────────────────────────────

@router.put("/pools/{pool_id_or_name}")
@router.put("/pool/{pool_id_or_name}", include_in_schema=False)
async def update_pool(
    pool_id_or_name: str,
    body: dict = Body(...),
    db: AsyncSession = Depends(get_db),
    auth_user: User = Depends(get_current_user),
) -> dict:
    _refuse_unknown_fields(body, UPDATE_FIELDS)
    pool = await _load_pool(pool_id_or_name, db)

    if "version" in body and pool.version != body["version"]:
        raise HTTPException(status_code=409, detail={"name": "OptimisticLockError"})

    if "names" in body:
        for pn in list(pool.names):
            await db.delete(pn)
        await db.flush()
        for i, name in enumerate(body["names"]):
            db.add(PoolName(pool_id=pool.pool_id, name=name, order=i))

    if "posts" in body:
        await _apply_pool_posts(pool, body["posts"], db)

    if "description" in body:
        pool.description = body["description"]

    new_src_url = (body.get("primarySourceUrl") or "").strip() or None
    if new_src_url:
        # `pool_url.url` の UNIQUE は **グローバル**なので、「この pool が primary を
        # 持っているか」だけでは守れない (kura#70)。他 pool が持つ URL を渡されると
        # UPDATE 側も INSERT 側も UniqueViolation → 500 になり、呼び手には原因不明の
        # サーバ不具合として見えていた (実測: 7 日で 135 回)。
        #
        # 所有者を先に引き、
        #   他 pool のもの   → 409 と **その pool_id** (「誰のものか」を言う。
        #                      ON CONFLICT DO NOTHING で畳むと、成功したように見えて
        #                      設定されないという別の嘘になる)
        #   この pool のもの → その行を primary にする (別行に持っていた場合の昇格)
        #   誰のものでもない → 従来どおり primary へ設定 / 追加
        # .unique(): PoolUrl は collection への joined eager load を持つので、
        # scalar_one_or_none() を直接呼ぶと InvalidRequestError (実測)。
        owner_row = (await db.execute(
            select(PoolUrl).where(PoolUrl.url == new_src_url)
        )).unique().scalar_one_or_none()
        if owner_row is not None and owner_row.pool_id != pool.pool_id:
            raise HTTPException(
                status_code=409,
                detail=(f"primarySourceUrl {new_src_url!r} is already owned by "
                        f"pool {owner_row.pool_id}"),
            )
        current = pool.primary_url
        if owner_row is not None:
            if not owner_row.is_primary:
                if current is not None and current is not owner_row:
                    current.is_primary = False
                owner_row.is_primary = True
        elif current is not None:
            current.url = new_src_url
        else:
            db.add(PoolUrl(pool_id=pool.pool_id, url=new_src_url, is_primary=True))

    if "scope" in body and pool.state:
        pool.state.scope = body["scope"]

    if "category" in body:
        result = await db.execute(select(PoolCategory).where(PoolCategory.name == body["category"]))
        cat = result.scalar_one_or_none()
        if cat:
            pool.category_id = cat.id

    pool.version += 1
    pool.last_edit_time = datetime.utcnow()

    await _commit_or_url_conflict(db, new_src_url)
    return _serialize_pool(await _load_pool(str(pool.pool_id), db))


#: The constraint that makes "one URL, one pool" true. The pre-check above and the
#: race-loser branch below key off the same name the database uses (kura#70).
POOL_URL_UNIQUE = "pool_url_url_unique"


async def _commit_or_url_conflict(db: AsyncSession, url: str | None) -> None:
    """Commit, and turn a lost race for `url` into the same 409 the pre-check gives.

    Reading the owner before writing answers the common case with a useful message, but it
    leaves a window: another request can claim the URL between that SELECT and this
    commit, and then the constraint speaks instead — as a 500, which is the failure
    kura#70 exists to remove. Rarer, and therefore worse: a 500 that reproduces once a
    month is the one nobody ever diagnoses. Review round 1 (finding 5) asked for both
    halves for exactly this reason.

    Only this constraint is folded. Catching IntegrityError broadly would report any
    constraint failure as "that URL is taken" — a wrong answer that reads like a right
    one, which costs more than the 500 did.
    """
    try:
        await db.commit()
    except IntegrityError as e:
        if not url or POOL_URL_UNIQUE not in str(getattr(e, "orig", e)):
            raise
        await db.rollback()
        owner = (await db.execute(
            select(PoolUrl.pool_id).where(PoolUrl.url == url).limit(1)
        )).scalar_one_or_none()
        raise HTTPException(
            status_code=409,
            detail=(f"primarySourceUrl {url!r} is already owned by "
                    f"pool {owner if owner is not None else 'another pool'}"),
        ) from e


class PoolReorderRequest(BaseModel):
    postIds: list[int]


@router.put("/pools/{pool_id_or_name}/order")
@router.put("/pool/{pool_id_or_name}/order", include_in_schema=False)
async def reorder_pool_posts(
    pool_id_or_name: str,
    body: PoolReorderRequest,
    db: AsyncSession = Depends(get_db),
    auth_user: User = Depends(get_current_user),
) -> dict:
    pool = await _load_pool(pool_id_or_name, db)
    pool_posts = {pp.post_id: pp for pp in pool._posts}
    for new_order, post_id in enumerate(body.postIds):
        if post_id in pool_posts:
            pool_posts[post_id].order = new_order
    pool.version += 1
    pool.last_edit_time = datetime.utcnow()
    await db.commit()
    return {"ok": True, "count": len(body.postIds)}


class PoolCoverRequest(BaseModel):
    coverPostId: int


@router.patch("/pools/{pool_id_or_name}/cover")
@router.patch("/pool/{pool_id_or_name}/cover", include_in_schema=False)
async def set_pool_cover(
    pool_id_or_name: str,
    body: PoolCoverRequest,
    db: AsyncSession = Depends(get_db),
    auth_user: User = Depends(get_current_user),
) -> dict:
    pool = await _load_pool(pool_id_or_name, db)
    pool_post = next((pp for pp in pool._posts if pp.post_id == body.coverPostId), None)
    if pool_post is None:
        raise HTTPException(status_code=404, detail="Post not found in pool")
    min_order = min((pp.order for pp in pool._posts), default=0)
    pool_post.order = min_order - 1
    pool.cover_post_id = body.coverPostId
    pool.version += 1
    pool.last_edit_time = datetime.utcnow()
    await db.commit()
    return {"ok": True, "newOrder": pool_post.order}


# ── DELETE ───────────────────────────────────────────────────────────

@router.delete("/pools/{pool_id_or_name}", status_code=200)
@router.delete("/pool/{pool_id_or_name}", status_code=200, include_in_schema=False)
async def delete_pool(
    pool_id_or_name: str,
    body: dict = Body(default={}),
    db: AsyncSession = Depends(get_db),
    auth_user: User = Depends(get_current_user),
) -> dict:
    pool = await _load_pool(pool_id_or_name, db)
    if "version" in body and pool.version != body["version"]:
        raise HTTPException(status_code=409, detail={"name": "OptimisticLockError"})
    pool_id = pool.pool_id
    await db.delete(pool)
    await db.commit()
    return {"deleted": True, "id": pool_id}


@router.post("/pools/{pool_id_or_name}/primary-source", status_code=200)
@router.post("/pool/{pool_id_or_name}/primary-source", status_code=200, include_in_schema=False)
async def set_primary_source(
    pool_id_or_name: str,
    body: dict = Body(default={}),
    db: AsyncSession = Depends(get_db),
    auth_user: User = Depends(get_current_user),
) -> dict:
    """Promote an existing source (alias) to primary. The url must already
    belong to this pool. Two UPDATEs (clear old → set new) so the partial unique
    index ux_pool_url_primary never sees two primaries on one pool."""
    url = (body.get("url") or "").strip()
    if not url:
        raise HTTPException(status_code=400, detail="url is required")
    pool = await _load_pool(pool_id_or_name, db)
    if not any(u.url == url for u in (pool.urls or [])):
        raise HTTPException(status_code=404, detail="url is not a source of this pool")
    await db.execute(
        text("UPDATE pool_url SET is_primary = false WHERE pool_id = :p AND is_primary = true"),
        {"p": pool.pool_id},
    )
    await db.execute(
        text("UPDATE pool_url SET is_primary = true WHERE pool_id = :p AND url = :u"),
        {"p": pool.pool_id, "u": url},
    )
    pool.version += 1
    await db.commit()
    db.expunge_all()  # raw-SQL writes bypassed the ORM; detach so _load_pool re-queries fresh (eager)
    return _serialize_pool(await _load_pool(str(pool.pool_id), db))


@router.get("/pool/{pool_id}/visibility")
async def pool_visibility(pool_id: int, db: AsyncSession = Depends(get_db),
                          user: User = Depends(get_current_user)) -> dict:
    if user.rank != "administrator":
        raise HTTPException(status_code=403, detail="Administrator required")
    hidden = await db.scalar(select(Pool.hidden).where(Pool.pool_id == pool_id))
    if hidden is None:
        raise HTTPException(status_code=404, detail="Pool not found")
    return {"poolId": pool_id, "hidden": hidden}


@router.put("/pool/{pool_id}/visibility")
async def set_pool_visibility(pool_id: int, body: dict = Body(...), db: AsyncSession = Depends(get_db),
                              user: User = Depends(get_current_user)) -> dict:
    if user.rank != "administrator":
        raise HTTPException(status_code=403, detail="Administrator required")
    if set(body) != {"hidden"} or type(body["hidden"]) is not bool:
        raise HTTPException(status_code=422, detail="hidden must be a boolean")
    pool = await db.scalar(select(Pool).where(Pool.pool_id == pool_id).with_for_update(of=Pool))
    if pool is None:
        raise HTTPException(status_code=404, detail="Pool not found")
    if pool.hidden != body["hidden"]:
        pool.hidden = body["hidden"]
        pool.version += 1
    await db.commit()
    return {"poolId": pool_id, "hidden": body["hidden"]}


@router.get("/pool/{pool_id}/tagger-exclude")
async def get_tagger_exclude(pool_id: int, db: AsyncSession = Depends(get_db)) -> dict:
    """migration 0099。ペア探索スコープ (censor_pair_search_scope) と同じ
    sidecar-table 流儀 -- 行が無ければ未設定(除外していない)。"""
    row = (await db.execute(text(
        "SELECT excluded FROM tagger_auto_exclude_pool WHERE pool_id = :p"
    ), {"p": pool_id})).first()
    return {"poolId": pool_id, "excluded": bool(row[0]) if row else False}


@router.put("/pool/{pool_id}/tagger-exclude")
async def put_tagger_exclude(
    pool_id: int,
    body: dict = Body(...),
    db: AsyncSession = Depends(get_db),
    auth_user: User = Depends(get_current_user),
) -> dict:
    """user指示(2026-07-20)「今後学習除外プールを設定したい...uiから一発で
    切り替えられるといいだろう」-- 自分の生成物/写真管理用など、tagger
    学習/自動タグ付けパイプラインに一切触らせたくない pool を永続的に
    除外する(0097のverify_status='pending'除外とは別 -- あちらは検証中
    のみの一時的な除外、こちらはpool単位で恒久的)。除外はtagger_governed_
    claim_auto_tagが直読(migration 0099)。"""
    excluded = bool(body.get("excluded", True))
    await db.execute(text(
        """INSERT INTO tagger_auto_exclude_pool (pool_id, excluded, updated_at, updated_by)
           VALUES (:p, :e, now(), :by)
           ON CONFLICT (pool_id) DO UPDATE SET excluded = :e, updated_at = now(), updated_by = :by"""
    ), {"p": pool_id, "e": excluded, "by": auth_user.name if auth_user else None})
    await db.commit()
    return {"poolId": pool_id, "excluded": excluded}


@router.post("/pools/{keep_id_or_name}/merge", status_code=200)
@router.post("/pool/{keep_id_or_name}/merge", status_code=200, include_in_schema=False)
async def merge_pools(
    keep_id_or_name: str,
    body: dict = Body(default={}),
    db: AsyncSession = Depends(get_db),
    auth_user: User = Depends(get_current_user),
) -> dict:
    """Merge source pools into a keep pool.

    Moves all posts (dedup by (pool_id,post_id)), PRESERVES each dropped pool's
    source URLs as non-primary aliases on the keep pool so the merge history /
    provenance survives, then deletes the dropped pools. Every source scheme is
    kept — http(s) and the adapters' own (provider:<folder>/<path>, …): a pool found
    by such an alias must still be found after the merge. Only kura's sentinel
    urls (manual:/legacy:/kura-merge:) are dropped, as they are
    not sources.
    Reads pool_post directly — no search-index dependency.
    """
    from_ids = body.get("from") or []
    if not isinstance(from_ids, list) or not from_ids:
        raise HTTPException(status_code=400, detail="from: [pool_id, ...] is required")
    keep = await _load_pool(keep_id_or_name, db)
    moved_posts = 0
    aliases_preserved = 0
    deleted: list[int] = []
    for raw in from_ids:
        try:
            fid = int(raw)
        except (ValueError, TypeError):
            continue
        if fid == keep.pool_id:
            continue
        if not (await db.execute(text("SELECT 1 FROM pool WHERE id = :f"), {"f": fid})).scalar():
            continue
        r = await db.execute(text(
            "INSERT INTO pool_post (pool_id, post_id, ord) "
            "SELECT :k, dp.post_id, "
            "       (SELECT COALESCE(max(ord), -1) FROM pool_post WHERE pool_id = :k) "
            "       + row_number() OVER (ORDER BY dp.ord) "
            "FROM pool_post dp "
            "WHERE dp.pool_id = :f "
            "  AND NOT EXISTS (SELECT 1 FROM pool_post kp WHERE kp.pool_id = :k AND kp.post_id = dp.post_id) "
            "ON CONFLICT DO NOTHING"
        ), {"k": keep.pool_id, "f": fid})
        moved_posts += r.rowcount or 0
        # reassign the dropped pool's source urls to keep as non-primary aliases
        # (url is globally UNIQUE so we move, not copy; skip ones keep already owns).
        # Not just http(s): provider:<folder>/<path> aliases were silently deleted with
        # the pool, so the next import of that share made the duplicate again
        # (non-HTTP source alias regression, 2026-11-04).
        r2 = await db.execute(text(
            "UPDATE pool_url SET pool_id = :k, is_primary = false "
            "WHERE pool_id = :f AND url !~ '^(manual|legacy|kura-merge):' "
            "  AND NOT EXISTS (SELECT 1 FROM pool_url e WHERE e.pool_id = :k AND e.url = pool_url.url)"
        ), {"k": keep.pool_id, "f": fid})
        aliases_preserved += r2.rowcount or 0
        await db.execute(text("DELETE FROM pool_name WHERE pool_id = :f"), {"f": fid})
        await db.execute(text("DELETE FROM pool WHERE id = :f"), {"f": fid})
        deleted.append(fid)
    keep.version += 1
    await db.commit()
    db.expunge_all()  # raw-SQL writes bypassed the ORM; detach so _load_pool re-queries fresh (eager)
    out = _serialize_pool(await _load_pool(str(keep.pool_id), db))
    out["_merge"] = {
        "movedPosts": moved_posts,
        "aliasesPreserved": aliases_preserved,
        "deleted": deleted,
    }
    return out
