import hashlib,importlib.util,json,os,pathlib,subprocess
from types import SimpleNamespace
import pytest
import psycopg2
from tagger import cl2_publish as p

@pytest.fixture(scope='module')
def database(tmp_path_factory):
 root=tmp_path_factory.mktemp('cl2-publisher-pg');root.chmod(0o700);bin=pathlib.Path('/usr/lib/postgresql/18/bin')
 env={'PATH':'/usr/bin:/bin','HOME':str(root),'LANG':'C.UTF-8'}
 def run(name,*args):return subprocess.run([str(bin/name),*args],env=env,capture_output=True,text=True,check=True,timeout=30)
 run('initdb','-D',str(root/'db'),'-A','trust','-U','fixture','--no-locale')
 run('pg_ctl','-D',str(root/'db'),'-l',str(root/'log'),'-o',f"-k {root} -p 55461 -c listen_addresses=''",'-w','start')
 try:
  c=psycopg2.connect(host=str(root),port=55461,user='fixture',dbname='postgres');c.autocommit=True
  with c.cursor() as q:
   q.execute('''CREATE ROLE kura LOGIN; CREATE ROLE kura_reconciler;
   CREATE TABLE kura_alembic_version(version_num text); INSERT INTO kura_alembic_version VALUES('0192');
   CREATE TABLE tagger_allowed_executor(db_role text,scope text); INSERT INTO tagger_allowed_executor VALUES('kura','release-publish');
   CREATE TABLE tagger_release(id serial PRIMARY KEY,commit_sha text,base_model_repo text,base_model_revision text,patch_set_digest text,model_sha256 text,model_data bytea,selected_tags_csv bytea,n_stock integer,n_out integer,patched jsonb,lockfile text,created_by text);
   INSERT INTO tagger_release(id,commit_sha,model_sha256,model_data,selected_tags_csv,patched) VALUES(14,'old','model',decode('010203','hex'),decode('0405','hex'),'["held-head"]');
   SELECT setval('tagger_release_id_seq',14);
   CREATE TABLE tagger_release_active(id int PRIMARY KEY,release_id int,generation bigint,updated_at timestamptz,updated_by text); INSERT INTO tagger_release_active VALUES(1,14,5,now(),'old'); ALTER TABLE tagger_release_active ADD COLUMN required_worker_generation integer NOT NULL DEFAULT 6;
   CREATE TABLE tagger_artifact(content_sha256 text PRIMARY KEY,byte_size bigint);
   CREATE TABLE tagger_release_manifest(release_digest text PRIMARY KEY,provenance jsonb,created_by text,release_id integer);
   CREATE TABLE tagger_release_component(release_digest text REFERENCES tagger_release_manifest,role text,ordinal int,content_sha256 text REFERENCES tagger_artifact,deriver_sha256 text,params jsonb,PRIMARY KEY(release_digest,role,ordinal));
   CREATE TABLE tagger_producer_threshold(producer text,category int,threshold double precision,evidence text,PRIMARY KEY(producer,category));
   CREATE TABLE post_tag_audit(id bigint,post_id int,release_id int,host text,source text,execution_receipt_sha256 text);
   CREATE TABLE tagger_execution_receipt(receipt_sha256 text PRIMARY KEY,body jsonb);
   CREATE TABLE post_tag(post_id int,tag_id int); CREATE TABLE post_tag_pin(post_id int,tag_id int,state text);
   CREATE TABLE head_policy(tag text,gate_tags jsonb); INSERT INTO head_policy VALUES('smegma','[]');
   CREATE VIEW tagger_current_head_policy AS SELECT * FROM head_policy;
   CREATE TABLE tagger_expected_worker(host text,profile text); INSERT INTO tagger_expected_worker VALUES('nodeb','wd14');
   CREATE TABLE tagger_worker_heartbeat(host text,profile text,generation int,release_id int,reported_at timestamptz);
   INSERT INTO tagger_worker_heartbeat VALUES('nodeb','wd14',6,14,now()); ALTER TABLE tagger_worker_heartbeat ADD COLUMN execution_receipt_sha256 text;
   GRANT SELECT,INSERT,UPDATE ON ALL TABLES IN SCHEMA public TO kura;
   GRANT SELECT,INSERT,UPDATE ON ALL TABLES IN SCHEMA public TO kura_reconciler;
   GRANT USAGE,SELECT ON ALL SEQUENCES IN SCHEMA public TO kura_reconciler;
   ''')
   path=pathlib.Path(__file__).resolve().parents[1]/'api/alembic/versions/0087_tagger_governed_publish.py'
   spec=importlib.util.spec_from_file_location('publish_migration',path);m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
   chunks=[];m.op=SimpleNamespace(execute=chunks.append);m.upgrade()
   q.execute(chunks[0]);q.execute("ALTER FUNCTION tagger_governed_publish_release(text,text,text,text,text,bytea,bytea,integer,integer,jsonb,text,text) OWNER TO kura_reconciler")
   q.execute("REVOKE ALL ON FUNCTION tagger_governed_publish_release(text,text,text,text,text,bytea,bytea,integer,integer,jsonb,text,text) FROM PUBLIC")
   q.execute("GRANT EXECUTE ON FUNCTION tagger_governed_publish_release(text,text,text,text,text,bytea,bytea,integer,integer,jsonb,text,text) TO kura")
  yield root,c
 finally:
  c.close();run('pg_ctl','-D',str(root/'db'),'-m','fast','-w','stop')

@pytest.fixture
def db(database):
 root,admin=database
 with admin.cursor() as q:
  q.execute("UPDATE tagger_release_active SET release_id=14,generation=5; DELETE FROM tagger_release_component; DELETE FROM tagger_release_manifest; DELETE FROM tagger_artifact; DELETE FROM tagger_producer_threshold; DELETE FROM tagger_release WHERE id<>14; UPDATE tagger_worker_heartbeat SET generation=6,release_id=14,reported_at=now(); UPDATE head_policy SET gate_tags='[]'; DELETE FROM post_tag_audit; DELETE FROM tagger_execution_receipt; DELETE FROM post_tag; DELETE FROM post_tag_pin; UPDATE tagger_worker_heartbeat SET execution_receipt_sha256=NULL")
 conn=psycopg2.connect(host=str(root),port=55461,user='kura',dbname='postgres')
 yield conn,admin,root
 conn.close()

FILES=[dict(content_sha256=s,byte_size=123+i) for i,s in enumerate(p.cl2_chars.FILES.values())]
def plan(conn):
 with conn.cursor() as q:r=p.build_plan(q,FILES,14)
 conn.rollback();return r

def test_governed_publish_preserves_bytes_heads_and_binds_three_rows(db):
 c,admin,_=db;before=plan(c);result=p.publish(c,before,FILES,'a'*40,execute=True)
 with admin.cursor() as q:
  q.execute('SELECT release_id,generation FROM tagger_release_active');assert q.fetchone()==(result['release_id'],6)
  q.execute('SELECT model_data,selected_tags_csv,patched FROM tagger_release WHERE id=%s',(result['release_id'],));r=q.fetchone();assert bytes(r[0])==b'\1\2\3' and bytes(r[1])==b'\4\5' and r[2]==['held-head']
  q.execute("SELECT count(*) FROM tagger_release_component WHERE role='character_base'");assert q.fetchone()==(3,)
  q.execute('SELECT threshold,evidence FROM tagger_producer_threshold');assert q.fetchone()==(.45,p.EVIDENCE)
 with pytest.raises(p.Refused,match='active_release_or_generation_changed'):p.publish(c,before,FILES,'a'*40,execute=True)

@pytest.mark.parametrize('change,reason',[("UPDATE tagger_release_active SET generation=6",'active_release_or_generation_changed'),("UPDATE head_policy SET gate_tags='[\"other\"]'",'plan_changed'),("UPDATE tagger_worker_heartbeat SET generation=5",'generation6_workers_not_ready')])
def test_changed_preconditions_leave_no_new_artifacts(db,change,reason):
 c,admin,_=db;before=plan(c)
 with admin.cursor() as q:q.execute(change)
 with pytest.raises(p.Refused,match=reason):p.publish(c,before,FILES,'a'*40,execute=True)
 with admin.cursor() as q:q.execute('SELECT count(*) FROM tagger_release_manifest');assert q.fetchone()==(0,)

def test_rollback_on_late_failure_including_governed_call(db):
 c,admin,_=db;before=plan(c)
 with admin.cursor() as q:q.execute("CREATE FUNCTION reject_manifest_link() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'fixture'; END $$; CREATE TRIGGER reject_manifest_link BEFORE UPDATE ON tagger_release_manifest FOR EACH ROW EXECUTE FUNCTION reject_manifest_link()")
 try:
  with pytest.raises(psycopg2.Error):p.publish(c,before,FILES,'a'*40,execute=True)
  with admin.cursor() as q:
   q.execute('SELECT release_id,generation FROM tagger_release_active');assert q.fetchone()==(14,5)
   q.execute('SELECT count(*) FROM tagger_release_component');assert q.fetchone()==(0,)
   q.execute('SELECT count(*) FROM tagger_producer_threshold');assert q.fetchone()==(0,)
 finally:
  with admin.cursor() as q:q.execute('DROP TRIGGER reject_manifest_link ON tagger_release_manifest; DROP FUNCTION reject_manifest_link()')

def test_row_lock_blocks_competing_publisher(db):
 c,admin,_=db;before=plan(c)
 blocker=psycopg2.connect(host=c.info.host,port=c.info.port,user='kura',dbname='postgres')
 try:
  with blocker.cursor() as q:q.execute('SELECT * FROM tagger_release_active FOR UPDATE')
  with pytest.raises(psycopg2.errors.LockNotAvailable):p.publish(c,before,FILES,'a'*40,execute=True)
 finally:blocker.rollback();blocker.close()

def test_rehearsal_requires_private_endpoint_before_writes(db):
 c,admin,root=db;before=plan(c)
 with admin.cursor() as q:q.execute('GRANT pg_read_all_settings TO kura')
 with pytest.raises(p.Refused,match='rehearsal_not_private_endpoint'):p.publish(c,before,FILES,'a'*40,private_directory='/not-the-cluster')
 result=p.publish(c,before,FILES,'a'*40,private_directory=root/'db')
 assert result['state']=='rehearsed_rolled_back'
 with admin.cursor() as q:q.execute('SELECT release_id,generation FROM tagger_release_active');assert q.fetchone()==(14,5)

@pytest.mark.parametrize('mutation,reason',[(None,None),('missing_receipt','ReceiptRefused'),('wrong_source','canary_character_source_mismatch'),('pin_conflict','canary_negative_pin_conflict'),('no_completion','canary_no_completion')])
def test_canary_requires_actual_new_release_receipts_and_completions(db,mutation,reason):
 from tagger import cl2_canary as canary,execution_receipt
 from tests.test_execution_receipt import body
 c,admin,_=db;published=p.publish(c,plan(c),FILES,'a'*40,execute=True)
 b=body();b.update(host='nodeb',profile='wd14',release_id=published['release_id'],component_source=canary.expected_source())
 if mutation=='wrong_source':b['component_source']='cl_tagger_v2='+'0'*64
 packet=execution_receipt.make(b);ref=packet['receipt_sha256']
 with admin.cursor() as q:
  q.execute('INSERT INTO tagger_execution_receipt VALUES(%s,%s::jsonb)',(ref,json.dumps(b)))
  q.execute('UPDATE tagger_worker_heartbeat SET release_id=%s,execution_receipt_sha256=%s',(published['release_id'],None if mutation=='missing_receipt' else ref))
  if mutation!='no_completion':q.execute("INSERT INTO post_tag_audit VALUES(10,1,%s,'nodeb','auto_tag',%s)",(published['release_id'],ref))
  if mutation=='pin_conflict':q.execute("INSERT INTO post_tag VALUES(1,2); INSERT INTO post_tag_pin VALUES(1,2,'absent')")
 if reason:
  with pytest.raises((p.Refused,execution_receipt.ReceiptRefused)) as e:canary.check(c,published,['nodeb'])
  if reason!='ReceiptRefused':assert str(e.value)==reason
 else:
  result=canary.check(c,published,['nodeb']);assert result['hosts'][0]['completions']==1 and result['state']=='canary_observed'
