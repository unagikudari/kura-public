"""Bounded online copying: lock source rows before insert, mirrors own all live changes."""
from sqlalchemy import text


async def copy_batch(db, limit=10000):
    if not 1 <= limit <= 50000:
        raise ValueError('Invalid copy budget')
    state=(await db.execute(text('SELECT * FROM claim_storage.migration FOR UPDATE NOWAIT'))).mappings().one()
    if state['phase']!='copying':
        return {'phase':state['phase'],'rows':0,'copied_rows':state['copied_rows']}
    # SHARE conflicts with UPDATE/DELETE. A delete cannot finish mirroring and
    # then have a stale copied row resurrected. Keep these locks through commit.
    rows=(await db.execute(text('''SELECT post_id,tag_id,actor_id,claimed_at
      FROM public.post_tag_actor_claim
      WHERE (post_id,tag_id,actor_id)>(:post,:tag,:actor)
      ORDER BY post_id,tag_id,actor_id LIMIT :lim FOR SHARE'''),
      {'post':state['cursor_post'],'tag':state['cursor_tag'],'actor':state['cursor_actor'],'lim':limit})).mappings().all()
    if not rows:
        await db.execute(text("UPDATE claim_storage.migration SET phase='verifying',updated_at=clock_timestamp()"))
        return {'phase':'verifying','rows':0,'copied_rows':state['copied_rows']}
    actors=sorted({r['actor_id'] for r in rows})
    await db.execute(text('INSERT INTO claim_storage.actor(actor_id) SELECT unnest(CAST(:actors AS text[])) ON CONFLICT DO NOTHING'),{'actors':actors})
    keys=dict((await db.execute(text('SELECT actor_id,id FROM claim_storage.actor WHERE actor_id=ANY(CAST(:actors AS text[]))'),{'actors':actors})).all())
    await db.execute(text('''INSERT INTO claim_storage.claim(post_id,tag_id,actor_key,claimed_at)
      SELECT * FROM unnest(CAST(:posts AS int[]),CAST(:tags AS int[]),CAST(:actors AS bigint[]),CAST(:times AS timestamptz[]))
      ON CONFLICT(post_id,tag_id,actor_key) DO UPDATE SET claimed_at=EXCLUDED.claimed_at
      WHERE claim_storage.claim.claimed_at IS DISTINCT FROM EXCLUDED.claimed_at'''),
      {'posts':[r['post_id'] for r in rows],'tags':[r['tag_id'] for r in rows],
       'actors':[keys[r['actor_id']] for r in rows],'times':[r['claimed_at'] for r in rows]})
    last=rows[-1]
    await db.execute(text('''UPDATE claim_storage.migration SET cursor_post=:post,cursor_tag=:tag,
      cursor_actor=:actor,copied_rows=copied_rows+:count,updated_at=clock_timestamp()'''),
      {'post':last['post_id'],'tag':last['tag_id'],'actor':last['actor_id'],'count':len(rows)})
    return {'phase':'copying','rows':len(rows),'copied_rows':state['copied_rows']+len(rows)}


async def verify_batch(db, span=1000):
    if not 1 <= span <= 10000:
        raise ValueError('Invalid verification budget')
    state=(await db.execute(text('SELECT * FROM claim_storage.migration FOR UPDATE NOWAIT'))).mappings().one()
    if state['phase'] not in ('verifying','ready'):
        raise ValueError('Copy must finish before verification')
    if state['phase']=='ready':
        return {'phase':'ready','differences':0}
    low=state['verified_through']
    # Jump to the next populated source OR target key; never skip an extra row
    # in the target just because its post disappeared from the source.
    next_id=(await db.execute(text('''SELECT min(post_id) FROM (
      (SELECT post_id FROM public.post_tag_actor_claim WHERE post_id>:low ORDER BY post_id LIMIT 1)
      UNION ALL (SELECT post_id FROM claim_storage.claim WHERE post_id>:low ORDER BY post_id LIMIT 1)) q'''),{'low':low})).scalar()
    if next_id is None:
        await db.execute(text("UPDATE claim_storage.migration SET phase='ready',updated_at=clock_timestamp()"))
        return {'phase':'ready','differences':0}
    high=min(next_id+span-1,2147483647)
    differences=(await db.execute(text('''WITH old AS MATERIALIZED (
      SELECT post_id,tag_id,actor_id,claimed_at FROM public.post_tag_actor_claim WHERE post_id BETWEEN :lo AND :hi),
      new AS MATERIALIZED (SELECT c.post_id,c.tag_id,a.actor_id,c.claimed_at FROM claim_storage.claim c
        JOIN claim_storage.actor a ON a.id=c.actor_key WHERE c.post_id BETWEEN :lo AND :hi),
      missing AS (SELECT * FROM old EXCEPT ALL SELECT * FROM new),
      extra AS (SELECT * FROM new EXCEPT ALL SELECT * FROM old)
      SELECT (SELECT count(*) FROM missing)+(SELECT count(*) FROM extra)'''),{'lo':next_id,'hi':high})).scalar_one()
    if differences:
        raise ValueError(f'Claim verification failed: {differences} differences; no cutover permitted')
    await db.execute(text('UPDATE claim_storage.migration SET verified_through=:hi,updated_at=clock_timestamp()'),{'hi':high})
    return {'phase':'verifying','differences':0,'verified_through':high}
