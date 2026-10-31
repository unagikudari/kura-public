"""Posts router — GET + PATCH endpoints."""

import asyncio
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import aiofiles
import json as _json
from fastapi import APIRouter, Body, Depends, File, Form, Query, Request, UploadFile
from fastapi import Response
from fastapi.responses import RedirectResponse

from api.errors import KuraError
from PIL import Image
from pydantic import BaseModel
from sqlalchemy import column, false, func, literal_column, or_, select, table, text, tuple_
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload, raiseload

from api.config import DATA_URL
from api.services import files, post_files, renditions
from api.services.hashing import get_security_hash
from api.services.tags import create_tag_named, get_or_create_tags, tag_by_name
from api import media_role
from api.db.models import (
    Comment,
    Pool,
    PoolPost,
    Post,
    PostFavorite,
    PostFeature,
    PostNote,
    PostScore,
    PostTag,
    Tag,
    TagCategory,
    TagImplication,
    TagName,
    User,
)
from api.db.session import get_db
from api.auth import get_current_user, get_optional_user
from api.schemas.common import MicroPool, MicroTag, MicroUser, PagedResult
from api.schemas.post import PostResponse

router = APIRouter(prefix="/api", tags=["posts"])

# szurubooru-style `type:` aliases. Moved here from the deleted
# api/services/search.py — TYPE_ALIASES was the only thing that module still
# exported that anyone used; its full search parser was dead code.
TYPE_ALIASES = {
    "image": "image", "animation": "animation", "animated": "animation",
    "anim": "animation", "gif": "animation", "video": "video",
    "webm": "video", "flash": "flash", "swf": "flash",
    "document": "document", "pdf": "document", "doc": "document",
}


# post_tag_pin の軽量参照 (ORM モデル未定義のまま検索句で使う)。
# locked:<tag> / removed:<tag> 検索トークン用 (user 2026-08-02:
# 「施錠状態にあるものだけをフィルタリングしたい」)。
_PIN_T = table("post_tag_pin", column("post_id"), column("tag_id"), column("state"))


class PostUpdateRequest(BaseModel):
    version: int
    tags: list[str] = []
    safety: Optional[str] = None
    source: Optional[str] = None

MIME_EXT = {
    "image/jpeg": "jpg",
    "image/png": "png",
    "image/gif": "gif",
    "image/webp": "webp",
    "image/bmp": "bmp",
    "image/avif": "avif",
    "video/mp4": "mp4",
    "video/webm": "webm",
    "application/x-shockwave-flash": "swf",
    "application/pdf": "pdf",
}


def _security_hash(post_id: int) -> str:
    """Delegates to the one implementation (kura#43-2).

    This was a byte-identical copy of api.services.hashing.get_security_hash —
    one of four. They agreed for years, and would have stopped agreeing the
    moment the filename salt was split out of KURA_SECRET, because only the
    canonical one was changed."""
    return get_security_hash(post_id)


def _legacy_fs_content_url(post: Post) -> str:
    ext = MIME_EXT.get(post.mime_type, "dat")
    return f"{DATA_URL.rstrip('/')}/posts/{post.post_id}_{_security_hash(post.post_id)}.{ext}"


def _legacy_fs_thumbnail_url(post: Post) -> str:
    return f"{DATA_URL.rstrip('/')}/generated-thumbnails/{post.post_id}_{_security_hash(post.post_id)}.jpg?v={post.version}"


def _content_url(post: Post) -> str:
    # pg-native (Phase 4b): serve WebP from PG via the API; the endpoint falls
    # back to the legacy FS file for rows without post.data (e.g. video bodies on
    # HDD). Relative URL — the frontend's mediaUrl() prepends the API base.
    return f"/api/post/{post.post_id}/content?v={post.version}"


def _thumbnail_url(post: Post) -> str:
    return f"/api/post/{post.post_id}/thumbnail?v={post.version}"


def _serialize_post(post: Post, auth_user: Optional[User] = None,
                    pool_post_counts: Optional[dict[int, int]] = None,
                    latest_auto_tag: Optional[dict] = None,
                    pin_states: Optional[dict[int, str]] = None,
                    localize: bool = False) -> PostResponse:
    tags = []
    for tag in post.tags:
        tags.append(MicroTag(
            id=tag.tag_id,
            localizedNames={row.locale: row.name for row in tag.localizations} if localize else None,
            names=[n.name for n in tag.names],
            category=tag.category.name if tag.category else "default",
            usages=tag.post_count or 0,
            pinned=(pin_states or {}).get(tag.tag_id),
        ))

    user = None
    if post.user:
        user = MicroUser(name=post.user.name)

    own_favorite = len(post.favorited_by) > 0  # single-user: any favorite = own favorite

    # Populate pool memberships when the relationship has been eagerly
    # loaded by the caller (single GET path). For list paths the relation
    # may be unloaded — we fall back to an empty list rather than risking
    # an async lazy-load error.
    pools_micro = []
    try:
        for pp in post._pools or []:
            pool = pp.pool
            if pool is None:
                continue
            pool_names = sorted(pool.names or [], key=lambda n: n.order)
            # postCount comes from a precomputed GROUP BY (pool_post_counts) so we
            # never eager-load Pool._posts — materialising a pool's full membership
            # just to len() it is what balloons the worker on list pages.
            if pool_post_counts is not None:
                post_count = pool_post_counts.get(pool.pool_id, 0)
            else:
                loaded = pool.__dict__.get("_posts")
                post_count = len(loaded) if loaded is not None else 0
            pools_micro.append(MicroPool(
                id=pool.pool_id,
                names=[n.name for n in pool_names],
                category=pool.category.name if pool.category else "default",
                description=pool.description,
                postCount=post_count,
            ))
    except Exception:
        # Lazy load not allowed in async context — skip rather than crash
        pools_micro = []
    return PostResponse(
        id=post.post_id,
        version=post.version,
        creationTime=post.creation_time,
        lastEditTime=post.last_edit_time,
        safety=post.safety,
        source=post.source,
        taggingSource=(latest_auto_tag or {}).get("actor_id"),
        taggingCompletedAt=(latest_auto_tag or {}).get("created_at"),
        taggingTagsWritten=(latest_auto_tag or {}).get("added_tags"),
        originalFilename=post.original_filename,
        type=post.type,
        mimeType=post.mime_type,
        checksum=post.checksum,
        checksumMD5=post.checksum_md5,
        fileSize=post.file_size,
        canvasWidth=post.canvas_width,
        canvasHeight=post.canvas_height,
        contentUrl=_content_url(post),
        thumbnailUrl=_thumbnail_url(post),
        flags=post.flags,
        tags=tags,
        pools=pools_micro,
        user=user,
        tagCount=len(post.tags),
        # kura#41: comment / post_note / post_feature / post_score は szurubooru
        # 由来の死表 — 実測 0 行で、書き手も存在しない (唯一の書き手だった
        # comments.py router は同 commit で削除)。以前はこの 4 つを毎ページ
        # selectinload していたので、/api/posts の 1 ページごとに「必ず空が返る
        # SELECT」が 4 本走っていた。API contract (szuru 互換 JSON) は保つため
        # フィールドは残し、値は 0 定数にする。favorited_by だけは生きている
        # (286 行) ので eager-load を残す。表そのものの drop は kura 1.0 cutover。
        commentCount=0,
        noteCount=0,
        favoriteCount=len(post.favorited_by),
        ownFavorite=own_favorite,
        featureCount=0,
        score=0,
    )


async def _latest_auto_tag_provenance(db: AsyncSession, post_id: int) -> Optional[dict]:
    """Most recent auto_tag post_tag_audit row for a post (task 4328fb0e,
    migration 0054). Single-post view only -- deliberately not batched for
    list pages (N+1 avoidance, matching _pool_post_counts' precedent).
    Explicitly scoped to source='auto_tag' (not "latest row of any source")
    so a future human-edit audit row (Phase B) can't silently make this
    look like the post was never machine-tagged -- see plan-review finding
    from primary-codex-primary, fact fb64f308."""
    row = (await db.execute(
        text(
            "SELECT actor_id, created_at, added_tags FROM post_tag_audit "
            "WHERE post_id = :pid AND source = 'auto_tag' "
            "ORDER BY created_at DESC, id DESC LIMIT 1"
        ),
        {"pid": post_id},
    )).mappings().one_or_none()
    return dict(row) if row else None


async def _pool_post_counts(db: AsyncSession, posts: list[Post]) -> dict[int, int]:
    """Post counts for every pool referenced by `posts`, computed with a single
    GROUP BY over pool_post. Lets serialization report MicroPool.postCount without
    eager-loading Pool._posts (the full membership) — N integers instead of
    thousands of ORM rows per page."""
    pool_ids = {
        pp.pool_id
        for p in posts
        for pp in (p.__dict__.get("_pools") or [])
    }
    if not pool_ids:
        return {}
    rows = (await db.execute(
        select(PoolPost.pool_id, func.count())
        .where(PoolPost.pool_id.in_(pool_ids))
        .group_by(PoolPost.pool_id)
    )).all()
    return {pid: cnt for pid, cnt in rows}


# ── Safe mode ────────────────────────────────────────────────────────────
# A browsing preference (NOT auth): when on, every read path shows ONLY posts
# whose safety is explicitly "safe". Anything not-safe OR unassessed is hidden.
# Driven by the `kura_safe=1` cookie (sent by the browser to same-origin /api
# reads) or an explicit `safe=1` query param (used by the SSR layer, which makes
# server-to-server calls that don't carry the browser cookie). Enforcing here —
# the single chokepoint every read funnels through — guarantees the grid, the
# manga/lightbox nearby strip, pool views and direct /post/{id} all agree on the
# same filtered set, so click target and shown set never diverge.
def safe_mode(request: Request) -> bool:
    return (
        request.cookies.get("kura_safe") == "1"
        or request.query_params.get("safe") == "1"
    )


# ── 総件数の高速経路 (2026-09-05, user 報告「kura が重い、特にタグ一括編集時」) ──
# /api/posts は毎ページで検索条件の**全件**に count(*) + sum(file_size) を掛けて
# いた。実測 (post 117 万件): 1girl (93 万件一致) で **598ms**、これが HTTP 時間の
# 90% 以上。投稿 40 件の取得自体は 40ms なので、待ち時間はほぼ全部この集計だった。
# しかもコーパスとタグの人気に比例して伸び続け、shared_buffers を 8GB にして
# ディスク読みを 0 にしても**この経路だけは速くならない** (read=0 でも 598ms —
# 93 万行を歩く CPU 時間だから)。
#
# kura は tag ごとの post_count を trigger で維持している。単一タグ検索なら
# それを使って **O(1) で正確な件数**が出せる (実測 2.4ms)。ただし post_count は
# 「フィルタ無しの全数」なので、endpoint が掛けている 2 つの絞りを引く:
#   - status='ok' 以外 (実測 1girl で 896 件。DB 全体でも非 ok は 3,463 件しか
#     無いので、status を IN で列挙すれば ix_post_status が効いて 13.6ms。
#     `<>` や `IS DISTINCT FROM` は索引が使えず 576ms の全表走査になる)
#   - advertisement タグ付き (既定で非表示。実測 8 件、6.4ms)
# 合計 ~22ms で**近似ではなく正確な値**。「10,000+」に落とす案より情報を失わない。
#
# 適用しない場合は必ず None を返して従来の集計に落ちる (fail-closed) —
# 総件数は UI の仮想スクロール範囲 (`new Array(total)`) にも使われるので、
# 過小に見積もると 1 万件目より先へスクロールできなくなる。
FAST_TOTAL_MIN = 50_000  # これ未満は従来の集計でも 100ms 以下 (実測 46k で 94ms)
# post.status の CHECK 制約から 'ok' を除いた集合。ズレると件数が狂うので
# tests/test_fast_total_matches_slow_total.py が DB の CHECK と一致を強制する。
NON_OK_STATUSES = ("pending", "exact_dup", "near_dup", "corrupt", "lost")


async def _fast_total_single_tag(db: AsyncSession, query: str, safe: bool) -> int | None:
    """単一タグ検索の総件数を tag.post_count から導く。適用外なら None。

    適用条件 (どれか外れたら None = 従来の集計へ):
      - safe mode が off (on だと safety='safe' の絞りが増える)
      - token がちょうど 1 つ、否定でも特殊フィルタ (`:`) でも wildcard でもない
      - その token が **ちょうど 1 つの tag** に解決する
        (`sakura` は `sakura_(sakura_wars)` 等の曖昧名ハブにも一致するため)
      - そのタグに**流入する含意 (implication) が無い** — tags_search は含意の子も
        含むので、流入があると post_count より検索一致のほうが多くなる
      - token が 'advertisement' でない (既定除外の論理が反転するため)
      - post_count >= FAST_TOTAL_MIN (小さい集合は従来経路のほうが総バイト数も返せる)
    """
    if safe:
        return None
    tokens = query.split()
    if len(tokens) != 1:
        return None
    token = tokens[0]
    if token.startswith("-") or ":" in token or "*" in token:
        return None
    tl = token.lower()
    # advertisement の既定除外は endpoint 側が **部分文字列**で判定している
    # (`if "advertisement" not in query.lower()`)。ここを完全一致で書くと、
    # `advertisement` を含む別タグ (例 advertisement_poster) を検索したとき
    # endpoint は除外を掛けないのに fast path は 8 件引いて**過小**になる
    # (primary-cc-primary の指摘、2026-09-05)。同じ式を使って非対称を作らない。
    ad_excluded = "advertisement" not in query.lower()

    tag_ids = set((await db.execute(
        select(TagName.tag_id).where(or_(
            func.lower(TagName.name) == tl,
            func.lower(TagName.name).like(tl + r"\_(%", escape="\\"),
        ))
    )).scalars().all())
    if len(tag_ids) != 1:
        return None
    tag_id = next(iter(tag_ids))

    if (await db.execute(
        select(TagImplication.child_id).where(TagImplication.child_id == tag_id).limit(1)
    )).first() is not None:
        return None

    post_count = (await db.execute(
        select(Tag.post_count).where(Tag.tag_id == tag_id))).scalar()
    if post_count is None or post_count < FAST_TOTAL_MIN:
        return None

    # No implication flows into this tag (checked above), so "matches the search"
    # is exactly "has this tag" in post_tag.
    has_tag = Post.post_id.in_(select(PostTag.post_id).where(PostTag.tag_id == tag_id))
    non_ok = (await db.execute(
        select(func.count()).select_from(Post)
        .where(Post.status.in_(NON_OK_STATUSES))
        .where(has_tag))).scalar() or 0
    ad = 0
    if ad_excluded:
        ad = (await db.execute(
            select(func.count()).select_from(Post)
            .where(Post.status == "ok")
            .where(has_tag)
            .where(select(PostTag.post_id)
                   .join(TagName, TagName.tag_id == PostTag.tag_id)
                   .where(PostTag.post_id == Post.post_id,
                          TagName.name == "advertisement")
                   .exists()))).scalar() or 0
    return int(post_count) - int(non_ok) - int(ad)


def _query_identifier(token: str) -> int:
    from api.errors import KuraError

    name, value = token.split(":", 1)
    try:
        return int(value)
    except ValueError as exc:
        raise KuraError(
            422, "invalid_search_identifier", context={"filter": name, "value": value},
            fix=f"{name}: には整数を1つ指定してください（例: {name}:123）。"
                "複数IDのカンマ区切りは使えません。",
        ) from exc


async def _build_where_clauses(db: AsyncSession, query: str) -> tuple[list, int | None]:
    """Parse query tokens into (WHERE clauses, pool_id or None).

    A leading '-' on any token negates the resulting clause. The '-' prefix
    is peeled off here before the token is dispatched so that every filter
    branch (md5/id/type/pool/source/safety/fav/tag) supports negation
    consistently.
    """
    clauses = []
    pool_id_filter: int | None = None
    tokens = []
    for raw_token in query.split():
        negated = raw_token.startswith("-")
        token = raw_token[1:] if negated else raw_token
        # `tag:x` は裸の `x` と同義 — pool mode と語彙を揃えるため (2026-09-05)。
        # 揃えないと、pool mode で `tag:` を付けた query を post mode に切り替えた
        # 瞬間に「`tag:x` という名前のタグ」を探して 0 件になる。
        if token.startswith("tag:"):
            token = token[4:]
        if not token:
            continue
        tokens.append((negated, token))
    # Validate the whole query before any filter resolves names through the DB.
    # A malformed identifier must not depend on preceding filter order.
    identifiers = {
        token: _query_identifier(token) for _, token in tokens
        if token.startswith(("id:", "pool:"))
    }
    for negated, token in tokens:
        clause = None
        if token.startswith("md5:"):
            clause = Post.checksum_md5 == token[4:]
        elif token.startswith("id:"):
            clause = Post.post_id == identifiers[token]
        elif token.startswith("type:"):
            value = token[5:]
            clause = Post.type == TYPE_ALIASES.get(value, value)
        elif token.startswith("pool:"):
            pool_id = identifiers[token]
            # Only set the special "list pool members in order" hint for
            # positive filters — negative pool filter is just exclusion.
            if not negated:
                pool_id_filter = pool_id
            pool_filter = select(PoolPost.post_id).where(PoolPost.pool_id == pool_id)
            clause = Post.post_id.in_(pool_filter)
        elif token.startswith("source:"):
            clause = Post.source.ilike(f"%{token[7:]}%")
        elif token.startswith("safety:"):
            clause = Post.safety == token[7:]
        elif token.startswith("locked:") or token.startswith("removed:"):
            # locked:<tag> = 施錠済み (present pin) / removed:<tag> = 人間解除済み
            # (absent pin)。'-' 前置の否定は他トークン同様に効く (-locked:x = 未施錠)。
            prefix, _, tname = token.partition(":")
            state = "present" if prefix == "locked" else "absent"
            pin_tag_id = (
                await db.execute(select(TagName.tag_id).where(func.lower(TagName.name) == tname.lower()))
            ).scalars().first()
            if pin_tag_id is None:
                clause = false()   # 未知タグ = 0 件 (負の場合は全件)
            else:
                clause = (
                    select(_PIN_T.c.post_id)
                    .where(_PIN_T.c.post_id == Post.post_id,
                           _PIN_T.c.tag_id == pin_tag_id,
                           _PIN_T.c.state == state)
                    .exists()
                )
        elif token == "fav:true":
            fav_filter = select(PostFavorite.post_id).where(PostFavorite.post_id == Post.post_id)
            clause = fav_filter.exists()
        elif token == "fav:false":
            fav_filter = select(PostFavorite.post_id).where(PostFavorite.post_id == Post.post_id)
            clause = ~fav_filter.exists()
        else:
            # Plain token = tag name filter.
            # ★ マッチする tag_id を先に Python 側で解決し、post_tag を
            #   `tag_id IN (リテラル)` で引く。OR-LIKE を相関 EXISTS の中に置くと
            #   プランナが選択性を見積れず post_tag_pkey を降順に全舐めして遅くなる
            #   (レアタグで顕著)。リテラル IN なら tag_id 先頭の索引 (ix_post_tag_tag_post) を使い高速。
            is_wildcard = "*" in token
            if is_wildcard:
                tag_cond = TagName.name.ilike(token.replace("*", "%"))
            else:
                # 完全一致 OR 曖昧名ハブ: `sakura` は `sakura_(sakura_wars)` 等の
                # 曖昧化(disambiguation)形にも一致 (`token\_(%`、`_` は escape)。
                tl = token.lower()
                tag_cond = or_(
                    func.lower(TagName.name) == tl,
                    func.lower(TagName.name).like(tl + r"\_(%", escape="\\"),
                )
            tag_ids = (
                await db.execute(select(TagName.tag_id).where(tag_cond))
            ).scalars().all()
            if tag_ids:
                # Membership is read from post_tag by integer id (no denormalized
                # name arrays; docs/notes/tag-membership-single-source.md). The
                # match keeps the old tags_search meaning: the tag itself, or a
                # tag that directly implies it (tag_implication parent -> child).
                implied_by = (
                    await db.execute(
                        select(TagImplication.parent_id)
                        .where(TagImplication.child_id.in_(tag_ids))
                    )
                ).scalars().all()
                # Inlined as SQL literals (ids come from the database): a bound list
                # would let a cached generic plan ignore how rare the tag is.
                match_ids = [literal_column(str(int(i))) for i in sorted(set(tag_ids) | set(implied_by))]
                # Literal ids: the planner drives from post_tag (tag_id, post_id) in
                # id order for rare tags and probes it for common ones. Measured on
                # production 2026-10-24: 0.5-6.6 ms single tag, 0.5 ms for a 73-id
                # implication set (GIN overlap: 68 ms), 2.4 ms for AND (GIN: 40 ms).
                members = select(PostTag.post_id).where(PostTag.tag_id.in_(match_ids))
                if negated:
                    # NOT EXISTS, not NOT IN: an untagged post matches `-tag`.
                    clause = select(PostTag.post_id).where(
                        PostTag.post_id == Post.post_id, PostTag.tag_id.in_(match_ids)).exists()
                else:
                    clause = Post.post_id.in_(members)
            else:
                clause = false()  # 一致タグ無し → この語にマッチする投稿は無い
        if clause is None:
            continue
        clauses.append(~clause if negated else clause)
    return clauses, pool_id_filter


def post_list_options(localize: bool):
    """Only the graph consumed by PostResponse; never traverse parent backrefs."""
    tags = selectinload(Post.tags).load_only(Tag.tag_id, Tag.post_count, raiseload=True)
    memberships = selectinload(Post._pools)
    pools = memberships.selectinload(PoolPost.pool).load_only(
        Pool.pool_id, Pool.description, raiseload=True)
    return (
        tags.raiseload("*"),
        tags.selectinload(Tag.names),
        tags.selectinload(Tag.category),
        tags.selectinload(Tag.localizations) if localize else tags.raiseload(Tag.localizations),
        selectinload(Post.user).load_only(User.name, raiseload=True),
        selectinload(Post.favorited_by),
        memberships.raiseload(PoolPost.post),
        pools.raiseload("*"),
        pools.selectinload(Pool.names),
        pools.selectinload(Pool.category),
    )


@router.get("/posts")
async def list_posts(
    localize: bool = False,
    query: str = Query("", alias="query"),
    offset: int = Query(0),
    limit: int = Query(40),
    sort: str = Query("", description="id|updated — override default order"),
    dir: str = Query("desc", pattern="^(asc|desc)$"),
    db: AsyncSession = Depends(get_db),
    auth_user: Optional[User] = Depends(get_optional_user),
    safe: bool = Depends(safe_mode),
) -> PagedResult[PostResponse]:
    limit = min(limit, 1000)

    base_stmt, pool_id_filter = await _listing_set(db, query, safe)

    # 単一タグ検索は post_count から正確な件数を O(1) で導く (上の docstring)。
    # totalBytes は post_count からは出せないので 0 を返す —
    # GridControls.svelte は sizeBytes=0 のとき容量表示を隠す作りなので、
    # UI 改修なしで「件数は正確・容量は非表示」に落ちる。小さい集合 (< 5 万) は
    # この経路を使わないので、そちらでは容量表示が残る。
    _fast = await _fast_total_single_tag(db, query, safe) if query else None
    if not query.strip():
        from api.read_models.post_listing import totals
        total, total_bytes = await totals(db,safe)
    elif _fast is not None:
        total, total_bytes = _fast, 0
    else:
        _sq = base_stmt.subquery()
        _agg = (await db.execute(
            select(func.count(), func.coalesce(func.sum(_sq.c.file_size), 0)).select_from(_sq)
        )).first()
        total = _agg[0] or 0
        total_bytes = int(_agg[1] or 0)

    base_stmt, keys, ascending = _listing_order(base_stmt, pool_id_filter, sort, dir)
    order_by = tuple(k.asc() if ascending else k.desc() for k in keys)

    stmt = (
        base_stmt
        .options(*post_list_options(localize))
        .order_by(*order_by)
        .offset(offset)
        .limit(limit)
    )

    result = await db.execute(stmt)
    posts = result.scalars().unique().all()
    pool_counts = await _pool_post_counts(db, posts)

    return PagedResult(
        query=query,
        offset=offset,
        limit=limit,
        total=total,
        totalBytes=total_bytes,
        results=[_serialize_post(p, auth_user, pool_counts, localize=localize) for p in posts],
    )


async def _listing_set(db: AsyncSession, query: str, safe: bool):
    """The posts a listing shows, before any ordering: list_posts and get_post_index both
    take their set from here, so the index counts exactly what the grid shows (#148)."""
    where_clauses, pool_id_filter = (await _build_where_clauses(db, query)) if query else ([], None)
    if safe:
        # AND safety=='safe' onto whatever the query asked for. It can only
        # narrow, so a stray `safety:unsafe` token cannot widen past the gate.
        where_clauses.append(Post.safety == "safe")

    # pg-native (Phase 4b): browse only the displayable curated set. near_dup /
    # exact_dup / corrupt / lost / not-yet-materialized rows are hidden (they have
    # no post.data). Matches the post_displayable view used by the mockup.
    base_stmt = select(Post).where(Post.status == "ok")
    for clause in where_clauses:
        base_stmt = base_stmt.where(clause)

    # user 報告 (2026-07-23, pool 822 / post 289615): グラビア/宣伝チャート等、
    # 本編と無関係な広告ページが混入している。`advertisement` タグを新設し、
    # 明示的に検索されない限りデフォルト非表示にする(削除ではなくフィルタ —
    # post は残る。他タグの通常のブロックリストと同じ「明示検索すれば出る」
    # パターン)。
    if "advertisement" not in query.lower():
        _ad_exists = (
            select(PostTag.post_id)
            .join(TagName, TagName.tag_id == PostTag.tag_id)
            .where(PostTag.post_id == Post.post_id, TagName.name == "advertisement")
        )
        base_stmt = base_stmt.where(~_ad_exists.exists())
    return base_stmt, pool_id_filter


def _listing_order(base_stmt, pool_id_filter, sort: str, dir: str):
    """A listing's total order: (statement with any join it needs, sort keys, ascending).
    Every order ends in post_id and runs all its keys the same way, so a position is the
    count of rows whose key tuple comes before the post's — get_post_index uses exactly
    this order, ties included (#148).

    Explicit sort override (shared grid toolbar) wins; otherwise pool:N uses
    PoolPost.order (page/reading order) and a plain list uses post_id desc."""
    ascending = dir == "asc"

    # Join the unique (pool_id, post_id) membership only for page ordering.
    # This lets the existing pool/ord index stop at LIMIT instead of evaluating
    # a correlated ordinal subquery for every pool member before sorting.
    # Keep the membership filters above: multiple/negated pool tokens still AND.
    # order_by is always a tuple (.order_by(*order_by) below) — even the
    # single-column branches, so every branch shares one deterministic-
    # pagination discipline (see the pool branches' comment for why this
    # matters: a bare non-unique sort key makes LIMIT/OFFSET pagination
    # order-unstable for ties across separate query executions).
    if sort == "id":
        return base_stmt, (Post.post_id,), ascending
    if sort == "updated":
        return base_stmt, (func.coalesce(Post.last_edit_time, Post.creation_time), Post.post_id), ascending
    if sort == "page" and pool_id_filter is not None:
        # Explicit page sort — same column as the pool default below, but dir
        # is honoured (the grid's ページ↑↓ toggle used to be a silent no-op:
        # the client omitted sort=page and the default branch ignores dir).
        # order_col alone is NOT a stable sort key — pool_post.ord commonly
        # has ties (実測 pool 7522: 48 組の重複 ord, 3〜4件ずつ). Postgres
        # does not guarantee a fixed order for ties across separate query
        # executions, so OFFSET/LIMIT pagination over ties is non-deterministic:
        # the same post can land in two overlapping pages (client-side
        # duplicate id -> Svelte each_key_duplicate) while another tied post
        # is skipped by both (renders as a permanently-null slot — "stops
        # loading thumbnails partway through scrolling", user 報告
        # 2026-07-20). post_id as a secondary key breaks every tie
        # deterministically without changing any row's *visible* order
        # relative to distinctly-ordered rows.
        base_stmt = base_stmt.join(PoolPost, (PoolPost.post_id == Post.post_id) & (PoolPost.pool_id == pool_id_filter))
        return base_stmt, (PoolPost.order, Post.post_id), ascending
    if pool_id_filter is not None:
        base_stmt = base_stmt.join(PoolPost, (PoolPost.post_id == Post.post_id) & (PoolPost.pool_id == pool_id_filter))
        return base_stmt, (PoolPost.order, Post.post_id), True
    return base_stmt, (Post.post_id,), False


@router.get("/post/{post_id}/index")
async def get_post_index(
    post_id: int,
    query: str = Query("", alias="query"),
    sort: str = Query("", description="id|updated|page — must match the grid's sort"),
    dir: str = Query("desc", pattern="^(asc|desc)$"),
    db: AsyncSession = Depends(get_db),
    safe: bool = Depends(safe_mode),
) -> dict:
    """Return the 0-based position of a post in the given query result.

    Ordering must match list_posts INCLUDING the sort/dir override — the grid's
    toolbar sort travels with the viewer URL, and an index computed against the
    default order lands the viewer ±N pages off (「開く位置がずれる」).
    Safe mode must filter the SAME way list_posts does, or the index points into a
    different set than the grid and the viewer opens on the wrong neighbours.
    """
    base_stmt, pool_id_filter = await _listing_set(db, query, safe)
    stmt, keys, ascending = _listing_order(base_stmt, pool_id_filter, sort, dir)
    target = (await db.execute(
        stmt.with_only_columns(*keys).where(Post.post_id == post_id).limit(1))).first()
    if target is None:
        # Not in this listing (hidden, filtered out, another pool): there is no position
        # to give. idx 0 keeps old clients working; `member` says it is not one.
        return {"idx": 0, "member": False}
    before = tuple_(*keys) < tuple_(*target) if ascending else tuple_(*keys) > tuple_(*target)
    count = (await db.execute(
        select(func.count()).select_from(stmt.with_only_columns(Post.post_id).where(before).subquery())
    )).scalar() or 0
    return {"idx": count, "member": True}


@router.get("/post/{post_id}/media")
async def get_post_media_redirect(
    post_id: int,
    w: Optional[int] = Query(None, description="Width hint (ignored; redirects to original)"),
    db: AsyncSession = Depends(get_db),
):
    """Compatibility shim for old kura-web builds that reference
    /api/post/{id}/media?w=1080. Resolves to /data/posts/{id}_{hash}.{ext}
    via 307 redirect so SW cache stores the canonical data URL.
    """
    stmt = select(Post).where(Post.post_id == post_id)
    result = await db.execute(stmt)
    post = result.scalar_one_or_none()
    if post is None:
        raise KuraError(404, "post_not_found", fix="Check the post id: the post does not exist, was deleted, or is hidden in safe mode.")
    return RedirectResponse(_content_url(post), status_code=307)


@router.get("/post/{post_id}/content")
async def get_post_content(post_id: int, request: Request, db: AsyncSession = Depends(get_db)):
    """pg-native (Phase 4b): serve the post's WebP bytes from PG (media_location=
    'pg'). For videos (media_location='hdd', data NULL) the body lives on the HDD/FS,
    so 307 to the legacy file URL. Falls back to FS for any not-yet-materialized row.

    kura#50 R2'': on a replica_redirect node this handler never touches the DB —
    local post.data is a copy of a cache and may be stale or NULL; the authority
    answers everything, 404s included."""
    if media_role.MEDIA_ROLE == "replica_redirect":
        return RedirectResponse(
            media_role.redirect_target(f"/api/post/{post_id}/content"), status_code=307)
    post = (await db.execute(select(Post).where(Post.post_id == post_id))).scalar_one_or_none()
    if post is None:
        raise KuraError(404, "post_not_found", fix="Check the post id: the post does not exist, was deleted, or is hidden in safe mode.")
    # Rendition tables are canonical-only: a replica has neither the rows nor the files.
    if media_role.MEDIA_ROLE == "authority":
        stored = await renditions.response_for(db, request, post_id, "view")
        if stored is not None:
            return stored
    if post.media_location == "pg":
        # Post.data is deferred (excluded from select(Post)); fetch just this one
        # row's blob explicitly so only the single image being served is in memory.
        data = (await db.execute(
            select(Post.data).where(Post.post_id == post_id)
        )).scalar_one_or_none()
        if data is not None:
            media_role.note_local_media_serve(
                "content", post_id, request.headers.get("x-kura-census-probe"))
            return Response(bytes(data), media_type="image/webp",
                            headers={"Cache-Control": "public, max-age=86400"})
    return RedirectResponse(_legacy_fs_content_url(post), status_code=307)


@router.get("/post/{post_id}/thumbnail")
async def get_post_thumbnail(post_id: int, request: Request, db: AsyncSession = Depends(get_db)):
    if media_role.MEDIA_ROLE == "replica_redirect":
        return RedirectResponse(
            media_role.redirect_target(f"/api/post/{post_id}/thumbnail"), status_code=307)
    post = (await db.execute(select(Post).where(Post.post_id == post_id))).scalar_one_or_none()
    if post is None:
        raise KuraError(404, "post_not_found", fix="Check the post id: the post does not exist, was deleted, or is hidden in safe mode.")
    # Rendition tables are canonical-only: a replica has neither the rows nor the files.
    if media_role.MEDIA_ROLE == "authority":
        stored = await renditions.response_for(db, request, post_id, "thumb")
        if stored is not None:
            return stored
    # Post.thumbnail is deferred; fetch just this row's blob explicitly.
    thumb = (await db.execute(
        select(Post.thumbnail).where(Post.post_id == post_id)
    )).scalar_one_or_none()
    if thumb is not None:
        media_role.note_local_media_serve(
            "thumbnail", post_id, request.headers.get("x-kura-census-probe"))
        return Response(bytes(thumb), media_type="image/webp",
                        headers={"Cache-Control": "public, max-age=86400"})
    # kura#56: a 307 to a file that is not there is a lie followed by a 404 on the
    # second hop — invisible to the redirect's caller, surfacing only as a blank
    # image. Measured 2026-09-05: 1,691 rows 307'd to a missing FS thumbnail, none
    # regenerable (no PG data, no FS original — 1,669 status=corrupt images, 22
    # documents). So look before pointing: if the FS thumbnail is not there, say
    # 404 here instead of redirecting to one.
    if not files.exists(files.names("thumbnail", post.post_id)[0]):
        raise KuraError(404, "thumbnail_not_found", fix="Regenerate the thumbnail, or wait for the media backfill to write it.")
    return RedirectResponse(_legacy_fs_thumbnail_url(post), status_code=307)


@router.get("/post-pin-states")
async def get_post_pin_states(
    ids: str = Query(...),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """一括編集ビュー用: post id 群の全 pin をタグ名付きでまとめて返す
    (user 2026-08-02「同一カテゴリのほかのタグがどうなっているかも確認できる」
    — 帯内の全タグ末尾に鍵を出すため、target 限定でなく post の全 pin)。
    返値 states: {post_id: {tag_name: 'present'|'absent'}}。pin は疎 (人間が
    明示判定したタグのみ) なので全量でも小さい。"""
    try:
        id_list = [int(x) for x in ids.split(",") if x.strip()][:500]
    except ValueError:
        raise KuraError(422, "invalid_ids", fix="Send ids as comma-separated integers, e.g. ?ids=1,2,3.")
    if not id_list:
        return {"states": {}}
    rows = await db.execute(text(
        "SELECT ptp.post_id, tn.name, ptp.state FROM post_tag_pin ptp "
        "JOIN tag_name tn ON tn.tag_id = ptp.tag_id AND tn.ord = 0 "
        "WHERE ptp.post_id = ANY(:ids)"),
        {"ids": id_list})
    out: dict[int, dict[str, str]] = {}
    for pid, name, state in rows.all():
        out.setdefault(pid, {})[name] = state
    return {"states": out}


@router.put("/post/{post_id}/tag-pin")
async def put_tag_pin(
    post_id: int,
    body: dict = Body(...),
    db: AsyncSession = Depends(get_db),
    auth_user: User = Depends(get_current_user),
) -> dict:
    """施錠/解錠 (user 2026-08-02): タグを変更せずに人間の明示判定を pin する。

    state='present' = 施錠 — このタグは正しいという明示宣言。post_tag_pin に
    state=present を upsert し、01_post_tag_pin_guard_ins trigger がタガーの
    上書きを拒否、gt_sync の pin sync (migration 0121) が pool-free の
    positive GT (human_correction) に変換する。タグが post に付いていることが
    前提 (無いタグの肯定は不整合なので 409)。
    state=null = 解錠 — pin 行を削除し「意見なし」に戻す。整合性不変条件
    (user 2026-08-02「スキーマに矛盾が出ないように」): pool-free の
    human_correction GT 行は pin の写像なので、pin を消すときは同じ (tag,
    post) の pool-free human_correction GT も撤回する — 残すと「意見なし」
    なのに GT が主張し続ける孤児になる。pool 由来 GT (source_pool_id あり)
    と implicit_review はこの endpoint の管轄外で触らない。
    削除アイコン側は既存の人間タグ編集 (PUT /post/{id} の tags 差分) がそのまま
    absent pin + negative GT になるので、この endpoint は present 専用。"""
    tag_name = (body.get("tag") or "").strip()
    state = body.get("state", "present")
    if not tag_name:
        raise KuraError(422, "tag_required", fix="Send the tag name in the body's `tag` field.")
    if state not in ("present", None):
        raise KuraError(422, "invalid_pin_state", context={"allowed": ["present", None]}, fix="Send state 'present' to lock the tag, or null to unlock it.")

    tag_id = (await db.execute(text(
        "SELECT tag_id FROM tag_name WHERE name = :name LIMIT 1"), {"name": tag_name}
    )).scalar_one_or_none()
    if tag_id is None:
        raise KuraError(404, "tag_not_found", context={"tag": tag_name}, fix="Check the spelling, or create the tag first.")

    if state == "present":
        on_post = (await db.execute(text(
            "SELECT 1 FROM post_tag WHERE post_id = :pid AND tag_id = :tid"),
            {"pid": post_id, "tid": tag_id})).scalar_one_or_none()
        if on_post is None:
            raise KuraError(409, "tag_not_on_post", context={"tag": tag_name, "post_id": post_id},
                            fix="施錠は付いているタグにのみ可能です。先にタグを付けてください。")
        await db.execute(text(
            "INSERT INTO post_tag_pin (post_id, tag_id, state, pinned_by, audit_id) "
            "VALUES (:pid, :tid, 'present', :by, NULL) "
            "ON CONFLICT (post_id, tag_id) DO UPDATE SET "
            "state = 'present', pinned_by = EXCLUDED.pinned_by, "
            "audit_id = NULL, pinned_at = now()"),
            {"pid": post_id, "tid": tag_id, "by": auth_user.name})
    else:
        await db.execute(text(
            "DELETE FROM post_tag_pin WHERE post_id = :pid AND tag_id = :tid"),
            {"pid": post_id, "tid": tag_id})
        # 不変条件: pool-free human_correction GT = 現存 pin の写像。pin を
        # 消したら pin 由来 GT も撤回 (pool 由来/implicit は対象外)。直接
        # DELETE は trg_tagger_gt_label_governed が拒否する (BUG-2, bc9787f8)
        # ため governed 関数 (0122, OWNER=kura_reconciler) 経由。
        await db.execute(text(
            "SELECT tagger_governed_retract_pin_gt(:tag, :pid)"),
            {"tag": tag_name, "pid": post_id})
    await db.commit()
    return {"post_id": post_id, "tag": tag_name, "pinned": state}


@router.put("/posts/tag-pin-bulk")
async def put_tag_pin_bulk(
    body: dict = Body(...),
    db: AsyncSession = Depends(get_db),
    auth_user: User = Depends(get_current_user),
) -> dict:
    """一括施錠/一括解錠 (user 2026-08-02「一括施錠に来歴を残す」+ undo)。

    単発 PUT /post/{id}/tag-pin と同じ意味論を N 件に適用するが、2 点強い:
    - 来歴: 各 pin に post_tag_audit 行 (source='bulk_lock:<tag>') を紐づける。
      2026-08-02 の 109 件誤施錠はタイムスタンプ相関でしか特定できなかった —
      以後は audit 1 クエリで「いつ・誰が・何件」が引ける。
    - 原子性: 1 リクエスト。クライアントの N 連打 (旧 lockSelected) を置換。
    state='present' は付いているタグのみ対象 (無いものは skipped に計上、409 に
    しない — 一括では部分成立が正)。state=null は pin 削除 + pin 由来 GT 撤回
    (単発と同じ governed 関数)。undo は同 endpoint の state=null で行う。"""
    tag_name = (body.get("tag") or "").strip()
    state = body.get("state", "present")
    post_ids = body.get("post_ids") or []
    if not tag_name:
        raise KuraError(422, "tag_required", fix="Send the tag name in the body's `tag` field.")
    if state not in ("present", None):
        raise KuraError(422, "invalid_pin_state", context={"allowed": ["present", None]}, fix="Send state 'present' to lock the tag, or null to unlock it.")
    if not isinstance(post_ids, list) or not post_ids or len(post_ids) > 1000:
        raise KuraError(422, "invalid_post_ids", context={"max": 1000}, fix="Send post_ids as a non-empty list of at most 1000 ids; split larger batches.")
    post_ids = [int(i) for i in post_ids]

    tag_id = (await db.execute(text(
        "SELECT tag_id FROM tag_name WHERE name = :name LIMIT 1"), {"name": tag_name}
    )).scalar_one_or_none()
    if tag_id is None:
        raise KuraError(404, "tag_not_found", context={"tag": tag_name}, fix="Check the spelling, or create the tag first.")

    if state == "present":
        tagged = {r[0] for r in (await db.execute(text(
            "SELECT post_id FROM post_tag WHERE tag_id = :tid AND post_id = ANY(:ids)"),
            {"tid": tag_id, "ids": post_ids})).all()}
        done = 0
        for pid in post_ids:
            if pid not in tagged:
                continue
            audit_id = (await db.execute(text(
                "INSERT INTO post_tag_audit "
                "(post_id, actor_type, actor_id, source, added_tags, removed_tags) "
                "VALUES (:pid, 'human', :actor, :src, '{}', '{}') RETURNING id"),
                {"pid": pid, "actor": auth_user.name,
                 "src": f"bulk_lock:{tag_name}"})).scalar_one()
            await db.execute(text(
                "INSERT INTO post_tag_pin (post_id, tag_id, state, pinned_by, audit_id) "
                "VALUES (:pid, :tid, 'present', :by, :aid) "
                "ON CONFLICT (post_id, tag_id) DO UPDATE SET "
                "state = 'present', pinned_by = EXCLUDED.pinned_by, "
                "audit_id = EXCLUDED.audit_id, pinned_at = now()"),
                {"pid": pid, "tid": tag_id, "by": auth_user.name, "aid": audit_id})
            done += 1
        await db.commit()
        return {"tag": tag_name, "locked": done, "skipped": len(post_ids) - done}

    done = 0
    for pid in post_ids:
        n = (await db.execute(text(
            "DELETE FROM post_tag_pin WHERE post_id = :pid AND tag_id = :tid"),
            {"pid": pid, "tid": tag_id})).rowcount
        if n:
            await db.execute(text(
                "SELECT tagger_governed_retract_pin_gt(:tag, :pid)"),
                {"tag": tag_name, "pid": pid})
            done += 1
    await db.commit()
    return {"tag": tag_name, "unlocked": done, "skipped": len(post_ids) - done}


@router.put("/posts/tag-remove-bulk")
async def put_tag_remove_bulk(
    body: dict = Body(...),
    db: AsyncSession = Depends(get_db),
    auth_user: User = Depends(get_current_user),
) -> dict:
    """1 つのタグを N 件の post から外す — **1 リクエストで**。

    2026-09-06、user 報告「過検知を検索して解除しているが、一括選択から削除に
    時間がかかる」。本番のアクセスログで実測したところ、20 分で PATCH 891 件、
    適用ループ内の間隔は**中央値 101 ms** で、これは PATCH 1 件の往復時間
    (実測 73-98 ms) そのものだった。つまり遅いのは DB でもクエリでもなく
    (個別クエリは sub-ms、index も揃っている)、**選択件数ぶんの HTTP 往復を
    1 件ずつ待っている**という構造だった (web/src/lib/bulkTag.ts の直列 for-await)。

    PATCH /post/{id} との違い:

    - **往復が N 回から 1 回**。500 件で 50 秒 → 数秒
    - **他人の編集を踏まない**。PATCH は post の全タグを read-modify-write で
      受け取るので、並行編集を丸ごと上書きしうる (だから version 照合が要る)。
      こちらは (post, tag) 1 組だけを狙って外すので、その事故が起こりえない
    - version 照合を持たない。上の理由で不要だから — ただし post.version は
      進める (他 client の楽観ロックが「変わった」と気づけるように)

    意味論は単発 PATCH の削除経路と同じに揃える。順序も同じ:

      1. `kura.tag_actor='human'` (これが無いと pin guard が machine 扱いする)
      2. post_tag から削除
      3. post_tag_audit を 1 post 1 行
      4. post_tag_pin を state='absent' で upsert (audit_id 付き)
      5. post.version += 1 / last_edit_time

    タグを持たない post は skipped に数え、404 にしない (一括では部分成立が正 —
    tag-pin-bulk と同じ判断)。"""
    tag_name = (body.get("tag") or "").strip()
    post_ids = body.get("post_ids") or []
    if not tag_name:
        raise KuraError(422, "tag_required", fix="Send the tag name in the body's `tag` field.")
    if not isinstance(post_ids, list) or not post_ids or len(post_ids) > 1000:
        raise KuraError(422, "invalid_post_ids", context={"max": 1000}, fix="Send post_ids as a non-empty list of at most 1000 ids; split larger batches.")
    post_ids = [int(i) for i in post_ids]

    tag_id = (await db.execute(text(
        "SELECT tag_id FROM tag_name WHERE name = :name AND ord = 0 LIMIT 1"),
        {"name": tag_name})).scalar_one_or_none()
    if tag_id is None:
        raise KuraError(404, "tag_not_found", context={"tag": tag_name}, fix="Check the spelling, or create the tag first.")

    # 単発 PATCH と同じ理由でトランザクション局所の GUC を先に置く: これが無いと
    # 01_post_tag_pin_guard_del が「machine が human のタグを消そうとしている」と
    # 読んで削除を黙って落とす。
    await db.execute(text("SELECT set_config('kura.tag_actor', 'human', true)"))

    targets = [r[0] for r in (await db.execute(text(
        "SELECT post_id FROM post_tag WHERE tag_id = :tid AND post_id = ANY(:ids)"),
        {"tid": tag_id, "ids": post_ids})).all()]
    if not targets:
        return {"tag": tag_name, "removed": 0, "skipped": len(post_ids)}

    await db.execute(text(
        "DELETE FROM post_tag WHERE tag_id = :tid AND post_id = ANY(:ids)"),
        {"tid": tag_id, "ids": targets})

    # audit と pin を 1 文で。pin は audit の id を要るので CTE で受け渡す —
    # post ごとに 2 往復していた tag-pin-bulk の形を、ここでは繰り返さない。
    await db.execute(text(
        "WITH aud AS ("
        "  INSERT INTO post_tag_audit "
        "  (post_id, actor_type, actor_id, source, added_tags, removed_tags) "
        "  SELECT pid, 'human', :actor, :src, '{}', ARRAY[:tag]::text[] "
        "  FROM unnest(CAST(:ids AS int[])) AS pid "
        "  RETURNING id, post_id"
        ") "
        "INSERT INTO post_tag_pin (post_id, tag_id, state, pinned_by, audit_id) "
        "SELECT aud.post_id, :tid, 'absent', :by, aud.id FROM aud "
        "ON CONFLICT (post_id, tag_id) DO UPDATE SET "
        "  state = 'absent', pinned_by = EXCLUDED.pinned_by, "
        "  audit_id = EXCLUDED.audit_id, pinned_at = now()"),
        {"ids": targets, "actor": auth_user.name, "src": f"bulk_remove:{tag_name}",
         "tag": tag_name, "tid": tag_id, "by": auth_user.name})

    # 楽観ロックを持つ client に「変わった」と分かる形で返す。
    await db.execute(text(
        "UPDATE post SET version = version + 1, last_edit_time = now() "
        "WHERE id = ANY(:ids)"), {"ids": targets})

    await db.commit()
    return {"tag": tag_name, "removed": len(targets),
            "skipped": len(post_ids) - len(targets)}


@router.get("/post/{post_id}")
async def get_post(
    post_id: int,
    localize: bool = False,
    db: AsyncSession = Depends(get_db),
    auth_user: Optional[User] = Depends(get_optional_user),
    safe: bool = Depends(safe_mode),
) -> PostResponse:
    stmt = (
        select(Post)
        .where(Post.post_id == post_id)
        .options(
            selectinload(Post.tags).selectinload(Tag.names),
            selectinload(Post.tags).selectinload(Tag.category),
            selectinload(Post.user),
            selectinload(Post.favorited_by),
            # Pool memberships — needed by _serialize_post.pools_micro.
            # Without these the relationship triggers a lazy load in async
            # context and silently falls through the try/except. Pool._posts is
            # NOT loaded here; the membership count comes from _pool_post_counts.
            selectinload(Post._pools).selectinload(PoolPost.pool).selectinload(Pool.names),
            selectinload(Post._pools).selectinload(PoolPost.pool).selectinload(Pool.category),
        )
    )
    result = await db.execute(stmt)
    post = result.scalar_one_or_none()
    if post is None:
        raise KuraError(404, "post_not_found", fix="Check the post id: the post does not exist, was deleted, or is hidden in safe mode.")
    if safe and post.safety != "safe":
        # Hidden in safe mode — same 404 as "not found" so safe mode never
        # confirms a non-safe post exists at this id.
        raise KuraError(404, "post_not_found", fix="Check the post id: the post does not exist, was deleted, or is hidden in safe mode.")
    pool_counts = await _pool_post_counts(db, [post])
    latest_tagging = await _latest_auto_tag_provenance(db, post_id)
    pin_rows = await db.execute(text(
        "SELECT tag_id, state FROM post_tag_pin WHERE post_id = :pid"), {"pid": post_id})
    pin_states = {tid: state for tid, state in pin_rows.all()}
    return _serialize_post(post, auth_user, pool_counts, latest_tagging, pin_states, localize=localize)


async def _get_or_create_tag(db: AsyncSession, name: str) -> Tag:
    """Return existing tag by primary name, or create a new one.

    Names are lowercased to match the canonical normalization in
    services.tags.get_or_create_tags — tag_name.name is case-sensitive UNIQUE,
    so mixing "Foo"/"foo" silently fragments a tag into two rows.
    """
    name = name.lower()
    result = await db.execute(
        select(Tag).join(TagName, Tag.tag_id == TagName.tag_id).where(TagName.name == name)
    )
    tag = result.unique().scalar_one_or_none()
    if tag is not None:
        return tag

    # Get default category
    cat_result = await db.execute(
        select(TagCategory).where(TagCategory.default.is_(True)).limit(1)
    )
    category = cat_result.scalar_one_or_none()
    if category is None:
        cat_result2 = await db.execute(select(TagCategory).limit(1))
        category = cat_result2.scalar_one_or_none()
    if category is None:
        raise KuraError(500, "no_tag_category", fix="Create a tag category (the default is 'general') before adding tags.")

    # **作成は services.tags に寄せる** (2026-07-30)。ここで直に INSERT していた版は
    # 並行実行で tag_name_name_key の UniqueViolation を出し、session を汚して
    # 後続 query を PendingRollbackError にしていた。SAVEPOINT の中で作れば
    # 衝突しても session は生き残る。
    tag = await create_tag_named(db, name, category)
    if tag is None:                       # 競合相手が先に作った → 読み直す
        tag = await tag_by_name(db, name)
    if tag is None:
        raise KuraError(500, "tag_create_failed", context={"tag": name}, fix="Retry; if it keeps failing, look for a conflicting tag_name row.")
    await db.refresh(tag, ["names", "category"])
    return tag


@router.put("/post/{post_id}")
@router.patch("/post/{post_id}")
async def update_post(
    post_id: int,
    body: PostUpdateRequest,
    db: AsyncSession = Depends(get_db),
    auth_user: User = Depends(get_current_user),
    localize: bool = False,
) -> PostResponse:
    stmt = (
        select(Post)
        .where(Post.post_id == post_id)
        .options(
            selectinload(Post.tags).selectinload(Tag.names),
            selectinload(Post.tags).selectinload(Tag.category),
            selectinload(Post.user),
            selectinload(Post.favorited_by),
        )
    )
    result = await db.execute(stmt)
    post = result.scalar_one_or_none()
    if post is None:
        raise KuraError(404, "post_not_found", fix="Check the post id: the post does not exist, was deleted, or is hidden in safe mode.")

    if post.version != body.version:
        raise KuraError(409, "version_conflict", context={"sent": body.version, "current": post.version}, fix="Reload the post and retry with its current version.")

    # INV-M (plan fe46c5fa Step C, migration 0057): this endpoint is THE
    # human tag-edit path. Identify the whole transaction as human via the
    # transaction-local GUC BEFORE any tag mutation can flush — otherwise
    # the 01_post_tag_pin_guard_ins trigger would treat a human re-adding a
    # tag they previously removed (pin state=absent) as a machine write and
    # silently drop it. set_config(..., true) resets at commit, so nothing
    # leaks to other requests on the pooled connection.
    await db.execute(text("SELECT set_config('kura.tag_actor', 'human', true)"))

    old_by_id: dict[int, Tag] = {t.tag_id: t for t in post.tags}

    # Update tags — **1 クエリで解決する** (2026-09-05, user 報告「kura が重い、特に
    # タグ一括編集時」の主因)。以前はここが名前ごとに _get_or_create_tag を逐次
    # await していた。この endpoint は read-modify-write で **post の全タグ**を受け
    #取るので、タグ 1 個の付け外しでも post が持つ数だけ往復する — 実測 平均 88.8
    # タグ/post (p95 131) なので ~89 往復/1 post。UI の一括編集 (web/src/lib/
    # bulkTag.ts) は post ごとに直列 PATCH なので、50 件選ぶと ~4,600 往復になり、
    # cache hit 51% の I/O と掛け算になっていた。
    # services.tags.get_or_create_tags は 1 クエリ (IN) で引き、欠けている名前だけ
    # 作る。重複排除され、作成は競合時に読み直す (直 INSERT 版が並行実行で
    # UniqueViolation → 500 を出した経緯がある) ので、逐次版より厳密に安全。
    new_tags: list[Tag] = await get_or_create_tags(
        db, [n.strip() for n in body.tags if n.strip()])

    post.tags = new_tags

    if body.safety is not None:
        post.safety = body.safety
    if body.source is not None:
        post.source = body.source

    post.version += 1
    post.last_edit_time = datetime.now(timezone.utc).replace(tzinfo=None)

    # Record the human diff (added/removed vs. what the post had) as a
    # post_tag_audit row + post_tag_pin rows. Only tags the human actually
    # CHANGED are pinned — a tag merely left in place carries no human
    # decision and stays machine-governed. A pin flips in place when a later
    # edit reverses the earlier decision (ON CONFLICT DO UPDATE — safe here:
    # post_tag_pin is deliberately outside the replication mesh, so the
    # 00_*_dup_skip insert-swallowing trigger pattern does not apply to it).
    new_by_id: dict[int, Tag] = {t.tag_id: t for t in new_tags}
    added_ids = [tid for tid in new_by_id if tid not in old_by_id]
    removed_ids = [tid for tid in old_by_id if tid not in new_by_id]
    if added_ids or removed_ids:
        # Primary names via a direct tag_name query, NOT tag.names. 元の理由は
        # 「_get_or_create_tag が names を eager load しない → 触ると lazy-load で
        # MissingGreenlet」(services/tagging.py complete_auto_tag と同じ罠)。
        # 2026-09-05 に解決を get_or_create_tags へ移して names は載るように
        # なったが、この 1 クエリは added+removed をまとめて引くので安く、
        # 「関係を触らない」ほうが将来の解決経路の変更に対して頑健なので残す。
        name_rows = await db.execute(
            select(TagName.tag_id, TagName.name)
            .where(TagName.tag_id.in_(added_ids + removed_ids), TagName.order == 0)
        )
        name_by_id = {tid: name for tid, name in name_rows.all()}

        def _primary_name(tid: int) -> str:
            return name_by_id.get(tid, str(tid))

        audit_id = (await db.execute(
            text(
                "INSERT INTO post_tag_audit "
                "(post_id, actor_type, actor_id, source, added_tags, removed_tags) "
                "VALUES (:pid, 'human', :actor, 'manual', :added, :removed) "
                "RETURNING id"
            ),
            {
                "pid": post_id,
                "actor": auth_user.name,
                "added": [_primary_name(tid) for tid in added_ids],
                "removed": [_primary_name(tid) for tid in removed_ids],
            },
        )).scalar_one()
        for tid, state in [(t, "present") for t in added_ids] + [(t, "absent") for t in removed_ids]:
            await db.execute(
                text(
                    "INSERT INTO post_tag_pin "
                    "(post_id, tag_id, state, pinned_by, audit_id) "
                    "VALUES (:pid, :tid, :state, :by, :aid) "
                    "ON CONFLICT (post_id, tag_id) DO UPDATE SET "
                    "state = EXCLUDED.state, pinned_by = EXCLUDED.pinned_by, "
                    "audit_id = EXCLUDED.audit_id, pinned_at = now()"
                ),
                {"pid": post_id, "tid": tid, "state": state,
                 "by": auth_user.name, "aid": audit_id},
            )

    await db.commit()

    # Re-query with full eager loading (db.refresh doesn't load nested relationships)
    stmt2 = (
        select(Post)
        .where(Post.post_id == post_id)
        .options(
            selectinload(Post.tags).selectinload(Tag.names),
            selectinload(Post.tags).selectinload(Tag.category),
            selectinload(Post.user),
            selectinload(Post.favorited_by),
            selectinload(Post._pools).selectinload(PoolPost.pool).selectinload(Pool.names),
            selectinload(Post._pools).selectinload(PoolPost.pool).selectinload(Pool.category),
            selectinload(Post._pools).selectinload(PoolPost.pool).selectinload(Pool._posts),
        )
    )
    result2 = await db.execute(stmt2)
    post = result2.scalar_one()

    return _serialize_post(post, auth_user, localize=localize)


class RegenerateThumbnailRequest(BaseModel):
    seek_seconds: float = 0.0


@router.post("/post/{post_id}/regenerate-thumbnail")
async def regenerate_thumbnail(
    post_id: int,
    body: RegenerateThumbnailRequest,
    db: AsyncSession = Depends(get_db),
    auth_user: User = Depends(get_current_user),
    localize: bool = False,
) -> PostResponse:
    """Re-extract a single frame from the video at `seek_seconds` and overwrite
    its thumbnail. Any caller can invoke this (matches the loose auth pattern
    other mutation endpoints use). Video posts only — image / pdf return 400.

    Bumps `post.version` so the cache-busted thumbnailUrl forces a re-fetch.
    """
    # Lazy import to avoid pulling upload.py's symbols at module load.
    from api.routers.upload import _regenerate_video_thumbnail

    stmt = (
        select(Post)
        .where(Post.post_id == post_id)
        .options(
            selectinload(Post.tags).selectinload(Tag.names),
            selectinload(Post.tags).selectinload(Tag.category),
            selectinload(Post.user),
            selectinload(Post.favorited_by),
        )
    )
    result = await db.execute(stmt)
    post = result.unique().scalar_one_or_none()
    if post is None:
        raise KuraError(404, "post_not_found", fix="Check the post id: the post does not exist, was deleted, or is hidden in safe mode.")
    if not (post.mime_type or "").startswith("video/"):
        raise KuraError(400, "thumbnail_seek_video_only", context={"mime": post.mime_type}, fix="Seek only applies to video posts; other posts keep their generated thumbnail.")

    src = files.names("content", post.post_id, post.mime_type)[0]
    thumb_names = files.names("thumbnail", post.post_id)
    if not src.exists():
        raise KuraError(404, "source_media_missing", context={"file": src.name}, fix="Restore the original file (or upload the post again) before regenerating its thumbnail.")

    seek = max(0.0, float(body.seek_seconds))
    try:
        await asyncio.to_thread(_regenerate_video_thumbnail, post_id, src, thumb_names, seek)
    except subprocess.CalledProcessError as e:
        # ffmpeg失敗 (codec 不一致 / 破損 etc) はそのまま 500、 詳細は stderr
        raise KuraError(
            500, "ffmpeg_failed",
            context={"stderr_tail": e.stderr.decode()[-200:] if e.stderr else "unknown"},
            fix="Check the source video with ffprobe, or retry with another seek time.",
        )
    except (OSError, ValueError, SyntaxError, Image.DecompressionBombError, subprocess.TimeoutExpired):
        raise KuraError(500, "thumbnail_regeneration_failed", fix="Retry with another seek time, or check that the source file is readable.")

    post.version += 1
    post.last_edit_time = datetime.now(timezone.utc).replace(tzinfo=None)
    await db.commit()

    # Re-query for full serialization (mirrors update_post pattern)
    stmt2 = (
        select(Post)
        .where(Post.post_id == post_id)
        .options(
            selectinload(Post.tags).selectinload(Tag.names),
            selectinload(Post.tags).selectinload(Tag.category),
            selectinload(Post.user),
            selectinload(Post.favorited_by),
            selectinload(Post._pools).selectinload(PoolPost.pool).selectinload(Pool.names),
            selectinload(Post._pools).selectinload(PoolPost.pool).selectinload(Pool.category),
            selectinload(Post._pools).selectinload(PoolPost.pool).selectinload(Pool._posts),
        )
    )
    post = (await db.execute(stmt2)).unique().scalar_one()
    return _serialize_post(post, auth_user, localize=localize)


MAX_DUPLICATE_HOPS = 10      # merges re-point to the ok root (merge_duplicate_posts.ok_root): real chains are short


@router.post("/post/{post_id}/favorite")
async def toggle_favorite(
    post_id: int,
    db: AsyncSession = Depends(get_db),
    auth_user: User = Depends(get_current_user),
    localize: bool = False,
) -> PostResponse:
    """Toggle favorite for the default (first) user."""
    # Resolve default user
    user_result = await db.execute(select(User).limit(1))
    user = user_result.scalar_one_or_none()
    if user is None:
        raise KuraError(500, "no_user", fix="Create a user account first; favorites are recorded per user.")

    # Lock the post's row (shared) before the favorites are read. A duplicate merge holds it for
    # update and moves the duplicate's favorites to its representative; a toggle that waited on
    # it then sees the merge's result and goes to the representative, instead of leaving a
    # favorite on a post browse no longer shows (#157). A favorite is a choice of the image.
    # Every post the toggle moves to is locked and looked at before anything is written; a
    # chain that loops or runs on past MAX_DUPLICATE_HOPS is refused rather than written to.
    followed: list[int] = []
    while True:
        row = (await db.execute(
            select(Post.status, Post.duplicate_of).where(Post.post_id == post_id)
            .with_for_update(read=True))).first()
        if row is None or row.status != "exact_dup" or row.duplicate_of is None:
            break
        followed.append(post_id)
        if row.duplicate_of in followed or len(followed) >= MAX_DUPLICATE_HOPS:
            raise KuraError(409, "duplicate_chain_unresolved",
                            fix=f"Post {followed[0]} is a merged duplicate whose chain {followed + [row.duplicate_of]} "
                                f"loops or is too long; repair duplicate_of so it ends on a post that is ok.")
        post_id = row.duplicate_of

    stmt = (
        select(Post)
        .where(Post.post_id == post_id)
        .options(
            selectinload(Post.tags).selectinload(Tag.names),
            selectinload(Post.tags).selectinload(Tag.category),
            selectinload(Post.user),
            selectinload(Post.favorited_by),
        )
    )
    result = await db.execute(stmt)
    post = result.scalar_one_or_none()
    if post is None:
        raise KuraError(404, "post_not_found", fix="Check the post id: the post does not exist, was deleted, or is hidden in safe mode.")

    existing = next((f for f in post.favorited_by if f.user_id == user.user_id), None)
    if existing:
        await db.delete(existing)
    else:
        fav = PostFavorite(
            post_id=post_id,
            user_id=user.user_id,
            time=datetime.now(timezone.utc).replace(tzinfo=None),
        )
        db.add(fav)

    await db.commit()
    db.expire_all()

    stmt2 = (
        select(Post)
        .where(Post.post_id == post_id)
        .options(
            selectinload(Post.tags).selectinload(Tag.names),
            selectinload(Post.tags).selectinload(Tag.category),
            selectinload(Post.user),
            selectinload(Post.favorited_by),
            selectinload(Post._pools).selectinload(PoolPost.pool).selectinload(Pool.names),
            selectinload(Post._pools).selectinload(PoolPost.pool).selectinload(Pool.category),
            selectinload(Post._pools).selectinload(PoolPost.pool).selectinload(Pool._posts),
        )
    )
    result2 = await db.execute(stmt2)
    post = result2.scalar_one()
    return _serialize_post(post, localize=localize)


@router.delete("/post/{post_id}", status_code=204)
async def delete_post(
    post_id: int,
    db: AsyncSession = Depends(get_db),
    auth_user: User = Depends(get_current_user),
) -> None:
    """Delete a post and its associated files."""
    result = await db.execute(select(Post).where(Post.post_id == post_id))
    post = result.scalar_one_or_none()
    if post is None:
        raise KuraError(404, "post_not_found", fix="Check the post id: the post does not exist, was deleted, or is hidden in safe mode.")

    # every name the files have — during the salt migration that is two each; a
    # name left behind keeps the inode alive and the deleted post reachable (kura#61)
    doomed = files.names("content", post_id, post.mime_type) + files.names("thumbnail", post_id)

    await db.delete(post)
    await db.commit()

    await asyncio.to_thread(files.delete_all_names, doomed)


@router.post("/posts")
async def create_post(
    content: Optional[UploadFile] = File(None),
    file: Optional[UploadFile] = File(None),
    metadata: Optional[str] = Form(None),
    db: AsyncSession = Depends(get_db),
    auth_user: User = Depends(get_current_user),
    localize: bool = False,
) -> PostResponse:
    upload = content or file
    if upload is None:
        raise KuraError(422, "upload_body_missing", fix="content または file フィールドで画像を送ってください。")
    raw = await upload.read()

    # metadata JSON パース (tags / safety / source)
    meta: dict = {}
    if metadata:
        try:
            meta = _json.loads(metadata)
        except Exception:
            pass
    tag_names: list[str] = meta.get("tags", [])
    # Default unassessed uploads to "sketchy", NOT "safe": unknown safety must
    # stay out of safe mode (safe mode shows only explicitly-safe posts).
    safety_val: str = meta.get("safety", "sketchy")
    source_val: Optional[str] = meta.get("source") or None

    sha1, md5 = await asyncio.to_thread(post_files.checksums, raw)

    # Duplicate check
    dup_result = await db.execute(select(Post).where(Post.checksum == sha1))
    if dup_result.scalar_one_or_none() is not None:
        raise KuraError(409, "post_already_exists", fix="すでに登録済みです (checksum重複)。同じ画像は取り込み済みです。")

    try:
        media = await asyncio.to_thread(post_files.prepare, raw)
    except post_files.InvalidPostMedia:
        raise KuraError(400, "unsupported_media", fix="対応形式 (画像・GIF・動画) のファイルを送ってください。")
    post_type = media.post_type
    mime, width, height = media.mime, media.width, media.height
    now = datetime.now(timezone.utc).replace(tzinfo=None)

    new_post = Post(
        user_id=auth_user.user_id if auth_user else None,
        version=1,
        creation_time=now,
        last_edit_time=now,
        safety=safety_val,
        type=post_type,
        status="pending",  # file-backed content awaits materialization, like /upload
        checksum=sha1,
        checksum_md5=md5,
        file_size=len(raw),
        source=source_val,
        original_filename=Path(upload.filename).name if upload.filename else None,
        canvas_width=width,
        canvas_height=height,
        mime_type=mime,
        flags_string="",
    )
    db.add(new_post)
    await db.flush()  # populate new_post.post_id

    await asyncio.to_thread(post_files.store, new_post.post_id, raw, media)

    # タグ付与 — PATCH と同じ理由で 1 クエリ (2026-09-05)。作成時も 1 post に
    # 数十〜百のタグが付くので、逐次解決だとその数だけ往復する。
    if tag_names:
        new_post.tags = await get_or_create_tags(
            db, [n.strip() for n in tag_names if n.strip()])

    # Visible on return, like /upload: the web page's drop and paste come here. A post that
    # cannot be made visible is not kept: a new install has no cull timer to retry a
    # 'pending' one, so it would stay invisible while the page counted it as added. Either
    # refusal removes the files this request wrote — they are named for a rolled-back id.
    from sqlalchemy.exc import IntegrityError
    from api.routers.upload import _is_sha256_conflict, _synchronous_materialize
    post_id = new_post.post_id

    async def discard() -> None:
        await db.rollback()
        await asyncio.to_thread(files.delete_all_names,
                                files.names("content", post_id, mime) + files.names("thumbnail", post_id))

    if not await _synchronous_materialize(db, new_post, raw, files.names("thumbnail", post_id)[0]):
        await discard()
        raise KuraError(422, "media_not_processable",
                        fix="このファイルは表示用に変換できませんでした。壊れていないか確認してください。")
    try:
        await db.commit()
    except IntegrityError as exc:
        if not _is_sha256_conflict(exc):
            raise
        await discard()
        raise KuraError(409, "post_already_exists", fix="すでに登録済みです (同じ内容の画像が同時に取り込まれました)。")
    await db.refresh(new_post, ["tags", "favorited_by", "user"])
    return _serialize_post(new_post, auth_user, localize=localize)
