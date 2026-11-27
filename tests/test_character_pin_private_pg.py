"""Execute the production pin trigger in a new, socket-only private PG cluster.

No API, live endpoint, existing DB or credentials. Minimal tables isolate the
pin trigger's contract; this is not a full claim-storage migration rehearsal.
"""
import ast
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import pytest

BIN = Path('/usr/lib/postgresql/18/bin')
MIG = Path(__file__).resolve().parents[1]/'api/alembic/versions/0057_invm_human_pin.py'


@pytest.fixture(scope='module')
def cluster():
    if not (BIN/'initdb').is_file(): pytest.skip('private PostgreSQL 18 binaries unavailable')
    root = Path(tempfile.mkdtemp(prefix='kura-cl2-pg-'))
    env = {'PATH':'/usr/bin:/bin','HOME':str(root),'LANG':'C.UTF-8'}
    def run(name, *args, input=None):
        return subprocess.run([str(BIN/name),*args], input=input,text=True,env=env,
            stdout=subprocess.PIPE,stderr=subprocess.PIPE,check=True,timeout=30).stdout
    started = False
    try:
        run('initdb','-D',str(root/'db'),'-A','trust','--no-locale','-U','fixture')
        run('pg_ctl','-D',str(root/'db'),'-l',str(root/'postgres.log'),'-o',
            f"-k {root} -p 59439 -c listen_addresses='' -c unix_socket_permissions=0700",'-w','start')
        started = True
        def sql(text):
            return run('psql','-X','-A','-t','-v','ON_ERROR_STOP=1','-h',str(root),'-p','59439',
                       '-U','fixture','-d','postgres',input=text)
        sql("CREATE TABLE post_tag(post_id int,tag_id int); CREATE TABLE post_tag_pin(post_id int,tag_id int,state text);")
        chunks=[]
        for n in ast.walk(ast.parse(MIG.read_text())):
            if isinstance(n,ast.Call) and isinstance(n.func,ast.Attribute) and n.func.attr=='execute' and n.args:
                arg=n.args[0]
                if isinstance(arg,ast.Constant) and isinstance(arg.value,str):
                    if 'CREATE FUNCTION post_tag_pin_guard_ins' in arg.value or 'CREATE TRIGGER "01_post_tag_pin_guard_ins"' in arg.value:
                        chunks.append((n.lineno,arg.value))
        assert len(chunks)==2
        for _,chunk in sorted(chunks): sql(chunk)
        yield sql
    finally:
        # Never remove an owned cluster until its server has stopped.
        if started: run('pg_ctl','-D',str(root/'db'),'-m','immediate','-w','stop')
        shutil.rmtree(root)


@pytest.mark.parametrize('actor', ['', 'script', 'cl_tagger_v2', 'pixai'])
def test_machine_insert_and_implication_cannot_revive_absent_pin(cluster, actor):
    result=cluster(f"""BEGIN;
SET LOCAL kura.tag_actor='{actor}';
INSERT INTO post_tag_pin VALUES(17,41,'absent');
WITH added AS (INSERT INTO post_tag VALUES(17,41),(17,42) RETURNING tag_id)
SELECT 'direct:'||string_agg(tag_id::text,',') FROM added;
WITH added AS (INSERT INTO post_tag SELECT 17,41 FROM post_tag WHERE tag_id=42 RETURNING tag_id)
SELECT 'implied:'||count(*) FROM added;
ROLLBACK;""")
    assert 'direct:42' in result
    assert 'implied:0' in result


def test_human_positive_control_proves_trigger_is_decisive(cluster):
    result=cluster("""BEGIN; SET LOCAL kura.tag_actor='human';
INSERT INTO post_tag_pin VALUES(17,41,'absent');
WITH added AS (INSERT INTO post_tag VALUES(17,41) RETURNING tag_id)
SELECT 'human:'||count(*) FROM added; ROLLBACK;""")
    assert 'human:1' in result
