"""Tags router — browse, autocomplete, and management."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, field_validator
from sqlalchemy import delete, func, select, text, or_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload, load_only, raiseload

from api.auth import get_current_user
from api.db.models import PostTag, Tag, TagCategory, TagLink, TagName, TagLocalization, User, TagRelation
from api import site_rules
from api.db.session import get_db
from api.errors import tag_name_taken_concurrently
from api.schemas.common import PagedResult

from api.services.tag_localization import localized_names, normalize_localizations, transfer_localizations

router = APIRouter(prefix="/api", tags=["tags"])


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _serialize_tag(tag: Tag, localize: bool = False) -> dict:
    names = [n.name for n in sorted(tag.names, key=lambda n: n.order)]
    return {
        "id": tag.tag_id,
        "version": tag.version,
        "names": names,
        **({"localizedNames": localized_names(tag)} if localize else {}),
        "category": tag.category.name if tag.category else "default",
        "categoryColor": tag.category.color if tag.category else "#cccccc",
        "description": tag.description,
        "creationTime": tag.creation_time,
        "lastEditTime": tag.last_edit_time,
        "usages": tag.post_count or 0,
        "suggestions": [],
        "implications": [],
    }


async def _find_tag(db: AsyncSession, name: str) -> Tag:
    stmt = (
        select(Tag)
        .join(TagName, TagName.tag_id == Tag.tag_id)
        .where(TagName.name == name)
        .options(selectinload(Tag.names), selectinload(Tag.category))
    )
    tag = (await db.execute(stmt)).scalar_one_or_none()
    if tag is None:
        raise HTTPException(status_code=404, detail=f"Tag not found: {name!r}")
    return tag


async def _find_category(db: AsyncSession, name: str) -> TagCategory:
    cat = (await db.execute(
        select(TagCategory).where(TagCategory.name == name)
    )).scalar_one_or_none()
    if cat is None:
        raise HTTPException(status_code=400, detail=f"Unknown category: {name!r}")
    return cat


# ── GET /api/tag-categories ───────────────────────────────────────────────────

@router.get("/tag-categories")
async def list_tag_categories(db: AsyncSession = Depends(get_db)) -> list[dict]:
    cats = (await db.execute(
        select(TagCategory).order_by(TagCategory.order)
    )).scalars().all()
    return [{"name": c.name, "color": c.color, "default": c.default} for c in cats]


# ── GET /api/tags ─────────────────────────────────────────────────────────────

@router.get("/tags")
async def search_tags(
    localize: bool = False,
    query: str = Query(""),
    category: str = Query(""),
    offset: int = Query(0),
    limit: int = Query(10, le=1000),
    sort: str = Query("usage"),
    thumbnails: bool = True,
    cleanup: Literal["all", "unused", "no_alias"] = "all",
    tag_id: int | None = None,
    db: AsyncSession = Depends(get_db),
) -> list[dict] | PagedResult:
    """Autocomplete (small limit, no offset, no category) or paginated browse."""
    is_browse = limit > 50 or offset > 0 or bool(category) or cleanup != "all" or tag_id is not None

    if not query and not is_browse:
        return []

    stmt = select(Tag).options(selectinload(Tag.names), selectinload(Tag.category))
    if not is_browse:
        stmt = stmt.options(load_only(Tag.tag_id, Tag.category_id, Tag.post_count))
    if not localize:
        stmt = stmt.options(raiseload(Tag.localizations))

    if query:
        stmt = stmt.where(or_(
            Tag.names.any(TagName.name.ilike(f"%{query}%")),
            Tag.localizations.any(TagLocalization.name.ilike(f"%{query}%")),
        ))

    # browse はジャンル別セクション表示なので category 順に並べる → そのため join。
    # category フィルタ指定時も同じ join を流用 (二重 join 回避)。
    if category or is_browse:
        stmt = stmt.join(TagCategory, Tag.category_id == TagCategory.id)
    if category:
        stmt = stmt.where(TagCategory.name == category)

    if tag_id is not None:
        stmt = stmt.where(Tag.tag_id == tag_id)
    if cleanup == "unused":
        stmt = stmt.where(func.coalesce(Tag.post_count, 0) == 0)
    elif cleanup == "no_alias":
        name_count = select(func.count(TagName.tag_name_id)).where(TagName.tag_id == Tag.tag_id).scalar_subquery()
        stmt = stmt.where(name_count <= 1)

    # EXISTS predicates and the category PK join never duplicate a Tag row.
    # DISTINCT over all Tag columns forced unnecessary comparison of descriptions.
    total = 0
    if is_browse:
        count_stmt = select(func.count()).select_from(stmt.with_only_columns(Tag.tag_id).subquery())
        total = (await db.execute(count_stmt)).scalar() or 0

    if sort == "name":
        stmt = stmt.order_by(Tag.tag_id)
    elif is_browse:
        # category.order → usage 降順。フロントのジャンル別セクションが上から順に積まれる。
        stmt = stmt.order_by(
            TagCategory.order, Tag.post_count.desc(), Tag.tag_id.desc()
        )
    else:
        stmt = stmt.order_by(Tag.post_count.desc(), Tag.tag_id.desc())

    stmt = stmt.offset(offset).limit(min(limit, 1000))
    tags = (await db.execute(stmt)).scalars().unique().all()

    if not is_browse:
        return [
            {
                "id": tag.tag_id,
                **({"localizedNames": localized_names(tag)} if localize else {}),
                "names": [n.name for n in tag.names],
                "category": tag.category.name if tag.category else "default",
                "usages": tag.post_count or 0,
            }
            for tag in tags
        ]

    # A monochrome-only tag must not scan its whole collection looking for a
    # color image. Probe a bounded sample and use an ordinary fallback instead.
    thumb: dict[int, int] = {}
    if tags and thumbnails:
        from api.services.tag_thumbnails import representatives
        thumb = await representatives(db, [t.tag_id for t in tags])

    results = []
    for t in tags:
        d = _serialize_tag(t, localize)
        pid = thumb.get(t.tag_id)
        d["thumbnailUrl"] = f"/api/post/{pid}/thumbnail" if pid else None
        results.append(d)
    return PagedResult(query=query, offset=offset, limit=limit, total=total, results=results)


@router.get("/tag-browser")
async def tag_browser(db: AsyncSession = Depends(get_db)):
    from api.services.tag_browser import sidebar
    return await sidebar(db)


# ── GET /api/tag/{tag_name} ───────────────────────────────────────────────────

@router.get("/tag/{tag_name}")
async def get_tag(
    tag_name: str,
    localize: bool = False,
    db: AsyncSession = Depends(get_db),
) -> dict:
    stmt = (
        select(Tag)
        .join(TagName, TagName.tag_id == Tag.tag_id)
        .where(TagName.name == tag_name)
        .options(
            selectinload(Tag.names),
            selectinload(Tag.category),
            selectinload(Tag.suggestions).selectinload(Tag.names),
            selectinload(Tag.implications).selectinload(Tag.names),
        )
    )
    tag = (await db.execute(stmt)).scalar_one_or_none()
    if tag is None:
        raise HTTPException(status_code=404, detail="Tag not found")

    data = _serialize_tag(tag, localize)
    data["suggestions"] = [
        {"names": [n.name for n in s.names], "category": s.category.name if s.category else "default", "usages": s.post_count or 0}
        for s in tag.suggestions
    ]
    data["implications"] = [
        {"names": [n.name for n in i.names], "category": i.category.name if i.category else "default", "usages": i.post_count or 0}
        for i in tag.implications
    ]
    return data


# ── PATCH /api/tag/{tag_name} ─────────────────────────────────────────────────

class TagPatchRequest(BaseModel):
    localizedNames: dict[str, str] | None = None

    @field_validator("localizedNames")
    @classmethod
    def validate_localizations(cls, value):
        if value is None:
            raise ValueError("Send a language/name object; use {} to remove all display names.")
        return normalize_localizations(value)

    names: list[str] | None = None
    category: str | None = None
    description: str | None = None
    version: int


@router.patch("/tag-by-id/{tag_id}")
async def patch_tag_by_id(
    tag_id: int,
    payload: TagPatchRequest,
    db: AsyncSession = Depends(get_db),
    auth_user: User = Depends(get_current_user),
    localize: bool = False,
) -> dict:
    tag = (await db.execute(select(Tag).where(Tag.tag_id == tag_id))).scalars().unique().one_or_none()
    if tag is None:
        raise HTTPException(status_code=404, detail={"code": "tag_not_found", "fix": "タグ一覧を再読み込みしてください。"})
    return await _patch_tag(tag, payload, db, localize)


@router.patch("/tag/{tag_name}")
async def patch_tag(
    tag_name: str,
    payload: TagPatchRequest,
    db: AsyncSession = Depends(get_db),
    auth_user: User = Depends(get_current_user),
    localize: bool = False,
) -> dict:
    tag = await _find_tag(db, tag_name)
    return await _patch_tag(tag, payload, db, localize)


async def _patch_tag(tag: Tag, payload: TagPatchRequest, db: AsyncSession, localize: bool = False) -> dict:
    locked = (await db.execute(select(Tag.tag_id).where(Tag.tag_id == tag.tag_id).with_for_update())).scalar_one_or_none()
    if locked is None:
        raise HTTPException(status_code=409, detail={"code": "tag_changed", "fix": "タグが削除または統合されました。タグ一覧を再読み込みしてください。"})
    await db.refresh(tag, ["version", "localizations", "names"])
    if tag.version != payload.version:
        raise HTTPException(status_code=409, detail="Version conflict — reload and retry")

    if payload.names is not None:
        names = [n.strip() for n in payload.names if n.strip()]
        if not names:
            raise HTTPException(status_code=400, detail="names must not be empty")
        for name in names:
            conflict = (await db.execute(
                select(TagName).where(TagName.name == name, TagName.tag_id != tag.tag_id)
            )).scalar_one_or_none()
            if conflict:
                raise HTTPException(status_code=409, detail=f"Name already in use: {name!r}")
        # 上の SELECT は**先着を早く 409 で返すための当たり判定**でしかない。
        # 同名への並行リネーム 2 本は両方がこれを空振りし、両方が INSERT して
        # 片方が tag_name_name_key で落ちる。その例外は session を汚すので、
        # **意図した 409 ではなく 500** になる (2026-07-30、trainer-cc-primary が
        # upload / posts の同型を直した際に 4 箇所目として指摘)。
        #
        # DELETE も SAVEPOINT の中に入れる — 外に出すと INSERT で落ちたときに
        # 削除だけ残り、**タグが名前を失う**。
        try:
            async with db.begin_nested():
                for tn in list(tag.names):
                    await db.delete(tn)
                await db.flush()
                for i, name in enumerate(names):
                    db.add(TagName(tag_id=tag.tag_id, name=name, order=i))
                await db.flush()          # ここで UNIQUE が効く
        except IntegrityError:
            # 競合相手が先に同じ名前を取った。savepoint が巻き戻っているので
            # 元の名前は保たれている。呼び出し側には意図どおり 409 を返す。
            raise tag_name_taken_concurrently(names)

    if payload.localizedNames is not None:
        incoming = payload.localizedNames
        existing = {row.locale: row for row in tag.localizations}
        for locale, row in existing.items():
            if locale not in incoming:
                tag.localizations.remove(row)
        for locale, name in incoming.items():
            if locale in existing:
                existing[locale].name = name
            else:
                tag.localizations.append(TagLocalization(locale=locale, name=name))

    if payload.category is not None:
        cat = await _find_category(db, payload.category)
        tag.category_id = cat.id

    if payload.description is not None:
        tag.description = payload.description or None

    tag.version = tag.version + 1
    tag.last_edit_time = _now()
    # A rename needs no per-post work: membership is post_tag by id and names
    # are resolved at read time (0179 retired the name arrays whose staleness
    # after renames was found on 2026-09-04).
    await db.commit()
    await db.refresh(tag, ["names", "category"])
    return _serialize_tag(tag, localize)


# ── DELETE /api/tag/{tag_name} ────────────────────────────────────────────────

@router.delete("/tag-by-id/{tag_id}", status_code=204)
async def delete_tag_by_id(
    tag_id: int,
    force: bool = Query(False),
    db: AsyncSession = Depends(get_db),
    auth_user: User = Depends(get_current_user),
) -> None:
    """名前でなく id で消す口。名前に '/' や制御的文字を含むタグ (HTML 断片が
    タグ化した debris 等、2026-09-04 に 6 件実在) は URL パスの /tag/{name} 系
    endpoint から**構造的に到達できない** — curation API が届かない領域を作らない。"""
    tag = (await db.execute(
        select(Tag).options(selectinload(Tag.names), selectinload(Tag.category))
        .where(Tag.tag_id == tag_id)
    )).scalars().unique().one_or_none()
    if tag is None:
        raise HTTPException(status_code=404, detail=f"Tag id {tag_id} not found")
    await _delete_tag_row(db, tag, force)


@router.delete("/tag/{tag_name}", status_code=204)
async def delete_tag(
    tag_name: str,
    force: bool = Query(False),
    db: AsyncSession = Depends(get_db),
    auth_user: User = Depends(get_current_user),
) -> None:
    tag = await _find_tag(db, tag_name)
    await _delete_tag_row(db, tag, force)


async def _delete_tag_row(db: AsyncSession, tag: Tag, force: bool) -> None:
    if not force and (tag.post_count or 0) > 0:
        raise HTTPException(
            status_code=409,
            detail=f"Tag has {tag.post_count} posts; use ?force=true to delete anyway",
        )
    # Same INV-M fix as merge_tags above: identify the transaction as a human
    # curation so 01_post_tag_pin_guard_del doesn't skip pinned rows.
    await db.execute(text("SELECT set_config('kura.tag_actor', 'human', true)"))
    # post_tag.tag_id / post_tag_pin.tag_id / post_tag_actor_claim.tag_id の FK は
    # **NO ACTION** — 旧コメントは「tag -> post_tag ON DELETE CASCADE」と書いていたが
    # 実スキーマに cascade は無く、post 付きタグの force 削除は常に FK violation で
    # 500 になっていた (2026-09-04、ゴミタグ掃除 이름_성 59,344 post で実測)。
    # 先に行を外す。tagger 系
    # テーブルの FK は敢えて残す: 学習履歴に絡むタグの削除は fail-loud が正しい。
    ids = {"tid": tag.tag_id}
    await db.execute(text("DELETE FROM post_tag_actor_claim WHERE tag_id = :tid"), ids)
    await db.execute(text("DELETE FROM post_tag_pin WHERE tag_id = :tid"), ids)
    await db.execute(text("DELETE FROM post_tag WHERE tag_id = :tid"), ids)
    await db.delete(tag)
    await db.commit()


# ── POST /api/tag/{source}/merge-into/{target} ────────────────────────────────

@router.post("/tag/{source_name}/merge-into/{target_name}")
async def merge_tags(
    source_name: str,
    target_name: str,
    db: AsyncSession = Depends(get_db),
    auth_user: User = Depends(get_current_user),
    localize: bool = False,
) -> dict:
    """Merge source into target: all posts gain target tag, source is deleted.

    2026-09-04 (artist タグ重複統合、user 依頼の表記ゆれ掃除) までこの merge は
    post_tag しか運ばず、source の **tag_link (作家帰属の SoT)・別名・pin (人手
    GT)・actor claim** を ORM cascade で黙って削除していた — merge した途端に
    帰属導線 (_derive_artist_tags) と検索別名が消える。identity タグの統合が
    情報を失わないよう、全て target へ移す:
      - tag_link: URL 重複以外を移す (uq_tag_link_tag_url)
      - post_tag_pin / post_tag_actor_claim: (post,tag) 衝突以外を移す
      - source の全名前: target の別名 (ord>0) として保存 — 旧名での検索・
        取り込み時の display-name 由来タグがこの tag へ解決し続ける (再重複の予防)
      - tagger_gt_label: identity タグの派生 GT は削除 (pins から再導出可能で、
        artist カテゴリは non-trainable — 誤って学習系に入った雑音)
      - tagger_head_candidate: canonical 名へ付け替え (履歴保持、重複は削除)
    gt/candidate は governed テーブルなので SET LOCAL ROLE kura_reconciler で
    行う (kura はそのメンバー — merge は人間指示の curation)。"""
    source = await _find_tag(db, source_name)
    target = await _find_tag(db, target_name)

    if source.tag_id == target.tag_id:
        raise HTTPException(status_code=400, detail="Source and target are the same tag")

    # Serialize edits/merges on both identities, in stable order to avoid deadlocks.
    locked = (await db.execute(select(Tag.tag_id).where(Tag.tag_id.in_([source.tag_id, target.tag_id]))
                              .order_by(Tag.tag_id).with_for_update())).scalars().all()
    if len(locked) != 2:
        raise HTTPException(status_code=409, detail={"code": "tag_changed", "fix": "タグが削除または統合されました。タグ一覧を再読み込みしてください。"})
    await db.refresh(source, ["localizations", "names", "category"])
    await db.refresh(target, ["localizations", "names", "category", "version"])
    await transfer_localizations(db, source, target)
    target.version += 1

    # INV-M (migration 0057): a merge is an authenticated human/curator decision,
    # not a machine re-tag — identify the transaction as human BEFORE the delete
    # below so 01_post_tag_pin_guard_del doesn't silently skip source's post_tag
    # rows a human had pinned present. Without this, those rows survive the
    # DELETE, and the subsequent `db.delete(source)` fails with a FK violation
    # (post_tag_tag_id_fkey) — the whole merge rolls back with a 500, harmless
    # but broken for any source tag with a human-pinned duplicate. Same pattern
    # as api/routers/posts.py's human tag-edit path; set_config(..., true)
    # resets at commit/rollback, nothing leaks to other requests.
    await db.execute(text("SELECT set_config('kura.tag_actor', 'human', true)"))

    ids = {"tid": target.tag_id, "sid": source.tag_id}

    # governed テーブル (gt_label / head_candidate) を扱うため。LOCAL なので
    # commit/rollback で自動的に戻る。
    await db.execute(text("SET LOCAL ROLE kura_reconciler"))
    await db.execute(text(
        "DELETE FROM tagger_gt_label WHERE tag_id = :sid"), ids)
    await db.execute(text("""
        UPDATE tagger_head_candidate c SET tag_id = :tid,
               tag = (SELECT name FROM tag_name WHERE tag_id = :tid AND ord = 0)
        WHERE c.tag_id = :sid AND NOT EXISTS (
            SELECT 1 FROM tagger_head_candidate x
            WHERE x.tag_id = :tid
              AND x.source_pool_id IS NOT DISTINCT FROM c.source_pool_id
              AND x.id <> c.id)
    """), ids)
    await db.execute(text(
        "DELETE FROM tagger_head_candidate WHERE tag_id = :sid"), ids)
    await db.execute(text("RESET ROLE"))

    await db.execute(text("""
        UPDATE tag_link SET tag_id = :tid
        WHERE tag_id = :sid AND url NOT IN (SELECT url FROM tag_link WHERE tag_id = :tid)
    """), ids)
    await db.execute(text("DELETE FROM tag_link WHERE tag_id = :sid"), ids)
    await db.execute(text("""
        UPDATE post_tag_pin SET tag_id = :tid
        WHERE tag_id = :sid AND NOT EXISTS (
            SELECT 1 FROM post_tag_pin p2
            WHERE p2.post_id = post_tag_pin.post_id AND p2.tag_id = :tid)
    """), ids)
    await db.execute(text("DELETE FROM post_tag_pin WHERE tag_id = :sid"), ids)
    await db.execute(text("""
        UPDATE post_tag_actor_claim SET tag_id = :tid
        WHERE tag_id = :sid AND NOT EXISTS (
            SELECT 1 FROM post_tag_actor_claim c2
            WHERE c2.post_id = post_tag_actor_claim.post_id
              AND c2.actor_id = post_tag_actor_claim.actor_id AND c2.tag_id = :tid)
    """), ids)
    await db.execute(text("DELETE FROM post_tag_actor_claim WHERE tag_id = :sid"), ids)

    await db.execute(text("""
        UPDATE post_tag SET tag_id = :tid
        WHERE tag_id = :sid
          AND post_id NOT IN (SELECT post_id FROM post_tag WHERE tag_id = :tid)
    """), ids)

    await db.execute(delete(PostTag).where(PostTag.tag_id == source.tag_id))

    # source の全名前を target の別名へ (ORM cascade の delete より先に SQL で
    # 付け替える — expire しないよう source.names には触らない)。
    await db.execute(text("""
        UPDATE tag_name tn SET tag_id = :tid, ord = x.new_ord
        FROM (SELECT tag_name_id,
                     (SELECT COALESCE(max(ord), 0) FROM tag_name WHERE tag_id = :tid)
                     + row_number() OVER (ORDER BY ord) AS new_ord
              FROM tag_name WHERE tag_id = :sid) x
        WHERE tn.tag_name_id = x.tag_name_id
    """), ids)

    count = (await db.execute(
        select(func.count()).select_from(PostTag).where(PostTag.tag_id == target.tag_id)
    )).scalar() or 0
    target.post_count = count
    target.last_edit_time = _now()

    # names は上で SQL 付け替え済み。ORM の cascade delete-orphan を評価させない
    # よう source を session から切り離してから、行として消す (db.delete(source)
    # だと cascade が「source の names」— 今は target の別名 — を消しに行く)。
    db.expunge(source)
    await db.execute(text("DELETE FROM tag WHERE id = :sid"), ids)
    await db.commit()
    await db.refresh(target, ["names", "category"])
    return _serialize_tag(target, localize)


# ── Tag external links (artist accounts on external platforms) ──────────────────

# Platform-implied `uncensored` default, applied only when the caller omits it
# (never overrides an explicit value). Some platforms reliably host uncensored
# work, some reliably censored; platforms that are artist-dependent are left
# UNKNOWN so it's verified per artist, not asserted.
# 表そのものは site adapter が持つ (kura#66 G6)。登録の無い platform は「不明」で、
# 既定値を入れない — 取得元を知らない構成では全 platform が不明になる。


def _serialize_link(l: TagLink) -> dict:
    return {
        "id": l.id,
        "tagId": l.tag_id,
        "platform": l.platform,
        "url": l.url,
        "handle": l.handle,
        "uncensored": l.uncensored,   # None | 0 | 1
        "verified": l.verified,
        "note": l.note,
        "createdAt": l.created_at.isoformat() if l.created_at else None,
        "updatedAt": l.updated_at.isoformat() if l.updated_at else None,
    }


class TagLinkCreate(BaseModel):
    platform: str
    url: str
    handle: str | None = None
    uncensored: int | None = None   # None=unknown, 0=censored, 1=uncensored
    verified: bool = False
    note: str | None = None


class TagLinkUpdate(BaseModel):
    platform: str | None = None
    url: str | None = None
    handle: str | None = None
    uncensored: int | None = None
    verified: bool | None = None
    note: str | None = None
    clear_uncensored: bool = False   # explicitly set uncensored back to NULL


def _validate_uncensored(v: int | None) -> None:
    if v is not None and v not in (0, 1):
        raise HTTPException(status_code=400, detail="uncensored must be null, 0, or 1")


@router.get("/tag/{tag_name}/links")
async def list_tag_links(tag_name: str, db: AsyncSession = Depends(get_db)) -> list[dict]:
    tag = await _find_tag(db, tag_name)
    rows = (await db.execute(
        select(TagLink).where(TagLink.tag_id == tag.tag_id).order_by(TagLink.platform, TagLink.id)
    )).scalars().all()
    return [_serialize_link(l) for l in rows]


@router.post("/tag/{tag_name}/links", status_code=201)
async def add_tag_link(
    tag_name: str,
    payload: TagLinkCreate,
    db: AsyncSession = Depends(get_db),
    auth_user: User = Depends(get_current_user),
) -> dict:
    tag = await _find_tag(db, tag_name)
    platform = payload.platform.strip().lower()
    url = payload.url.strip()
    if not platform or not url:
        raise HTTPException(status_code=400, detail="platform and url are required")
    _validate_uncensored(payload.uncensored)
    # platform-implied guess only when caller leaves it unknown
    uncensored = payload.uncensored
    if uncensored is None:
        _unc = site_rules.platform_uncensored_default(platform)
        if _unc is True:
            uncensored = 1
        elif _unc is False:
            uncensored = 0
    dup = (await db.execute(
        select(TagLink).where(TagLink.tag_id == tag.tag_id, TagLink.url == url)
    )).scalar_one_or_none()
    if dup:
        raise HTTPException(status_code=409, detail="This URL is already linked to the tag")
    link = TagLink(
        tag_id=tag.tag_id, platform=platform, url=url, handle=(payload.handle or None),
        uncensored=uncensored, verified=payload.verified, note=(payload.note or None),
    )
    db.add(link)
    await db.commit()
    await db.refresh(link)
    return _serialize_link(link)


@router.patch("/tag-link/{link_id}")
async def update_tag_link(
    link_id: int,
    payload: TagLinkUpdate,
    db: AsyncSession = Depends(get_db),
    auth_user: User = Depends(get_current_user),
) -> dict:
    link = (await db.execute(select(TagLink).where(TagLink.id == link_id))).scalar_one_or_none()
    if link is None:
        raise HTTPException(status_code=404, detail="Tag link not found")
    if payload.platform is not None:
        link.platform = payload.platform.strip().lower()
    if payload.url is not None:
        link.url = payload.url.strip()
    if payload.handle is not None:
        link.handle = payload.handle or None
    if payload.clear_uncensored:
        link.uncensored = None
    elif payload.uncensored is not None:
        _validate_uncensored(payload.uncensored)
        link.uncensored = payload.uncensored
    if payload.verified is not None:
        link.verified = payload.verified
    if payload.note is not None:
        link.note = payload.note or None
    link.updated_at = _now()
    try:
        await db.commit()
    except Exception:
        await db.rollback()
        raise HTTPException(status_code=409, detail="Update violates a constraint (duplicate URL?)")
    await db.refresh(link)
    return _serialize_link(link)


@router.delete("/tag-link/{link_id}", status_code=204)
async def delete_tag_link(
    link_id: int,
    db: AsyncSession = Depends(get_db),
    auth_user: User = Depends(get_current_user),
) -> None:
    link = (await db.execute(select(TagLink).where(TagLink.id == link_id))).scalar_one_or_none()
    if link is None:
        raise HTTPException(status_code=404, detail="Tag link not found")
    await db.delete(link)
    await db.commit()


@router.get("/tag-links")
async def discover_tag_links(
    platform: str | None = Query(None),
    uncensored: int | None = Query(None, description="filter: 0 or 1"),
    verified: bool | None = Query(None),
    exclude_category: str | None = Query(None, max_length=80),
    tag_ids: str | None = Query(None, max_length=1200),
    limit: int = Query(200, le=1000),
    db: AsyncSession = Depends(get_db),
) -> list[dict]:
    """Cross-artist discovery — e.g. every artist with a verified uncensored
    account. Uses the stored tag_link.pool_id (migration 0063; the account's kura
    pool) for the cover + pool name; installed extensions add what they know about
    each link under `extensions`."""
    conds = ["1=1"]
    params: dict = {"lim": limit}
    if tag_ids is not None:
        parts = tag_ids.split(',')
        if not parts or len(parts) > 100 or any(not p.isascii() or not p.isdecimal() or not 0 < int(p) < 2147483648 for p in parts):
            raise HTTPException(422, "tag_ids must contain 1–100 positive integer IDs")
        conds.append("tl.tag_id = ANY(:tag_ids)")
        params["tag_ids"] = sorted({int(p) for p in parts})
    if exclude_category:
        conds.append("tc.name <> :exclude_category")
        params["exclude_category"] = exclude_category
    if platform:
        conds.append("tl.platform = :platform"); params["platform"] = platform.strip().lower()
    if uncensored is not None:
        conds.append("tl.uncensored = :unc"); params["unc"] = uncensored
    if verified is not None:
        conds.append("tl.verified = :ver"); params["ver"] = verified
    sql = f"""
        SELECT tl.id, tl.tag_id, tc.name AS tag_category, tl.platform, tl.url, tl.handle, tl.uncensored,
               tl.verified, tl.note, tl.created_at, tl.updated_at,
               (SELECT min(name) FROM tag_name WHERE tag_id = tl.tag_id) AS tag_name,
               tl.pool_id, pl.cover_post_id,
               (SELECT name FROM pool_name WHERE pool_id = tl.pool_id AND ord = 0 LIMIT 1) AS pool_name
        FROM tag_link tl
        JOIN tag t ON t.id=tl.tag_id
        JOIN tag_category tc ON tc.id=t.category_id
        LEFT JOIN pool pl ON pl.id = tl.pool_id
        WHERE {' AND '.join(conds)}
        ORDER BY tl.platform, tag_name
        LIMIT :lim
    """
    rows = (await db.execute(text(sql), params)).mappings().all()
    from api import extension_hooks
    annotations = await extension_hooks.annotate_tag_links(
        db, [{"id": r["id"], "pool_id": r["pool_id"], "url": r["url"]} for r in rows])
    out = []
    for r in rows:
        out.append({
            "id": r["id"], "tagId": r["tag_id"], "platform": r["platform"], "url": r["url"],
            "handle": r["handle"], "uncensored": r["uncensored"], "verified": r["verified"],
            "note": r["note"],
            "createdAt": r["created_at"].isoformat() if r["created_at"] else None,
            "updatedAt": r["updated_at"].isoformat() if r["updated_at"] else None,
            "tagName": r["tag_name"], "tagCategory": r["tag_category"],
            "poolId": r["pool_id"],
            "poolName": r["pool_name"],
            "poolCover": f"/api/post/{r['cover_post_id']}/thumbnail" if r["cover_post_id"] else None,
            # what installed extensions know about this link, keyed by extension id
            "extensions": annotations.get(r["id"], {}),
        })
    return out


# ── tag relations (migration 0064) — related / exclusive のペア関係 ──

_RELATION_KINDS = ("related", "exclusive")


class TagRelationCreate(BaseModel):
    other: str
    kind: str
    note: str | None = None


@router.get("/tag/{tag_name}/relations")
async def list_tag_relations(tag_name: str, db: AsyncSession = Depends(get_db)) -> list[dict]:
    """target タグから見た関係一覧。name は相手タグの primary name。"""
    tag = await _find_tag(db, tag_name)
    rows = (await db.execute(text("""
        SELECT tn.name, tr.kind, tr.note
        FROM tag_relation tr
        JOIN LATERAL (
            SELECT name FROM tag_name
            WHERE tag_id = CASE WHEN tr.a_tag_id = :tid THEN tr.b_tag_id ELSE tr.a_tag_id END
            ORDER BY ord LIMIT 1
        ) tn ON true
        WHERE :tid IN (tr.a_tag_id, tr.b_tag_id)
        ORDER BY tr.kind, tn.name
    """), {"tid": tag.tag_id})).mappings().all()
    return [{"name": r["name"], "kind": r["kind"], "note": r["note"]} for r in rows]


@router.post("/tag/{tag_name}/relations", status_code=201)
async def add_tag_relation(
    tag_name: str,
    payload: TagRelationCreate,
    db: AsyncSession = Depends(get_db),
    auth_user: User = Depends(get_current_user),
) -> dict:
    if payload.kind not in _RELATION_KINDS:
        raise HTTPException(status_code=400, detail=f"kind must be one of {_RELATION_KINDS}")
    tag = await _find_tag(db, tag_name)
    other = await _find_tag(db, payload.other.strip())
    if tag.tag_id == other.tag_id:
        raise HTTPException(status_code=400, detail="a tag cannot relate to itself")
    a, b = sorted((tag.tag_id, other.tag_id))
    await db.execute(text("""
        INSERT INTO tag_relation (a_tag_id, b_tag_id, kind, note)
        VALUES (:a, :b, :kind, :note)
        ON CONFLICT (a_tag_id, b_tag_id) DO UPDATE SET kind = :kind, note = :note
    """), {"a": a, "b": b, "kind": payload.kind, "note": payload.note})
    await db.commit()
    return {"a": a, "b": b, "kind": payload.kind, "note": payload.note}


@router.delete("/tag/{tag_name}/relations/{other_name}", status_code=204)
async def delete_tag_relation(
    tag_name: str,
    other_name: str,
    db: AsyncSession = Depends(get_db),
    auth_user: User = Depends(get_current_user),
) -> None:
    tag = await _find_tag(db, tag_name)
    other = await _find_tag(db, other_name)
    a, b = sorted((tag.tag_id, other.tag_id))
    res = await db.execute(text(
        "DELETE FROM tag_relation WHERE a_tag_id = :a AND b_tag_id = :b"
    ), {"a": a, "b": b})
    await db.commit()
    if res.rowcount == 0:
        raise HTTPException(status_code=404, detail="relation not found")


@router.get("/tag-relations")
async def list_all_tag_relations(db: AsyncSession = Depends(get_db)) -> list[dict]:
    """全関係の一括取得 (/tags/manage の関係プール表示用)。テーブルは小さい
    (家族単位の手作業キュレーション) ので全件 1 発が最適 — per-tag N+1 を避ける。"""
    rows = (await db.execute(text("""
        SELECT an.name AS a_name, ac.color AS a_color,
               bn.name AS b_name, bc.color AS b_color,
               tr.kind, tr.note
        FROM tag_relation tr
        JOIN tag_name an ON an.tag_id = tr.a_tag_id AND an.ord = 0
        JOIN tag ta ON ta.id = tr.a_tag_id
        JOIN tag_category ac ON ac.id = ta.category_id
        JOIN tag_name bn ON bn.tag_id = tr.b_tag_id AND bn.ord = 0
        JOIN tag tb ON tb.id = tr.b_tag_id
        JOIN tag_category bc ON bc.id = tb.category_id
        ORDER BY tr.kind, an.name, bn.name
    """))).mappings().all()
    return [{"a": r["a_name"], "aColor": r["a_color"], "b": r["b_name"],
             "bColor": r["b_color"], "kind": r["kind"], "note": r["note"]} for r in rows]
