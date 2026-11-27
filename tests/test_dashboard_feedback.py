"""Real asyncpg/ordinary-role persistence and two-user isolation; private PG only."""
import asyncio,importlib.util
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4
import pytest
from fastapi import FastAPI,HTTPException,Response
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import create_async_engine,async_sessionmaker
from sqlalchemy.engine import URL
from tests.test_character_pin_private_pg import cluster
from api.routers import dashboard_evaluation as r


def test_auth_and_closed_inputs():
 app=FastAPI();app.include_router(r.router)
 async def absent():raise HTTPException(401,'login required')
 app.dependency_overrides[r.get_current_user]=absent
 with TestClient(app) as client:
  assert client.get('/api/tagger-dashboard/evaluation').status_code==401
 for changes in ({'criterion':'invented'},{'body':'   '},{'body':'x'*2001},{'page_revision':'bad'},{'user_id':9}):
  with pytest.raises(ValueError):r.Feedback(**dict(dict(request_id=uuid4(),criterion='general',body='a',page_revision=r.REVISION),**changes))


def test_replica_refuses_before_query(monkeypatch):
 monkeypatch.setenv('KURA_DB_ROLE','replica')
 with pytest.raises(HTTPException) as error:r.canonical_comments()
 assert error.value.status_code==503


def test_asyncpg_persistence_identity_pagination(cluster):
 spec=importlib.util.spec_from_file_location('feedback_migration',Path(__file__).parents[1]/'api/alembic/versions/0193_dashboard_feedback.py')
 mig=importlib.util.module_from_spec(spec);spec.loader.exec_module(mig)
 cluster('''CREATE ROLE kura LOGIN; CREATE ROLE replicator; CREATE TABLE public."user"(id integer PRIMARY KEY,name text); INSERT INTO public."user" VALUES(1,'alice'),(2,'bob'); GRANT USAGE,CREATE ON SCHEMA public TO kura; GRANT SELECT,REFERENCES ON public."user" TO kura;''')
 statements=[];mig.op=SimpleNamespace(execute=statements.append);mig.upgrade()
 cluster('BEGIN; SET ROLE kura; '+ ';'.join(statements)+'; COMMIT;')
 assert cluster("SELECT has_table_privilege('replicator','dashboard_feedback','SELECT')").strip()=='f'
 async def run():
  url=URL.create('postgresql+asyncpg',username='kura',database='postgres',query={'host':cluster('SHOW unix_socket_directories').strip(),'port':'59439'})
  engine=create_async_engine(url);sessions=async_sessionmaker(engine,expire_on_commit=False)
  alice=SimpleNamespace(user_id=1);bob=SimpleNamespace(user_id=2)
  data=dict(request_id=uuid4(),criterion='actionability',body='<script>alert(1)</script>',page_revision=r.REVISION)
  try:
   async with sessions() as db:
    first=await r.post_comment(r.Feedback(**data),db,alice)
    assert await r.post_comment(r.Feedback(**data),db,alice)==first
    with pytest.raises(HTTPException) as error:await r.post_comment(r.Feedback(**dict(data,body='different')),db,alice)
    assert error.value.status_code==409
   async with sessions() as db:
    response=Response();allrows=await r.comments(response,None,db,bob)
    assert response.headers['Cache-Control']=='private, no-store'
    assert allrows['comments'][0]['author']=='alice'
    assert allrows['comments'][0]['body']==data['body']
    with pytest.raises(HTTPException) as error:await r.delete_comment(first['id'],db,bob)
    assert error.value.status_code==404
   async with sessions() as db:
    await r.delete_comment(first['id'],db,alice)
    assert not (await r.comments(Response(),None,db,bob))['comments']
    for i in range(52):await r.post_comment(r.Feedback(**dict(data,request_id=uuid4(),body=str(i))),db,bob)
    page=await r.comments(Response(),None,db,alice)
    assert len(page['comments'])==50 and page['next_before']
    last=await r.comments(Response(),page['next_before'],db,alice)
    assert len(last['comments'])==2
    assert {v['id'] for v in page['comments']}.isdisjoint(v['id'] for v in last['comments'])
  finally:await engine.dispose()
 asyncio.run(run())


def test_reports_have_all_criteria_and_explicit_limitations():
 import json
 ids={x['id'] for x in r.CRITERIA}
 for p in r.ROOT.glob('*-review.json'):
  report=json.loads(p.read_text())
  assert {x['id'] for x in report['criteria']}==ids
  assert isinstance(report['method'],str) and report['method'] and report['limitations']
 component=(Path(__file__).parents[1]/'web/src/lib/components/EvaluationPane.svelte').read_text()
 assert '@html' not in component


def test_review_definition_matching_refuses_missing_and_changed_inputs(tmp_path,monkeypatch):
 import json,hashlib
 root=tmp_path/'api'/'dashboard_evaluation';root.mkdir(parents=True)
 monkeypatch.setattr(r,'ROOT',root)
 assert not r.current_definition_matches()
 expected={}
 for name in r.REVIEWED_FILES:
  path=tmp_path/name;path.parent.mkdir(parents=True,exist_ok=True);path.write_text('reviewed')
  expected[name]=hashlib.sha256(path.read_bytes()).hexdigest()
 (root/'review-scope.json').write_text(json.dumps({'files':expected,'reviewed_revision':r.REVISION}))
 assert r.current_definition_matches()
 (tmp_path/r.REVIEWED_FILES[0]).write_text('changed')
 assert not r.current_definition_matches()


def test_saved_review_fingerprint_tracks_actual_definition_without_claiming_approval():
 import json,hashlib,subprocess
 repo=Path(__file__).parents[1]
 saved=json.loads((r.ROOT/'review-scope.json').read_text())
 for name,digest in saved['files'].items():
  assert hashlib.sha256(subprocess.check_output(['git','show',f'{r.REVISION}:{name}'],cwd=repo)).hexdigest()==digest
 assert r.current_definition_matches() == all(
  hashlib.sha256((repo/name).read_bytes()).hexdigest()==digest
  for name,digest in saved['files'].items())
 report=json.loads((r.ROOT/'self-review.json').read_text())
 assert '実装者' in report['reviewer'] and '独立レビューではない' in report['method']


def test_current_reviews_keep_raw_assessments_and_honest_observation_scope():
 import json,hashlib
 from collections import Counter
 root=r.ROOT
 assert {p.name for p in root.glob('*-review.json')}=={'self-review.json','gemini-review.json'}
 provenance=json.loads((root/'review-provenance.json').read_text())
 assert provenance['reviewed_revision']==r.REVISION
 assert provenance['live_api_revision'].startswith('5fafecce')
 assert provenance['capture']['writes']==provenance['capture']['page_errors']==0
 for name,digest in provenance['retained_text_sha256'].items():
  assert hashlib.sha256((root/name).read_bytes()).hexdigest()==digest
 for name,raw in [('self','codex-full.txt'),('gemini','gemini-full.txt')]:
  report=json.loads((root/f'{name}-review.json').read_text())
  assert report==json.loads((root/raw).read_text())
  assert report['reviewed_revision']==r.REVISION
  assert {item['id'] for item in report['criteria'] if item['assessment']!=item['verdict']}==({'situation','accessibility'} if name=='gemini' else set())
  assert Counter(item['assessment'] for item in report['criteria'])==provenance['grade_counts'][name]
  assert any(item['verdict']!='pass' for item in report['criteria'])
 assert provenance['gemini_observation']=='source_text_only_no_browser'
 notes=json.loads((root/'review-notes.json').read_text())
 assert 'no_fabricated_new_claude_review' in {n['id'] for n in notes['notes']}
 with pytest.raises(ValueError):r.Feedback(request_id=uuid4(),criterion='general',body='old definition',page_revision='1302822e38aee9e974fd94ff46b846e70ca9348b')


def test_previous_1302822e_review_history_is_byte_preserved():
 import hashlib,subprocess
 repo=Path(__file__).parents[1]
 history=r.ROOT/'history/1302822e'
 # Snapshot of the previously published evaluation, including original critiques
 # and separate correction notes; later reviews must not rewrite those authors.
 source='70fdb8262686465cad822358a1eca0e5a79fdb57'
 names=subprocess.check_output(['git','ls-tree','--name-only',source+':api/dashboard_evaluation'],cwd=repo,text=True).splitlines()
 for name in names:
  if name=='history':continue
  old=subprocess.check_output(['git','show',source+':api/dashboard_evaluation/'+name],cwd=repo)
  assert hashlib.sha256((history/name).read_bytes()).digest()==hashlib.sha256(old).digest()


def test_plain_summary_precedes_originals_without_hiding_lower_grades():
 import asyncio
 doc=asyncio.run(r.evaluation(Response(),SimpleNamespace(user_id=1)))
 assert doc['reports'][0]['reviewer']=='Codex（実装者）'
 assert len(doc['reports'])==2
 assert doc['reports'][1]['reviewer']=='Gemini（独立・テキスト検査）'
 # Original columns and all ten critiques remain in the response.
 assert len(doc['reports'][1]['criteria'])==10
 assert {x['id'] for x in doc['reports'][1]['criteria'] if x['verdict']!=x['assessment']}=={'situation','accessibility'}
