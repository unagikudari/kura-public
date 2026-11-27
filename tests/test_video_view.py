"""Synthetic videos only: immutable originals, compatible output and ranged serving."""
import hashlib
import importlib
import subprocess
from types import SimpleNamespace

import pytest
from starlette.requests import Request
from api.services import renditions, video_view
import psycopg2
from test_collector_next import postgres, run  # noqa: F401
from test_pool_search_candidates import search_database  # noqa: F401



@pytest.fixture
def db(search_database, monkeypatch, tmp_path):
    conn = psycopg2.connect(search_database)
    migration = importlib.import_module('api.alembic.versions.0177_rendition_store')
    with conn, conn.cursor() as c:
        c.execute('CREATE TABLE post(id int PRIMARY KEY, media_location text, data bytea, thumbnail bytea)')
        monkeypatch.setattr(migration, 'op', SimpleNamespace(execute=c.execute))
        migration.upgrade()
    monkeypatch.setattr(renditions, 'ROOT', tmp_path / 'renditions')
    yield SimpleNamespace(conn=conn, dsn=search_database, root=tmp_path / 'renditions')
    conn.close()


def rows(conn, sql, params=None):
    with conn.cursor() as c:
        c.execute(sql, params)
        result = c.fetchall()
    conn.rollback()
    return result


def insert(db, *posts):
    with db.conn, db.conn.cursor() as c:
        for post in posts:
            c.execute("INSERT INTO post VALUES (%s,'hdd',%s,%s)", post)


def source(tmp_path):
    path = tmp_path / 'source.webm'
    subprocess.run(['ffmpeg', '-nostdin', '-v', 'error', '-f', 'lavfi', '-i',
                    'testsrc=size=65x49:rate=10', '-t', '0.5', '-c:v', 'libvpx-vp9',
                    '-threads', '1', '-pix_fmt', 'yuv444p', '-color_range', 'pc', str(path)], check=True)
    return path


def test_encode_preserves_original_and_publishes_compatible_faststart(tmp_path):
    src = source(tmp_path); original = src.read_bytes()
    output = tmp_path / 'view.mp4'
    video_view.encode(src, output)
    assert src.read_bytes() == original
    assert video_view.probe_mp4(output) == (66, 50)
    data = output.read_bytes()
    assert data.index(b'moov') < data.index(b'mdat')
    stored = renditions.store_video(output, tmp_path / 'store')
    assert stored.mime == 'video/mp4' and stored.sha256 == hashlib.sha256(data).digest()
    assert stored.path.read_bytes() == data
    assert renditions.store_video(output, tmp_path / 'store') == stored
    with pytest.raises(ValueError): video_view.probe_mp4(src)


def migrate(db, monkeypatch):
    migration = importlib.import_module('api.alembic.versions.0188_video_view_renditions')
    with db.conn, db.conn.cursor() as c:
        c.execute('ALTER TABLE post ADD COLUMN sha256 bytea, ADD COLUMN "mime-type" text')
        monkeypatch.setattr(migration, 'op', SimpleNamespace(execute=c.execute))
        migration.upgrade()
        repair = importlib.import_module('api.alembic.versions.0189_video_only_view_invalidation')
        monkeypatch.setattr(repair, 'op', SimpleNamespace(execute=c.execute))
        repair.upgrade()


@pytest.mark.parametrize('change', ['sha256', 'mime'])
def test_mp4_attachment_range_and_identity_invalidation(db, monkeypatch, tmp_path, change):
    migrate(db, monkeypatch)
    insert(db, (1, None, None))
    src = source(tmp_path); out = tmp_path / 'view.mp4';video_view.encode(src, out)
    stored = renditions.store_video(out)
    obj, ref = renditions.attach_params(1, 'view', stored, video_view.RECIPE)
    with db.conn, db.conn.cursor() as c:
        c.execute('UPDATE post SET sha256=%s,"mime-type"=%s WHERE id=1', (b'o'*32, 'video/webm'))
        c.execute(renditions.psycopg(renditions.UPSERT_OBJECT), obj)
        c.execute(renditions.psycopg(renditions.ATTACH), ref)
    assert rows(db.conn, "SELECT mime FROM rendition_object") == [('video/mp4',)]
    with db.conn, db.conn.cursor() as c:
        c.execute('UPDATE post SET sha256=sha256 WHERE id=1')
    assert rows(db.conn, 'SELECT count(*) FROM post_rendition') == [(1,)]
    # Identity replacement must not keep a stale playback copy (data stays NULL).
    with db.conn, db.conn.cursor() as c:
        if change == 'sha256':
            c.execute('UPDATE post SET sha256=%s WHERE id=1', (b'x'*32,))
        else:
            c.execute('UPDATE post SET "mime-type"=%s WHERE id=1', ('video/mp4',))
    assert rows(db.conn, 'SELECT count(*) FROM post_rendition') == [(0,)]
    assert stored.path.exists()  # immutable file retained for outstanding readers/GC


def test_served_mp4_supports_byte_ranges(tmp_path, monkeypatch):
    monkeypatch.setattr(renditions, "ROOT", tmp_path / "store")
    from starlette.applications import Starlette
    from starlette.routing import Route
    from starlette.testclient import TestClient
    src = source(tmp_path); out = tmp_path / 'view.mp4'; video_view.encode(src, out)
    stored = renditions.store_video(out)
    class DB:
        async def execute(self, *_):
            return SimpleNamespace(first=lambda: (stored.sha256, stored.mime))
    async def endpoint(request):
        return await renditions.response_for(DB(), request, 1, 'view')
    with TestClient(Starlette(routes=[Route('/view', endpoint)])) as client:
        result = client.get('/view', headers={'Range': 'bytes=0-1'})
        assert result.status_code == 206 and result.content == out.read_bytes()[:2]
        assert result.headers['content-type'] == 'video/mp4'
        assert result.headers['content-range'] == f'bytes 0-1/{stored.byte_size}'
        assert client.get('/view', headers={'If-None-Match': result.headers['etag']}).status_code == 304


def setup_backfill(db, monkeypatch, tmp_path):
    import scripts.video_view_backfill as job
    migrate(db, monkeypatch)
    insert(db, (1, None, None))
    src = source(tmp_path)
    with db.conn, db.conn.cursor() as c:
        c.execute('ALTER TABLE post ADD COLUMN version integer NOT NULL DEFAULT 1')
        c.execute('CREATE TABLE pool_post(pool_id integer,post_id integer,ord integer)')
        c.execute('INSERT INTO pool_post VALUES(5432,1,0)')
        c.execute('UPDATE post SET sha256=%s,"mime-type"=%s WHERE id=1',
                  (hashlib.sha256(src.read_bytes()).digest(), 'video/webm'))
    monkeypatch.setattr(job.files, 'content_path', lambda *_:src)
    return job, src


def test_backfill_dry_run_apply_and_repeat_preserve_identity(db, monkeypatch, tmp_path):
    job, src = setup_backfill(db, monkeypatch, tmp_path)
    original = src.read_bytes()
    before = rows(db.conn, 'SELECT sha256,"mime-type" FROM post')
    assert list(job.run(db.conn, 5432))[0]['action'] == 'would_attach'
    assert rows(db.conn, 'SELECT count(*) FROM post_rendition') == [(0,)]
    assert list(job.run(db.conn, 5432, True))[0]['action'] == 'attached'
    assert list(job.run(db.conn, 5432, True)) == []
    import scripts.rendition_backfill as image_job
    assert image_job.restore_columns(db.conn, db.root, 10, lambda _: None) == {'view': 0, 'thumb': 0}
    assert rows(db.conn, 'SELECT sha256,"mime-type" FROM post') == before
    assert rows(db.conn, 'SELECT version FROM post') == [(2,)]
    assert src.read_bytes() == original


def test_backfill_refuses_identity_change_during_encode(db, monkeypatch, tmp_path):
    job, src = setup_backfill(db, monkeypatch, tmp_path)
    original_encode = video_view.encode
    def changed(source, output):
        original_encode(source, output)
        with db.conn, db.conn.cursor() as c:
            c.execute('UPDATE post SET sha256=%s WHERE id=1', (b'z'*32,))
    monkeypatch.setattr(video_view, 'encode', changed)
    with pytest.raises(ValueError, match='identity changed'):
        list(job.run(db.conn, 5432, True))
    assert rows(db.conn, 'SELECT count(*) FROM post_rendition') == [(0,)]


def test_cli_apply_uses_one_transaction_per_attachment(db, monkeypatch, tmp_path, capsys):
    import sys
    job, src = setup_backfill(db, monkeypatch, tmp_path)
    monkeypatch.setenv('DATABASE_URL', db.dsn)
    monkeypatch.setattr(sys, 'argv', ['video_view_backfill.py', '--pool', '5432', '--apply'])
    job.main()
    assert '"action": "attached"' in capsys.readouterr().out
    assert rows(db.conn, 'SELECT count(*) FROM post_rendition') == [(1,)]


def test_duration_uses_stream_fallback_and_rejects_unknown():
    assert video_view.duration({'format': {'duration': 'N/A'}, 'streams': [{'duration': '1.25'}]}) == 1.25
    for value in (None, 'N/A', 'nan', 'inf', '-1', '0'):
        with pytest.raises(video_view.DurationUnavailable):
            video_view.duration({'format': {'duration': value}, 'streams': []})


def test_missing_duration_skips_one_item_and_continues(db, monkeypatch, tmp_path):
    job, src = setup_backfill(db, monkeypatch, tmp_path)
    with db.conn, db.conn.cursor() as c:
        c.execute('INSERT INTO post SELECT 2,media_location,data,thumbnail,sha256,"mime-type",version FROM post WHERE id=1')
        c.execute('INSERT INTO pool_post VALUES(5432,2,1)')
    original = video_view.encode
    calls = []
    def encode(source, output):
        calls.append(source)
        if len(calls) == 1: raise video_view.DurationUnavailable('unknown')
        original(source, output)
    monkeypatch.setattr(video_view, 'encode', encode)
    results = list(job.run(db.conn, 5432, True))
    assert results[0] == {'post_id': 1, 'action': 'skipped', 'reason': 'duration_unavailable'}
    assert results[1]['post_id'] == 2 and results[1]['action'] == 'attached'
    assert rows(db.conn, 'SELECT post_id FROM post_rendition') == [(2,)]


def test_concurrent_view_yields_after_transaction_releases_lock(db, monkeypatch, tmp_path):
    job, src = setup_backfill(db, monkeypatch, tmp_path)
    original = video_view.encode
    def encode(source, output):
        original(source, output)
        stored = renditions.store_video(output)
        obj, ref = renditions.attach_params(1, 'view', stored, video_view.RECIPE)
        with db.conn, db.conn.cursor() as c:
            c.execute(renditions.psycopg(renditions.UPSERT_OBJECT), obj)
            c.execute(renditions.psycopg(renditions.ATTACH), ref)
    monkeypatch.setattr(video_view, 'encode', encode)
    results = job.run(db.conn, 5432, True)
    assert next(results)['action'] == 'concurrent_view_preserved'
    assert db.conn.get_transaction_status() == psycopg2.extensions.TRANSACTION_STATUS_IDLE
    results.close()


def test_video_to_image_does_not_delete_new_image_view(db, monkeypatch, tmp_path):
    from io import BytesIO
    from PIL import Image
    job, src = setup_backfill(db, monkeypatch, tmp_path)
    image = BytesIO(); Image.new('RGB', (8, 8), 'green').save(image, 'WEBP')
    stored = renditions.store(image.getvalue())
    obj, ref = renditions.attach_params(1, 'view', stored, 'webp-cap1600-q82')
    with db.conn, db.conn.cursor() as c:
        c.execute(renditions.psycopg(renditions.UPSERT_OBJECT), obj)
        c.execute(renditions.psycopg(renditions.ATTACH), ref)
        c.execute('UPDATE post SET "mime-type"=%s,sha256=%s WHERE id=1', ('image/webp', b'z'*32))
    assert bytes(rows(db.conn, 'SELECT sha256 FROM post_rendition')[0][0]) == stored.sha256
