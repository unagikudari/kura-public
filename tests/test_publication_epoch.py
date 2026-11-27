"""Real governed publish + asyncpg claim gate; no live connections."""
import asyncio,importlib.util
from pathlib import Path
from unittest.mock import AsyncMock
from sqlalchemy import text
import pytest
from tests.test_cl2_publish import database,db
from tests.test_character_inheritance_asyncpg import engine_for,cl_predecessor,governed
from tests.test_character_pin_private_pg import cluster
from tagger import character_inheritance as carry
from api.services import tagging


def test_multiple_publications_do_not_require_worker_code_bumps(db,monkeypatch):
 c,admin,_=db;published=cl_predecessor(c,admin)
 monkeypatch.setattr(tagging,'upsert_heartbeat',AsyncMock())
 async def run():
  engine=engine_for(c)
  try:
   async with engine.connect() as conn:
    for epoch in (7,8,9):
     prior=await carry.lock_predecessor(conn)
     new=await governed(conn);await carry.attach(conn,prior,new,'model')
     await conn.commit()
     row=(await conn.execute(text('SELECT generation,required_worker_generation FROM tagger_release_active'))).one()
     assert tuple(row)==(epoch,6)
     await tagging.enforce_claim_guard(conn,host='nodeb',worker_id='nodeb-wd14',profile='wd14',release_id=new,generation=6,provider='CPUExecutionProvider',model_sha256='model')
     with pytest.raises(tagging.ClaimNotEligibleError,match='stale worker code'):
      await tagging.enforce_claim_guard(conn,host='nodeb',worker_id='nodeb-wd14',profile='wd14',release_id=new,generation=5,provider='CPUExecutionProvider',model_sha256='model')
     with pytest.raises(tagging.ClaimNotEligibleError,match='active release_id'):
      await tagging.enforce_claim_guard(conn,host='nodeb',worker_id='nodeb-wd14',profile='wd14',release_id=prior['release'],generation=6,provider='CPUExecutionProvider',model_sha256='model')
     # Sweep's captured epoch is stale on each real governed publication.
     assert row.generation!=prior['generation']
     await conn.commit()
     with admin.cursor() as q:q.execute('UPDATE tagger_worker_heartbeat SET release_id=%s,reported_at=now()',(new,))
  finally:await engine.dispose()
 asyncio.run(run())


def test_declared_floor_still_fences_incompatible_workers(db):
 c,admin,_=db;cl_predecessor(c,admin)
 with admin.cursor() as q:q.execute('UPDATE tagger_release_active SET required_worker_generation=7')
 try:
  with c.cursor() as q:
   with pytest.raises(carry.InheritanceRefused,match='compatible_workers_not_ready'):carry.lock_predecessor_sync(q)
 finally:
  c.rollback()
  with admin.cursor() as q:q.execute('UPDATE tagger_release_active SET required_worker_generation=6')


def test_migration_preserves_floor_without_governed_dml_and_is_idempotent(cluster):
 spec=importlib.util.spec_from_file_location('floor',Path(__file__).parents[1]/'api/alembic/versions/0194_worker_code_floor.py');m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
 cluster("CREATE ROLE kura LOGIN; CREATE TABLE tagger_release_active(id int PRIMARY KEY,generation bigint); INSERT INTO tagger_release_active VALUES(1,6); ALTER TABLE tagger_release_active OWNER TO kura; GRANT USAGE,CREATE ON SCHEMA public TO kura;")
 cluster("CREATE FUNCTION deny_dml() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'governed_write_forbidden'; END $$; CREATE TRIGGER guarded BEFORE UPDATE ON tagger_release_active FOR EACH ROW EXECUTE FUNCTION deny_dml();")
 cluster('BEGIN;SET ROLE kura;'+m.REPLICA_DDL+'COMMIT;')
 assert cluster('SELECT generation,required_worker_generation FROM tagger_release_active').strip()=='6|6'
 cluster('BEGIN;SET ROLE kura;'+m.REPLICA_DDL+'COMMIT;')
 # Deliberate direct updates still fail after migration, even for the table owner.
 import subprocess
 with pytest.raises(subprocess.CalledProcessError):cluster('SET ROLE kura;UPDATE tagger_release_active SET required_worker_generation=1;')
 assert cluster('SELECT generation,required_worker_generation FROM tagger_release_active').strip()=='6|6'


def test_real_sweep_aborts_if_a_publication_moves_its_epoch(db,monkeypatch):
 from tagger import sweep
 from sqlalchemy.ext.asyncio import async_sessionmaker
 from types import SimpleNamespace
 c,admin,_=db;cl_predecessor(c,admin)
 async def run():
  engine=engine_for(c)
  monkeypatch.setattr(sweep,'async_session',async_sessionmaker(engine,expire_on_commit=False))
  monkeypatch.setattr(sweep,'_taggable_done_count',AsyncMock(return_value=100))
  monkeypatch.setattr(sweep,'_ensemble_changed_at',AsyncMock(return_value=None))
  monkeypatch.setattr(sweep,'_candidate_ids',AsyncMock(return_value=[1]))
  monkeypatch.setattr(sweep,'_record_run',AsyncMock())
  monkeypatch.setattr(sweep,'release_lease',AsyncMock())
  async def publish_between_reads(*args,**kwargs):
   async with engine.begin() as conn:
    prior=await carry.lock_predecessor(conn);new=await governed(conn);await carry.attach(conn,prior,new,'model')
   return SimpleNamespace(fencing_token=1)
  monkeypatch.setattr(sweep,'acquire_lease',publish_between_reads)
  try:
   result=await sweep.run_sweep(max_posts=10,max_fraction=.1,dry_run=False)
   assert result['status']=='aborted_toctou' and result['flipped']==0
   sweep.release_lease.assert_awaited_once()
  finally:await engine.dispose()
 asyncio.run(run())
