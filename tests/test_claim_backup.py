"""A normalized backup must restore the real relational graph, without the checkpoint."""
import json
from pathlib import Path
import tempfile
from collections import namedtuple
import pytest
import psycopg2
from test_collector_next import postgres,run  # noqa:F401
from test_pool_search_candidates import search_database  # noqa:F401
from test_claim_storage import initialize
from api.claim_storage.migration import copy_batch,verify_batch
from api.claim_storage.cutover import activate
from scripts import claim_storage_backup as backup_script
from scripts import claim_archive_restore_check as checker
from scripts.claim_storage_activate import verify_backup


def test_normalized_backup_restores_dictionary_claims_constraints_and_mode(search_database,tmp_path,monkeypatch):
    async def setup(sessions):
        await initialize(sessions)
        async with sessions.begin() as db:
            await copy_batch(db,50);await copy_batch(db,50)
        while True:
            async with sessions.begin() as db:result=await verify_batch(db)
            if result['phase']=='ready':break
        async with sessions.begin() as db:await activate(db)
    run(search_database,setup)
    monkeypatch.setattr(backup_script,'DATABASE_URL',search_database)
    # The artifact is small; isolate transport/filesystem policy from real
    # pg_dump/restore verification. Production requires a different mounted FS.
    monkeypatch.setattr(backup_script,'_external_destination',lambda target,mount:None)
    original_dump=backup_script._write_backup
    def fenced_dump(target,env):
        with psycopg2.connect(host=env['PGHOST'],port=env['PGPORT'],user=env['PGUSER'],
                              password=env['PGPASSWORD'],dbname=env['PGDATABASE']) as db:
            with db.cursor() as cursor:
                cursor.execute('SELECT pg_try_advisory_xact_lock(%s)',(backup_script.WRITER_LOCK_KEY,))
                assert cursor.fetchone()[0] is False
                cursor.execute('SELECT pg_try_advisory_xact_lock_shared(%s)',(backup_script.WRITER_LOCK_KEY,))
                assert cursor.fetchone()[0] is True
        return original_dump(target,env)
    monkeypatch.setattr(backup_script,'_write_backup',fenced_dump)
    target=tmp_path/'normalized.dump'
    result=backup_script.backup(target,tmp_path)
    assert result['bytes']>0 and not result['database_restore_verified']
    assert target.stat().st_mode & 0o777==0o600
    usage=namedtuple('usage','total used free')
    monkeypatch.setattr(checker.shutil,'disk_usage',lambda root:usage(1024**4,0,1024**4))
    # Keep the private Unix socket pathname short; a TCP port is never opened.
    with tempfile.TemporaryDirectory(prefix='claim-check-test-') as work:
        original=checker.fill_parents
        async def interrupted(*args):raise RuntimeError('intentional interruption after data copy')
        monkeypatch.setattr(checker,'fill_parents',interrupted)
        with pytest.raises(RuntimeError,match='intentional'):checker.check(target,Path(work),scratch_root=Path(work))
        job=next(Path(work).glob('restore-check-*'))
        assert json.loads((job/'verification-state.json').read_text())['data_complete']
        monkeypatch.setattr(checker,'fill_parents',original)
        proof=checker.check(target,Path(work),resume=job,scratch_root=Path(work))
        assert not job.exists()
    assert proof['claims']==18 and proof['actor_rows']==2
    assert proof['validated_foreign_keys']==3 and proof['source_phase']=='active'
    assert proof['storage_kind']=='normalized' and not proof['legacy_archive_present']
    target.with_suffix('.restore-check.json').write_text(json.dumps(proof))
    assert verify_backup(target,require_restore=True,expected='normalized')['schema']=='claim_storage'
    with pytest.raises(ValueError,match='verification'):verify_backup(target,expected='legacy')
    async def inactive(sessions):
        from sqlalchemy import text
        async with sessions.begin() as db:
            await db.execute(text("UPDATE claim_storage.migration SET phase='ready'"))
    run(search_database,inactive)
    bad=tmp_path/'inactive.dump'
    original_dump(bad,backup_script.pg_environment(search_database))
    with tempfile.TemporaryDirectory(prefix='claim-inactive-test-') as work:
        with pytest.raises(ValueError,match='not authoritative'):
            checker.check(bad,Path(work))
    proof['source_phase']='copying'
    target.with_suffix('.restore-check.json').write_text(json.dumps(proof))
    with pytest.raises(ValueError,match='canonical'):verify_backup(target,require_restore=True,expected='normalized')
    with target.open('ab') as stream:stream.write(b'changed')
    with pytest.raises(ValueError,match='verification'):verify_backup(target,expected='normalized')


def test_unmounted_backup_target_refuses_before_database_access(tmp_path):
    with pytest.raises(ValueError,match='mount'):
        backup_script.backup(tmp_path/'new.dump',tmp_path/'not-mounted')
    assert not (tmp_path/'new.dump').exists()
