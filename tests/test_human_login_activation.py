"""Real API/PG handoff probes; unit-control shim starts only our private API.

The shim is a test driver, not claimed production systemd/custody evidence.
"""
import asyncio
import contextlib
import json
import os
from pathlib import Path
import queue
import socket
import subprocess
import sys
import threading
import time
from types import SimpleNamespace

import psycopg2
import pytest

from tests.test_human_login import (cluster, database, source_inventory, sql, RAW, ROOT,
                                  KeyRefused)
from scripts.human_login_keys import run as operator
from api.human_login_activation import Activation


@pytest.fixture
def live_activation(database,source_inventory,tmp_path,monkeypatch):
    config,deny=source_inventory
    env=Path(config['sources'][0]['environment_file'])
    env.write_text(env.read_text().replace("postgresql://","postgresql+asyncpg://")+'KURA_DB_ROLE=canonical\nKURA_HUMAN_LOGIN_MODE=bootstrap\n')
    inventory=tmp_path/'inventory.json';inventory.write_text(json.dumps(config));inventory.chmod(0o600)
    key=tmp_path/'old-key';key.write_text(RAW);key.chmod(0o600)
    revision=subprocess.check_output(['git','-C',str(ROOT),'rev-parse','HEAD'],text=True).strip()
    with socket.socket() as sock:
        sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
    program=tmp_path/'private_api.py'
    program.write_text('''import os,sys
sys.path.insert(0,sys.argv[1])
from pathlib import Path
from scripts.human_login_source import environment
os.environ.update(environment(Path(sys.argv[2])))
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import create_async_engine,async_sessionmaker
from api.routers.auth import router
from api.db.session import get_db
import uvicorn
engine=create_async_engine(os.environ['DATABASE_URL'].replace('postgresql://','postgresql+asyncpg://'),hide_parameters=True)
factory=async_sessionmaker(engine,expire_on_commit=False)
app=FastAPI();app.include_router(router)
async def own_db():
    async with factory() as db:yield db
app.dependency_overrides[get_db]=own_db
@app.get('/api/build')
async def build():return {'commit':sys.argv[4][:8],'dirty':False}
uvicorn.run(app,host='127.0.0.1',port=int(sys.argv[3]),log_level='error')
''')
    running=[];restarts=[]
    def start():
        if running:
            running[0].terminate();running[0].wait(timeout=10);running.clear()
        child=subprocess.Popen([sys.executable,str(program),str(ROOT),str(env),str(port),revision],
                               stdout=subprocess.DEVNULL,stderr=(tmp_path/'api.log').open('ab'))
        running.append(child)
        deadline=time.monotonic()+10
        while time.monotonic()<deadline:
            if child.poll() is not None:raise AssertionError('private API startup failed: '+(tmp_path/'api.log').read_text()[-2000:])
            try:
                with socket.create_connection(('127.0.0.1',port),timeout=.1):return
            except OSError:time.sleep(.02)
        raise AssertionError('private API startup deadline')
    start()
    original_run=subprocess.run
    unit='kura-auth-private-fixture.service'
    def unit_driver(command,*args,**kwargs):
        if command[:4]==['systemctl','--user','show',unit]:
            text=f'EnvironmentFiles={env} (ignore_errors=no)\nWorkingDirectory={ROOT}\nActiveState=active\nMainPID={running[0].pid}\n'
            return subprocess.CompletedProcess(command,0,text,'')
        if command==['systemctl','--user','restart',unit]:
            restarts.append(True);start();return subprocess.CompletedProcess(command,0,b'',b'')
        return original_run(command,*args,**kwargs)
    monkeypatch.setattr(subprocess,'run',unit_driver)
    args=SimpleNamespace(environment=env,operation='import',cut_inventory=inventory,apply=True,
        receipt=tmp_path/'receipt.json',staged_environment=tmp_path/'staged.env',api_unit=unit,
        api_url=f'http://127.0.0.1:{port}',activation_key_file=key,revision=revision,activation_timeout=30)
    errors=queue.Queue()
    @contextlib.contextmanager
    def activation_input(*,command=b'activate\n',lose_custody=False):
        read,write=os.pipe()
        reader=os.fdopen(read,'r');monkeypatch.setattr(sys,'stdin',reader)
        def external_operator():
            try:
                deadline=time.monotonic()+10
                while not args.staged_environment.exists():
                    if time.monotonic()>deadline:raise AssertionError('no staged activation')
                    time.sleep(.01)
                assert sql(database,'SELECT count(*) FROM human_login_key')==[(0,)]
                with psycopg2.connect(database[0]) as db:
                    with db.cursor() as cur:
                        cur.execute("SET lock_timeout='100ms'")
                        with pytest.raises(psycopg2.errors.LockNotAvailable):
                            cur.execute('UPDATE human_account SET retired_at=now()')
                env.write_bytes(args.staged_environment.read_bytes());start()
                if lose_custody:deny.chmod(0o600)
                os.write(write,command)
            except BaseException as exc:errors.put(exc)
            finally:os.close(write)
        thread=threading.Thread(target=external_operator);thread.start()
        try:yield
        finally:
            thread.join(timeout=15);reader.close()
            assert not thread.is_alive()
            if not errors.empty():raise errors.get()
    try:yield args,activation_input,restarts
    finally:
        for child in running:
            child.terminate();child.wait(timeout=10)


def test_same_live_cut_spans_staging_commit_and_real_login_probe(database,live_activation,monkeypatch):
    args,inputs,_=live_activation;sql(database,'DELETE FROM human_login_key')
    original=Activation.verify_committed
    def verify_with_live_barrier(self):
        assert sql(database,'SELECT count(*) FROM human_login_key')==[(1,)]
        with psycopg2.connect(database[0]) as db:
            with db.cursor() as cur:
                cur.execute("SET lock_timeout='100ms'")
                with pytest.raises(psycopg2.errors.LockNotAvailable):
                    cur.execute('UPDATE human_account SET retired_at=now()')
        return original(self)
    monkeypatch.setattr(Activation,'verify_committed',verify_with_live_barrier)
    with inputs():asyncio.run(operator(args))
    receipt=json.loads(args.receipt.read_text())
    assert receipt['state']=='imported' and receipt['target']['outcome']=='committed'
    assert receipt['active_unit']['revision']==args.revision
    assert sql(database,'SELECT count(*) FROM human_login_key')==[(1,)]
    assert sql(database,'SELECT count(*) FROM user_token WHERE enabled')==[(0,)]
    for secret in (RAW,'person@example.invalid'):
        assert secret not in args.receipt.read_text()
    # Only after the positive probe/cut release can a source mutation acquire its lock.
    sql(database,'UPDATE human_account SET retired_at=now()')


@pytest.mark.parametrize('lose_custody,command',[(False,b'abort\n'),(True,b'activate\n')])
def test_precommit_abort_or_lost_cut_rolls_back_keys_and_proves_bootstrap(database,live_activation,lose_custody,command):
    args,inputs,restarts=live_activation;sql(database,'DELETE FROM human_login_key')
    with inputs(command=command,lose_custody=lose_custody):
        with pytest.raises(KeyRefused):asyncio.run(operator(args))
    receipt=json.loads(args.receipt.read_text())
    assert receipt['state']=='aborted_blocked' and restarts==[True]
    assert receipt['target']['outcome']=='not_committed'
    assert receipt['source_cut']['sources'][0]['writers']
    assert sql(database,'SELECT count(*) FROM human_login_key')==[(0,)]
    assert 'KURA_HUMAN_LOGIN_MODE=bootstrap' in args.environment.read_text()


def test_postcommit_probe_failure_never_claims_auth_rollback(database,live_activation,monkeypatch):
    args,inputs,restarts=live_activation;sql(database,'DELETE FROM human_login_key')
    def failed(self):raise KeyRefused('injected_postcommit_probe_failure')
    monkeypatch.setattr(Activation,'verify_committed',failed)
    with inputs():
        with pytest.raises(KeyRefused,match='injected_postcommit'):asyncio.run(operator(args))
    receipt=json.loads(args.receipt.read_text())
    assert receipt['state']=='committed_probe_unconfirmed'
    assert receipt['target']['outcome']=='committed'
    assert receipt['target']['transaction_id'].isdigit()
    assert receipt['source_cut']['sources'][0]['backend_pid']>0
    assert receipt['source_cut']['sources'][0]['account_snapshot_digest'].startswith('sha256:')
    assert receipt['staged_environment_digest'].startswith('sha256:')
    assert receipt['started_at'] and receipt['updated_at']
    assert sql(database,'SELECT count(*) FROM human_login_key')==[(1,)]
    assert not restarts and 'KURA_HUMAN_LOGIN_MODE=active' in args.environment.read_text()


def test_apply_does_not_reopen_an_existing_exact_import(database,live_activation,monkeypatch):
    args,_,_=live_activation
    read,write=os.pipe();reader=os.fdopen(read,'r');monkeypatch.setattr(sys,'stdin',reader)
    try:
        with pytest.raises(KeyRefused,match='already_imported_reconciliation_required'):
            asyncio.run(operator(args))
    finally:reader.close();os.close(write)
    assert not args.staged_environment.exists()
    assert json.loads(args.receipt.read_text())['state']=='aborted_before_cut'


def test_target_backend_loss_before_commit_leaves_no_usable_key(database,live_activation,monkeypatch):
    args,inputs,_=live_activation;sql(database,'DELETE FROM human_login_key')
    original=Activation.prepare
    def prepare_then_disconnect(self,cut,target):
        original(self,cut,target)
        assert sql(database,'SELECT pg_terminate_backend(%s)',(target['backend_pid'],))==[(True,)]
    monkeypatch.setattr(Activation,'prepare',prepare_then_disconnect)
    with inputs():
        with pytest.raises(Exception):asyncio.run(operator(args))
    assert sql(database,'SELECT count(*) FROM human_login_key')==[(0,)]
    # The commit acknowledgment failed: don't relabel it a proven committed cut.
    receipt=json.loads(args.receipt.read_text())
    assert receipt['state']=='commit_uncertain'
    assert receipt['target']['outcome']=='unknown'
    assert receipt['source_cut']['sources'][0]['denylist_files']
    assert receipt['target']['transaction_id'].isdigit()
    import urllib.request,urllib.error
    request=urllib.request.Request(args.api_url+'/api/auth/login',
        data=json.dumps({'email':'person@example.invalid','key':RAW}).encode(),
        headers={'Content-Type':'application/json'})
    with pytest.raises(urllib.error.HTTPError) as failure:urllib.request.urlopen(request,timeout=2)
    assert failure.value.code==401


@pytest.mark.parametrize('timeout',[float('inf'),float('nan'),0,-1])
def test_activation_requires_finite_positive_deadline(timeout,tmp_path):
    with pytest.raises(KeyRefused,match='activation_bounds_required'):
        Activation(environment_file=tmp_path/'absent',staged_environment=tmp_path/'stage',
            unit='fixture.service',api_url='http://127.0.0.1:12345',key_file=tmp_path/'key',
            revision='a'*40,timeout=timeout,input_fd=0,record=lambda x:None)


def test_slow_http_body_cannot_outlive_whole_probe_deadline():
    from http.server import ThreadingHTTPServer,BaseHTTPRequestHandler
    class Slow(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200);self.send_header('Content-Length','20');self.end_headers()
            self.wfile.write(b'{');self.wfile.flush();time.sleep(2)
        def log_message(self,*args):pass
    server=ThreadingHTTPServer(('127.0.0.1',0),Slow);server.daemon_threads=True
    thread=threading.Thread(target=server.serve_forever);thread.start()
    probe=object.__new__(Activation)
    probe.api_url=f'http://127.0.0.1:{server.server_address[1]}'
    probe.deadline=time.monotonic()+.15
    started=time.monotonic()
    try:
        with pytest.raises(KeyRefused,match='activation_http_unavailable'):probe.request('/api/build')
        assert time.monotonic()-started<1
    finally:server.shutdown();server.server_close();thread.join(timeout=2)
