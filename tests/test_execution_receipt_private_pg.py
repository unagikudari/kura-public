"""Real PG18 receipt DDL/immutability/compatibility; isolated minimal schema, not full fleet migration."""
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace as N
import subprocess
import pytest
from tests.test_character_pin_private_pg import cluster
from tests.test_execution_receipt import body
from tagger import execution_receipt as c

@pytest.fixture(scope='module')
def receipt_db(cluster):
    cluster("""CREATE ROLE kura; CREATE ROLE kura_reconciler; CREATE ROLE replicator;
CREATE TABLE tagger_allowed_executor(scope text,db_role text);
INSERT INTO tagger_allowed_executor VALUES ('worker-heartbeat','fixture'),('worker-heartbeat','kura');
GRANT SELECT ON tagger_allowed_executor TO kura_reconciler;
CREATE TABLE tagger_worker_heartbeat(worker_id text PRIMARY KEY,host text,profile text,generation bigint,release_id integer,provider text,model_sha256 text,reported_at timestamptz);
ALTER TABLE tagger_worker_heartbeat OWNER TO kura_reconciler;
CREATE TABLE post_tag_audit(id int);
""")
    path=Path(__file__).resolve().parents[1]/'api/alembic/versions/0192_tagger_execution_receipt.py'
    spec=importlib.util.spec_from_file_location('receipt_migration',path);m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
    statements=[]
    # The owner-only statements go through api/alembic/owner.as_owner (SET SESSION AUTHORIZATION around each):
    # replayed here as they are issued, from a session that starts as `kura` like the privileged upgrade's.
    bind=N(execute=lambda t:(statements.append(str(t)),N(one=lambda:('kura','kura')))[1])
    m.op=N(execute=lambda t:statements.append(str(t)),get_bind=lambda:bind);m.upgrade()
    statements=[x for x in statements if not x.startswith('SELECT session_user')]
    assert sum('SESSION AUTHORIZATION "kura_reconciler"' in x for x in statements)==6
    cluster('GRANT CREATE ON SCHEMA public TO kura,kura_reconciler;ALTER TABLE tagger_worker_heartbeat OWNER TO kura;'
            'ALTER TABLE post_tag_audit OWNER TO kura;GRANT SELECT,INSERT,UPDATE ON tagger_worker_heartbeat TO kura_reconciler;')
    cluster('BEGIN;SET SESSION AUTHORIZATION kura;'+ ';'.join(statements)+';COMMIT;')
    assert cluster("SELECT count(*) FROM pg_proc p JOIN pg_roles r ON r.oid=p.proowner WHERE proname='tagger_upsert_heartbeat' AND rolname='kura_reconciler'").strip()=='2'
    # Subscriber pre-stage/repeated propagation is idempotent.
    cluster('BEGIN;'+m.REPLICA_DDL+'COMMIT;')
    assert cluster("SELECT count(*) FROM pg_constraint WHERE conname IN ('heartbeat_execution_receipt_hex','audit_execution_receipt_hex') AND NOT convalidated").strip()=='2'
    cluster('BEGIN;'+m.VALIDATION_DDL+'COMMIT;')
    assert cluster("SELECT has_table_privilege('replicator','tagger_execution_receipt','SELECT')").strip()=='f'
    return cluster

def quote(x):return "'"+x.replace("'","''")+"'"

def insert():
    p=c.make(body());wire=c.canonical(p['body']).decode()
    return p['receipt_sha256'], "INSERT INTO tagger_execution_receipt(receipt_sha256,canonical_body,body) VALUES (%s,%s,%s::jsonb);" % (quote(p['receipt_sha256']),quote(wire),quote(wire))

def test_real_receipt_dedupe_heartbeat_and_legacy_null(receipt_db):
    ref,sql=insert()
    out=receipt_db("BEGIN;"+sql+sql.replace(';',' ON CONFLICT DO NOTHING;')+f"""
SELECT tagger_upsert_heartbeat('w','nodeb','wd14',6::bigint,14,'ROCMExecutionProvider','model','{ref}');
SELECT 'ref:'||execution_receipt_sha256 FROM tagger_worker_heartbeat WHERE worker_id='w';
SELECT tagger_upsert_heartbeat('w','nodeb','wd14',5::bigint,14,'CPUExecutionProvider','model');
SELECT 'unknown:'||(execution_receipt_sha256 IS NULL)::text FROM tagger_worker_heartbeat WHERE worker_id='w';
SELECT 'count:'||count(*) FROM tagger_execution_receipt; ROLLBACK;""")
    assert 'ref:'+ref in out and 'unknown:true' in out and 'count:1' in out

@pytest.mark.parametrize('mutation',["UPDATE tagger_execution_receipt SET first_seen_at=now()", "DELETE FROM tagger_execution_receipt", "TRUNCATE tagger_execution_receipt"])
def test_history_is_immutable(receipt_db,mutation):
    _,sql=insert()
    with pytest.raises(subprocess.CalledProcessError) as e:receipt_db('BEGIN;'+sql+mutation+';ROLLBACK;')
    assert 'execution_receipt_immutable' in e.value.stderr

def test_tampered_digest_and_wrong_context_refused(receipt_db):
    ref,sql=insert()
    with pytest.raises(subprocess.CalledProcessError):receipt_db('BEGIN;'+sql.replace(ref,'f'*64)+'ROLLBACK;')
    with pytest.raises(subprocess.CalledProcessError) as e:
        receipt_db('BEGIN;'+sql+f"SELECT tagger_upsert_heartbeat('w','other','wd14',6::bigint,14,'CPUExecutionProvider','model','{ref}');ROLLBACK;")
    assert 'execution_receipt_context' in e.value.stderr


def test_kura_executor_can_insert_and_call_both_signatures(receipt_db):
    ref,sql=insert()
    out=receipt_db('BEGIN; SET SESSION AUTHORIZATION kura;'+sql+f"""
SELECT tagger_upsert_heartbeat('authorized','nodeb','wd14',6::bigint,14,'ROCMExecutionProvider','model','{ref}');
SELECT tagger_upsert_heartbeat('authorized','nodeb','wd14',5::bigint,14,'CPUExecutionProvider','model');
SELECT 'stored:'||count(*) FROM tagger_execution_receipt;
RESET SESSION AUTHORIZATION; ROLLBACK;""")
    assert 'stored:1' in out
