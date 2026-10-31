"""Real PostgreSQL cache replacement; no production database or SSH is used."""
import uuid

import psycopg2
from psycopg2 import extensions, sql
import pytest

from tagger.heads import fetch_excluded as sync
from test_upload_original_identity import database_url  # disposable cluster


@pytest.fixture
def stores(database_url):
    base = database_url.replace('+asyncpg', '')
    schema = 'exclusion_' + uuid.uuid4().hex
    source, target = schema + '_source', schema + '_target'
    conn = psycopg2.connect(base)
    with conn:
        with conn.cursor() as c:
            for name in (source, target):
                c.execute(sql.SQL('CREATE SCHEMA {}').format(sql.Identifier(name)))
            c.execute(sql.SQL('SET search_path TO {}').format(sql.Identifier(source)))
            c.execute('CREATE TABLE pool(id int PRIMARY KEY, verify_status text)')
            c.execute('CREATE TABLE pool_post(pool_id int, post_id int)')
            c.execute('CREATE TABLE tag_name(tag_id int, name text)')
            c.execute('CREATE TABLE post_tag(tag_id int, post_id int)')
            c.execute("INSERT INTO pool VALUES (1,'excluded'),(2,'verified')")
            c.execute('INSERT INTO pool_post VALUES (1,2),(1,3),(2,9)')
            c.execute("INSERT INTO tag_name VALUES (1,'tagger-exclude'),(2,'other')")
            c.execute('INSERT INTO post_tag VALUES (1,3),(1,4),(2,9)')
            c.execute(sql.SQL('SET search_path TO {}').format(sql.Identifier(target)))
            c.execute('CREATE TABLE tagger_gt_exclude(post_id integer PRIMARY KEY)')
            c.execute('INSERT INTO tagger_gt_exclude VALUES (2),(8)')
    conn.close()
    dsns = tuple(extensions.make_dsn(base, options=f'-csearch_path={name}') for name in (source, target))
    yield dsns
    conn = psycopg2.connect(base)
    with conn:
        with conn.cursor() as c:
            for name in (source, target):
                c.execute(sql.SQL('DROP SCHEMA {} CASCADE').format(sql.Identifier(name)))
    conn.close()


def execute(dsn, statement):
    with psycopg2.connect(dsn) as conn:
        with conn.cursor() as c:
            c.execute(statement)
            result = c.fetchall() if c.description else None
    conn.close()
    return result


def ids(dsn):
    return execute(dsn, 'SELECT post_id FROM tagger_gt_exclude ORDER BY post_id')


def test_unions_deduplicates_and_preserves_unchanged_rows(stores):
    source, target = stores
    before = execute(target, 'SELECT xmin::text FROM tagger_gt_exclude WHERE post_id=2')
    assert sync.refresh(source, target) == 3
    assert ids(target) == [(2,), (3,), (4,)]
    assert execute(target, 'SELECT xmin::text FROM tagger_gt_exclude WHERE post_id=2') == before
    first = execute(target, 'SELECT xmin::text FROM tagger_gt_exclude ORDER BY post_id')
    assert sync.refresh(source, target) == 3
    assert execute(target, 'SELECT xmin::text FROM tagger_gt_exclude ORDER BY post_id') == first


def test_successfully_read_empty_snapshot_is_valid(stores):
    source, target = stores
    execute(source, 'DELETE FROM pool_post; DELETE FROM post_tag')
    assert sync.refresh(source, target) == 0
    assert ids(target) == []


def test_large_snapshot_is_complete_across_batches(stores):
    source, target = stores
    execute(source, 'INSERT INTO pool_post SELECT 1, generate_series(10,6010)')
    assert sync.refresh(source, target) == 6004
    assert len(ids(target)) == 6004


def test_source_failure_does_not_clear_cache(stores):
    source, target = stores
    execute(source, 'DROP TABLE pool_post')
    with pytest.raises(psycopg2.errors.UndefinedTable):
        sync.refresh(source, target)
    assert ids(target) == [(2,), (8,)]


def test_failed_insert_rolls_back_prior_deletions(stores):
    source, target = stores
    execute(target, """CREATE FUNCTION refuse_insert() RETURNS trigger LANGUAGE plpgsql AS $$
      BEGIN RAISE EXCEPTION 'simulated destination failure'; END $$;
      CREATE TRIGGER fail_insert BEFORE INSERT ON tagger_gt_exclude
      FOR EACH ROW EXECUTE FUNCTION refuse_insert();""")
    with pytest.raises(psycopg2.errors.RaiseException):
        sync.refresh(source, target)
    assert ids(target) == [(2,), (8,)]


def test_invalid_source_ids_preserve_cache(stores):
    source, target = stores
    execute(source, 'INSERT INTO pool_post VALUES(1,-5)')
    with pytest.raises(ValueError, match='invalid post identifier'):
        sync.refresh(source, target)
    assert ids(target) == [(2,), (8,)]


def test_source_transaction_is_read_only(stores):
    class CheckedConnection(extensions.connection):
        def cursor(self, *args, **kwargs):
            if kwargs.get('name'):
                with super().cursor() as check:
                    check.execute('SHOW transaction_read_only')
                    assert check.fetchone()[0] == 'on'
            return super().cursor(*args, **kwargs)
    def connect(dsn, **kwargs):
        return psycopg2.connect(dsn, connection_factory=CheckedConnection, **kwargs)
    assert sync.refresh(*stores, connect=connect) == 3


def test_missing_configuration_never_connects(monkeypatch, capsys):
    monkeypatch.delenv('KURA_EXCLUDE_SOURCE_DSN', raising=False)
    monkeypatch.delenv('KURA_EXCLUDE_TARGET_DSN', raising=False)
    monkeypatch.setattr(sync, 'refresh', lambda *args: pytest.fail('must not connect'))
    assert sync.main() == 2
    assert 'KURA_EXCLUDE_SOURCE_DSN' in capsys.readouterr().err


def test_failure_log_does_not_expose_connection_credentials(monkeypatch, capsys):
    monkeypatch.setenv('KURA_EXCLUDE_SOURCE_DSN', 'source-secret')
    monkeypatch.setenv('KURA_EXCLUDE_TARGET_DSN', 'target-secret')
    def fail(*args):
        raise RuntimeError('password=do-not-log host=private-server')
    monkeypatch.setattr(sync, 'refresh', fail)
    assert sync.main() == 1
    error = capsys.readouterr().err
    assert 'RuntimeError' in error
    for secret in ('source-secret','target-secret','do-not-log','private-server'):
        assert secret not in error


def test_cycle_stops_before_training_when_exclusion_refresh_fails(tmp_path):
    from pathlib import Path
    import os
    import shutil
    import subprocess
    scripts = tmp_path / 'scripts'
    data = tmp_path / 'data'
    scripts.mkdir(); data.mkdir()
    shutil.copy2(Path(sync.__file__).with_name('update_cycle.sh'), scripts / 'update_cycle.sh')
    (scripts / 'fetch_excluded.py').write_text('raise SystemExit(1)\n')
    (scripts / 'cycle_signal.py').write_text('from pathlib import Path\nPath("failure-signal").write_text("recorded")\n')
    for name in ('train_heads.py','auto_apply.py','retire_pools.py','record_generation.py'):
        (scripts / name).write_text('from pathlib import Path\nPath("must-not-run").touch()\n')
    result = subprocess.run(['bash', str(scripts / 'update_cycle.sh')], env={
        'PATH':os.environ['PATH'], 'HOME':str(tmp_path), 'TAGGER_HEADS_DATA':str(data),
        'KTOK':'test-only', 'PY':'python3'
    }, capture_output=True, text=True)
    assert result.returncode == 1
    assert (data / 'failure-signal').exists()
    assert not (data / 'must-not-run').exists()
    assert 'exclusions FAILED' in (data / 'cycle.log').read_text()
