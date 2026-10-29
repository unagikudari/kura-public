# Description: Every read of the post tagging queue, in one place, so the queue can move tables by editing one file.
# Kind: lib
# Effects: read-only
# Requires: kura DB
# Runs-on: the API host and the sweep (tagger/sweep.py)
# Version: 2.0
"""Reads of the post tagging queue, from `post_tagging_work`.

docs/notes/tagging-work-table-plan.md moves the queue out of the post row. Stage 0
put every reader here; stage 2 points them at the work table, which the stage 1
mirror keeps equal to `post.tagging_*` until stage 3 makes it the only copy.
Writers are the four governed functions and are not here.

Every post has exactly one work row (the mirror's insert trigger, the seed, and
the foreign key's cascade), so counting the work table counts posts.

Kept free of api.services imports: the sweep imports this module too.
"""

from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


async def by_status(db: AsyncSession) -> dict[str, int]:
    """Post count for every queue status present."""
    rows = (await db.execute(text(
        "SELECT status, count(*) AS n FROM post_tagging_work GROUP BY status"
    ))).mappings().all()
    return {r["status"]: int(r["n"]) for r in rows}


async def stats(db: AsyncSession) -> dict[str, int]:
    """pending (claimable now) / in_flight (leased) / done / failed / skipped."""
    row = (await db.execute(text(
        """
        SELECT
          COUNT(*) FILTER (WHERE status='pending'
                           AND (lease_until IS NULL OR lease_until < now())) AS pending,
          COUNT(*) FILTER (WHERE status='pending'
                           AND lease_until IS NOT NULL AND lease_until >= now()) AS in_flight,
          COUNT(*) FILTER (WHERE status='done')    AS done,
          COUNT(*) FILTER (WHERE status='failed')  AS failed,
          COUNT(*) FILTER (WHERE status='skipped') AS skipped
        FROM post_tagging_work
        """
    ))).mappings().one()
    return {k: int(row[k]) for k in ("pending", "in_flight", "done", "failed", "skipped")}


async def done_count(db: AsyncSession) -> int:
    """Posts whose tagging is done — the sweep's denominator."""
    return (await db.execute(
        text("SELECT count(*) FROM post_tagging_work WHERE status = 'done'")
    )).scalar_one()


async def requeue_candidates(db: AsyncSession, release_id: int, limit: int, changed_at) -> list[int]:
    """Done posts that have not been through this ensemble since it last changed.

    **時刻で比べる。** `release_id` の一致だけでは、release が同じまま head が
    増えた場合に候補が 0 件になる (それが 2026-08-05 までの状態)。
    """
    if limit <= 0:
        return []
    rows = (await db.execute(
        text(
            "SELECT w.post_id FROM post_tagging_work w "
            "WHERE w.status = 'done' "
            "AND NOT EXISTS ("
            "  SELECT 1 FROM post_tag_audit a "
            "  WHERE a.post_id = w.post_id AND a.release_id = :rid "
            # `_ensemble_changed_at` は GREATEST(COALESCE(...,'epoch'), ...) なので
            # **NULL にならない**。`:changed_at IS NULL OR` を書いたら asyncpg が
            # 型を決められず `AmbiguousParameterError` で落ち、2026-08-05 23:19 から
            # sweep が 15 分ごとに失敗していた (601,069 件が再処理されない直接の
            # 原因)。**起きない場合のための分岐が、起きる障害を作っていた。**
            "    AND a.created_at >= :changed_at) "
            "ORDER BY w.post_id LIMIT :limit"
        ),
        {"rid": release_id, "limit": limit, "changed_at": changed_at},
    )).scalars().all()
    return list(rows)


async def attempts(db: AsyncSession, post_id: int) -> int | None:
    """Failed-completion count of one post, or None when it has no work row.

    The caller holds post's row lock (FOR NO KEY UPDATE) first: that lock is what
    serializes completions of the same post, and it stays on post.
    """
    return (await db.execute(
        text("SELECT attempts FROM post_tagging_work WHERE post_id = :pid"), {"pid": post_id}
    )).scalar_one_or_none()
