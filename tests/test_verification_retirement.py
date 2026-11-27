"""Retire the queue, not the historical evidence or ordinary human feedback."""
import asyncio
import importlib.util
import os
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]


def test_old_workflow_requests_cannot_write():
    from api.routers import pools
    from api.auth import get_current_user
    from api.db.session import get_db
    app = FastAPI(); app.include_router(pools.router); app.include_router(pools.dashboard_router)
    app.dependency_overrides[get_current_user] = lambda: object()
    # Any database work would fail: retired requests must stop before touching data.
    app.dependency_overrides[get_db] = lambda: object()
    with TestClient(app) as client:
        for path, body in [('/api/tagger-dashboard/head/example/generate-pool',None),
                           ('/api/tagger-dashboard/verify-pools/cleanup',None),
                           ('/api/pools',{'names':['test'],'verify_status':'pending'}),
                           ('/api/pools',{'names':['test'],'tagger_source':'legacy'})]:
            assert client.post(path,json=body).status_code == 410
        assert client.patch('/api/pools/1/verify-status',json={'status':'verified'}).status_code == 410


def test_scheduled_sync_only_collects_ordinary_human_feedback(monkeypatch):
    from api.tagger_core import gt_sync
    class Session:
        async def __aenter__(self): return self
        async def __aexit__(self,*args): pass
        commit = AsyncMock()
        rollback = AsyncMock()
    monkeypatch.setattr(gt_sync,'async_session',Session)
    monkeypatch.setattr(gt_sync.gt_lock,'hold',AsyncMock())
    monkeypatch.setattr(gt_sync,'load_config',AsyncMock(return_value={'min_gt_labels':3}))
    monkeypatch.setattr(gt_sync,'_sync_global_negatives',AsyncMock(return_value=11))
    monkeypatch.setattr(gt_sync,'_sync_global_positives',AsyncMock(return_value=7))
    monkeypatch.setattr(gt_sync,'_sync_reviewed_locked_positives',AsyncMock(return_value=2))
    monkeypatch.setattr(gt_sync,'_detect_global_candidates',AsyncMock(return_value=['example']))
    for name in ('_open_candidate_pools','_sync_human_corrections','_sync_implicit_review'):
        monkeypatch.setattr(gt_sync,name,AsyncMock(side_effect=AssertionError('must not rederive historical pool labels')))
    result = asyncio.run(gt_sync.run_sync())
    assert result == {'_global':{'negatives_synced':11,'positives_synced':9,'candidates_flagged':['example']}}
    Session.commit.assert_awaited_once()
    gt_sync._open_candidate_pools.assert_not_called()


def test_claim_releases_retired_queue_but_keeps_explicit_exclusion():
    dsn=os.environ.get('KURA_TEST_DSN')
    if not dsn: pytest.skip('KURA_TEST_DSN required (temporary tables only)')
    import psycopg2
    spec=importlib.util.spec_from_file_location('retirement',ROOT/'api/alembic/versions/0190_retire_verification_pool_gate.py')
    migration=importlib.util.module_from_spec(spec);spec.loader.exec_module(migration)
    conn=psycopg2.connect(dsn)
    try:
        with conn, conn.cursor() as cur:
            cur.execute('CREATE TEMP TABLE tagger_allowed_executor (db_role text,scope text) ON COMMIT DROP')
            cur.execute("INSERT INTO tagger_allowed_executor VALUES (session_user,'tagging-pipeline')")
            cur.execute('CREATE TEMP TABLE post (id int,"type" varchar,"mime-type" varchar,status text) ON COMMIT DROP')
            cur.execute('CREATE TEMP TABLE post_tagging_work (post_id int,status text,lease_until timestamptz,attempts int) ON COMMIT DROP')
            cur.execute('CREATE TEMP TABLE pool (id int,tagger_source text,verify_status text) ON COMMIT DROP')
            cur.execute('CREATE TEMP TABLE pool_post (pool_id int,post_id int) ON COMMIT DROP')
            cur.execute('CREATE TEMP TABLE tagger_auto_exclude_pool (pool_id int,excluded bool) ON COMMIT DROP')
            cur.execute("INSERT INTO post SELECT id,'image','image/png',CASE WHEN id=6 THEN 'duplicate' ELSE 'ok' END FROM generate_series(1,6) id")
            cur.execute("INSERT INTO post_tagging_work SELECT id,CASE WHEN id=4 THEN 'failed' ELSE 'pending' END,CASE WHEN id=3 THEN now()+interval '1 hour' END,2 FROM generate_series(1,6) id")
            cur.execute("INSERT INTO pool VALUES (10,'legacy','pending'),(20,NULL,NULL)")
            cur.execute('INSERT INTO pool_post VALUES (10,1),(20,2)')
            cur.execute('INSERT INTO tagger_auto_exclude_pool VALUES (20,true)')
            # Negative control: the exact old function excludes the pending review post.
            cur.execute(migration.PREVIOUS_SQL.replace('public.','pg_temp.'))
            cur.execute('SELECT id FROM pg_temp.tagger_governed_claim_auto_tag(10,60)');assert {r[0] for r in cur.fetchall()}=={5}
            cur.execute('UPDATE post_tagging_work SET lease_until=NULL WHERE post_id=5')
            cur.execute(migration.CLAIM_SQL.replace('public.','pg_temp.'))
            cur.execute('SELECT id FROM pg_temp.tagger_governed_claim_auto_tag(10,60)');assert {r[0] for r in cur.fetchall()}=={1,5}
            cur.execute('SELECT verify_status,tagger_source FROM pool WHERE id=10');assert cur.fetchone()==('pending','legacy')
            cur.execute('SELECT DISTINCT attempts FROM post_tagging_work');assert cur.fetchall()==[(2,)]
            cur.execute('SELECT lease_until IS NULL FROM post_tagging_work WHERE post_id=2');assert cur.fetchone()==(True,)
    finally: conn.close()


def test_human_pin_sync_preserves_pool_provenance_and_is_idempotent():
    dsn=os.environ.get('KURA_TEST_DSN')
    if not dsn: pytest.skip('KURA_TEST_DSN required')
    import psycopg2
    spec=importlib.util.spec_from_file_location('retirement',ROOT/'api/alembic/versions/0190_retire_verification_pool_gate.py')
    m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
    conn=psycopg2.connect(dsn)
    try:
        with conn, conn.cursor() as cur:
            cur.execute('CREATE TEMP TABLE tagger_allowed_executor (db_role text,scope text) ON COMMIT DROP')
            cur.execute("INSERT INTO tagger_allowed_executor VALUES(session_user,'gt-sync')")
            cur.execute('CREATE TEMP TABLE post (id int,status text) ON COMMIT DROP')
            cur.execute('CREATE TEMP TABLE tag_name (tag_id int,name text,ord int) ON COMMIT DROP')
            cur.execute('CREATE TEMP TABLE post_tag_pin (post_id int,tag_id int,state text) ON COMMIT DROP')
            cur.execute('CREATE TEMP TABLE post_tag (post_id int,tag_id int,locked bool) ON COMMIT DROP')
            cur.execute('CREATE TEMP TABLE tagger_gt_label (tag text,tag_id int,post_id int,verdict text,source_pool_id int,derived_from text,verified_at timestamptz,UNIQUE(tag_id,post_id)) ON COMMIT DROP')
            cur.execute("INSERT INTO post VALUES (1,'ok'),(2,'ok'),(3,'ok'),(4,'duplicate'),(5,'ok')")
            cur.execute("INSERT INTO tag_name VALUES (1,'test',0)")
            cur.execute("INSERT INTO post_tag_pin VALUES (1,1,'absent'),(2,1,'present'),(3,1,'absent'),(4,1,'absent'),(5,1,'present')")
            cur.execute('INSERT INTO post_tag VALUES (2,1,false),(5,1,true)')
            cur.execute("INSERT INTO tagger_gt_label VALUES ('test',1,1,'positive',123,'implicit_review',now()),('test',1,2,'negative',456,'implicit_review',now())")
            for sql in m.GT_SQL: cur.execute(sql.replace('public.','pg_temp.'))
            for name in ('neg','pos'):cur.execute(f'SELECT pg_temp.tagger_governed_sync_{name}_gt_from_pins()')
            cur.execute('SELECT post_id,verdict,source_pool_id,derived_from FROM tagger_gt_label ORDER BY post_id')
            assert cur.fetchall()==[(1,'negative',123,'human_correction'),(2,'positive',456,'human_correction'),(3,'negative',None,'human_correction')]
            for name in ('neg','pos'):
                cur.execute(f'SELECT pg_temp.tagger_governed_sync_{name}_gt_from_pins()');assert cur.fetchone()==(0,)
    finally: conn.close()
