"""Explicit queue-bound review. Unknown is an answer, never an absent pin."""
import asyncio,hashlib,json,re
from api import media_role
from api.services import renditions
from api.services.pool_visibility import post_visible_sql
from sqlalchemy import text
from api.errors import KuraError
from api.tagger_core.post_exclusion import not_excluded
from api.services.review_queues import load_queues,ReviewQueueRefused

async def bounded_transaction(db):
 """Finite waits apply to each statement, including advisory and row locks."""
 await db.execute(text("SET LOCAL statement_timeout='5s'"))
 await db.execute(text("SET LOCAL lock_timeout='1s'"))

def digest(value):return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':'),allow_nan=False).encode()).hexdigest()
def refuse(reason):raise KuraError(409,reason,fix='Reload this review image and its context before judging again.')
def select_queue(ref,rows):
 if not re.fullmatch("[a-f0-9]{64}",ref):refuse("human_review_queue_unknown")
 found=[q for q in (rows or []) if q['queue_sha256']==ref and q['schema'] in ('human-review-set/2','human-review-set/3')]
 if len(found)!=1:refuse('human_review_queue_unknown')
 return found[0]

def queue(ref):
 """Legacy file reader, retained only for pre-migration compatibility."""
 try:return select_queue(ref,load_queues())
 except ReviewQueueRefused:
  raise KuraError(503,'human_review_queue_unavailable',fix='Review request integrity could not be verified.') from None

async def resolve_queue(db,ref):
 from api.services.review_set_registry import available,by_ref,catalog_available
 if not await available(db):
  if await catalog_available(db):refuse('human_review_queue_unknown')
  return await asyncio.to_thread(queue,ref)
 try:return select_queue(ref,await by_ref(db,ref))
 except ReviewQueueRefused:
  raise KuraError(503,'human_review_queue_unavailable',fix='Review request integrity could not be verified.') from None
async def reviewable_post_ids(db, queue):
 from api.services.review_adjudication import is_adjudication
 if queue["purpose"] not in ("historical_label_recheck", "training_support") or is_adjudication(queue):
  rows = await db.execute(text(
   "SELECT p.id FROM post p WHERE p.id=ANY(:ids) AND " + post_visible_sql("p.id")
  ), {"ids": queue["post_ids"]})
  allowed = set(rows.scalars())
  return [pid for pid in queue["post_ids"] if pid in allowed]
 from api.tagger_core.review_censor_variants import review_candidate
 variant_condition = review_candidate("p.id",":tag")
 allowed=(await db.execute(text(
  "SELECT p.id FROM post p WHERE p.id=ANY(:ids) AND p.status='ok' AND "
  + post_visible_sql("p.id") + " AND " + not_excluded("p.id") + " AND (" + variant_condition + """
      OR EXISTS (SELECT 1 FROM tagger_human_review_event e
                 WHERE e.queue_sha256=:ref AND e.post_id=p.id))"""
 ),{"ids":queue["post_ids"],"tag":queue["tag"],"ref":queue["queue_sha256"]})).scalars().all()
 ids=set(allowed)
 return [pid for pid in queue["post_ids"] if pid in ids]

def rendition_hash(sha,mime):
 path=renditions.path_for(bytes(sha),mime)
 try:
  with path.open('rb') as f:
   h=hashlib.sha256();total=0
   while part:=f.read(262144):
    total+=len(part)
    if total>16*1024*1024:refuse('human_review_media_too_large')
    h.update(part)
 except FileNotFoundError:return None  # content reader falls back to post.data
 except OSError:refuse('human_review_media_unreadable')
 if h.digest()!=bytes(sha):refuse('human_review_rendition_corrupt')
 return h.hexdigest()

def is_blind_evaluation(queue):
 return queue['source_provenance']['kind'] in ('base_comparison','dedicated_candidate')


def blind_image_context(context):
 # Explicit allowlist so future audit fields stay server-side.
 return {key:context[key] for key in ('queue_sha256','post_id','tag_id','tag','display_sha256')}


async def context(db,q,pid,lock=False):
 await bounded_transaction(db)
 if media_role.MEDIA_ROLE!='authority':refuse('human_review_media_authority_required')
 if pid not in q['post_ids']:refuse('human_review_post_outside_queue')
 if lock:
  await db.execute(text('SELECT id FROM post WHERE id=:p FOR UPDATE'),{'p':pid})
  await db.execute(text('SELECT tag_id FROM post_tag WHERE post_id=:p FOR UPDATE'),{'p':pid})
  # Existing rendition replacement must not race the accepted display context.
  # New rows require the post FK key lock and are blocked by the post lock.
  await db.execute(text("SELECT post_id FROM post_rendition WHERE post_id=:p AND kind='view' FOR SHARE"),{'p':pid})
 visible = await db.scalar(text("SELECT " + post_visible_sql(":pid")), {"pid": pid})
 if not visible:refuse('human_review_post_hidden')
 tag=(await db.execute(text('SELECT tag_id FROM tag_name WHERE name=:n AND ord=0'+(' FOR SHARE' if lock else '')),{'n':q['tag']})).scalar_one_or_none()
 if tag is None:refuse('human_review_tag_changed')
 row=(await db.execute(text("SELECT p.version,p.status,p.type,p.media_location,encode(CASE WHEN p.media_location='pg' THEN sha256(p.data) END,'hex') AS media_sha256,o.sha256 AS rendition_sha256,o.mime AS rendition_mime FROM post p LEFT JOIN post_rendition r ON r.post_id=p.id AND r.kind='view' LEFT JOIN rendition_object o ON o.sha256=r.sha256 WHERE p.id=:p"),{'p':pid})).mappings().one_or_none()
 if row is None or row['status']!='ok' or row['type']!='image':refuse('human_review_media_unavailable')
 display_sha=row['media_sha256']
 if row['rendition_sha256'] is not None:
  try:
   display_sha=(await asyncio.wait_for(asyncio.to_thread(rendition_hash,row['rendition_sha256'],row['rendition_mime']),8)) or display_sha
  except TimeoutError:refuse('human_review_media_read_timeout')
 if display_sha is None:refuse('human_review_legacy_fs_media_unsupported')
 if display_sha!=q['media_sha256'][str(pid)]:refuse('human_review_media_changed')
 pin=(await db.execute(text('SELECT state,pinned_at,audit_id,pinned_by FROM post_tag_pin WHERE post_id=:p AND tag_id=:t'+(' FOR UPDATE' if lock else '')),{'p':pid,'t':tag})).mappings().one_or_none()
 on=(await db.execute(text('SELECT EXISTS(SELECT 1 FROM post_tag WHERE post_id=:p AND tag_id=:t)'),{'p':pid,'t':tag})).scalar_one()
 ev=(await db.execute(text('SELECT id,decision,actor_name,received_at FROM tagger_human_review_event WHERE queue_sha256=:q AND post_id=:p ORDER BY id DESC LIMIT 1'),{'q':q['queue_sha256'],'p':pid})).mappings().one_or_none()
 ctx=dict(queue_sha256=q['queue_sha256'],post_id=pid,tag_id=tag,tag=q['tag'],post_version=row['version'],display_sha256=display_sha,tag_present=on,pin=None if pin is None else dict(pin,state=pin['state'],pinned_at=pin['pinned_at'].isoformat()),previous_event_id=ev['id'] if ev else None)
 # Keep the evidence/version check for what is judged, without encoding
 # the existing tag or pin (or the tag-dependent post edit version) in a
 # publicly reproducible token. Blind answers do not mutate any of them.
 digest_input = dict(blind_image_context(ctx), previous_event_id=ctx['previous_event_id']) if is_blind_evaluation(q) else ctx
 return dict(context=ctx,context_sha256=digest(digest_input),previous_judgment=None if ev is None else dict(ev,received_at=ev['received_at'].isoformat()),content_url=f'/api/post/{pid}/content')

async def apply_human_decision(db,ctx,state,actor):
 """Same normal human tag/audit/pin rules, one-tag scope, no commit here."""
 pid,tid,tag=ctx['post_id'],ctx['tag_id'],ctx['tag'];want=state=='present';changed=ctx['tag_present']!=want
 await db.execute(text("SELECT set_config('kura.tag_actor','human',true)"))
 if changed:
  if want:await db.execute(text('INSERT INTO post_tag(post_id,tag_id) VALUES(:p,:t) ON CONFLICT DO NOTHING'),{'p':pid,'t':tid})
  else:await db.execute(text('DELETE FROM post_tag WHERE post_id=:p AND tag_id=:t'),{'p':pid,'t':tid})
  await db.execute(text("UPDATE post SET version=version+1,last_edit_time=(now() AT TIME ZONE 'UTC') WHERE id=:p"),{'p':pid})
 audit=(await db.execute(text("INSERT INTO post_tag_audit(post_id,actor_type,actor_id,source,added_tags,removed_tags) VALUES(:p,'human',:a,'manual',:adds,:removes) RETURNING id"),{'p':pid,'a':actor,'adds':[tag] if changed and want else [],'removes':[tag] if changed and not want else []})).scalar_one()
 await db.execute(text('INSERT INTO post_tag_pin(post_id,tag_id,state,pinned_by,audit_id) VALUES(:p,:t,:s,:a,:audit) ON CONFLICT(post_id,tag_id) DO UPDATE SET state=EXCLUDED.state,pinned_by=EXCLUDED.pinned_by,audit_id=EXCLUDED.audit_id,pinned_at=now()'),{'p':pid,'t':tid,'s':state,'a':actor,'audit':audit})
 actual=(await db.execute(text('SELECT EXISTS(SELECT 1 FROM post_tag WHERE post_id=:p AND tag_id=:t)'),{'p':pid,'t':tid})).scalar_one()
 if actual!=want:refuse('human_review_tag_mutation_refused')
 return audit

async def submit(db,q,pid,body,user,*,group_review=None):
 await bounded_transaction(db)
 if (await db.execute(text("SELECT current_setting('transaction_isolation')"))).scalar_one()!='read committed':
  refuse('human_review_transaction_isolation_unsupported')
 payload=dict(queue_sha256=q['queue_sha256'],post_id=pid,**body)
 request_sha=digest(payload)
 # Serialize UUID across distinct posts/queues too: one request cannot produce two events.
 await db.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:key,0))"),{'key':f'human-review:{user.user_id}:{body["request_id"]}'})
 old=(await db.execute(text('SELECT id,request_sha256,decision FROM tagger_human_review_event WHERE actor_id=:a AND request_id=CAST(:r AS uuid)'),{'a':user.user_id,'r':body['request_id']})).mappings().one_or_none()
 if old:
  if old['request_sha256']!=request_sha:refuse('human_review_request_reused')
  return dict(outcome='succeeded',state='judgment_recorded',event_id=old['id'],decision=old['decision'],replayed=True,quality_granted=False)
 if q['purpose']=='historical_label_recheck':
  from api.services.review_reselection import lock_queue, retired_refs
  await lock_queue(db,q['queue_sha256'])
  if q['queue_sha256'] in (await retired_refs(db) or set()):
   refuse('human_review_queue_retired')
 from api.services.review_adjudication import has_adjudicated, is_adjudication, check_reviewer
 third_review=is_adjudication(q)
 if third_review or q['purpose'] in ('candidate_evaluation','production_precision','base_comparison'):
  from api.services.review_set_registry import lock_evaluation_answers
  await lock_evaluation_answers(db,q['tag'])
 current=await context(db,q,pid,lock=True)
 if pid not in await reviewable_post_ids(db,q):refuse('human_review_post_unavailable')
 if current['context_sha256']!=body['expected_context_sha256']:refuse('human_review_context_stale')
 ctx=current['context'];audit=None
 if q['purpose']=='candidate_evaluation' and await has_adjudicated(db,q,pid,user.user_id):
  refuse('human_review_independent_reviewer_required')
 if third_review:
  reviewer=await check_reviewer(db,q,pid,user.user_id)
  if reviewer.original:refuse('human_review_independent_reviewer_required')
  if not reviewer.disputed:refuse('human_review_disagreement_resolved')
  if (current.get('previous_judgment') or {}).get('decision') in ('present','absent'):
   refuse('human_review_disagreement_resolved')
 if not third_review and q['schema']!='human-review-set/3' and body['decision']!='uncertain':
  audit=await apply_human_decision(db,ctx,body['decision'],user.name)
 source=dict(queue={k:q[k] for k in ('schema','tag','purpose','frame_sha256','model_sha256','post_ids','source_provenance','media_sha256')},observed=ctx,provenance='authenticated_explicit_user_response',not_independence_proof=True)
 if group_review is not None:source['group_review']=group_review
 event=(await db.execute(text('INSERT INTO tagger_human_review_event(actor_id,actor_name,request_id,request_sha256,queue_sha256,post_id,decision,source_context,previous_event_id,audit_id) VALUES(:a,:n,CAST(:r AS uuid),:sha,:q,:p,:d,CAST(:source AS jsonb),:previous,:audit) RETURNING id'),{'a':user.user_id,'n':user.name,'r':body['request_id'],'sha':request_sha,'q':q['queue_sha256'],'p':pid,'d':body['decision'],'source':json.dumps(source),'previous':ctx['previous_event_id'],'audit':audit})).scalar_one()
 return dict(outcome='succeeded',state='judgment_recorded',event_id=event,decision=body['decision'],replayed=False,quality_granted=False)
