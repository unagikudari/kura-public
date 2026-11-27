"""Canonical learning progress. A lease/start event is not a worker heartbeat."""
from sqlalchemy import text

LABELS = {
    "measuring": "学習開始を記録（実行継続は未確認）",
    "registration_pending": "評価結果の登録待ち（自動再送）",
    "retry_wait": "自動再試行待ち",
    "registered": "評価履歴を登録済み（公開とは別）",
    "refused": "登録を拒否・原因の確認が必要",
    "settled": "この学習要求は終了",
}
NEXT = {
    "measuring": "observe_worker_or_recover_after_lease",
    "registration_pending": "retry_identical_registration",
    "retry_wait": "retry_after_deadline",
    "registered": "evaluate_candidate",
    "refused": "inspect_refusal",
    "settled": "await_changed_inputs",
}

async def recent(db, *, limit=50):
    if type(limit) is not int or not 1<=limit<=100:
        raise ValueError("learning_limit_invalid")
    # This support table is intentionally canonical-only. Replica APIs cannot
    # report it; absence means no observation, never idle or completed.
    present=(await db.execute(text(
        "SELECT to_regclass('public.tagger_learning_execution') IS NOT NULL"
    ))).scalar_one()
    if not present:
        return []
    rows=(await db.execute(text("""
        WITH latest AS (
          SELECT DISTINCT ON (x.tag_id) x.*,e.tag,e.occurred_at AS lifecycle_at
          FROM tagger_learning_execution x JOIN tagger_lifecycle_event e ON e.id=x.event_id
          ORDER BY x.tag_id,x.event_id DESC
        )
        SELECT latest.*,extract(epoch FROM updated_at)::double precision AS observed_at,
          greatest(0,extract(epoch FROM clock_timestamp()-updated_at))::double precision AS age_seconds,
          lease.expires_at AS lease_expires_at
        FROM latest
        LEFT JOIN tagger_lifecycle_lock lease ON latest.state='measuring'
          AND lease.scope='lifecycle:' || latest.tag AND lease.expires_at>clock_timestamp()
          AND NOT EXISTS (
            SELECT FROM tagger_lifecycle_event newer WHERE newer.tag_id=latest.tag_id
              AND (newer.occurred_at,newer.id)>(latest.lifecycle_at,latest.event_id)
          )
        ORDER BY updated_at DESC,event_id DESC LIMIT :limit
    """),{"limit":limit})).mappings().all()
    return [{
        "tag":r["tag"],"event":r["event_id"],
        "state":{"registration_pending":"measured","retry_wait":"held","refused":"stopped","settled":"stopped"}.get(r["state"],r["state"]),
        "execution_state":r["state"],"label":LABELS[r["state"]],
        "fresh":False,"runtime_verified":False,
        "observed_at":r["observed_at"],"age_seconds":r["age_seconds"],
        "retry_after":r["retry_after"].isoformat() if r["retry_after"] else None,
        "lease_expires_at":r["lease_expires_at"].isoformat() if r["lease_expires_at"] else None,
        "failure_count":r["failure_count"],"reason":r["reason"],
        "next":NEXT[r["state"]],"source":"canonical_learning_execution",
    } for r in rows]

async def current(db):
    rows=await recent(db,limit=1)
    return rows[0] if rows else None
