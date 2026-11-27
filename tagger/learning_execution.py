"""Canonical durable learning state; lifecycle leases fence every write."""
from __future__ import annotations
from contextlib import asynccontextmanager
import json
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

async def read(db, event_id):
    row = (await db.execute(text(
        "SELECT *, retry_after IS NULL OR retry_after<=clock_timestamp() AS due "
        "FROM tagger_learning_execution WHERE event_id=:event"),
        {"event": event_id})).mappings().one_or_none()
    return dict(row) if row is not None else None

async def step(db, *, tag, event_id, lease, previous, action, payload=None):
    row = (await db.execute(text(
        "SELECT * FROM tagger_learning_step(:event,:tag,:token,:revision,:action,CAST(:payload AS jsonb))"),
        {"event":event_id,"tag":tag,"token":lease.fencing_token,
         "revision":previous["revision"] if previous else 0,"action":action,
         "payload":json.dumps(payload,allow_nan=False) if payload is not None else None}
    )).mappings().one()
    await db.commit()
    return dict(row)

@asynccontextmanager
async def measurement_lock(db, event_id):
    """Serialize observers across controllers without blocking lifecycle writes."""
    key = f"tagger-remote-learning:{event_id}"
    async with db.bind.connect() as conn:
        try:
            claimed, backend_pid = (await conn.execute(text(
                "SELECT pg_try_advisory_lock(hashtextextended(:key, 0)), pg_backend_pid()"
            ), {"key": key})).one()
            await conn.commit()
            yield (conn, backend_pid) if claimed else None
        finally:
            # A connection with a session advisory lock must never return to
            # the pool. Invalidate even if the observer is cancelled.
            await conn.invalidate()


async def run(tag, event_id, state, state_path, args):
    from . import learn

    async with learn.async_session() as db:
        async with measurement_lock(db, event_id) as claim:
            if claim is None:
                return "busy"
            return await _run(tag, event_id, state, state_path, args, claim)


async def _run(tag, event_id, state, state_path, args, claim):
    # Import lazily so learn's CLI and tests use the same session factory.
    from . import learn
    from api.tagger_core.transitions import acquire_lease, release_lease, TransitionError
    import asyncio
    key=learn.key_of(tag,event_id)
    remote = bool(getattr(args, "remote_config", None))
    measurement_timeout = int(args.lease_hours * 3600) - learn.LEASE_MARGIN_S
    lease_seconds = learn.LEASE_MARGIN_S if remote else int(args.lease_hours * 3600)
    scope = "lifecycle:" + tag
    async with learn.async_session() as db:
        try:
            lease=await acquire_lease(db,scope,lease_seconds=lease_seconds)
        except TransitionError:
            learn.log("lease.busy",tag=tag)
            return "busy"
        row=None
        async def advance(action,payload=None):
            nonlocal row
            row=await step(db,tag=tag,event_id=event_id,lease=lease,previous=row,action=action,payload=payload)
        try:
            row=await read(db,event_id)
            queued=await learn.still_queued(db,tag,event_id)
            await db.rollback()
            if row and row["state"] in ("registered","refused","settled"):
                return "moved"
            if not queued:
                if row:
                    registered=(await db.execute(text(
                        "SELECT 1 FROM tagger_registration_request WHERE lifecycle_event_id=:event LIMIT 1"),
                        {"event":event_id})).first() is not None
                    latest=(await db.execute(text(
                        "SELECT id FROM tagger_lifecycle_event WHERE tag_id=:tag_id ORDER BY occurred_at DESC,id DESC LIMIT 1"),
                        {"tag_id":row["tag_id"]})).scalar_one()
                    await db.rollback()
                    if registered and row["pending_payload"] is not None:
                        await advance("registered")
                    elif registered or latest!=event_id:
                        await advance("settle")
                    else:
                        # A temporary human disposition removes this event from
                        # the runnable queue, but does not finish the work.
                        return "human"
                return "moved"
            if row and not row["due"]:
                return "backoff"
            payload=row["pending_payload"] if row else None
            worker = getattr(args, "worker_profile", None)
            if payload is None and worker is not None:
                from tagger.remote_learning import assigned_worker_matches
                if not assigned_worker_matches(tag, event_id, worker):
                    return "other_worker"
            # One-way recovery of an old outbox, under the same live lease. The
            # legacy file stays untouched; after import PostgreSQL is the truth.
            if row is None:
                payload=state.get("pending",{}).get(key)
                await advance("import",payload) if payload is not None else await advance("start")
            if payload is None:
                if row["state"]!="measuring": await advance("start")
                # The remote controller and worker own their per-job locks and
                # immutable receipts. The DB lease fences mutations only.
                if remote:
                    if not await release_lease(db, lease):
                        return "busy"
                    lease = None
                outcome=await asyncio.to_thread(learn.run_child,tag,event_id,measurement_timeout,args)
                conn, backend_pid = claim
                try:
                    current_backend = await conn.scalar(text("SELECT pg_backend_pid()"))
                    await conn.commit()
                except DBAPIError:
                    return "busy"
                if current_backend != backend_pid:
                    return "busy"
                if remote:
                    try:
                        lease = await acquire_lease(db, scope, lease_seconds=lease_seconds)
                    except TransitionError:
                        return "busy"
                    current = await read(db, event_id)
                    queued = await learn.still_queued(db, tag, event_id)
                    await db.rollback()
                    if not queued or current is None or current["revision"] != row["revision"]:
                        return "moved"
                kind=(outcome or {}).get("outcome")
                payload=(outcome or {}).get("registration")
                if payload is None:
                    if kind=="escalated":
                        blocking=[e for e in outcome.get("escalations",[]) if e.get("blocking")]
                        unverified=[e for e in blocking if "missing_check" in (e.get("detail") or {})]
                        if blocking and not unverified:
                            # Preserve existing human disposition semantics. Text
                            # never goes into the execution state/log; legacy
                            # lifecycle reasons remain in their existing channel.
                            reasons="; ".join(str(e.get("summary","")) for e in blocking)
                            done=await learn.defer_structural(db,tag,event_id,lease,reasons)
                            await advance("human_hold" if done=="human" else "settle")
                            return done
                    if kind and kind.endswith("(gt-unchanged)"):
                        from api.tagger_core.transitions import record_lifecycle_event
                        await record_lifecycle_event(db,tag=tag,from_stage="training",to_stage="candidate",
                            reason="settled: GT unchanged since the last registration",lease=lease)
                        await advance("settle")
                        return "settled"
                    if kind=="noop":
                        await advance("child_failed")
                        return "failed"
                    await advance("child_failed")
                    return "failed"
                if payload.get("lifecycle_event_id") not in (None,0,event_id):
                    await advance("child_failed")
                    return "failed"
                payload=dict(payload,lifecycle_event_id=event_id)
                review = (payload.get("metrics") or {}).get("production_review") or {}
                if review.get("scope") == "catalog_extension_outside_frozen_gt_groups":
                    size = (await db.execute(text(
                        "SELECT octet_length(CAST(:payload AS jsonb)::text)"
                    ), {"payload": json.dumps(payload, allow_nan=False)})).scalar_one()
                    if size > 1_048_576:
                        learn.log("catalog_extension.payload_too_large", tag=tag, ev=event_id, bytes=size)
                        await advance("child_failed")
                        return "failed"
                    learn.log("catalog_extension.payload_size", tag=tag, ev=event_id, bytes=size)
                await advance("payload",payload)
            try:
                status,reply=await asyncio.to_thread(learn.post_registration,payload,lease)
            except Exception:
                await advance("registration_unavailable")
                return "failed"
            if status==200:
                await advance("registered")
                if row["state"]!="registered":
                    return "refused"
                learn.log("registered",tag=tag,ev=event_id,id=reply.get("id"))
                return "registered"
            if status==409:
                # A lease/lifecycle race is transient; never make an unparsed
                # 409 a permanently stuck training event.
                try:
                    detail=json.loads(reply) if isinstance(reply,str) else reply
                    error=detail.get("detail",detail).get("error")
                except (ValueError,TypeError,AttributeError):
                    error=None
                if error=="event_already_has_a_result":
                    await advance("refuse")
                    return "refused"
            await advance("registration_failed")
            return "failed"
        finally:
            await db.rollback()
            if lease is not None:
                try: await release_lease(db,lease)
                except TransitionError: pass


async def settle_consumed(args, *, limit=64):
    """Close outboxes even when their event has left the training queue.

    Read ordering is bounded; each actual transition rechecks under the lifecycle
    lease. Successful HTTP replies are not needed to learn the committed result.
    """
    from . import learn
    async with learn.async_session() as db:
        rows=(await db.execute(text("""
            SELECT x.event_id,e.tag
            FROM tagger_learning_execution x
            JOIN tagger_lifecycle_event e ON e.id=x.event_id
            WHERE x.state NOT IN ('registered','refused','settled')
              AND (EXISTS(SELECT FROM tagger_registration_request r WHERE r.lifecycle_event_id=x.event_id)
                OR EXISTS(SELECT FROM tagger_lifecycle_event newer WHERE newer.tag_id=x.tag_id
                   AND (newer.occurred_at,newer.id)>(e.occurred_at,e.id)))
            ORDER BY x.updated_at,x.event_id LIMIT :limit
        """),{"limit":limit})).mappings().all()
    for row in rows:
        await run(row["tag"],row["event_id"],{},None,args)
    return len(rows)
