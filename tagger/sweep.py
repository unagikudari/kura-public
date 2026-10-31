#!/usr/bin/env python3
# Description: bounded, idempotent, active-release-pinned backlog sweep — replaces scripts/backlog_requeue_topup.sh
# Kind: tool
# Effects: mutating (requeues post.tagging_status via governed function)
# Runs-on: primary
"""tagger/sweep.py — bounded, idempotent, active-release-pinned backlog sweep.

Replaces scripts/backlog_requeue_topup.sh (uncommitted, built by
primary-cc-primary 2026-07-20). That script's predicate --
`NOT EXISTS(post_tag_audit a WHERE a.post_id=p.id AND a.release_id IS NOT
NULL)` -- checks "was this post EVER tagged by ANY release", not "was it
tagged by the CURRENTLY ACTIVE release". Once a post was patched by any
release, it was never re-swept even after a newer release shipped -- the
actual root cause of this session's incident, not the release_id=2
hardcode that was originally suspected. Also replaces this session's own
reckless one-shot 723k-post bulk UPDATE against a stale model (the
"タガーを更新しろ means retrain first, never sweep with a stale model"
incident).

Design (plan:kura-tagger-lifecycle-redesign Step 16):
  - Resolves (release_id, generation) from tagger_release_active ONCE at
    the start of a run and pins to it -- every predicate and the final
    TOCTOU check reference this same pinned pair, never re-resolved
    mid-run.
  - Correct predicate: NOT EXISTS(post_tag_audit WHERE post_id=p.id AND
    release_id = :active_release_id) -- "was this post tagged by the
    ACTIVE release", not "by any release ever" (the bug above).
  - scope='backlog-sweep' lease required (api.tagger_core.transitions.acquire_lease)
    -- the exact mutual-exclusion mechanism that was missing when
    primary-cc-primary's timer and primary-cc-2's bulk UPDATE both independently
    wrote to the same backlog on 2026-07-20 with no shared coordination.
    This alone is the concurrency guard -- no separate per-release
    idempotency record on top of it. An EARLIER version of this script
    also called tagger_record_operation(scope='backlog-sweep',
    target_id=str(active_release_id)), treating "sweep against this
    release" as a run-once operation; that was wrong for this use case
    and was removed after it silently blocked every run past the first
    (caught live: a 500-post bounded run returned
    "already_swept_this_release" with zero candidates checked, because an
    earlier 2-post smoke test had already consumed the one-shot token for
    the same release_id). The actual per-post idempotency the plan's INV
    calls for is already provided by the query's own
    NOT EXISTS(post_tag_audit ... release_id = :active_release_id)
    predicate -- a post that was just flipped to pending drops out of the
    candidate set on the next invocation without any extra bookkeeping,
    and this script is DESIGNED to be invoked repeatedly (a 15-min timer
    chipping through a 700k+ backlog in bounded passes), not once per
    release.
  - Hard ceiling: --max-posts (absolute count) AND --max-fraction (of the
    currently-done taggable backlog) -- refuses to requeue more than the
    operator explicitly bounded, even if the candidate set is larger.
  - --dry-run: reports the candidate count without calling the governed
    mutation at all.
  - TOCTOU: re-reads tagger_release_active.generation immediately before
    calling tagger_governed_requeue; if it no longer matches the pinned
    generation (someone published a new release mid-run), aborts without
    requeuing -- a fresh invocation will pin the new one instead.

Closed 2026-08-04 (migration 0130): every run -- including dry runs and
TOCTOU aborts -- now writes a tagger_sweep_run row via
tagger_governed_record_sweep_run. The gap mattered: an empty audit table
was read as "the sweep never ran" when in fact it had (2026-07-20,
release 2), its only trace being tagger_operation -- a table that is NOT
replicated, so a replica reads zero even when the canonical has rows. An
audit table is empty for two different reasons ("it did not happen" and
"nobody writes it"); the second must not be readable as the first.
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import text

from api.db.session import async_session
from api.services import tagging_queue
from api.tagger_core.transitions import TransitionError, acquire_lease, release_lease

DEFAULT_MAX_POSTS = 5000
DEFAULT_MAX_FRACTION = 0.25


async def _resolve_active(db) -> tuple[int, int]:
    row = (await db.execute(
        text("SELECT release_id, generation FROM tagger_release_active WHERE id = 1")
    )).one_or_none()
    if row is None or row.release_id is None:
        raise TransitionError("sweep: no active tagger_release configured")
    return row.release_id, row.generation


async def _ensemble_changed_at(db):
    """**配備中のアンサンブルが最後に変わった時刻。**

    2026-08-05 まで sweep の判定は `release_id` だけだった:

        NOT EXISTS (post_tag_audit WHERE post_id=p.id AND release_id=:rid)

    しかし **head policy が増えても release_id は変わらない**。新しい head を
    配備しても、既にその release でタグ済みの post は「済み」のままで
    **永久に再タグされない** — release を publish する人間が居ないと掃除が
    始まらない構造だった (user 2026-08-05:「誰かが操作しないと reap が走らない
    のは問題だ。機構化しているとは言えない」「人間はタグ付けするだけだ」)。

    アンサンブルは release と head policy の**両方**で決まるので、両方の
    更新時刻の新しいほうを見る。policy が 1 行増えれば、その時刻より前に
    タグ付けされた post は全部「古いアンサンブルで付いた」ことになる。
    """
    return (await db.execute(text(
        "SELECT GREATEST("
        "  COALESCE((SELECT max(created_at) FROM tagger_head_policy), 'epoch'::timestamptz),"
        "  COALESCE((SELECT max(updated_at) FROM tagger_patch WHERE status='approved'),"
        "           'epoch'::timestamptz))"
    ))).scalar()


async def _candidate_ids(db, release_id: int, limit: int, changed_at) -> list[int]:
    """このアンサンブルをまだ通っていない post (SQL は api/services/tagging_queue.py)。"""
    return await tagging_queue.requeue_candidates(db, release_id, limit, changed_at)


async def _taggable_done_count(db) -> int:
    return await tagging_queue.done_count(db)


async def _record_run(db, summary: dict, started_at) -> None:
    """tagger_sweep_run に実行を刻む (0130 の governed 関数)。

    この表は 0079 で作られたが **書くコードが一度も存在しなかった**。その結果
    「sweep は走ったか」を調べる人が空の表を見て「一度も走っていない」と結論する
    形になっていた (2026-08-04 に実際そう推論しかけた — 本当は 2026-07-20 に
    走っており、痕跡は複製されない tagger_operation にだけあった)。

    監査表が空である理由は「起きなかった」と「書く人が居ない」の 2 通りある。
    後者を前者と読み違えさせないために書く。

    記録の失敗で sweep 自体を落とさない — 監査は sweep の目的ではない。ただし
    黙って諦めず stderr に出す (fail-open にはするが silent にはしない)。
    """
    # **表の status 語彙へ写す。** tagger_sweep_run の CHECK は
    # running / completed / aborted_toctou / failed で、"dry_run" と
    # "no_candidates" は含まれない — それらは status ではなく **別の次元** で、
    # 表は既に dry_run boolean と flipped_count を持っている。
    # 「dry run だった」は status ではなく dry_run=true、「候補が無かった」は
    # status ではなく flipped_count=0 で表す。制約を広げるのではなく、
    # 2 つの次元を混ぜていた側 (私) を直す。
    _STATUS = {"completed": "completed", "dry_run": "completed",
               "no_candidates": "completed", "aborted_toctou": "aborted_toctou"}
    status = _STATUS.get(summary["status"], "failed")
    try:
        await db.execute(
            text("""SELECT tagger_governed_record_sweep_run(
                        :rid, :gen, :ceiling, :flipped, :dry, :status, :started)"""),
            {"rid": summary["active_release_id"], "gen": summary["active_generation"],
             "ceiling": summary["ceiling"], "flipped": summary["flipped"],
             "dry": summary["dry_run"], "status": status,
             "started": started_at})
        await db.commit()
    except Exception as e:  # noqa: BLE001
        # **rollback を忘れない。** 例外を握るだけだと session が
        # InFailedSQLTransactionError のまま残り、finally の release_lease() まで
        # 巻き添えで失敗する — stale-lease CRIT (2026-07-22) の形。実際 2026-08-04
        # 22:27 の自動 sweep でそうなった (記録関数が primary に未適用だったため)。
        # fail-open にするなら、後続が使える状態まで戻すのが fail-open。
        try:
            await db.rollback()
        except Exception:  # noqa: BLE001
            pass
        print(f"[sweep] WARNING: tagger_sweep_run への記録に失敗: {e}", file=sys.stderr)


async def run_sweep(*, max_posts: int, max_fraction: float, dry_run: bool) -> dict:
    started_at = datetime.now(timezone.utc)
    async with async_session() as db:
        release_id, generation = await _resolve_active(db)
        total_done = await _taggable_done_count(db)
        ceiling = min(max_posts, int(total_done * max_fraction))
        # **アンサンブルが変わった時刻**を判定に入れる。head policy が増えても
        # release_id は変わらないので、これが無いと候補は 0 件のままになる。
        changed_at = await _ensemble_changed_at(db)
        candidates = await _candidate_ids(db, release_id, ceiling, changed_at)

        summary = {
            "active_release_id": release_id, "active_generation": generation,
            "ensemble_changed_at": changed_at.isoformat() if changed_at else None,
            "total_done": total_done, "ceiling": ceiling,
            "candidate_count": len(candidates),
            "dry_run": dry_run, "flipped": 0,
            "status": "dry_run" if dry_run else "pending",
        }
        if dry_run or not candidates:
            summary["status"] = "dry_run" if dry_run else "no_candidates"
            await _record_run(db, summary, started_at)
            return summary

        lease = await acquire_lease(db, "backlog-sweep", lease_seconds=120)
        try:
            # TOCTOU: re-check the active pointer hasn't moved since we pinned it
            # (e.g. a publish landed between resolve and here).
            _, current_generation = await _resolve_active(db)
            if current_generation != generation:
                summary["status"] = "aborted_toctou"
                await _record_run(db, summary, started_at)
                return summary

            row = (await db.execute(
                text("SELECT tagger_governed_requeue(:ids, :token) AS n"),
                {"ids": candidates, "token": lease.fencing_token},
            )).one()
            await db.commit()
            summary["flipped"] = row.n
            summary["status"] = "completed"
            await _record_run(db, summary, started_at)
            return summary
        finally:
            # Release the backlog-sweep lease on every exit path (including the
            # TOCTOU abort) so it never lingers into a stale-lease CRIT
            # (incident 2026-07-22). Fencing-matched + idempotent; non-fatal on
            # failure (reclaim-on-acquire self-heals on the next sweep).
            try:
                await release_lease(db, lease)
            except TransitionError as e:
                print(f"[sweep] WARNING: lease release failed: {e}", file=sys.stderr)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--max-posts", type=int, default=DEFAULT_MAX_POSTS,
                     help=f"absolute hard ceiling on posts flipped per run (default {DEFAULT_MAX_POSTS})")
    ap.add_argument("--max-fraction", type=float, default=DEFAULT_MAX_FRACTION,
                     help=f"fraction of the current done-backlog, second hard ceiling (default {DEFAULT_MAX_FRACTION})")
    ap.add_argument("--dry-run", action="store_true", help="report candidates without requeuing")
    a = ap.parse_args()

    try:
        summary = asyncio.run(run_sweep(
            max_posts=a.max_posts, max_fraction=a.max_fraction, dry_run=a.dry_run,
        ))
    except TransitionError as e:
        print(f"[sweep] FAILED: {e}", file=sys.stderr)
        return 1

    print(f"[sweep] {summary}")
    return 0 if summary["status"] in ("dry_run", "no_candidates", "completed", "already_swept_this_release") else 1


if __name__ == "__main__":
    sys.exit(main())
