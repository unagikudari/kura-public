"""Tagging pipeline — claim/complete/stats service layer.

Atomic claim uses `FOR UPDATE SKIP LOCKED` so multiple workers can run
concurrently without collision, and leases auto-expire on crash so the
next tick reclaims without manual intervention.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import func, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from api.claim_storage import service as claim_storage
from api.services import tagging_queue
from api.config import DATA_DIR, DATA_URL
from api.db.models import Pool, PoolPost, Post, PostTag, TagName
from api.schemas.tagging import ClaimedItem, KindStats, StatsResponse, TagItem
from api.services.hashing import get_security_hash
from api.services.tags import expand_implications, get_or_create_tags
from api.tagger_core.transitions import TransitionError, upsert_heartbeat

MAX_ATTEMPTS = 3

# Evidence-gated reap authority (plan:kura-tagger-self-improvement-loop, user
# 2026-07-22). A worker proposes reap gates (reap_domain = patched ∪ veto); the
# server only reaps a gate ALSO listed here — earned by measured GT-positive
# recall, never hand-picked. The 2026-07-22 shadow+recall dry-run proved a
# blanket domain would destroy TPs on low-recall tags (smegma recall 17% → 75%
# shadow reap was FN-driven, not FPs). Authority grows as the base fine-tune
# lifts recall. Read fresh per completion (tiny file) so an updated set takes
# effect without an app restart.
# kura#44: この doc は 6h ごとに evaluator が書き換える **runtime 状態**であって
# ソースではない。tagger/ 配下に tracked で置いていたので production tree が
# 恒常 dirty になり (/api/build が dirty:true を返し続け、commit hash が
# ground truth である前提を毎回濁していた)、`data/` (gitignore 済) へ移した。
# 上書きは KURA_REAP_AUTHORITY_PATH。無ければ fail-closed (何も reap しない)。
from api.state_paths import reap_authority_path as _reap_authority_path

_REAP_AUTHORIZED_PATH = _reap_authority_path()


def _reap_authorized() -> set[str]:
    """**旧経路**: 手書き JSON。`_reap_authorized_tags()` の fallback としてのみ残す。"""
    try:
        return set(json.loads(_REAP_AUTHORIZED_PATH.read_text()).get("authorized") or [])
    except Exception:  # noqa: BLE001
        return set()  # fail-closed: no file / bad json → reap nothing


# reap 権限のキャッシュ。完了ごとに引かない。0068 が rev5 の宿題として挙げていた
# 「新しい行が再起動を待たずに効く」を TTL で満たす。
_REAP_TTL_S = 60.0
_reap_cache: tuple[float, frozenset[str]] | None = None


async def _reap_authorized_tags(db: AsyncSession) -> frozenset[str]:
    """既存タグを消してよい tag の集合。**スキーマから導く。**

    `tagger_head_policy.apply_mode='replace'` = 削除安全性の校正を経た head。
    2026-08-05 まではここが `tagger/reap_authorized.json` の手書き 9 行だった
    ので、**昇格しても列に載らない限り永久に掃除されなかった** (monochrome は
    corpus 601,069 件・in-gate 精度 0.031 のまま再生産され続けた)。
    タグが平等でないとはこのこと。

    **2 つの lane の和を取る。** 権限の出どころは lane ごとに 1 つずつある:

    - content head lane: `tagger_current_head_policy` (0186) の効力のある版が
      head bundle で replace。
    - release lane (稼働 release の patched ∪ veto): `data/tagger/reap_authorized.json`。
      `kura-reap-authorization.timer` が 6h ごとに GT-positive recall を測り直して
      書き換える (tagger/reap_authorization_eval.py)。2026-10-25 時点で
      anilingus / erection / male_pubic_hair — どれも wd14_logit_patch の tag。

    JSON は移行の残りではない (以前の docstring はそう書いていた)。release lane には
    policy 行が無いので、ここが唯一の権限源になる。結果は _REAP_TTL_S (60 s) だけ
    キャッシュする。release が tag を外しても、次の再測定 (最大 6h) までは JSON に残る。
    """
    global _reap_cache
    import time as _time
    now = _time.monotonic()
    if _reap_cache is not None and now - _reap_cache[0] < _REAP_TTL_S:
        return _reap_cache[1]
    tags = set(_reap_authorized())
    try:
        # The policy in force per tag (tagger_current_head_policy, 0186), deployed
        # (a head bundle) and replace. Not "any approved version": an older
        # version's replace must not outlive a newer one, and a tag whose latest
        # version is not a head (anilingus v3, 2026-10-25) has no head to reap by.
        # In a savepoint: this runs inside the completion's transaction, and a
        # failed statement would otherwise abort it, so the except below could
        # not keep the completion going (found 2026-10-25 when a test DB lacked
        # the view: every later statement of the completion failed).
        async with db.begin_nested():
            rows = (await db.execute(text(
                "SELECT c.tag FROM tagger_current_head_policy c "
                "WHERE c.apply_mode = 'replace' AND c.artifact_kind = 'content_head_bundle'"
            ))).scalars().all()
        tags |= set(rows)
    except Exception as e:  # noqa: BLE001
        # policy を読めなくても JSON 側の掃除は止めない (fail-open ではなく、
        # **既に認可されているものを維持する**)。増える側だけが読めていない。
        print(f"[tagging] tagger_head_policy を読めない ({type(e).__name__}) — "
              f"旧 JSON の {len(tags)} タグで継続", flush=True)
    out = frozenset(tags)
    _reap_cache = (now, out)
    return out


# ── claim ─────────────────────────────────────────────────────────────


async def claim_auto_tag(db: AsyncSession, limit: int, lease_seconds: int,
                         commit: bool = True) -> list[ClaimedItem]:
    """Atomic claim via the governed function (migration 0083) — post.tagging_*
    are governed columns as of 0079, a direct UPDATE from the general `kura`
    role now raises. tagger_governed_claim_auto_tag reproduces this
    function's original raw-SQL query exactly (same SELECT ... FOR UPDATE
    SKIP LOCKED + UPDATE), just running as kura_reconciler.

    Coupling note: the function hardcodes the taggable type list as a SQL
    literal ('image','animation','video') since a SECURITY DEFINER function
    can't import Post.TAGGABLE_TYPES — if that tuple ever changes, migration
    0083's tagger_governed_claim_auto_tag needs a matching new migration.
    """
    rows = await db.execute(
        text("SELECT * FROM tagger_governed_claim_auto_tag(:limit, :lease_seconds)"),
        {"limit": limit, "lease_seconds": lease_seconds},
    )

    claimed: list[ClaimedItem] = []
    for row in rows.mappings():
        mime = row["mime_type"] or ""
        sec_hash = get_security_hash(row["id"])
        # szurubooru stores files as {id}_{security_hash}{ext} where
        # security_hash = HMAC-MD5(KURA_FS_NAME_SALT, str(id))[:16]. Using checksum
        # produces 404s for every post (see 2026-04-20 incident).
        ext = _disk_ext(row["id"], sec_hash, mime)
        content_url = f"{DATA_URL.rstrip('/')}/posts/{row['id']}_{sec_hash}{ext}"
        claimed.append(
            ClaimedItem(
                id=row["id"],
                kind="auto_tag",
                attempts=row["tagging_attempts"],
                lease_expires_at=row["tagging_locked_until"],
                content_url=content_url,
                mime_type=mime or None,
            )
        )
    if commit:  # False: governed_window.run commits, outside its retry
        await db.commit()
    return claimed


async def claim_pool_meta(db: AsyncSession, limit: int, lease_seconds: int) -> list[ClaimedItem]:
    now = datetime.now(timezone.utc)
    lease_until = now + timedelta(seconds=lease_seconds)

    rows = await db.execute(
        text(
            """
            WITH picked AS (
                SELECT id
                FROM pool
                WHERE meta_tagging_status = 'pending'
                  AND (meta_tagging_locked_until IS NULL OR meta_tagging_locked_until < :now)
                ORDER BY meta_tagging_locked_until NULLS FIRST, id
                LIMIT :limit
                FOR UPDATE SKIP LOCKED
            )
            UPDATE pool p
            SET meta_tagging_locked_until = :lease_until
            FROM picked
            WHERE p.id = picked.id
            RETURNING p.id, p.meta_tagging_attempts, p.meta_tagging_locked_until
            """
        ),
        {"now": now, "lease_until": lease_until, "limit": limit},
    )

    pools_meta = rows.mappings().all()
    if not pools_meta:
        return []

    ids = [r["id"] for r in pools_meta]
    detail_rows = await db.execute(
        text(
            """
            SELECT p.id AS pool_id,
                   COALESCE(
                       (SELECT pn.name FROM pool_name pn
                        WHERE pn.pool_id = p.id ORDER BY pn.ord LIMIT 1),
                       ''
                   ) AS name,
                   ARRAY(
                       SELECT pp.post_id FROM pool_post pp
                       WHERE pp.pool_id = p.id ORDER BY pp.ord
                   ) AS post_ids
            FROM pool p
            WHERE p.id = ANY(:ids)
            """
        ),
        {"ids": ids},
    )
    detail = {r["pool_id"]: r for r in detail_rows.mappings()}

    claimed: list[ClaimedItem] = []
    for r in pools_meta:
        d = detail.get(r["id"], {})
        claimed.append(
            ClaimedItem(
                id=r["id"],
                kind="pool_meta",
                attempts=r["meta_tagging_attempts"],
                lease_expires_at=r["meta_tagging_locked_until"],
                name=d.get("name") or "",
                post_ids=list(d.get("post_ids") or []),
            )
        )
    await db.commit()
    return claimed


# ── node state ───────────────────────────────────────────────────────


async def report_node_state(db: AsyncSession, host: str, source: str | None) -> None:
    """Upsert this worker's claim-time check-in into tagger_node_state
    (migration 0052, task 4328fb0e). release_id stays NULL until a real
    tagger_release registry exists -- the free-text model identifier goes
    into `detail` instead, so drift is visible via a plain SELECT today
    without waiting on that pipeline (mirrors post_tag_audit.actor_id's same
    "release id, or a model identifier if none exists yet" fallback).

    UPDATE-then-INSERT, NOT `INSERT ... ON CONFLICT DO UPDATE`: the
    primary<->trainer replication mesh (kura_topology.sql) installs a
    skip-on-conflict BEFORE INSERT trigger on every user table, including
    this one, to defend bidirectional replication against write conflicts.
    That trigger silently no-ops ANY insert (conflict-clause or not) once a
    row for the host exists -- ON CONFLICT DO UPDATE never gets a chance to
    run, so a repeat claim from an already-known host would silently stop
    updating (verified live: a second call left `detail` at its first-ever
    value). A plain UPDATE bypasses the trigger entirely (it only fires on
    INSERT) and is exactly the write pattern the mesh design expects.
    """
    result = await db.execute(
        text(
            "UPDATE tagger_node_state SET detail = :detail, reported_at = now() "
            "WHERE host = :host"
        ),
        {"host": host, "detail": source},
    )
    if result.rowcount == 0:
        await db.execute(
            text(
                "INSERT INTO tagger_node_state (host, release_id, verified, detail, reported_at) "
                "VALUES (:host, NULL, false, :detail, now())"
            ),
            {"host": host, "detail": source},
        )
    await db.commit()


# ── claim guard (plan:kura-tagger-lifecycle-redesign Step 11) ──────────


class ClaimNotEligibleError(Exception):
    """A worker's self-reported identity failed the auto_tag claim guard."""


async def enforce_claim_guard(
    db: AsyncSession, *, host: str, worker_id: str, profile: str,
    release_id: int | None, generation: int, provider: str, model_sha256: str | None,
) -> None:
    """Fail-closed compatibility gate for kind=auto_tag claims.

    Records the caller's self-reported heartbeat first (so a rejected
    worker is still visible in tagger_fleet, not silently invisible), then
    requires:
      - host has a tagger_expected_worker row with a profile assigned
      - the reported profile matches that row's profile exactly
      - the reported release_id matches tagger_release_active.release_id

    Direct fix for this session's root-cause incident: nodec/desk silently
    ran CPUExecutionProvider while their unit names implied GPU, and a
    stale-release worker could claim posts indefinitely with nobody
    checking. A worker that fails this can no longer claim at all (409),
    instead of tagging with a model nobody verified is the right one.

    Deliberately NOT gated here yet:
      - provider/model_sha256: recorded for fleet observability
        (tagger_fleet.degraded_reason) but there is no "expected provider"
        source of truth to compare against — inventing one would be
        guessing, not implementing.
      - `generation` as a queue-ownership token (distinguishing pre- vs
        post-consolidation worker code, Step 12): recorded but not yet
        compared against anything, since tagger/worker.py -- the thing
        that would give it real meaning -- does not exist yet.
    """
    try:
        await upsert_heartbeat(
            db, worker_id=worker_id, host=host, profile=profile, generation=generation,
            release_id=release_id, provider=provider, model_sha256=model_sha256,
        )
    except TransitionError as e:
        raise ClaimNotEligibleError(f"heartbeat upsert failed: {e}") from e

    expected = (await db.execute(
        text("SELECT profile FROM tagger_expected_worker WHERE host = :host"),
        {"host": host},
    )).one_or_none()
    if expected is None:
        raise ClaimNotEligibleError(f"host {host!r} has no tagger_expected_worker row (unknown host)")
    if expected.profile is None:
        raise ClaimNotEligibleError(
            f"host {host!r} has no expected profile assigned yet (node-role SoT not backfilled)"
        )
    if expected.profile != profile:
        raise ClaimNotEligibleError(
            f"host {host!r} expects profile {expected.profile!r}, worker reported {profile!r}"
        )

    active = (await db.execute(
        text("SELECT release_id, generation FROM tagger_release_active WHERE id = 1"),
    )).one_or_none()
    if active is None or active.release_id is None:
        raise ClaimNotEligibleError("no active tagger_release configured")
    if release_id != active.release_id:
        raise ClaimNotEligibleError(
            f"worker reports release_id={release_id}, active release_id={active.release_id}"
        )
    # Worker-CODE generation gate (plan:kura-tagger-self-improvement-loop —
    # user 2026-07-22: より新しいタガーを上書きできないように). release_id/
    # model_sha256 only prove the WEIGHTS are current; they do NOT prove the
    # worker's CODE applies the current behavior. The anilingus gated-veto is a
    # CODE behavior (WORKER_GENERATION bumped to 2): an old-code worker
    # hot-swaps release 6's weights but silently re-adds the tag the veto is
    # meant to cancel, overwriting a newer tagger's correction. WORKER_GENERATION
    # is a code constant (loop.py / nodec_autotag_worker.py) reported in the
    # claim; tagger_release_active.generation is the floor. A worker below the
    # floor can no longer claim (409) — it CANNOT overwrite. This is the
    # queue-ownership token 0080's docstring reserved for exactly this and left
    # unwired until the veto made it load-bearing.
    if active.generation is not None and generation < active.generation:
        raise ClaimNotEligibleError(
            f"worker code generation={generation} is below the active floor "
            f"{active.generation} (stale worker code — pull latest + restart). "
            f"A stale worker must not overwrite a newer tagger's output."
        )


# ── complete ──────────────────────────────────────────────────────────


class PostGone(ValueError):
    """The post was deleted after it was claimed; there is nothing left to complete.

    Its work row went with it (ON DELETE CASCADE), so no lease, attempts or tags
    remain to settle. The route answers 404 `tagging_post_gone` and the workers drop
    the item instead of reporting it failed (which would only meet the same 404).
    """

    def __init__(self, post_id: int) -> None:
        super().__init__(f"post {post_id} not found")
        self.post_id = post_id


async def complete_auto_tag(
    db: AsyncSession, post_id: int, status: str, tags: list[TagItem], error: str | None,
    source: str | None = None, reap_scope: str = "off",
    host: str | None = None, release_id: int | None = None,
    veto_gates: list[str] | None = None, generation: int | None = None,
    commit: bool = True,
) -> tuple[str, int, int, int]:
    """Returns (final_status, tags_applied, attempts, tags_skipped_pinned)."""
    # Claim sets and their post_tag projection are one per-post write aggregate.
    # Fence writer ownership before touching its relations, then serialize
    # completions without blocking FK key-share checks on the post identity.
    if source and status == "ok" and tags:
        claim_backend = await claim_storage.backend(db, source)
    else:
        await claim_storage.completion_lease(db)
        claim_backend = None
    # FOR NO KEY UPDATE on post (key_share=True without read=True), then the
    # attempts from the work table: the post lock is what serializes completions
    # of one post (docs/notes/tagging-work-table-plan.md, stage 2).
    locked = await db.execute(
        select(Post.post_id).where(Post.post_id == post_id)
        .with_for_update(key_share=True)
    )
    if locked.scalar_one_or_none() is None:
        raise PostGone(post_id)
    current_attempts = await tagging_queue.attempts(db, post_id)
    if current_attempts is None:
        raise ValueError(f"post {post_id} has no tagging work row")

    tags_applied = 0
    tags_skipped_pinned = 0
    added_names: list[str] = []
    removed_names: list[str] = []
    veto_removed_names: list[str] = []
    if status == "ok":
        if tags:
            # R7 (plan kura-tagger-audit-and-remediation rev6): reap only
            # runs inside this `if tags:` block, inheriting the exact same
            # guard the add-side already relies on -- a status="ok"
            # completion with an EMPTY tags list (transient inference
            # hiccup) skips this whole block, so it can never wipe a post's
            # unlocked tags. A non-"ok" completion never reaches here at
            # all (see the `else` branch below).
            names = [t.name for t in tags]
            categories = {t.name: t.category for t in tags if t.category}
            tag_objs = await get_or_create_tags(db, names, categories)
            tag_ids = [t.tag_id for t in tag_objs if t.tag_id is not None]
            if tag_ids:
                # INV-M (plan fe46c5fa Step C, migration 0057): a tag the
                # human explicitly REMOVED from this post is pinned absent —
                # a machine completion must not re-add it (the 6348/6186
                # incident). The 01_post_tag_pin_guard_ins trigger enforces
                # this unconditionally at the DB (this session never sets
                # kura.tag_actor, so it counts as machine — fail-safe); the
                # pre-filter here exists so the skip is COUNTED and visible
                # in the completion response instead of silent.
                pinned_rows = await db.execute(
                    text(
                        "SELECT tag_id FROM post_tag_pin "
                        "WHERE post_id = :pid AND state = 'absent' "
                        "AND tag_id = ANY(:ids)"
                    ),
                    {"pid": post_id, "ids": tag_ids},
                )
                pinned_ids = {r[0] for r in pinned_rows.fetchall()}
                if pinned_ids:
                    tags_skipped_pinned = len(pinned_ids)
                    tag_ids = [tid for tid in tag_ids if tid not in pinned_ids]
            # ── veto-authority reap (plan:kura-tagger-self-improvement-loop,
            # user 2026-07-22「タガーが古いタガーのタグを打ち消し続ける構造」).
            # The gated-veto is otherwise suppress-only (infer.py pops the gate
            # tag from the in-memory score dict): it stops the tag being
            # re-ADDED but never DELETES the old tagger's already-present tag,
            # and the self-scoped reap below can't touch it because the legacy
            # tag is unclaimed (8,221/8,281 anilingus rows had no claim row), so
            # an over-applied gate tag survived indefinitely. Here the veto is
            # granted DELETE authority over its OWN gate tags only — the small,
            # human-validated set the worker reports in veto_gates: for each
            # gate present on the post but ABSENT from this run's output, reap
            # it, UNLESS locked (INV-A upload-origin) or human present-pinned
            # (INV-M). Fail-closed identification for non-repudiation: fires
            # ONLY with full provenance (source+host+release_id present); the
            # deletions are merged into this completion's post_tag_audit
            # removed_tags (source='auto_tag'), attributing them to this
            # actor/host/release — the append-only trigger
            # (trg_post_tag_audit_append_only) makes that record immutable, so
            # the deleting node's tagger+veto is identified and cannot
            # repudiate the removal. (A distinct source='auto_tag_veto' marker
            # would be finer-grained but the post_tag_audit.source CHECK only
            # permits auto_tag/manual and the row replicates primary→trainer, so a
            # new value needs a coordinated CHECK migration on BOTH nodes first
            # — deferred; release_id already pins the veto-bundle + generation.)
            # Evidence gate: the worker proposes reap_domain (patched ∪ veto),
            # but the server only reaps a gate with VALIDATED GT-positive recall
            # (reap_authorized.json). Without this, a low-recall tag (smegma
            # recall 17%, 2026-07-22 dry-run) would have its true positives
            # reaped as if FPs — its 75% non-reproduction was the tagger's own
            # misses, not FPs. Authority grows as the base fine-tune lifts
            # recall. fail-closed (bad/missing file → empty → reap nothing).
            _auth = await _reap_authorized_tags(db)
            reap_gates = [g for g in (veto_gates or []) if g in _auth]
            if reap_gates and source and host and release_id is not None:
                keep_names = [t.name for t in tags]
                reaped = await db.execute(
                    text(
                        "DELETE FROM post_tag pt "
                        "USING tag_name tn "
                        "WHERE pt.tag_id = tn.tag_id "
                        "AND pt.post_id = :pid AND NOT pt.locked "
                        "AND tn.name = ANY(:gates) AND tn.name <> ALL(:keep) "
                        "AND NOT EXISTS ("
                        "  SELECT 1 FROM post_tag_pin pin "
                        "  WHERE pin.post_id = pt.post_id AND pin.tag_id = pt.tag_id "
                        "  AND pin.state = 'present') "
                        "RETURNING tn.name"
                    ),
                    {"pid": post_id, "gates": reap_gates, "keep": keep_names},
                )
                veto_removed_names = [r[0] for r in reaped.fetchall()]
            # rev7 (plan kura-tagger-audit-and-remediation, fact 00702a48):
            # full-replacement reap (rev6) is REMOVED, not just left
            # dormant — it had zero valid callers and could never gain one
            # (production runs many independent per-head specialist
            # workers, none of which represents the full tag ensemble for
            # a post; unconditional reap would have each head's completion
            # delete every OTHER head's tags — this fired live for ~7
            # minutes before the emergency fix, commit 914be3282). Replaced
            # by self-scoped retraction: an actor may retract ONLY tags IT
            # currently claims (post_tag_actor_claim, 0073) and no longer
            # proposes, never touching another actor's claims, a locked
            # upload-origin row, or a human present-pin.
            #
            # Ordering is load-bearing: (5a) self-scoped reap reads THIS
            # actor's PRE-run claim rows to find what it just dropped, so it
            # MUST run before (5b) the claim snapshot-replace overwrites
            # those same rows — reversing the order would make the reap a
            # permanent no-op (it would only ever see the just-written,
            # already-current claim set).
            if reap_scope == "own" and source and tag_ids:
                # (5a) Self-scoped reap. Candidate = tags THIS actor
                # currently claims, no longer proposes, and no OTHER actor
                # currently claims (post_tag_actor_claim reflects each live
                # actor's LATEST non-empty run via snapshot-replace, so
                # "no other actor claims it" is accurate evidence, not
                # first-adder guesswork). Positive membership (`= ANY`), not
                # `<> ALL` — this DELETE can only ever touch tags proven to
                # be this actor's own stale claims.
                reap_ids = await claim_storage.candidates(db, claim_backend, post_id, tag_ids)
                if reap_ids:
                    del_stmt = text(
                        "DELETE FROM post_tag WHERE post_id = :pid AND NOT locked "
                        "AND tag_id = ANY(:reap_ids) RETURNING tag_id"
                    )
                    deleted_ids = {
                        row[0] for row in
                        (await db.execute(del_stmt, {"pid": post_id, "reap_ids": reap_ids})).fetchall()
                    }
                    if deleted_ids:
                        del_name_rows = await db.execute(
                            select(TagName.tag_id, TagName.name)
                            .where(TagName.tag_id.in_(deleted_ids), TagName.order == 0)
                        )
                        del_name_by_id = {tid: name for tid, name in del_name_rows.all()}
                        removed_names = [del_name_by_id[tid] for tid in deleted_ids if tid in del_name_by_id]
            if source and tag_ids:
                # (5b) Claim snapshot-replace — runs for EVERY non-empty
                # completion with a reported actor, independent of this
                # completion's own reap_scope: another actor's future
                # self-scoped reap depends on this actor's claims being
                # accurate evidence, whether or not THIS run opted into
                # retraction itself. Delete-then-reinsert (not append/
                # UPSERT): an append-only table would let a still-running
                # actor accumulate claims forever, permanently blocking a
                # legitimate reap by another actor once it drops a tag
                # (codex round-2, task 19f71efe — the mirror-image of
                # rev6's over-deletion). Both statements run in this same
                # transaction, atomic with the reap above and the add-side
                # insert below.
                await claim_storage.replace(db, claim_backend, post_id, tag_ids)
            if tag_ids:
                # RETURNING under ON CONFLICT DO NOTHING yields only the rows
                # that were ACTUALLY inserted -- tags the post already had
                # are silently skipped by the conflict clause and never
                # appear here. This is the true "added" diff for the audit
                # row below, not the requested tag list (task 4328fb0e
                # review finding: a re-tag mustn't claim pre-existing tags
                # as newly added).
                stmt = pg_insert(PostTag).values(
                    [{"post_id": post_id, "tag_id": tid} for tid in tag_ids]
                ).on_conflict_do_nothing(
                    index_elements=["post_id", "tag_id"]
                ).returning(PostTag.tag_id)
                inserted_ids = {row[0] for row in (await db.execute(stmt)).fetchall()}
                tags_applied = len(inserted_ids)
                if inserted_ids:
                    # Not tag_objs[i].names: a freshly-created Tag's `names`
                    # relationship isn't populated in-memory (only flushed to
                    # the DB), and touching it here triggers an implicit
                    # lazy load that crashes under async SQLAlchemy
                    # (MissingGreenlet). A direct tag_name query sidesteps
                    # that entirely.
                    name_rows = await db.execute(
                        select(TagName.tag_id, TagName.name)
                        .where(TagName.tag_id.in_(inserted_ids), TagName.order == 0)
                    )
                    name_by_id = {tid: name for tid, name in name_rows.all()}
                    added_names = [name_by_id[tid] for tid in inserted_ids if tid in name_by_id]
            # implication 展開 (例: character → copyright を post に materialize)。
            # human が外したタグ (pin state=absent) は 01_post_tag_pin_guard_ins
            # がここ経由の再付与も落とす (INV-M — implication も機械経路)。
            # locked=False: WD14 起源の implication は reap 対象のまま (upload
            # 起源の implication は _apply_tags 側で既に locked=True 済み)。
            # delete/add の後に呼ぶ — post の最終タグ状態を反映してから展開する。
            await expand_implications(db, post_id, locked=False)
        # Provenance (task 4328fb0e, revised post plan-review fb64f308):
        # one post_tag_audit row per successful completion, not a mutable
        # snapshot on post -- a re-tag of the same post (already observed
        # live: desk -> nodec -> canonical in sequence) must not erase the
        # prior run's record. Only written when the worker actually reports
        # a source (actor_id is NOT NULL; an older/unpatched worker omitting
        # it simply produces no audit row rather than an invalid one).
        # Written even when added_names is empty -- "this run touched the
        # post and found nothing new" is itself meaningful audit history,
        # not a no-op to skip. removed_names (rev6) reflects actual reap
        # deletions, replacing the previously-hardcoded '{}'.
        if source:
            # removed_names (claim-scoped self-reap) + veto_removed_names
            # (veto-authority reap) are disjoint by construction — veto-reap
            # DELETEs its gate tags earlier in this same transaction, so the
            # claim-reap can no longer find them — so concatenation never
            # double-counts. Both are attributed to this actor/host/release in
            # one immutable audit row (non-repudiation).
            await db.execute(
                text(
                    "INSERT INTO post_tag_audit "
                    "(post_id, actor_type, actor_id, source, added_tags, removed_tags, host, release_id) "
                    "VALUES (:pid, 'script', :actor_id, 'auto_tag', :added, :removed, :host, :release_id)"
                ),
                {"pid": post_id, "actor_id": source, "added": added_names,
                 "removed": removed_names + veto_removed_names,
                 "host": host, "release_id": release_id},
            )
        # post.tagging_* are governed (0079) — go through the governed
        # function (0083) instead of a direct UPDATE.
        await db.execute(
            text("SELECT tagger_governed_complete_ok(:pid)"),
            {"pid": post_id},
        )
        final = "done"
    else:
        # Count attempts on genuine failed completions, NOT at claim time: a
        # worker that OOM-crashes mid-batch (MemoryMax=6G) never completes, so
        # counting at claim let infra crashes burn the retry budget and flip a
        # taggable post to `failed`. The increment now lives here so only real
        # error-completions count toward MAX_ATTEMPTS.
        new_attempts = current_attempts + 1
        final = "failed" if new_attempts >= MAX_ATTEMPTS else "pending"
        # post.tagging_* are governed (0079) — go through the governed
        # function (0083) instead of a direct UPDATE.
        await db.execute(
            text("SELECT tagger_governed_complete_retry(:pid, :s, :err)"),
            {"pid": post_id, "s": final, "err": error},
        )
        current_attempts = new_attempts

    if commit:  # False: governed_window.run commits, outside its retry
        await db.commit()
    return final, tags_applied, current_attempts, tags_skipped_pinned


async def complete_pool_meta(
    db: AsyncSession, pool_id: int, status: str, tags: list[TagItem], error: str | None
) -> tuple[str, int, int]:
    """Returns (final_status, tags_applied, attempts)."""
    row = await db.execute(
        select(Pool.meta_tagging_attempts).where(Pool.pool_id == pool_id)
    )
    current_attempts = row.scalar_one_or_none()
    if current_attempts is None:
        raise ValueError(f"pool {pool_id} not found")

    tags_applied = 0
    if status == "ok":
        if tags:
            names = [t.name for t in tags]
            categories = {t.name: t.category for t in tags if t.category}
            tag_objs = await get_or_create_tags(db, names, categories)
            tag_ids = [t.tag_id for t in tag_objs if t.tag_id is not None]
            if tag_ids:
                post_id_rows = await db.execute(
                    select(PoolPost.post_id).where(PoolPost.pool_id == pool_id)
                )
                post_ids = [r[0] for r in post_id_rows.all()]
                if post_ids:
                    stmt = pg_insert(PostTag).values(
                        [
                            {"post_id": pid, "tag_id": tid}
                            for pid in post_ids
                            for tid in tag_ids
                        ]
                    ).on_conflict_do_nothing(index_elements=["post_id", "tag_id"])
                    await db.execute(stmt)
                tags_applied = len(tag_ids)
        await db.execute(
            text(
                "UPDATE pool SET meta_tagging_status='done', "
                "meta_tagging_locked_until=NULL, meta_tagging_last_error=NULL "
                "WHERE id=:pid"
            ),
            {"pid": pool_id},
        )
        final = "done"
    else:
        # Count attempts on genuine failed completions, not at claim time (see
        # complete_auto_tag) — a crashed worker shouldn't burn the retry budget.
        new_attempts = current_attempts + 1
        final = "failed" if new_attempts >= MAX_ATTEMPTS else "pending"
        await db.execute(
            text(
                "UPDATE pool SET meta_tagging_status=:s, "
                "meta_tagging_attempts = meta_tagging_attempts + 1, "
                "meta_tagging_locked_until=NULL, meta_tagging_last_error=:err "
                "WHERE id=:pid"
            ),
            {"s": final, "err": error, "pid": pool_id},
        )
        current_attempts = new_attempts

    await db.commit()
    return final, tags_applied, current_attempts


# ── stats ─────────────────────────────────────────────────────────────


async def get_stats(db: AsyncSession) -> StatsResponse:
    p = await tagging_queue.stats(db)
    pool_rows = await db.execute(
        text(
            """
            SELECT
              COUNT(*) FILTER (WHERE meta_tagging_status='pending'
                               AND (meta_tagging_locked_until IS NULL OR meta_tagging_locked_until < now())) AS pending,
              COUNT(*) FILTER (WHERE meta_tagging_status='pending'
                               AND meta_tagging_locked_until IS NOT NULL AND meta_tagging_locked_until >= now()) AS in_flight,
              COUNT(*) FILTER (WHERE meta_tagging_status='done')    AS done,
              COUNT(*) FILTER (WHERE meta_tagging_status='failed')  AS failed,
              COUNT(*) FILTER (WHERE meta_tagging_status='skipped') AS skipped
            FROM pool
            """
        )
    )
    q = pool_rows.mappings().one()
    return StatsResponse(
        post=KindStats(**{k: p[k] for k in ("pending", "in_flight", "done", "failed", "skipped")}),
        pool=KindStats(**{k: q[k] for k in ("pending", "in_flight", "done", "failed", "skipped")}),
    )


# ── helpers ───────────────────────────────────────────────────────────


_MIME_EXT = {
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/png": ".png",
    "image/gif": ".gif",
    "image/webp": ".webp",
    "image/bmp": ".bmp",
    "video/mp4": ".mp4",
    "video/webm": ".webm",
    "video/quicktime": ".mov",
}


def _ext_for(mime: str) -> str:
    return _MIME_EXT.get((mime or "").lower(), "")


# Candidate extensions probed when the DB mime-type is unreliable. Ordered by
# observed frequency among mismatched posts (jpg files mislabelled webp etc).
_DISK_EXT_CANDIDATES = (
    ".jpg", ".png", ".webp", ".jpeg", ".gif", ".mp4", ".webm", ".mov", ".bmp",
)


def _disk_ext(post_id: int, sec_hash: str, mime: str) -> str:
    """Resolve the extension of the real file on disk.

    The DB ``mime-type`` is unreliable: ~8k posts are recorded as
    ``image/webp`` but stored as ``.jpg`` (and similar), which made the
    mime-derived ``content_url`` 404. The canonical filename prefix
    ``{id}_{security_hash}`` is known exactly, so probe candidate extensions
    with a cheap ``stat`` (mime-derived ext first) and use whatever actually
    exists. Falls back to the mime mapping when no file is found.
    """
    posts_dir = DATA_DIR / "posts"
    prefix = f"{post_id}_{sec_hash}"
    mime_ext = _ext_for(mime)
    ordered = ([mime_ext] if mime_ext else []) + [
        e for e in _DISK_EXT_CANDIDATES if e != mime_ext
    ]
    for ext in ordered:
        # lexists: the name, not its target. An archived original is a symlink whose target
        # is on the archive disk — absent on a node without it (kura-on-nodeb plan, rev 8),
        # and on the node with it a stat through it is a needless read of the USB HDD.
        if os.path.lexists(posts_dir / f"{prefix}{ext}"):
            return ext
    return mime_ext
