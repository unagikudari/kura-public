#!/usr/bin/env python3
# Description: derives tagger_gt_label rows from post_tag_audit corrections + verify-pool review, no new human action required
# Kind: tool
# Effects: mutating (writes tagger_gt_label via governed function)
# Runs-on: primary
"""api/tagger_core/gt_sync.py — turns existing verify-pool review activity into
structured, trainable ground-truth labels (plan follow-up, user directive
2026-07-20: design + implement the "correction -> trainable data" gap).

Closes the gap trainer-cc-primary's chastity_cage diagnosis found (task
c53b31bc, see also 0095's migration docstring): a human correcting tags
during pool review IS real training signal, but nothing ever turned it
into a queryable positive/negative label set. This derives one entirely
from data that already exists -- no new human action, no new UI:

  - A human ADDING a tag during pool review = the tagger missed a true
    positive -> verdict='positive', derived_from='human_correction'.
  - A human REMOVING a tag = the tagger over-triggered a false positive
    -> verdict='negative', derived_from='human_correction'.
  - Once a pool reaches verify_status='verified', any post in it that was
    NEVER touched by a correction for that tag is implicitly reviewed --
    the human saw it and did not object, so its CURRENT tag state
    (present or absent) is taken as the verdict, derived_from=
    'implicit_review'. A later real correction always supersedes this
    (tagger_governed_upsert_gt_label's ON CONFLICT DO UPDATE).

Scope: only tags currently open in tagger_head_candidate (status=
'candidate') x their source_pool_id -- this is a targeted sync for tags
actively being considered for training, not a full-history backfill of
every pool ever reviewed.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from sqlalchemy import text

from api.db.session import async_session
from api.tagger_core.config import is_non_trainable, load_config

SCOPE = "gt-sync"


async def _sync_global_negatives(db) -> int:
    """INV-C (plan kura-tagger-self-improvement-loop): every human tag DELETION
    anywhere -- in a learning pool OR not -- is a reliable false-positive signal
    and becomes NEGATIVE GT. One bulk governed call (migration 0106), not the
    per-row upsert (~31k absent pins would be pathological)."""
    return (await db.execute(
        text("SELECT tagger_governed_sync_neg_gt_from_pins()")
    )).scalar_one()


async def _sync_global_positives(db) -> int:
    """INV-C revision 2 (migration 0121, user 2026-08-02): a PRESENT pin is an
    explicit human decision — created by adding the tag or by the 施錠/lock
    control — and is exactly as reliable as the deletion behind an absent pin,
    so it becomes pool-free POSITIVE GT. 0106's asymmetry (positives only from
    verified pools) protected against "untouched = correct", which carried no
    decision; a pin does. Untouched tags still derive nothing globally."""
    return (await db.execute(
        text("SELECT tagger_governed_sync_pos_gt_from_pins()")
    )).scalar_one()


async def _detect_global_candidates(db, min_correction_posts: float) -> list[str]:
    """A tag heavily corrected across the WHOLE corpus (not just one pool) is an
    over-detection hotspot needing a suppression head. Flags any UNLOCKED tag
    with >= min_correction_posts distinct deleted-from posts as a head_candidate
    with source_pool_id=NULL (global). Category-BLIND (user: カテゴリに依存しない)
    -- the only gate is NOT locked. add_head_candidate dedups NULL-source on tag
    alone (0106). Returns the tags newly flagged."""
    # Exclude tags that carry a 'dismissed' head_candidate: a dismissal is an
    # explicit "don't pursue this tag" decision (e.g. structurally untrainable,
    # near-zero positive GT -- the 22 dismissed 2026-07-21) and global deletion
    # volume must not silently resurrect it. Re-entry of a dismissed tag is
    # governed by the reconciler's positive-GT gate at DISPATCH time (unlimited
    # v-next once real positives accumulate), not by re-flagging it as a
    # candidate here off deletion count alone.
    rows = (await db.execute(text(
        "SELECT tn.name AS tag, count(DISTINCT ptp.post_id) AS n, "
        "       min(tc.name) AS category "
        "FROM post_tag_pin ptp "
        "JOIN tag_name tn ON tn.tag_id = ptp.tag_id AND tn.ord = 0 "
        "JOIN tag tg ON tg.id = ptp.tag_id "
        "JOIN tag_category tc ON tc.id = tg.category_id "
        "LEFT JOIN post_tag pt ON pt.post_id = ptp.post_id AND pt.tag_id = ptp.tag_id "
        "WHERE ptp.state = 'absent' AND NOT COALESCE(pt.locked, false) "
        # dismissal は「追わない」判断だが、**その後に promoted した事実は
        # dismissal を上書きしている** — 配備まで行ったタグを 07-21 の dismissal が
        # 永久追放するのは判断の順序の逆転 (2026-09-03: dark_labia_minora が
        # 人手補正 890 件を積んでも再入できなかった)。最新の判断だけが立つ。
        "AND NOT EXISTS (SELECT 1 FROM tagger_head_candidate d "
        "                WHERE d.tag = tn.name AND d.status = 'dismissed' "
        "                AND NOT EXISTS (SELECT 1 FROM tagger_head_candidate p2 "
        "                                WHERE p2.tag = d.tag AND p2.status = 'promoted' "
        "                                AND p2.added_at > d.added_at)) "
        # gate hardening #2 (trainer review 1aacc111): a RENAME pins the old
        # name absent (Paruru absent) while the new name is present (paruru), so
        # the absent-pin is not an over-detection deletion. Exclude a pin-absent
        # tag when the same post carries a case-variant PRESENT — else one rename
        # re-registers the old name as a global over-detection hotspot.
        "AND NOT EXISTS (SELECT 1 FROM post_tag pt2 "
        "                JOIN tag_name tn2 ON tn2.tag_id = pt2.tag_id AND tn2.ord = 0 "
        "                WHERE pt2.post_id = ptp.post_id "
        "                AND tn2.tag_id <> ptp.tag_id AND lower(tn2.name) = lower(tn.name)) "
        "GROUP BY tn.name HAVING count(DISTINCT ptp.post_id) >= :thresh"
    ), {"thresh": min_correction_posts})).mappings().all()
    flagged = []
    for r in rows:
        # Non-trainable exclusion (plan tagger-vnext-deterministic-runner
        # Step 5 / E-M2): identity/meta tags (artist/copyright/character/meta
        # categories, or the literal workflow marker 'tagger-exclude') are not
        # visual concepts -- flagging them only manufactures HOLD registrations.
        # ONE shared predicate (api/tagger_core/config.py::is_non_trainable), applied at
        # every candidate entry point.
        if is_non_trainable(r["tag"], r["category"]):
            continue
        reason = (f"全体で {r['n']} 件の投稿からこのタグが人手削除された(過検出ホットスポット、"
                  f"プール非依存の自動検出、閾値 min_gt_labels={min_correction_posts:.0f})")
        added = (await db.execute(text(
            "SELECT tagger_governed_add_head_candidate(:tag, NULL, :reason, 'auto:global-deletions')"
        ), {"tag": r["tag"], "reason": reason})).scalar_one()
        if added:
            flagged.append(r["tag"])
            continue
        # 追加できない = 同 (tag, NULL) の行が既にある (add の dedup は終端行も
        # 数える)。配備済み (promoted 行 + policy) のタグなら過検知再入の辺
        # (migration 0150) で開き直す — 「負例 GT が baseline を超えて成長した」
        # は関数自身が確かめる (満たさなければ 0 で何も起きない)。dispatch の
        # 成長幅 (+min_gt_negative) の裁定は従来どおり reconciler のゲート。
        reopened = (await db.execute(text(
            "SELECT tagger_governed_reopen_overdetected_candidate(:tag, :reason)"
        ), {"tag": r["tag"], "reason": reason})).scalar_one()
        if reopened:
            flagged.append(r["tag"])
    return flagged


async def _open_candidate_pools(db) -> list[dict]:
    rows = (await db.execute(text(
        "SELECT DISTINCT tag, source_pool_id FROM tagger_head_candidate "
        "WHERE status = 'candidate' AND source_pool_id IS NOT NULL"
    ))).mappings().all()
    return [dict(r) for r in rows]


async def _sync_human_corrections(db, tag: str, pool_id: int) -> tuple[int, int]:
    """Returns (positive_count, negative_count) upserted from real corrections."""
    rows = (await db.execute(text(
        "SELECT pa.post_id, pa.added_tags, pa.removed_tags "
        "FROM post_tag_audit pa JOIN pool_post pp ON pp.post_id = pa.post_id "
        # Duplicate-marked posts must not contribute GT: a redundant copy would
        # double-count one image in the training signal. See the module note.
        "JOIN post po ON po.id = pa.post_id AND po.status = 'ok' "
        "WHERE pp.pool_id = :pool_id AND pa.actor_type = 'human' "
        "AND (:tag = ANY(pa.added_tags) OR :tag = ANY(pa.removed_tags)) "
        # LATEST EVENT WINS. A human may remove a tag and put it back minutes
        # later; both are separate post_tag_audit rows, and without an ORDER BY
        # the loop below upserted them in arbitrary order, so a stale removal
        # could overwrite the re-add. Measured on post 364188: 'anal_object_
        # insertion' removed 04:02:29, re-added 04:06:49 by the SAME person, yet
        # GT held verdict='negative' -- the retraction was never ingested
        # ([[gt-negative-never-retracted]], trainer round-9 review; 143 rows /
        # 13 tags fleet-wide). Ordering matches ix_post_tag_audit_post_id.
        "ORDER BY pa.post_id, pa.created_at DESC, pa.id DESC"
    ), {"pool_id": pool_id, "tag": tag})).all()

    pos = neg = 0
    seen: set[int] = set()
    for r in rows:
        # Rows arrive newest-first per post; the first one that actually decides
        # this tag wins and the rest of that post's history is history.
        if r.post_id in seen:
            continue
        added = r.added_tags or []
        removed = r.removed_tags or []
        # gate hardening #2 (trainer review 1aacc111): a rename edit both ADDS
        # and REMOVES a case-variant of the same concept (paruru / Paruru). The
        # old `tag in added → positive else negative` scattered a FALSE NEGATIVE
        # on the uppercase side (measured: 'Paruru' negative on 208 posts, all
        # 208 actually hold artist:paruru). Skip a row that is a rename for this
        # tag so it never derives a GT label. (7 observed pairs are case-only.)
        rename_concepts = {t.lower() for t in added} & {t.lower() for t in removed}
        if tag.lower() in rename_concepts:
            # Not a verdict either way -- fall through to this post's next-newest
            # event rather than marking the post decided, so a real correction
            # underneath a later rename is still honoured.
            continue
        seen.add(r.post_id)
        if tag in added:
            verdict = "positive"
            pos += 1
        else:
            verdict = "negative"
            neg += 1
        await db.execute(text(
            "SELECT tagger_governed_upsert_gt_label(:tag, :post_id, :verdict, :pool_id, 'human_correction')"
        ), {"tag": tag, "post_id": r.post_id, "verdict": verdict, "pool_id": pool_id})
    return pos, neg


async def _sync_implicit_review(db, tag: str, pool_id: int) -> tuple[int, int]:
    """Returns (positive_count, negative_count) upserted from untouched-but-verified posts.
    Only runs if the pool has actually reached verify_status='verified'."""
    verified = (await db.execute(text(
        "SELECT verify_status FROM pool WHERE id = :pool_id"
    ), {"pool_id": pool_id})).scalar_one_or_none()
    if verified != "verified":
        return 0, 0

    tag_id = (await db.execute(text(
        "SELECT tag_id FROM tag_name WHERE name = :tag LIMIT 1"
    ), {"tag": tag})).scalar_one_or_none()
    if tag_id is None:
        return 0, 0  # tag doesn't exist as a real tag yet -- nothing to check current state against

    rows = (await db.execute(text(
        "SELECT pp.post_id, (pt.tag_id IS NOT NULL) AS has_tag "
        "FROM pool_post pp "
        # Same exclusion as the correction path: a near_dup/exact_dup copy shares
        # its twin's pixels, so deriving an implicit verdict from it counts one
        # image twice (and, when the twin was human-corrected but the copy was
        # not, contributes a STALE pre-correction verdict that contradicts it).
        "JOIN post po ON po.id = pp.post_id AND po.status = 'ok' "
        "LEFT JOIN post_tag pt ON pt.post_id = pp.post_id AND pt.tag_id = :tag_id "
        "WHERE pp.pool_id = :pool_id "
        "AND NOT EXISTS ("
        "  SELECT 1 FROM post_tag_audit pa WHERE pa.post_id = pp.post_id AND pa.actor_type = 'human' "
        "  AND (:tag = ANY(pa.added_tags) OR :tag = ANY(pa.removed_tags))"
        ")"
    ), {"pool_id": pool_id, "tag_id": tag_id, "tag": tag})).all()

    pos = neg = 0
    for r in rows:
        verdict = "positive" if r.has_tag else "negative"
        if verdict == "positive":
            pos += 1
        else:
            neg += 1
        await db.execute(text(
            "SELECT tagger_governed_upsert_gt_label(:tag, :post_id, :verdict, :pool_id, 'implicit_review')"
        ), {"tag": tag, "post_id": r.post_id, "verdict": verdict, "pool_id": pool_id})
    return pos, neg


async def run_sync() -> dict:
    """tagger_governed_upsert_gt_label has no fencing-token/lease requirement
    (confirmed: it only checks session_user against tagger_allowed_executor,
    unlike record_lifecycle_event which requires a Lease). An earlier
    version of this function acquired a `lifecycle:<tag>` lease anyway "to
    be safe" -- but that lease was never actually passed to anything, and
    holding it live for up to 60s meant this script collided with
    reconciler.py's real, load-bearing use of that same scope: a real run
    right after this one saw 5 genuine training dispatches go out to
    trainer and then fail to record their candidate->training transition,
    because gt_sync's decorative lease was still held. Removed entirely --
    gt_sync.py has no scope it needs exclusivity over."""
    summary = {}
    async with async_session() as db:
        # INV-C, global pass (pool-independent): (1) every human deletion
        # anywhere -> negative GT; (2) over-detected tags (heavily deleted
        # corpus-wide) -> head_candidate. Runs BEFORE the pool pass so a tag
        # flagged here also gets its pool positives synced below in the same
        # run. min_gt_labels is reused as the corpus-deletion threshold (a tag
        # not correctable to that floor can't be dispatched anyway).
        try:
            cfg = await load_config(db)
            neg_synced = await _sync_global_negatives(db)
            pos_synced = await _sync_global_positives(db)
            flagged = await _detect_global_candidates(db, cfg["min_gt_labels"])
            await db.commit()
            summary["_global"] = {"negatives_synced": neg_synced, "positives_synced": pos_synced,
                                  "candidates_flagged": flagged}
            print(f"[gt_sync] global: {neg_synced} negative GT from deletions, "
                  f"{pos_synced} positive GT from present pins, "
                  f"{len(flagged)} over-detection candidate(s) flagged: {flagged}")
        except Exception as e:  # noqa: BLE001
            await db.rollback()
            print(f"[gt_sync] global pass FAILED: {e}", file=sys.stderr)
            summary["_global"] = {"error": str(e)}

        targets = await _open_candidate_pools(db)
        for t in targets:
            tag, pool_id = t["tag"], t["source_pool_id"]
            key = f"{tag}:pool{pool_id}"
            try:
                hp, hn = await _sync_human_corrections(db, tag, pool_id)
                ip, iin = await _sync_implicit_review(db, tag, pool_id)
                await db.commit()
                summary[key] = {"human_positive": hp, "human_negative": hn,
                                 "implicit_positive": ip, "implicit_negative": iin}
                print(f"[gt_sync] {key}: human(+{hp}/-{hn}) implicit(+{ip}/-{iin})")
            except Exception as e:  # noqa: BLE001
                await db.rollback()
                print(f"[gt_sync] {key} FAILED: {e}", file=sys.stderr)
                summary[key] = {"error": str(e)}
    return summary


def main() -> int:
    import asyncio
    summary = asyncio.run(run_sync())
    failed = [key for key, result in summary.items() if "error" in result]
    print(f"[gt_sync] done: {len(summary)} groups processed, {len(failed)} failed")
    if failed:
        print(f"[gt_sync] retry failed groups before reconciliation: {failed}", file=sys.stderr)
    # The systemd auto-cycle must not reconcile/promote against an incomplete sync.
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
