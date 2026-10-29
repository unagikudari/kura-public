"""Tag management — get or create tags by name."""

from datetime import datetime

from fastapi import HTTPException
from sqlalchemy import select, text, func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from api.db.models import Tag, TagCategory, TagName, TagLocalization


async def expand_implications(db: AsyncSession, post_id: int, locked: bool) -> int:
    """post が持つタグの implication child を post_tag に materialize する。

    例: `reze_(chainsaw_man)`(character) が付いていれば copyright `chainsaw_man`
    も付与する。冪等 (ON CONFLICT DO NOTHING)。1段のみ (parent→child)。
    post_count は post_tag の statement-level トリガーが自動更新する。

    `locked` は呼び出し元の provenance をそのまま継承する — 必須引数(デフォルト無し)
    にして呼び出し元に明示させる: upload 経由 (`_apply_tags`) は locked=True、
    WD14 タガー経由 (`complete_auto_tag`) は locked=False。plan
    kura-tagger-audit-and-remediation rev6 — これを省略すると implied row が
    常にスキーマデフォルト(false)になり、upload でロックされた親タグの implication
    がロックされないまま reap 対象になってしまう(rev6 が是正した回帰)。
    """
    result = await db.execute(
        text(
            # CAST the SELECT-list param: :pid appears twice and asyncpg collapses
            # named params to a single positional $1; the bare param in the SELECT
            # list has no column context, so PG infers an inconsistent type vs the
            # integer WHERE usage → AmbiguousParameterError on newer asyncpg/PG18.
            "INSERT INTO post_tag (post_id, tag_id, locked) "
            "SELECT DISTINCT CAST(:pid AS INTEGER), ti.child_id, CAST(:locked AS BOOLEAN) "
            "FROM post_tag pt JOIN tag_implication ti ON ti.parent_id = pt.tag_id "
            "WHERE pt.post_id = :pid "
            "ON CONFLICT (post_id, tag_id) DO NOTHING"
        ),
        {"pid": post_id, "locked": locked},
    )
    return result.rowcount or 0


async def get_default_category(db: AsyncSession) -> TagCategory:
    result = await db.execute(
        select(TagCategory).where(TagCategory.default.is_(True)).limit(1)
    )
    cat = result.scalar_one_or_none()
    if cat is None:
        result = await db.execute(select(TagCategory).limit(1))
        cat = result.scalar_one_or_none()
    return cat


async def create_tag_named(
    db: AsyncSession,
    name: str,
    category: TagCategory | None,
) -> Tag | None:
    """Tag + TagName を **SAVEPOINT の中で**作る。名前が競合したら None を返す。

    ## なぜ必要か (2026-07-30 実測)
    `tag_name.name` は UNIQUE (`tag_name_name_key`) — これは正しい不変条件で、
    同義タグが別行に分裂するのを防いでいる。壊れていたのは呼び出し側で、
    **SELECT して無ければ INSERT** という書き方が 3 箇所に複製されていた
    (upload.py / posts.py / この関数)。並行リクエストでは両方が SELECT を空振りし、
    両方が INSERT して片方が UniqueViolation で落ちる。

    さらに悪いのは、その例外が **session 全体を汚す**こと。以降の query が
    `PendingRollbackError` になり、リクエストは 500 で終わる。
    実際に 3 枚同時 upload で **2 枚が 500** になった (v1/v2 の regional-binding 画像)。
    症状は「タグが重複した」ではなく「アップロードが失敗した」なので原因が見えにくい。

    `begin_nested()` = SAVEPOINT なので、衝突しても**巻き戻るのは savepoint だけ**で
    session は生き残る。Tag だけ作って TagName で落ちる = 名前無しの孤児 Tag も
    残らない (両方が同じ savepoint の中)。

    呼び出し側は None を受けたら**名前で読み直す** — 競合相手が作った行がそこにある。
    """
    # A display label is not a machine identifier. Require explicit candidate
    # selection instead of accidentally creating a second concept from a label.
    candidates = (await db.execute(
        select(Tag.tag_id, TagName.name).join(TagName, TagName.tag_id == Tag.tag_id)
        .where(TagName.order == 0, Tag.localizations.any(
            func.lower(TagLocalization.name) == name.lower()))
        .order_by(Tag.tag_id).limit(20)
    )).all()
    if candidates:
        raise HTTPException(status_code=409, detail={
            "code": "localized_tag_requires_selection",
            "candidates": [{"id": row[0], "name": row[1]} for row in candidates],
            "fix": "この語は言語別表示名です。検索候補からタグを選択するか、正式名を指定してください。",
        })
    try:
        async with db.begin_nested():
            now = datetime.utcnow()
            tag = Tag(
                category_id=category.id if category else 1,
                version=1,
                creation_time=now,
                last_edit_time=now,
                post_count=0,
            )
            db.add(tag)
            await db.flush()                       # tag_id を確定させる
            db.add(TagName(tag_id=tag.tag_id, name=name, order=0))
            await db.flush()                       # ここで UNIQUE が効く
        return tag
    except IntegrityError:
        return None


async def tag_by_name(db: AsyncSession, name: str) -> Tag | None:
    """タグ名から Tag を引く。competition で負けたときの読み直しに使う。"""
    r = await db.execute(
        select(Tag)
        .join(TagName, TagName.tag_id == Tag.tag_id)
        .where(TagName.name == name)
        .options(selectinload(Tag.names), selectinload(Tag.category))
    )
    return r.scalars().unique().one_or_none()


async def get_or_create_tags(
    db: AsyncSession,
    names: list[str],
    categories: dict[str, str] | None = None,
) -> list[Tag]:
    """Return Tag objects for each name, creating missing ones.

    categories: 任意の {tag_name: category_name} (general/character/...)。
    ★新規作成タグにのみ category を適用する。既存タグのカテゴリは変更しない
      (分類専用 — ユーザーの手動分類や backfill 結果を上書きしないため)。
    未指定/不明カテゴリの新規タグは default(general) で作成。
    """
    if not names:
        return []

    lower_names = [n.lower() for n in names]

    result = await db.execute(
        select(Tag)
        .join(TagName, TagName.tag_id == Tag.tag_id)
        .where(TagName.name.in_(lower_names))
        .options(selectinload(Tag.names), selectinload(Tag.category))
    )
    existing = result.scalars().unique().all()

    found_names = {n.name for t in existing for n in t.names}
    missing = [n for n in lower_names if n not in found_names]

    if missing:
        default_cat = await get_default_category(db)
        # 新規タグに付けたい category 名 (小文字キーで突き合わせ) を id へ解決
        cat_lower = {k.lower(): v for k, v in (categories or {}).items()}
        wanted = {cat_lower[n] for n in missing if cat_lower.get(n)}
        cat_by_name: dict[str, TagCategory] = {}
        if wanted:
            rows = await db.execute(
                select(TagCategory).where(TagCategory.name.in_(wanted))
            )
            cat_by_name = {c.name: c for c in rows.scalars()}

        for name in missing:
            cat = cat_by_name.get(cat_lower.get(name)) or default_cat
            # **競合したら読み直す。** 直に INSERT していた版は並行実行で
            # UniqueViolation を出し、session を汚してリクエストを 500 にしていた。
            tag = await create_tag_named(db, name, cat)
            if tag is None:
                tag = await tag_by_name(db, name)
            if tag is not None:
                existing.append(tag)

    return existing
