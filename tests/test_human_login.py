"""Actual private PG18 transactions; never connect to configured production DSNs."""
from contextlib import closing
import asyncio
import importlib.util
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import tempfile
import sys
import uuid
from datetime import datetime, timezone

import psycopg2
import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

from api.db.models import User, UserToken
from api.human_login import KeyRefused, change_key, import_keys, key_digest, login
from api.human_login_cut import SourceCut
import schema_history

ROOT = Path(__file__).resolve().parents[1]
RAW = "1" * 64
OTHER = "2" * 64
AT = datetime(2026, 1, 1, tzinfo=timezone.utc)


@pytest.fixture(scope="module")
def cluster():
    binary = Path("/usr/lib/postgresql/18/bin")
    if not (binary / "initdb").exists():
        pytest.fail("PG18 required: this acceptance test may not silently skip")
    home = Path(tempfile.mkdtemp(prefix="kura-auth-pg-"))
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]
    subprocess.run([str(binary / "initdb"), "-D", str(home / "db"), "-A", "trust", "--no-locale", "--encoding=UTF8", "-U", "test_owner"],
                   check=True, capture_output=True)
    subprocess.run([str(binary / "pg_ctl"), "-D", str(home / "db"), "-l", str(home / "server.log"),
                    "-o", f"-k {home} -p {port} -c listen_addresses= -c fsync=off", "-w", "start"],
                   check=True, capture_output=True)
    try:
        yield home, port
    finally:
        subprocess.run([str(binary / "pg_ctl"), "-D", str(home / "db"), "-m", "immediate", "-w", "stop"],
                       check=True, capture_output=True)
        shutil.rmtree(home)


@pytest.fixture
def database(cluster, monkeypatch):
    monkeypatch.setenv("KURA_DB_ROLE", "canonical")
    monkeypatch.setenv("KURA_HUMAN_LOGIN_MODE", "active")
    home, port = cluster
    name = "auth_" + uuid.uuid4().hex
    dsn = f"dbname={name} user=test_owner host={home} port={port}"
    with closing(psycopg2.connect(f"dbname=postgres user=test_owner host={home} port={port}")) as c:
        c.autocommit = True
        c.cursor().execute('CREATE DATABASE "' + name + '"')
    sync_url = f"postgresql+psycopg2://test_owner@/{name}?host={home}&port={port}"
    async_url = f"postgresql+asyncpg://test_owner@/{name}?host={home}&port={port}"
    engine = create_engine(sync_url)
    User.__table__.create(engine); UserToken.__table__.create(engine)
    spec = importlib.util.spec_from_file_location("auth_migration", schema_history.migration("0167_human_login_key.py"))
    migration = importlib.util.module_from_spec(spec); spec.loader.exec_module(migration)
    with engine.begin() as c:
        class MigrationOperations:
            def execute(self, statement):
                c.execute(text(statement))
        original = migration.op
        migration.op = MigrationOperations()
        try:
            migration.upgrade()
        finally:
            migration.op = original
        c.execute(text("INSERT INTO public.\"user\"(id,name,email,rank,broker_trust_tier,creation_time,version,password_revision,avatar_style) "
                       "VALUES(1,'person','person@example.invalid','administrator','admin',now(),1,0,'gravatar')"))
        c.execute(text("INSERT INTO human_login_key VALUES(:digest,1,:at,NULL)"), {"digest": key_digest(RAW), "at": AT})
    engine.dispose()
    yield dsn, async_url
    with closing(psycopg2.connect(f"dbname=postgres user=test_owner host={home} port={port}")) as c:
        c.autocommit = True
        c.cursor().execute('DROP DATABASE "' + name + '" WITH (FORCE)')


def run(database, body):
    async def scenario():
        engine = create_async_engine(database[1], hide_parameters=True)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        try:
            return await body(factory)
        finally:
            await engine.dispose()
    return asyncio.run(scenario())


def sql(database, command, args=()):
    with psycopg2.connect(database[0]) as c:
        with c.cursor() as cur:
            cur.execute(command, args)
            return cur.fetchall() if cur.description else None


def test_local_login_rank_session_and_revoke(database):
    async def body(factory):
        async with factory() as db:
            result = await login(db, "PERSON@example.invalid", RAW)
            assert result["username"] == "person"
        async with factory() as db:
            await change_key(db, user_id=1, operation="revoke", predecessor=key_digest(RAW))
        async with factory() as db:
            with pytest.raises(KeyRefused, match="invalid_key"):
                await login(db, "person@example.invalid", RAW)
    run(database, body)
    assert sql(database, 'SELECT rank FROM public."user"') == [("administrator",)]
    assert sql(database, 'SELECT enabled FROM user_token') == [(True,)]
    assert sql(database, "SELECT expiration_time = creation_time + interval '30 days' FROM user_token") == [(True,)]


def test_unknown_mismatch_and_ambiguous_identity(database):
    async def body(factory):
        for email, key, expected in [("person@example.invalid", OTHER, "invalid_key"),
                                      ("other@example.invalid", RAW, "email_mismatch")]:
            async with factory() as db:
                with pytest.raises(KeyRefused, match=expected):
                    await login(db, email, key)
        sql(database, 'INSERT INTO public."user"(id,name,email,rank,creation_time,version,password_revision,avatar_style) '
            "VALUES(2,'duplicate','PERSON@example.invalid','regular',now(),1,0,'gravatar')")
        async with factory() as db:
            with pytest.raises(KeyRefused, match="identity_ambiguous"):
                await login(db, "person@example.invalid", RAW)
    run(database, body)
    assert sql(database, "SELECT count(*) FROM user_token") == [(0,)]


def test_revoke_first_blocks_and_prevents_login(database):
    async def body(factory):
        async with factory() as revoker:
            await revoker.execute(text('SELECT id FROM public."user" WHERE id=1 FOR UPDATE'))
            await revoker.execute(text("UPDATE human_login_key SET revoked_at=now()"))
            async def waiter():
                async with factory() as db:
                    await login(db, "person@example.invalid", RAW)
            task = asyncio.create_task(waiter())
            await asyncio.sleep(.15)
            assert not task.done()
            await revoker.commit()
            with pytest.raises(KeyRefused, match="invalid_key"):
                await task
    run(database, body)
    assert sql(database, "SELECT count(*) FROM user_token") == [(0,)]


def test_login_first_holds_lock_through_token_commit(database, monkeypatch):
    import api.human_login as module
    original = module.locked_key
    async def body(factory):
        entered, release = asyncio.Event(), asyncio.Event()
        async def barrier(db, digest):
            row = await original(db, digest)
            if asyncio.current_task().get_name() == "login-first":
                entered.set(); await release.wait()
            return row
        monkeypatch.setattr(module, "locked_key", barrier)
        async def first():
            async with factory() as db:
                return await login(db, "person@example.invalid", RAW)
        async def revoke():
            async with factory() as db:
                await change_key(db, user_id=1, operation="revoke", predecessor=key_digest(RAW))
        a = asyncio.create_task(first(), name="login-first")
        await asyncio.wait_for(entered.wait(), 2)
        b = asyncio.create_task(revoke())
        await asyncio.sleep(.15); assert not b.done()
        release.set(); await a; await b
    run(database, body)
    assert sql(database, "SELECT enabled FROM user_token") == [(True,)]


def test_simultaneous_rotation_consumes_old_key_once(database):
    async def body(factory):
        async def rotate(raw):
            async with factory() as db:
                await change_key(db, user_id=1, operation="rotate", predecessor=key_digest(RAW), replacement=key_digest(raw))
        outcomes = await asyncio.gather(rotate(OTHER), rotate("3" * 64), return_exceptions=True)
        assert sum(x is None for x in outcomes) == 1
        assert sum(isinstance(x, KeyRefused) and x.reason == "predecessor_consumed" for x in outcomes) == 1
    run(database, body)
    assert sql(database, "SELECT count(*) FROM human_login_key WHERE revoked_at IS NULL") == [(1,)]


def test_failed_rotation_rolls_back_old_revocation(database):
    async def body(factory):
        from sqlalchemy.exc import IntegrityError
        async with factory() as db:
            with pytest.raises(IntegrityError):
                await change_key(db, user_id=1, operation="rotate", predecessor=key_digest(RAW), replacement=key_digest(RAW))
    run(database, body)
    assert sql(database, "SELECT revoked_at FROM human_login_key") == [(None,)]


def test_replica_and_bootstrap_refuse_before_database(database, monkeypatch):
    async def body(factory):
        monkeypatch.setenv("KURA_DB_ROLE", "replica")
        async with factory() as db:
            with pytest.raises(KeyRefused, match="canonical_required"):
                await login(db, "person@example.invalid", RAW)
            assert not db.in_transaction()
        monkeypatch.setenv("KURA_DB_ROLE", "canonical")
        monkeypatch.setenv("KURA_HUMAN_LOGIN_MODE", "bootstrap")
        async with factory() as db:
            with pytest.raises(KeyRefused, match="handoff"):
                await login(db, "person@example.invalid", RAW)
            assert not db.in_transaction()
    run(database, body)


def test_router_no_broker_and_storage_failure_is_redacted(database, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from api.routers.auth import router
    from api.db.session import get_db
    import importlib.util
    assert importlib.util.find_spec("api.broker_client") is None   # no broker path left to fall back on
    engine = create_async_engine(database[1], hide_parameters=True)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    app = FastAPI(); app.include_router(router)
    async def get_private():
        async with factory() as db:
            yield db
    app.dependency_overrides[get_db] = get_private
    with TestClient(app) as client:
        assert client.post('/api/auth/login', json={'email':'person@example.invalid','key':RAW}).status_code == 200
        assert client.post('/api/auth/login', json={'email':'other@example.invalid','key':RAW}).status_code == 403
        sql(database, 'DROP TABLE human_login_key')
        response = client.post('/api/auth/login', json={'email':'person@example.invalid','key':RAW})
        assert response.status_code == 503 and RAW not in response.text and key_digest(RAW) not in response.text


@pytest.fixture
def source_inventory(database, tmp_path):
    """Real local custodian, scratch SQL and dedicated masked test unit.

    This tests actual checks; it is not evidence of the production fleet cut.
    """
    sql(database, "CREATE TABLE human_account(email text PRIMARY KEY,key_hash text,key_issued_at timestamptz,retired_at timestamptz)")
    sql(database, "INSERT INTO human_account VALUES(%s,%s,%s,NULL)",
        ("person@example.invalid", key_digest(RAW), AT))
    unit = "kura-auth-test-" + uuid.uuid4().hex + ".service"
    units = Path.home() / ".config/systemd/user"
    units.mkdir(parents=True, exist_ok=True)
    mask = units / unit; mask.symlink_to('/dev/null')
    subprocess.run(['systemctl','--user','daemon-reload'], check=True, capture_output=True)
    env = tmp_path / 'source.env'
    # Parse only our explicitly synthesized private cluster DSN.
    parts = psycopg2.extensions.parse_dsn(database[0])
    env.write_text(f"DATABASE_URL=postgresql://test_owner@/{parts['dbname']}?host={parts['host']}&port={parts['port']}\n")
    env.chmod(0o600)
    directory = tmp_path / 'frozen'; directory.mkdir()
    deny = directory / 'revoked_clients.json'; deny.write_text('{"revoked":[]}')
    deny.chmod(0o400); directory.chmod(0o500)
    config = {'sources':[{'id':'private-test-source','ssh_host':None,
            'python':sys.executable,
            'environment_file':str(env),'denylist_files':[str(deny)],
            'units':[unit],'writer_paths':[str(tmp_path/'absent-writer.py')]}]}
    try:
        yield config, deny
    finally:
        directory.chmod(0o700); deny.chmod(0o600)
        mask.unlink()
        subprocess.run(['systemctl','--user','daemon-reload'], check=True, capture_output=True)


def users(database):
    return [dict(zip(('id','email','rank','broker_trust_tier'), r)) for r in sql(
        database, 'SELECT id,email,rank,broker_trust_tier FROM public."user"')]


def test_complete_bootstrap_replay_dry_run_and_source_write_exclusion(database, source_inventory, monkeypatch):
    config, _ = source_inventory
    monkeypatch.setenv('KURA_HUMAN_LOGIN_MODE','bootstrap')
    sql(database, 'DELETE FROM human_login_key')
    with SourceCut(config, users(database)) as cut:
        # Real source writes cannot slip between snapshot and target commit.
        with psycopg2.connect(database[0]) as c:
            with c.cursor() as cur:
                cur.execute("SET lock_timeout='100ms'")
                with pytest.raises(psycopg2.errors.LockNotAvailable):
                    cur.execute('UPDATE human_account SET retired_at=now()')
        async def body(factory):
            async with factory() as db:
                assert await import_keys(db, cut.keys, custody=cut, dry_run=True) == 'validated_not_applied'
            assert sql(database,'SELECT count(*) FROM human_login_key') == [(0,)]
            async with factory() as db:
                assert await import_keys(db, cut.keys, custody=cut) == 'imported'
            async with factory() as db:
                assert await import_keys(db, cut.keys, custody=cut) == 'unchanged'
        run(database,body)
    async def after(factory):
        async with factory() as db:
            with pytest.raises(KeyRefused,match='source_cut_closed'):
                await import_keys(db,cut.keys,custody=cut)
    run(database,after)


@pytest.mark.parametrize('change', [
    "UPDATE human_login_key SET issued_at=issued_at + interval '1 second'",
    "UPDATE human_login_key SET revoked_at=now()",
    "DELETE FROM human_login_key; INSERT INTO human_login_key VALUES('" + key_digest(OTHER) + "',1,now(),NULL)",
])
def test_import_conflicts_never_resurrect(database, source_inventory, monkeypatch, change):
    config,_=source_inventory; monkeypatch.setenv('KURA_HUMAN_LOGIN_MODE','bootstrap')
    sql(database,change)
    before=sql(database,'SELECT * FROM human_login_key')
    with SourceCut(config,users(database)) as cut:
        async def body(factory):
            async with factory() as db:
                with pytest.raises(KeyRefused,match='import_state_conflict'):
                    await import_keys(db,cut.keys,custody=cut)
        run(database,body)
    assert sql(database,'SELECT * FROM human_login_key') == before


def test_source_revocation_before_barrier_is_not_lost(database, source_inventory):
    config,_=source_inventory
    sql(database,'UPDATE human_account SET retired_at=now()')
    with pytest.raises(KeyRefused,match='source_set_invalid'):
        SourceCut(config,users(database))


def test_source_file_mutation_and_lost_lock_refuse(database, source_inventory, monkeypatch):
    config,deny=source_inventory; monkeypatch.setenv('KURA_HUMAN_LOGIN_MODE','bootstrap')
    sql(database,'DELETE FROM human_login_key')
    with SourceCut(config,users(database)) as cut:
        # Simulated custody loss: operator changes permissions. The actual child
        # detects it and exits; merely retaining its old snapshot cannot import.
        deny.chmod(0o600)
        async def body(factory):
            async with factory() as db:
                with pytest.raises(KeyRefused,match='source_custody'):
                    await import_keys(db,cut.keys,custody=cut)
        run(database,body)
    assert sql(database,'SELECT count(*) FROM human_login_key') == [(0,)]


def test_live_mode_closes_bootstrap(database, source_inventory):
    config,_=source_inventory
    with SourceCut(config,users(database)) as cut:
        async def body(factory):
            async with factory() as db:
                with pytest.raises(KeyRefused,match='bootstrap_closed'):
                    await import_keys(db,cut.keys,custody=cut)
                assert not db.in_transaction()
        run(database,body)


def test_unrelated_role_cannot_read_or_mutate_verifiers(database):
    role='unrelated_'+uuid.uuid4().hex
    sql(database,'CREATE ROLE '+role)
    with psycopg2.connect(database[0]) as c:
        with c.cursor() as cur:
            cur.execute('SET ROLE '+role)
            with pytest.raises(psycopg2.errors.InsufficientPrivilege):
                cur.execute('SELECT * FROM human_login_key')
            c.rollback()
            cur.execute('SET ROLE '+role)
            with pytest.raises(psycopg2.errors.InsufficientPrivilege):
                cur.execute('UPDATE human_login_key SET revoked_at=now()')


def test_cli_help_is_config_free_and_key_output_is_exclusive(tmp_path):
    from scripts.human_login_keys import stage_key
    path=tmp_path/'new-key'
    raw=stage_key(path)
    assert len(raw)==64 and path.stat().st_mode & 0o777 == 0o600
    with pytest.raises(FileExistsError): stage_key(path)
    assert path.read_text().strip()==raw
    result=subprocess.run([sys.executable,
                           str(ROOT/'scripts/human_login_keys.py'),'--help'],capture_output=True,text=True)
    assert result.returncode==0 and 'import' in result.stdout


@pytest.mark.parametrize('mutation,reason', [
    ("UPDATE human_account SET key_hash='malformed'",'invalid_digest'),
    ('UPDATE human_account SET key_issued_at=NULL','source_timestamp_missing'),
    ("UPDATE public.\"user\" SET rank='regular'",'source_rank_conflict'),
])
def test_source_bad_credential_data_refuses(database,source_inventory,mutation,reason):
    config,_=source_inventory; sql(database,mutation)
    with pytest.raises(KeyRefused,match=reason): SourceCut(config,users(database))


def test_union_of_denylists_refuses_a_denied_account(database,source_inventory):
    config,deny=source_inventory
    deny.chmod(0o600); deny.write_text('{"revoked":["human:person@example.invalid"]}'); deny.chmod(0o400)
    with pytest.raises(KeyRefused,match='source_set_invalid'): SourceCut(config,users(database))


def test_missing_source_file_is_not_an_empty_denylist(database,source_inventory):
    config,_=source_inventory
    config['sources'][0]['denylist_files'].append('/nonexistent-kura-test-denylist')
    with pytest.raises(KeyRefused,match='source_custody_refused'): SourceCut(config,users(database))


def test_migration_removes_inherited_named_grants_and_rejects_publication(database):
    spec=importlib.util.spec_from_file_location('migration',schema_history.migration('0167_human_login_key.py'))
    migration=importlib.util.module_from_spec(spec);spec.loader.exec_module(migration)
    role='replica_'+uuid.uuid4().hex
    sql(database,'CREATE ROLE '+role)
    sql(database,'DROP TABLE human_login_key')
    sql(database,'ALTER DEFAULT PRIVILEGES GRANT SELECT ON TABLES TO '+role)
    with psycopg2.connect(database[0]) as c:
        class Ops:
            def execute(self,statement):
                with c.cursor() as cur:cur.execute(statement)
        migration.op=Ops();migration.upgrade()
    assert sql(database,"SELECT has_table_privilege(%s,'human_login_key','SELECT')",(role,)) == [(False,)]
    assert migration.REPLICA_DDL is None
    sql(database,'DROP TABLE human_login_key')
    pub='unsafe_'+uuid.uuid4().hex
    sql(database,'CREATE PUBLICATION '+pub+' FOR ALL TABLES')
    with psycopg2.connect(database[0]) as c:
        class Ops:
            def execute(self,statement):
                with c.cursor() as cur:cur.execute(statement)
        migration.op=Ops()
        with pytest.raises(psycopg2.errors.RaiseException,match='must not be published'):
            migration.upgrade()
    assert sql(database,"SELECT to_regclass('human_login_key')") == [(None,)]


def test_old_token_still_authenticates_after_login_key_revoke(database):
    import base64
    from starlette.requests import Request
    from api.auth import get_current_user
    async def body(factory):
        async with factory() as db:
            session=await login(db,'person@example.invalid',RAW)
        async with factory() as db:
            await change_key(db,user_id=1,operation='revoke',predecessor=key_digest(RAW))
        header='Token '+base64.b64encode((session['username']+':'+session['token']).encode()).decode()
        request=Request({'type':'http','headers':[(b'authorization',header.encode())]})
        async with factory() as db:
            user=await get_current_user(request,db)
            assert user.user_id==1 and user.rank=='administrator'
    run(database,body)


@pytest.mark.parametrize("payload,reason", [("{", "source_custody_timeout"), ("", "source_custody_timeout")])
def test_source_partial_response_keeps_absolute_deadline(monkeypatch, payload, reason):
    import time
    import api.human_login_cut as module
    monkeypatch.setattr(module, 'SOURCE_READ_TIMEOUT', .1)
    child = subprocess.Popen([sys.executable, '-c',
        'import sys,time;sys.stdout.write(sys.argv[1]);sys.stdout.flush();time.sleep(5)', payload],
        stdout=subprocess.PIPE, text=True)
    started=time.monotonic()
    try:
        with pytest.raises(KeyRefused, match=reason): SourceCut._read(child)
        assert time.monotonic()-started < 2
    finally:
        child.terminate();child.wait(timeout=5)


def test_source_fragmented_complete_response_is_read(monkeypatch):
    import api.human_login_cut as module
    monkeypatch.setattr(module, 'SOURCE_READ_TIMEOUT', 2)
    child=subprocess.Popen([sys.executable,'-c',
        'import sys,time;sys.stdout.write(\'{"held":\');sys.stdout.flush();time.sleep(.02);print("true}")'],
        stdout=subprocess.PIPE,text=True)
    try: assert SourceCut._read(child)=={'held':True}
    finally: child.wait(timeout=5)


@pytest.mark.parametrize('apply', [False])
def test_operator_import_writes_exact_redacted_private_receipt(database,source_inventory,tmp_path,apply):
    import json
    from types import SimpleNamespace
    from scripts.human_login_keys import run as operator
    config,_=source_inventory
    env=Path(config['sources'][0]['environment_file'])
    env.write_text(env.read_text()+'KURA_DB_ROLE=canonical\nKURA_HUMAN_LOGIN_MODE=bootstrap\n')
    inventory=tmp_path/'inventory.json';inventory.write_text(json.dumps(config));inventory.chmod(0o600)
    receipt=tmp_path/'receipt.json'
    sql(database,'DELETE FROM human_login_key')
    args=SimpleNamespace(environment=env,operation='import',cut_inventory=inventory,apply=apply,receipt=receipt)
    asyncio.run(operator(args))
    value=json.loads(receipt.read_text())
    assert receipt.stat().st_mode & 0o777 == 0o600
    assert value['state']==('imported' if apply else 'validated_not_applied')
    assert value['target']['outcome']==('committed' if apply else 'rolled_back')
    assert value['target']['transaction_id'].isdigit() and value['target']['backend_pid']>0
    source=value['source_cut']['sources'][0]
    assert source['backend_pid']>0 and source['account_snapshot_digest'].startswith('sha256:')
    assert source['writers']==[{'unit':config['sources'][0]['units'][0], 'active_state':'inactive','unit_file_state':'masked'}]
    assert source['denylist_files'][0][0]==config['sources'][0]['denylist_files'][0]
    for secret in (RAW,key_digest(RAW),'person@example.invalid'):
        assert secret not in receipt.read_text()
    assert sql(database,'SELECT count(*) FROM human_login_key')==[(1 if apply else 0,)]
    with pytest.raises(FileExistsError): asyncio.run(operator(args))
    assert json.loads(receipt.read_text())==value


def test_failed_import_keeps_pending_receipt(database,source_inventory,tmp_path):
    import json
    from types import SimpleNamespace
    from scripts.human_login_keys import run as operator
    config,_=source_inventory
    env=Path(config['sources'][0]['environment_file'])
    env.write_text(env.read_text()+'KURA_DB_ROLE=canonical\nKURA_HUMAN_LOGIN_MODE=bootstrap\n')
    inventory=tmp_path/'inventory.json';inventory.write_text(json.dumps(config));inventory.chmod(0o600)
    receipt=tmp_path/'receipt.json'
    sql(database,"UPDATE human_login_key SET issued_at=issued_at+interval '1 second'")
    args=SimpleNamespace(environment=env,operation='import',cut_inventory=inventory,apply=False,receipt=receipt)
    with pytest.raises(KeyRefused,match='import_state_conflict'): asyncio.run(operator(args))
    value=json.loads(receipt.read_text())
    assert value['state']=='pending'
    assert value['source_cut']['sources'][0]['backend_pid']>0


def test_source_backpressured_write_respects_deadline():
    import time
    child=subprocess.Popen([sys.executable,'-c','import time;time.sleep(5)'],stdin=subprocess.PIPE,text=True)
    started=time.monotonic()
    try:
        with pytest.raises(KeyRefused,match='source_custody_timeout'):
            SourceCut._write(child,b'x'*1000000,started+.1)
        assert time.monotonic()-started<2
    finally:child.terminate();child.wait(timeout=5)


def test_receipt_update_failure_preserves_last_complete_evidence(tmp_path,monkeypatch):
    from scripts.human_login_keys import private_receipt
    import api.human_login_activation as activation
    path=tmp_path/'cut-receipt.json'
    with private_receipt(path) as record:
        record({'state':'awaiting_activation','source_cut':{'sources':[{'id':'source','backend_pid':42}]}})
        before=path.read_bytes()
        def unavailable(*args):raise OSError('injected replacement failure')
        monkeypatch.setattr(activation.os,'replace',unavailable)
        with pytest.raises(OSError):record({'state':'commit_uncertain'})
        assert path.read_bytes()==before
        assert json.loads(before)['source_cut']['sources'][0]['backend_pid']==42
    assert path.stat().st_mode & 0o777==0o600
