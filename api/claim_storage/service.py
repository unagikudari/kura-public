"""Claim snapshot replacement and retraction over integer identity references."""
from dataclasses import dataclass
from sqlalchemy import text
from . import legacy


@dataclass(frozen=True)
class Backend:
    actor: str
    key: int | None
    normalized: bool


# Database-local, transaction-scoped writer-ownership fence. Shared completion
# leases do not dirty a singleton heap tuple or create row-lock WAL/multixacts.
WRITER_LOCK_KEY = 98432275


async def transition_lease(db):
    await db.execute(text('SELECT pg_advisory_xact_lock(:key)'),{'key':WRITER_LOCK_KEY})


async def completion_lease(db):
    await db.execute(text('SELECT pg_advisory_xact_lock_shared(:key)'),{'key':WRITER_LOCK_KEY})


async def backend(db,actor):
    await completion_lease(db)
    # Do not lock actor relations until AFTER the ownership lease. Otherwise a
    # transition holding the fence and waiting for relation locks can deadlock.
    row=(await db.execute(text('''SELECT m.phase,
      (SELECT id FROM claim_storage.actor WHERE actor_id=:actor) AS key
      FROM claim_storage.migration m'''),{'actor':actor})).one()
    return Backend(actor,row.key,row.phase=='active')


async def candidates(db,source,pid,keep_ids):
    if not source.normalized:
        return await legacy.candidates(db,pid,source.actor,keep_ids)
    if source.key is None:
        return []
    rows=await db.execute(text('''SELECT c.tag_id FROM claim_storage.claim c
      WHERE c.post_id=:pid AND c.actor_key=:actor AND c.tag_id<>ALL(CAST(:keep AS int[]))
      AND NOT EXISTS(SELECT 1 FROM claim_storage.claim o
        WHERE o.post_id=:pid AND o.tag_id=c.tag_id AND o.actor_key<>:actor)'''),
      {'pid':pid,'actor':source.key,'keep':keep_ids})
    return list(rows.scalars())


async def replace(db,source,pid,tag_ids):
    if not tag_ids:
        raise ValueError('A claim snapshot requires a nonempty completion')
    if not source.normalized:
        return await legacy.replace(db,pid,source.actor,tag_ids)
    key=source.key
    if key is None:
        key=(await db.execute(text('''INSERT INTO claim_storage.actor(actor_id) VALUES(:actor)
          ON CONFLICT DO NOTHING RETURNING id'''),{'actor':source.actor})).scalar()
        if key is None:
            key=(await db.execute(text('SELECT id FROM claim_storage.actor WHERE actor_id=:actor'),{'actor':source.actor})).scalar_one()
    await db.execute(text('DELETE FROM claim_storage.claim WHERE post_id=:pid AND actor_key=:actor'),{'pid':pid,'actor':key})
    await db.execute(text('''INSERT INTO claim_storage.claim(post_id,tag_id,actor_key)
      SELECT :pid,x,:actor FROM unnest(CAST(:tags AS int[])) x'''),{'pid':pid,'actor':key,'tags':tag_ids})
