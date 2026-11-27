"""Real private PG18 through SQLAlchemy+asyncpg (not psycopg2 parameter replay)."""
import asyncio,json
import pytest
from sqlalchemy import text
from sqlalchemy.engine import URL
from sqlalchemy.ext.asyncio import create_async_engine
from tests.test_cl2_publish import database,db,FILES,plan
from tagger import cl2_publish as phase2,character_inheritance as carry


def engine_for(c):
 return create_async_engine(URL.create('postgresql+asyncpg',username='kura',database='postgres',query={'host':c.info.host,'port':str(c.info.port)}))

def cl_predecessor(c,admin):
 published=phase2.publish(c,plan(c),FILES,'a'*40,execute=True)
 with admin.cursor() as q:
  q.execute('UPDATE tagger_worker_heartbeat SET generation=6,release_id=%s,reported_at=now()',(published['release_id'],))
 return published

async def governed(conn,commit='b'*40):
 return (await conn.execute(text('''SELECT tagger_governed_publish_release(:commit,r.base_model_repo,r.base_model_revision,r.patch_set_digest,
 r.model_sha256,r.model_data,r.selected_tags_csv,r.n_stock,r.n_out,r.patched,r.lockfile,'fixture')
 FROM tagger_release r WHERE r.id=(SELECT release_id FROM tagger_release_active WHERE id=1)'''),{'commit':commit})).scalar_one()


def test_asyncpg_inheritance_preserves_exact_manifest_and_refuses_next_floor(db):
 c,admin,_=db;published=cl_predecessor(c,admin)
 async def go():
  engine=engine_for(c)
  try:
   async with engine.begin() as conn:
    prior=await carry.lock_predecessor(conn);new=await governed(conn)
    await carry.attach(conn,prior,new,'model')
   async with engine.connect() as conn:
    from api.routers.tagger import _release_character_component
    from tagger.character_policy import decode
    meta=await _release_character_component(conn,new)
    assert decode(dict(id=new,character_component=meta))['threshold']==.45
    assert meta['description']['components']==prior['components']
    with pytest.raises(carry.InheritanceRefused,match='compatible_workers_not_ready'):
     await carry.lock_predecessor(conn)
   return new
  finally:await engine.dispose()
 new=asyncio.run(go())
 with admin.cursor() as q:
  q.execute('SELECT release_id,generation FROM tagger_release_active');assert q.fetchone()==(new,7)
  q.execute('SELECT count(*) FROM tagger_release_component WHERE role=\'character_base\'');assert q.fetchone()==(6,)


def test_actual_regular_publish_uses_inheritance_with_asyncpg(db,monkeypatch,tmp_path):
 from tagger import publish_release,sync_patch_registry
 from api import config
 c,admin,_=db;cl_predecessor(c,admin)
 monkeypatch.setattr(config,'DATABASE_URL',engine_for(c).url.render_as_string(hide_password=False))
 monkeypatch.setattr(publish_release,'_load_build',lambda _:dict(commit_sha='b'*40,base_model_repo='fixture',base_model_revision='fixture',patch_set_digest='patch',model_sha256='new-model',model_bytes=b'new wd14',csv_bytes=b'tags',n_stock=1,n_out=1,patched=['head'],lockfile=None))
 async def unchanged_registry(conn):pass
 monkeypatch.setattr(sync_patch_registry,'sync_registry',unchanged_registry)
 new=asyncio.run(publish_release.publish(tmp_path,'fixture'))
 with admin.cursor() as q:
  q.execute("SELECT count(*) FROM tagger_release_manifest m JOIN tagger_release_component c USING(release_digest) WHERE m.release_id=%s AND c.role='character_base'",(new,));assert q.fetchone()==(3,)
  q.execute('SELECT model_data FROM tagger_release WHERE id=%s',(new,));assert bytes(q.fetchone()[0])==b'new wd14'


def test_asyncpg_failure_to_attach_rolls_back_publish(db):
 c,admin,_=db;published=cl_predecessor(c,admin)
 with admin.cursor() as q:q.execute("CREATE FUNCTION block_character_copy() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'fixture'; END $$; CREATE TRIGGER block_character_copy BEFORE INSERT ON tagger_release_component FOR EACH ROW EXECUTE FUNCTION block_character_copy()")
 async def go():
  engine=engine_for(c)
  try:
   async with engine.begin() as conn:
    prior=await carry.lock_predecessor(conn);new=await governed(conn);await carry.attach(conn,prior,new,'model')
  finally:await engine.dispose()
 try:
  with pytest.raises(Exception):asyncio.run(go())
  with admin.cursor() as q:q.execute('SELECT release_id,generation FROM tagger_release_active');assert q.fetchone()==(published['release_id'],6)
 finally:
  with admin.cursor() as q:q.execute('DROP TRIGGER block_character_copy ON tagger_release_component; DROP FUNCTION block_character_copy()')


def test_sync_ensemble_inheritance_and_legacy_noop(db):
 c,admin,_=db
 with c.cursor() as q:assert carry.lock_predecessor_sync(q)['components']==[]
 c.rollback();published=cl_predecessor(c,admin)
 with c.cursor() as q:
  prior=carry.lock_predecessor_sync(q)
  q.execute("SELECT tagger_governed_publish_release('b',NULL,NULL,NULL,'model',NULL,NULL,1,1,'[]',NULL,'fixture')");new=q.fetchone()[0]
  carry.attach_sync(q,prior,new,'model')
 c.commit()
 with admin.cursor() as q:q.execute("SELECT count(*) FROM tagger_release_manifest m JOIN tagger_release_component c USING(release_digest) WHERE m.release_id=%s AND c.role='character_base'",(new,));assert q.fetchone()==(3,)


def test_real_asyncpg_receipt_insert_and_completion_binding(db):
 from api.services.execution_receipts import remember,for_completion
 from tagger.execution_receipt import make
 from tests.test_execution_receipt import body
 c,admin,_=db
 with admin.cursor() as q:
  q.execute('ALTER TABLE tagger_execution_receipt ADD COLUMN IF NOT EXISTS canonical_body text; ALTER TABLE tagger_worker_heartbeat ADD COLUMN IF NOT EXISTS worker_id text')
  q.execute("UPDATE tagger_worker_heartbeat SET worker_id='nodeb-wd14'")
 b=body();b.update(host='nodeb',profile='wd14',release_id=14);packet=make(b)
 async def go():
  engine=engine_for(c)
  try:
   async with engine.begin() as conn:
    ref=await remember(conn,packet,host='nodeb',profile='wd14',release_id=14)
    assert await remember(conn,packet,host='nodeb',profile='wd14',release_id=14)==ref
    await conn.execute(text("UPDATE tagger_worker_heartbeat SET execution_receipt_sha256=:ref WHERE worker_id='nodeb-wd14'"),{'ref':ref})
    assert await for_completion(conn,receipt_sha256=ref,worker_id='nodeb-wd14',host='nodeb',release_id=14)==ref
   async with engine.begin() as conn:
    with pytest.raises(ValueError):await for_completion(conn,receipt_sha256=ref,worker_id='nodeb-wd14',host='other',release_id=14)
  finally:await engine.dispose()
 asyncio.run(go())


def test_old_dual_typed_parameter_really_fails_with_asyncpg(db):
 from sqlalchemy.exc import DBAPIError
 from tagger.execution_receipt import make,canonical
 from tests.test_execution_receipt import body
 c,admin,_=db
 with admin.cursor() as q:q.execute('ALTER TABLE tagger_execution_receipt ADD COLUMN IF NOT EXISTS canonical_body text')
 packet=make(body());wire=canonical(packet['body']).decode()
 async def go():
  engine=engine_for(c)
  try:
   async with engine.begin() as conn:
    with pytest.raises(DBAPIError) as exc:
     await conn.execute(text('INSERT INTO tagger_execution_receipt(receipt_sha256,canonical_body,body) VALUES(:ref,:wire,CAST(:wire AS jsonb))'),{'ref':packet['receipt_sha256'],'wire':wire})
    assert 'inconsistent types' in str(exc.value).lower()
  finally:await engine.dispose()
 asyncio.run(go())


def test_malformed_predecessor_cannot_be_silently_dropped(db):
 c,admin,_=db;published=cl_predecessor(c,admin)
 with admin.cursor() as q:q.execute("UPDATE tagger_release_component SET params=params-'threshold' WHERE ordinal=0 AND role='character_base'")
 async def go():
  engine=engine_for(c)
  try:
   async with engine.begin() as conn:
    await carry.lock_predecessor(conn)
  finally:await engine.dispose()
 from tagger.pixai_chars import RequiredCharacterComponentError
 with pytest.raises(RequiredCharacterComponentError,match="cl2_threshold_invalid"):asyncio.run(go())
 with admin.cursor() as q:q.execute('SELECT release_id,generation FROM tagger_release_active');assert q.fetchone()==(published['release_id'],6)


def test_actual_0192_upgrade_and_validation_through_asyncpg(database):
 import importlib.util,pathlib,psycopg2
 from alembic.migration import MigrationContext
 from alembic.operations import Operations
 root,admin=database
 with admin.cursor() as q:q.execute('CREATE DATABASE migration_async')
 setup=psycopg2.connect(host=str(root),port=55461,user='fixture',dbname='migration_async');setup.autocommit=True
 with setup.cursor() as q:
  q.execute('''GRANT CREATE ON SCHEMA public TO kura,kura_reconciler;
  CREATE TABLE tagger_allowed_executor(scope text,db_role text); INSERT INTO tagger_allowed_executor VALUES('worker-heartbeat','kura');
  CREATE TABLE tagger_worker_heartbeat(worker_id text PRIMARY KEY,host text,profile text,generation bigint,release_id int,provider text,model_sha256 text,reported_at timestamptz);
  CREATE TABLE post_tag_audit(id bigint);
  ALTER TABLE tagger_worker_heartbeat OWNER TO kura; ALTER TABLE post_tag_audit OWNER TO kura;
  GRANT SELECT ON tagger_allowed_executor TO kura_reconciler;
  GRANT SELECT,INSERT,UPDATE ON tagger_worker_heartbeat TO kura_reconciler;
  CREATE FUNCTION tagger_upsert_heartbeat(text,text,text,bigint,integer,text,text) RETURNS void LANGUAGE plpgsql AS $$ BEGIN RETURN; END $$;
  ALTER FUNCTION tagger_upsert_heartbeat(text,text,text,bigint,integer,text,text) OWNER TO kura_reconciler;
  ''')
 path=pathlib.Path(__file__).resolve().parents[1]/'api/alembic/versions/0192_tagger_execution_receipt.py'
 spec=importlib.util.spec_from_file_location('actual_asyncpg_0192',path);m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
 async def go():
  engine=create_async_engine(URL.create('postgresql+asyncpg',username='fixture',database='migration_async',query={'host':str(root),'port':'55461'}))
  try:
   async with engine.begin() as conn:
    await conn.execute(text('SET SESSION AUTHORIZATION kura'))
    def upgrade(sync):
     m.op=Operations(MigrationContext.configure(sync));m.upgrade()
    await conn.run_sync(upgrade)
   async with engine.begin() as conn:
    for statement in m.VALIDATION_DDL.split(';'):
     if statement.strip():await conn.execute(text(statement))
    rows=(await conn.execute(text("SELECT convalidated FROM pg_constraint WHERE conname IN ('heartbeat_execution_receipt_hex','audit_execution_receipt_hex')"))).scalars().all()
    assert rows==[True,True]
    who=(await conn.execute(text('SELECT session_user,current_user'))).one();assert tuple(who)==('kura','kura')
  finally:await engine.dispose()
 try:asyncio.run(go())
 finally:setup.close()
