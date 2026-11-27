"""Real private PG18 full baseline + asyncpg under kura, never live state."""
import asyncio,hashlib,importlib,json,subprocess,sys,uuid
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlsplit,parse_qs
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine,async_sessionmaker
sys.path.insert(0,str(Path(__file__).parent))
from full_schema import cluster,load
from api.services import human_review as s
from api.routers import human_review as routes
from api.errors import KuraError
M=importlib.import_module('api.alembic.versions.0200_human_review_event')
USER=SimpleNamespace(user_id=42,name='reviewer')
@pytest.fixture(scope='module')
def pg():
 with cluster() as c:yield c
@pytest.fixture
def url(pg):
 u=load(*pg);v=urlsplit(u);q=parse_qs(v.query)
 sql="""ALTER ROLE kura LOGIN; ALTER TABLE pool ADD COLUMN hidden boolean NOT NULL DEFAULT false; INSERT INTO tag(id,category_id,creation_time,version) VALUES(1,(SELECT id FROM tag_category ORDER BY id LIMIT 1),now(),1); INSERT INTO tag_name(tag_name_id,tag_id,name,ord) VALUES(1,1,'fixture',0);
 INSERT INTO post(id,creation_time,safety,type,checksum,"mime-type",version,status,data,media_location) VALUES(101,now(),'safe','image','fixture101','image/png',1,'ok',decode('010203','hex'),'pg'),(102,now(),'safe','image','fixture102','image/png',1,'ok',decode('010203','hex'),'pg');
 SELECT set_config('kura.tag_actor','human',true); INSERT INTO post_tag(post_id,tag_id) VALUES(101,1); INSERT INTO post_tag_pin(post_id,tag_id,state,pinned_by) VALUES(101,1,'present','old'),(102,1,'absent','old'); SET ROLE kura_reconciler; INSERT INTO tagger_gt_label(tag,post_id,verdict,derived_from,tag_id) VALUES('fixture',101,'positive','human_correction',1),('fixture',102,'negative','human_correction',1); RESET ROLE;"""
 subprocess.run(['/usr/lib/postgresql/18/bin/psql','-h',q['host'][0],'-p',q['port'][0],'-U','postgres','-d',v.path.lstrip('/'),'-XAtq','-v','ON_ERROR_STOP=1','-c',sql],check=True,capture_output=True)
 return u.replace('postgres@','kura@',1)

def queue():
 return dict(schema='human-review-set/2',queue_sha256='a'*64,tag='fixture',purpose='historical_label_recheck',frame_sha256='b'*64,model_sha256=None,post_ids=[101,102],media_sha256={str(i):hashlib.sha256(b'\1\2\3').hexdigest() for i in (101,102)},source_provenance={'kind':'historical_human_training','artifact_sha256':'c'*64})
async def setup(url):
 engine=create_async_engine(url);sessions=async_sessionmaker(engine,expire_on_commit=False)
 async with sessions() as db:
  for stmt in M.STATEMENTS:await db.execute(text(stmt))
  await db.commit()
 return engine,sessions
async def snapshot(db):
 return {t:(await db.execute(text(f'SELECT coalesce(jsonb_agg(to_jsonb(x)),\'[]\'::jsonb) FROM (SELECT * FROM {t} ORDER BY 1,2) x'))).scalar_one() for t in ('post_tag','post_tag_pin','tagger_gt_label','post_tag_audit')}
async def submit(sessions,q,pid,decision,body=None):
 async with sessions() as db:
  ctx=await s.context(db,q,pid)
  body=body or dict(request_id=str(uuid.uuid4()),expected_context_sha256=ctx['context_sha256'],decision=decision)
  r=await s.submit(db,q,pid,body,USER);await db.commit();return r,body

@pytest.mark.parametrize('purpose',['historical_label_recheck','candidate_evaluation','training_support'])
def test_same_present_reconfirms_replay_and_uncertain_preserves_all(url,purpose):
 async def go():
  engine,ss=await setup(url);q=queue()
  if purpose=='candidate_evaluation':q.update(purpose=purpose,model_sha256='d'*64,source_provenance={'kind':'frozen_candidate_sample','artifact_sha256':'c'*64,'supersedes_queue_sha256':'e'*64})
  if purpose=='training_support':q.update(purpose=purpose,source_provenance={'kind':'frozen_training_support','artifact_sha256':'c'*64,'supersedes_queue_sha256':'e'*64})
  try:
   async with ss() as db:
    assert (await s.context(db,q,101))['previous_judgment'] is None
   a,body=await submit(ss,q,101,'present');b,_=await submit(ss,q,101,'present',body)
   assert a['event_id']==b['event_id'] and b['replayed']
   async with ss() as db:
    confirmed=(await s.context(db,q,101))['context']
    assert confirmed['pin']['pinned_by']=='reviewer' and confirmed['post_version']==1
    audit=(await db.execute(text('SELECT id,added_tags,removed_tags FROM post_tag_audit WHERE post_id=101'))).one()
    assert list(audit[1])==list(audit[2])==[] and confirmed['pin']['audit_id']==audit[0]
    before=await snapshot(db)
   for pid in (101,102):await submit(ss,q,pid,'uncertain')
   async with ss() as db:
    assert await snapshot(db)==before
    assert (await s.context(db,q,101))['previous_judgment']['decision']=='uncertain'
    assert (await db.execute(text('SELECT count(*) FROM tagger_human_review_event'))).scalar_one()==3
    with pytest.raises(Exception):await db.execute(text('DELETE FROM tagger_human_review_event'))
  finally:await engine.dispose()
 asyncio.run(go())

def test_decisive_edit_stale_and_queue_membership(url):
 async def go():
  engine,ss=await setup(url);q=queue()
  try:
   a,body=await submit(ss,q,101,'absent')
   async with ss() as db:
    ctx=await s.context(db,q,101);assert ctx['context']['tag_present'] is False and ctx['context']['pin']['state']=='absent' and ctx['context']['post_version']==2
    with pytest.raises(KuraError):await s.submit(db,q,101,dict(body,request_id=str(uuid.uuid4())),USER)
   await submit(ss,q,101,'present')
   async with ss() as db:
    with pytest.raises(KuraError):await s.context(db,q,999)
    with pytest.raises(KuraError):await s.submit(db,dict(q,queue_sha256='d'*64),101,body,USER)
    with pytest.raises(KuraError):await s.context(db,dict(q,media_sha256={'101':'e'*64}),101)
  finally:await engine.dispose()
 asyncio.run(go())

def test_atomic_failure_restores_tag_and_pin(url,monkeypatch):
 async def go():
  engine,ss=await setup(url);q=queue()
  try:
   async with ss() as db:
    before=await snapshot(db);ctx=await s.context(db,q,101)
    await db.execute(text("CREATE FUNCTION fail_review_insert() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'fixture_refusal'; END $$"))
    await db.execute(text('CREATE TRIGGER fail_review_insert BEFORE INSERT ON tagger_human_review_event FOR EACH ROW EXECUTE FUNCTION fail_review_insert()'));await db.commit()
   monkeypatch.setattr(s,'queue',lambda ref:q)
   async with ss() as db:
    with pytest.raises(Exception):await routes.judge('a'*64,101,routes.Judgment(request_id=uuid.uuid4(),expected_context_sha256=ctx['context_sha256'],decision='absent'),db,USER)
   async with ss() as db:assert await snapshot(db)==before
  finally:await engine.dispose()
 asyncio.run(go())

def test_real_http_auth_attribution_spoof_refusal_and_concurrent_cas(url,monkeypatch):
 async def go():
  import base64,httpx
  from fastapi import FastAPI
  from fastapi.responses import JSONResponse
  from api import access
  engine,ss=await setup(url);q=queue();monkeypatch.setattr(s,'queue',lambda ref:q if ref=='a'*64 else (_ for _ in ()).throw(KuraError(409,'unknown',fix='refresh')))
  monkeypatch.setattr(access,'async_session',ss)
  app=FastAPI();app.add_middleware(access.CommandAuthMiddleware);app.include_router(routes.router)
  @app.exception_handler(KuraError)
  async def handler(request,e):return JSONResponse(status_code=e.status,content={'detail':e.body()})
  async def dependency():
   async with ss() as db:yield db
  app.dependency_overrides[routes.get_db]=dependency
  async with ss() as db:
   await db.execute(text('INSERT INTO "user"(id,creation_time,version,name,password_revision,rank,avatar_style) VALUES(42,now(),1,\'reviewer\',0,\'administrator\',\'gravatar\')'))
   await db.execute(text("INSERT INTO user_token(id,user_id,token,enabled,creation_time,version) VALUES(42,42,'fixture-only-token',true,now(),1)"));await db.commit()
  headers={'Authorization':'Token '+base64.b64encode(b'reviewer:fixture-only-token').decode()};base='/api/tagger-review/'+'a'*64
  try:
   async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test') as client:
    assert (await client.get(base)).status_code==401
    assert (await client.post(base+'/posts/101/judgments',json={})).status_code==401
    before=(await client.get(base,headers=headers)).json();assert before['remaining']==2
    image=(await client.get(base+'/posts/101',headers=headers)).json()
    body=dict(request_id=str(uuid.uuid4()),expected_context_sha256=image['context_sha256'],decision='uncertain')
    assert (await client.post(base+'/posts/101/judgments',json=dict(body,actor_id=99),headers=headers)).status_code==422
    a,b=await asyncio.gather(*(client.post(base+'/posts/101/judgments',json=dict(body,request_id=str(uuid.uuid4())),headers=headers) for _ in range(2)))
    assert sorted([a.status_code,b.status_code])==[200,409]
    response=await client.get(base,headers=headers);assert response.json()['remaining']==1 and response.json()['decisions']=={'101':'uncertain'}
   async with ss() as db:
    assert (await db.execute(text('SELECT actor_id,actor_name,decision FROM tagger_human_review_event'))).all()==[(42,'reviewer','uncertain')]
  finally:await engine.dispose()
 asyncio.run(go())


@pytest.mark.parametrize('lock_kind',['post','request','rendition','tag_name'])
def test_lock_contention_is_bounded_and_leaves_no_decision(url,monkeypatch,lock_kind):
 async def go():
  import time
  engine,ss=await setup(url);q=queue();monkeypatch.setattr(s,'queue',lambda ref:q)
  try:
   async with ss() as db:
    ctx=await s.context(db,q,101);before=await snapshot(db)
   request=str(uuid.uuid4())
   if lock_kind=='rendition':
    async with ss() as db:
     await db.execute(text("INSERT INTO rendition_object VALUES(decode(:sha,'hex'),'image/webp',3,1,1)"),{'sha':'d'*64})
     await db.execute(text("INSERT INTO post_rendition(post_id,kind,sha256,recipe) VALUES(101,'view',decode(:sha,'hex'),'fixture')"),{'sha':'d'*64});await db.commit()
   async with ss() as holder:
    if lock_kind=='post':await holder.execute(text('SELECT id FROM post WHERE id=101 FOR UPDATE'))
    elif lock_kind=='rendition':await holder.execute(text("SELECT * FROM post_rendition WHERE post_id=101 AND kind='view' FOR UPDATE"))
    elif lock_kind=='tag_name':await holder.execute(text('SELECT * FROM tag_name WHERE tag_id=1 FOR UPDATE'))
    else:await holder.execute(text('SELECT pg_advisory_xact_lock(hashtextextended(:k,0))'),{'k':f'human-review:42:{request}'})
    async with ss() as blocked:
     start=time.monotonic()
     with pytest.raises(KuraError) as caught:
      await routes.judge('a'*64,101,routes.Judgment(request_id=request,expected_context_sha256=ctx['context_sha256'],decision='absent'),blocked,USER)
     assert caught.value.status==503 and 'human_review_database_busy' in str(caught.value.body())
     assert .7 < time.monotonic()-start < 4
     assert (await blocked.execute(text('SELECT count(*) FROM tagger_human_review_event'))).scalar_one()==0
    await holder.rollback()
   async with ss() as db:assert await snapshot(db)==before
  finally:await engine.dispose()
 asyncio.run(go())


def test_rendition_precedes_pg_bytes_missing_falls_back_corruption_refuses(url,tmp_path,monkeypatch):
 async def go():
  engine,ss=await setup(url);q=queue();path=tmp_path/'view.webp';data=b'rendition differs from stored pg';sha=hashlib.sha256(data).digest()
  monkeypatch.setattr(s.renditions,'path_for',lambda *args:path)
  try:
   async with ss() as db:
    await db.execute(text("INSERT INTO rendition_object(sha256,mime,byte_size,width,height) VALUES(:sha,'image/webp',:n,1,1)"),{'sha':sha,'n':len(data)})
    await db.execute(text("INSERT INTO post_rendition(post_id,kind,sha256,recipe) VALUES(101,'view',:sha,'fixture')"),{'sha':sha});await db.commit()
   # Missing rendition exactly follows content reader's PG fallback.
   async with ss() as db:assert (await s.context(db,q,101))['context']['display_sha256']==hashlib.sha256(b'\1\2\3').hexdigest()
   path.write_bytes(data)
   async with ss() as db:
    with pytest.raises(KuraError):await s.context(db,q,101)
   from api.routers.posts import get_post_content
   from starlette.requests import Request
   async with ss() as db:
    response=await get_post_content(101,Request({'type':'http','headers':[]}),db)
    assert hashlib.sha256(Path(response.path).read_bytes()).hexdigest()==sha.hex()
   frozen=dict(q,media_sha256=dict(q['media_sha256'],**{'101':sha.hex()}))
   async with ss() as db:assert (await s.context(db,frozen,101))['context']['display_sha256']==sha.hex()
   path.write_bytes(b'changed after display')
   async with ss() as db:
    with pytest.raises(KuraError) as caught:await s.context(db,frozen,101)
    assert 'human_review_rendition_corrupt' in str(caught.value.body())
   monkeypatch.setattr(s.media_role,'MEDIA_ROLE','replica_redirect')
   async with ss() as db:
    with pytest.raises(KuraError) as caught:await s.context(db,frozen,101)
    assert 'human_review_media_authority_required' in str(caught.value.body())
  finally:await engine.dispose()
 asyncio.run(go())


def test_out_of_queue_administrative_row_does_not_count_as_response(url,monkeypatch):
 async def go():
  engine,ss=await setup(url);q=queue();monkeypatch.setattr(s,'queue',lambda ref:q)
  try:
   await submit(ss,q,101,'uncertain')
   async with ss() as db:
    await db.execute(text("INSERT INTO tagger_human_review_event(actor_id,actor_name,request_id,request_sha256,queue_sha256,post_id,decision,source_context) SELECT actor_id,actor_name,CAST(:uuid AS uuid),request_sha256,queue_sha256,999,decision,source_context FROM tagger_human_review_event LIMIT 1"),{'uuid':str(uuid.uuid4())});await db.commit()
    result=await routes.review_set(q['queue_sha256'],db,USER)
    assert result['remaining']==1 and result['decisions']=={101:'uncertain'}
  finally:await engine.dispose()
 asyncio.run(go())

@pytest.mark.parametrize('decision,pid',[('present',102),('absent',101)])
def test_decisive_mutation_matches_normal_human_update(url,decision,pid):
 async def go():
  from api.routers.posts import update_post,PostUpdateRequest
  engine,ss=await setup(url);q=queue()
  async def normalized(db):
   return dict(tags=(await db.execute(text('SELECT tag_id FROM post_tag WHERE post_id=:p ORDER BY tag_id'),{'p':pid})).scalars().all(),
    pin=(await db.execute(text('SELECT state,pinned_by FROM post_tag_pin WHERE post_id=:p AND tag_id=1'),{'p':pid})).one(),
    audit=(await db.execute(text('SELECT actor_type,actor_id,source,added_tags,removed_tags FROM post_tag_audit WHERE post_id=:p ORDER BY id DESC LIMIT 1'),{'p':pid})).one(),
    version=(await db.execute(text('SELECT version FROM post WHERE id=:p'),{'p':pid})).scalar_one(),
    gt=(await db.execute(text('SELECT tag,verdict,derived_from FROM tagger_gt_label WHERE post_id=:p'),{'p':pid})).all(),
    pin_links_latest=(await db.execute(text('SELECT audit_id=(SELECT max(id) FROM post_tag_audit WHERE post_id=:p) FROM post_tag_pin WHERE post_id=:p AND tag_id=1'),{'p':pid})).scalar_one())
  try:
   # Real update handler commits; outer connection transaction confines it to a savepoint.
   async with engine.connect() as conn:
    outer=await conn.begin()
    from sqlalchemy.ext.asyncio import AsyncSession
    async with AsyncSession(bind=conn,join_transaction_mode='create_savepoint',expire_on_commit=False) as db:
     await update_post(pid,PostUpdateRequest(version=1,tags=['fixture'] if decision=='present' else []),db,USER)
     normal=await normalized(db)
    await outer.rollback()
   async with ss() as db:
    await db.execute(text("SET TIME ZONE 'Asia/Tokyo'"));ctx=await s.context(db,q,pid)
    await s.submit(db,q,pid,dict(request_id=str(uuid.uuid4()),expected_context_sha256=ctx['context_sha256'],decision=decision),USER)
    assert await normalized(db)==normal
    assert abs((await db.execute(text("SELECT extract(epoch FROM (last_edit_time-(now() AT TIME ZONE 'UTC'))) FROM post WHERE id=:p"),{'p':pid})).scalar_one())<1
    await db.commit()
  finally:await engine.dispose()
 asyncio.run(go())

@pytest.mark.parametrize('availability',['replica','missing_table','available'])
def test_v2_storage_availability_does_not_hide_v1_queue(url,tmp_path,monkeypatch,availability):
 async def go():
  from api.routers import pools
  from api.services import review_queues as queue_reader
  queue_root=tmp_path/'queues';queue_root.mkdir();score_root=tmp_path/'scores';score_root.mkdir()
  monkeypatch.setenv('KURA_REVIEW_QUEUE_DIR',str(queue_root));monkeypatch.setenv('KURA_REVIEW_SCORE_DIR',str(score_root))
  original_loader=queue_reader.load_queues
  monkeypatch.setattr(queue_reader,'load_queues',lambda:sorted(original_loader(),key=lambda q:q['schema'],reverse=True))
  monkeypatch.setenv('KURA_DB_ROLE','replica' if availability=='replica' else 'canonical')
  def write(q):
   raw=json.dumps(q,sort_keys=True).encode();sha=hashlib.sha256(raw).hexdigest();(queue_root/(sha+'.json')).write_bytes(raw);return sha
  base={k:v for k,v in queue().items() if k not in ('queue_sha256','media_sha256','source_provenance')};base.update(schema='human-review-set/1',purpose='candidate_evaluation',model_sha256='d'*64)
  base_ref=write(base)
  frozen=dict(schema='frozen-review-scores/1',queue_sha256=base_ref,model_sha256=base['model_sha256'],frame_sha256=base['frame_sha256'],threshold=.5,scores={'101':.8,'102':.2})
  raw=json.dumps(frozen,sort_keys=True).encode();(score_root/(hashlib.sha256(raw).hexdigest()+'.json')).write_bytes(raw)
  if availability=='available':engine,ss=await setup(url)
  else:engine=create_async_engine(url);ss=async_sessionmaker(engine)
  try:
   async with ss() as db:before=(await pools.review_queues(db,USER))['queues']
   write({k:v for k,v in queue().items() if k!='queue_sha256'})
   async with ss() as db:
    await db.execute(text("SET LOCAL statement_timeout='13s'"));await db.execute(text("SET LOCAL lock_timeout='2500ms'"))
    after=await pools.review_queues(db,USER)
    assert (await db.execute(text("SELECT current_setting('statement_timeout'),current_setting('lock_timeout')"))).one()==('13s','2500ms')
    if availability=='available':await db.execute(text('SELECT pg_sleep(5.1)'))
   assert next(q for q in after['queues'] if q['purpose']=='candidate_evaluation')==before[0]
   assert before[0]['comparison']['state']=='compared' and before[0]['comparison']['compared']==2
   if availability=='available':assert len(after['queues'])==2 and not after['unavailable_queues']
   else:assert len(after['queues'])==1 and len(after['unavailable_queues'])==1 and after['unavailable_queues'][0]['total']==2
  finally:await engine.dispose()
 asyncio.run(go())


def test_repeatable_read_refuses_instead_of_weakening_context_cas(url):
 async def go():
  engine,ss=await setup(url);q=queue()
  try:
   async with ss() as db:
    await db.execute(text('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ'))
    ctx=await s.context(db,q,101)
    with pytest.raises(KuraError) as caught:await s.submit(db,q,101,dict(request_id=str(uuid.uuid4()),expected_context_sha256=ctx['context_sha256'],decision='uncertain'),USER)
    assert 'human_review_transaction_isolation_unsupported' in str(caught.value.body())
    assert (await db.execute(text('SELECT count(*) FROM tagger_human_review_event'))).scalar_one()==0
  finally:await engine.dispose()
 asyncio.run(go())


@pytest.mark.parametrize('purpose',['candidate_evaluation','training_support'])
def test_candidate_counts_ignore_prior_pins_and_keep_uncertain_distinct(url,monkeypatch,purpose):
 async def go():
  engine,ss=await setup(url)
  q=queue()|dict(purpose='candidate_evaluation',model_sha256='d'*64,
    source_provenance={'kind':'frozen_candidate_sample','artifact_sha256':'c'*64,'supersedes_queue_sha256':'e'*64})
  if purpose=='training_support':q.update(purpose=purpose,model_sha256=None,source_provenance={'kind':'frozen_training_support','artifact_sha256':'c'*64,'supersedes_queue_sha256':'e'*64})
  monkeypatch.setattr(s,'queue',lambda ref:q)
  try:
   async with ss() as db:
    initial=await routes.review_set(q['queue_sha256'],db,USER)
    assert initial['remaining']==2 and initial['counts']==dict(present=0,absent=0,uncertain=0)
   await submit(ss,q,101,'uncertain');await submit(ss,q,102,'absent')
   async with ss() as db:
    done=await routes.review_set(q['queue_sha256'],db,USER)
    assert done['remaining']==0 and done['counts']==dict(present=0,absent=1,uncertain=1)
    assert done['quality_granted'] is False
    recorded=(await db.execute(text('SELECT source_context FROM tagger_human_review_event ORDER BY id LIMIT 1'))).scalar_one()
    assert recorded['queue']['source_provenance']==q['source_provenance']
    assert recorded['queue']['media_sha256']==q['media_sha256']
  finally:await engine.dispose()
 asyncio.run(go())


def test_dashboard_candidate_supersession_counts_events_not_prior_pins(url,tmp_path,monkeypatch):
 async def go():
  from api.routers.pools import review_queues
  from api.services.review_queues import load_queues
  from api.services.review_comparison import load_scores
  from test_review_queues import write
  engine,ss=await setup(url)
  original=queue();original={k:v for k,v in original.items() if k not in ('queue_sha256','source_provenance','media_sha256')}
  original.update(schema='human-review-set/1',purpose='candidate_evaluation',model_sha256='d'*64)
  old=write(tmp_path,original)
  new=original|dict(schema='human-review-set/2',media_sha256=queue()['media_sha256'],source_provenance={'kind':'frozen_candidate_sample','artifact_sha256':'c'*64,'supersedes_queue_sha256':old.stem})
  successor=write(tmp_path,new)
  scores=tmp_path.parent/(tmp_path.name+'-scores');scores.mkdir()
  write(scores,dict(schema='frozen-review-scores/1',queue_sha256=old.stem,model_sha256='d'*64,frame_sha256=original['frame_sha256'],threshold=.5,scores={'101':.7,'102':.2}))
  monkeypatch.setenv('KURA_REVIEW_QUEUE_DIR',str(tmp_path));monkeypatch.setenv('KURA_REVIEW_SCORE_DIR',str(scores))
  try:
   assert old.stem in load_scores(load_queues(tmp_path),scores)
   async with ss() as db:
    result=await review_queues(db,USER);active,=result['queues']
    assert active['queue_sha256']==successor.stem and active['schema']=='human-review-set/2'
    assert active['remaining']==active['total']==2
    assert active['judged']==dict(present=0,absent=0,uncertain=0)
    assert active['review_state']=='awaiting_responses'
  finally:await engine.dispose()
 asyncio.run(go())


def test_training_support_dashboard_keeps_old_request_and_exposes_one_fresh_cta(url,tmp_path,monkeypatch):
 async def go():
  from api.routers.pools import review_queues
  from api.services.review_queues import load_queues
  from test_review_queues import write
  engine,ss=await setup(url)
  qr=tmp_path/'queues';qr.mkdir();rr=tmp_path/'requests';rr.mkdir(mode=0o700)
  original={k:v for k,v in queue().items() if k not in ('queue_sha256','source_provenance','media_sha256')}
  original.update(schema='human-review-set/1',purpose='training_support',model_sha256=None)
  old=write(qr,original)
  new=original|dict(schema='human-review-set/2',media_sha256=queue()['media_sha256'],source_provenance={'kind':'frozen_training_support','artifact_sha256':'c'*64,'supersedes_queue_sha256':old.stem})
  successor=write(qr,new)
  for ref in (old.stem,successor.stem):
   path=write(rr,dict(schema='review-request/1',queue_sha256=ref,frame_sha256=original['frame_sha256'],reason='class_group_support',evidence_sha256='c'*64,requested_at='2026-11-17T00:00:00Z'));path.chmod(0o600)
  monkeypatch.setenv('KURA_REVIEW_QUEUE_DIR',str(qr));monkeypatch.setenv('KURA_REVIEW_REQUEST_DIR',str(rr))
  q=next(q for q in load_queues(qr) if q['queue_sha256']==successor.stem)
  try:
   async with ss() as db:
    initial=await review_queues(db,USER);active,=initial['queues']
    assert active['request']['queue_sha256']==successor.stem
    assert active['remaining']==2 and active['judged']==dict(present=0,absent=0,uncertain=0)
    before=await snapshot(db)
   for pid in q['post_ids']:await submit(ss,q,pid,'uncertain')
   async with ss() as db:
    final=await review_queues(db,USER);active,=final['queues']
    assert active['remaining']==0 and active['decisive']==0 and active['judged']['uncertain']==2
    assert active['review_state']=='review_completed_quality_unresolved'
    assert await snapshot(db)==before
   assert old.exists() and len(list(rr.glob('*.json')))==2
  finally:await engine.dispose()
 asyncio.run(go())


def test_explicit_review_syncs_positive_for_prelocked_tag(url):
    from api.tagger_core.gt_sync import _sync_reviewed_locked_positives

    async def go():
        engine, sessions = await setup(url)
        try:
            async with sessions() as db:
                await db.execute(text("SELECT set_config('kura.tag_actor','human',true)"))
                await db.execute(text("UPDATE post_tag SET locked=true WHERE post_id=101 AND tag_id=1"))
                await db.execute(text("SELECT tagger_governed_retract_pin_gt('fixture',101)"))
                assert await _sync_reviewed_locked_positives(db) == 0
                await db.commit()
            await submit(sessions, queue(), 101, 'present')
            async with sessions() as db:
                assert await _sync_reviewed_locked_positives(db) == 1
                assert await _sync_reviewed_locked_positives(db) == 0
                assert (await db.execute(text("SELECT verdict FROM tagger_gt_label WHERE post_id=101"))).scalar_one() == 'positive'
        finally:
            await engine.dispose()
    asyncio.run(go())


def test_prelocked_sync_does_not_resurrect_a_concurrently_removed_pin(url):
    from api.tagger_core.gt_sync import _sync_reviewed_locked_positives

    async def go():
        engine, sessions = await setup(url)
        try:
            async with sessions() as db:
                await db.execute(text("SELECT set_config('kura.tag_actor','human',true)"))
                await db.execute(text("UPDATE post_tag SET locked=true WHERE post_id=101 AND tag_id=1"))
                await db.execute(text("SELECT tagger_governed_retract_pin_gt('fixture',101)"))
                await db.commit()
            await submit(sessions, queue(), 101, 'present')
            async with sessions() as unlock, sessions() as sync:
                pid = (await sync.execute(text("SELECT pg_backend_pid()"))).scalar_one()
                await unlock.execute(text("DELETE FROM post_tag_pin WHERE post_id=101 AND tag_id=1"))
                await unlock.execute(text("SELECT tagger_governed_retract_pin_gt('fixture',101)"))
                task = asyncio.create_task(_sync_reviewed_locked_positives(sync))
                try:
                    async with asyncio.timeout(3):
                        while not (await unlock.execute(text(
                            "SELECT cardinality(pg_blocking_pids(:pid)) > 0"
                        ), {"pid": pid})).scalar_one():
                            await asyncio.sleep(.01)
                    await unlock.commit()
                    assert await task == 0
                    await sync.commit()
                finally:
                    if not task.done():
                        task.cancel()
                        await asyncio.gather(task, return_exceptions=True)
                assert (await unlock.execute(text(
                    "SELECT count(*) FROM tagger_gt_label WHERE post_id=101"
                ))).scalar_one() == 0
        finally:
            await engine.dispose()

    asyncio.run(go())


@pytest.mark.parametrize(('purpose', 'kind'), [
    ('base_comparison', 'base_comparison'),
    ('candidate_evaluation', 'dedicated_candidate'),
])
def test_blind_image_response_and_token_do_not_disclose_current_tags(url, monkeypatch, purpose, kind):
    from api.routers import human_review as routes

    async def go():
        engine, sessions = await setup(url)
        ordinary = queue()
        blind = dict(ordinary, schema='human-review-set/3', queue_sha256='b' * 64,
                     purpose=purpose, source_provenance={'kind': kind})
        queues = {q['queue_sha256']: q for q in (ordinary, blind)}

        async def resolve(db, ref):
            return queues[ref]

        monkeypatch.setattr(s, 'resolve_queue', resolve)
        try:
            from test_review_set_registry import migrate
            await migrate(engine)
            async with sessions() as db:
                original = await routes.review_image(ordinary['queue_sha256'], 101, db, USER)
                assert original == await s.context(db, ordinary, 101)
                assert original['context']['tag_present'] is True
                before = await routes.review_image(blind['queue_sha256'], 101, db, USER)
                assert set(before) == {'context', 'context_sha256', 'content_url'}
                assert set(before['context']) == {
                    'queue_sha256', 'post_id', 'tag_id', 'tag', 'display_sha256',
                }
            await submit(sessions, ordinary, 101, 'absent')
            async with sessions() as db:
                changed = await routes.review_image(ordinary['queue_sha256'], 101, db, USER)
                assert changed['context']['tag_present'] is False
                assert changed['context']['pin']['state'] == 'absent'
                assert await routes.review_image(blind['queue_sha256'], 101, db, USER) == before
                result = await routes.judge(blind['queue_sha256'], 101, routes.Judgment(
                    request_id=uuid.uuid4(), expected_context_sha256=before['context_sha256'],
                    decision='present'), db, USER)
                assert result['decision'] == 'present'
                after = await routes.review_image(blind['queue_sha256'], 101, db, USER)
                assert after['context'] == before['context']
                assert 'previous_judgment' not in after
                assert after['context_sha256'] != before['context_sha256']
                with pytest.raises(KuraError, match='human_review_context_stale'):
                    await routes.judge(blind['queue_sha256'], 101, routes.Judgment(
                        request_id=uuid.uuid4(), expected_context_sha256=before['context_sha256'],
                        decision='absent'), db, USER)
                unchanged = await routes.review_image(ordinary['queue_sha256'], 101, db, USER)
                assert unchanged == changed
        finally:
            await engine.dispose()

    asyncio.run(go())


@pytest.mark.parametrize("blind", [False, True])
def test_group_judgment_is_atomic_without_crossing_the_blind_boundary(url, blind):
    from api.services import grouped_review
    from test_review_set_registry import migrate

    async def go():
        engine, sessions = await setup(url)
        try:
            await migrate(engine)
            first = queue()
            second = dict(first, queue_sha256='e' * 64, tag='similar_tag')
            if blind:
                for q in (first, second):
                    q.update(schema='human-review-set/3', purpose='base_comparison',
                             source_provenance={'kind':'base_comparison'})
            config = {'tags':['fixture','similar_tag','extra_tag'], 'mode':'multiple',
                      'exclusive':[['fixture','similar_tag']], 'implies':{}}
            linked = ('example',config,{'fixture':first,'similar_tag':second})
            async with sessions() as db:
                await db.execute(text("""
                    INSERT INTO tag(id,category_id,creation_time,version)
                    SELECT i,category_id,now(),1 FROM tag CROSS JOIN (VALUES (2),(3)) as x(i) WHERE id=1
                """))
                await db.execute(text("""
                    INSERT INTO tag_name(tag_name_id,tag_id,name,ord)
                    VALUES(2,2,'similar_tag',0),(3,3,'extra_tag',0)
                """))
                await db.commit()
                before = await snapshot(db)
                described = await grouped_review.describe(db,linked,first['queue_sha256'],101)
                assert set(described['options'][0]) == {'tag','definition','target','answered'}
                request = dict(request_id=str(uuid.uuid4()), answer='selected',
                               selected=['similar_tag','extra_tag'],
                               expected_context_sha256=described['context_sha256'])
                recorded = await grouped_review.submit(db,linked,first['queue_sha256'],101,request,USER)
                assert recorded['recorded']==2
                await db.commit()
                replay = await grouped_review.submit(db,linked,first['queue_sha256'],101,request,USER)
                assert replay['replayed']
                decisions = (await db.execute(text("""
                    SELECT queue_sha256, decision, audit_id FROM tagger_human_review_event ORDER BY queue_sha256
                """))).all()
                assert [(ref, decision) for ref,decision,_ in decisions] == [
                    (first['queue_sha256'],'absent'),(second['queue_sha256'],'present')]
                if blind:
                    assert all(audit is None for _,_,audit in decisions)
                    assert await snapshot(db)==before
                else:
                    pins = (await db.execute(text("""
                        SELECT tag_id,state FROM post_tag_pin WHERE post_id=101 ORDER BY tag_id
                    """))).all()
                    assert pins==[(1,'absent'),(2,'present'),(3,'present')]
        finally:
            await engine.dispose()

    asyncio.run(go())


def test_hidden_only_review_member_is_excluded_without_answer(url, monkeypatch):
 async def go():
  engine, sessions = await setup(url)
  q = dict(queue(), schema='human-review-set/3', purpose='base_comparison')
  async def resolve(db, ref):
   return q
  monkeypatch.setattr(s, 'resolve_queue', resolve)
  try:
   async with sessions.begin() as db:
    await db.execute(text("""INSERT INTO pool(id,category_id,version,creation_time,hidden)
         SELECT 701,id,1,now(),true FROM pool_category ORDER BY id LIMIT 1"""))
    await db.execute(text("INSERT INTO pool_post(pool_id,post_id,ord) VALUES(701,101,0)"))
   async with sessions() as db:
    result = await routes.review_set(q['queue_sha256'], db, USER)
    assert result['post_ids'] == [102]
    assert result['excluded_count'] == 1 and result['remaining'] == 1 and result['decisions'] == {}
    with pytest.raises(KuraError, match='human_review_post_hidden'):
     await s.context(db, q, 101)
    assert await db.scalar(text('SELECT count(*) FROM tagger_human_review_event')) == 0
  finally:
   await engine.dispose()
 asyncio.run(go())
