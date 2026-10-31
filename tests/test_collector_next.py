"""Real PostgreSQL in a private temporary cluster: no production DB/HTTP calls."""
import asyncio
from io import BytesIO
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from uuid import UUID, uuid4

from PIL import Image
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

from api.collector import service
from api.collector import label_service
from api.collector.labels import fold_observations, normalize_label
from api.collector.storage import MediaInputError, content_path, store_media
from scripts.collector_next_backup import backup, pg_environment
import schema_history

REPO = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="session")
def postgres(tmp_path_factory):
    binary = Path("/usr/lib/postgresql/18/bin")
    if not (binary / "initdb").exists():
        pytest.skip("PostgreSQL 18 test binaries required")
    root = tmp_path_factory.mktemp("collector-pg")
    data, socket = root / "data", root / "socket"
    socket.mkdir()
    subprocess.run([str(binary / "initdb"), "-D", str(data), "-A", "trust", "-U", "collector_test", "--no-locale"],
                   check=True, capture_output=True)
    subprocess.run([str(binary / "pg_ctl"), "-D", str(data), "-l", str(root / "log"),
                    "-o", f"-k {socket} -p 65439 -c listen_addresses='' -c shared_buffers=16MB -c max_connections=20", "-w", "start"],
                   check=True, capture_output=True)
    try:
        yield f"postgresql://collector_test@/postgres?host={socket}&port=65439"
    finally:
        subprocess.run([str(binary / "pg_ctl"), "-D", str(data), "-m", "immediate", "-w", "stop"],
                       check=True, capture_output=True)


@pytest.fixture
def database(postgres):
    subprocess.run(["psql", "-X", "-v", "ON_ERROR_STOP=1", "-c", "DROP SCHEMA IF EXISTS collector_next CASCADE"],
                   env=pg_environment(postgres), check=True, capture_output=True)
    subprocess.run(["psql", "-X", "-v", "ON_ERROR_STOP=1", "-f", str(REPO / "api/collector/schema.sql")],
                   env=pg_environment(postgres), check=True, capture_output=True)
    subprocess.run(["psql", "-X", "-v", "ON_ERROR_STOP=1", "-f", str(REPO / "api/collector/migrations/0002_import_paths.sql")],
                   env=pg_environment(postgres), check=True, capture_output=True)
    subprocess.run(["psql", "-X", "-v", "ON_ERROR_STOP=1", "-f", str(REPO / "api/collector/migrations/0003_labels.sql")],
                   env=pg_environment(postgres), check=True, capture_output=True)
    subprocess.run(["psql", "-X", "-v", "ON_ERROR_STOP=1", "-f", str(REPO / "api/collector/migrations/0004_import_operations.sql")],
                   env=pg_environment(postgres), check=True, capture_output=True)
    subprocess.run(["psql", "-X", "-v", "ON_ERROR_STOP=1", "-f", str(REPO / "api/collector/migrations/0005_tag_summary.sql")],
                   env=pg_environment(postgres), check=True, capture_output=True)
    subprocess.run(["psql", "-X", "-v", "ON_ERROR_STOP=1", "-f", str(REPO / "api/collector/migrations/0006_label_producer_origin.sql")],
                   env=pg_environment(postgres), check=True, capture_output=True)
    return postgres


def image(root, color="red"):
    data = BytesIO()
    Image.new("RGB", (80, 60), color).save(data, "PNG")
    payload = data.getvalue()
    return store_media(BytesIO(payload), root), payload


@pytest.fixture(scope="session")
def webm_payload():
    return subprocess.run([
        "ffmpeg", "-nostdin", "-v", "error", "-f", "lavfi", "-i", "color=c=blue:s=96x64:r=5",
        "-t", "0.4", "-an", "-c:v", "libvpx-vp9", "-threads", "1", "-f", "webm", "pipe:1",
    ], check=True, capture_output=True).stdout


def test_webm_original_and_preview(tmp_path, webm_payload):
    stored = store_media(BytesIO(webm_payload), tmp_path)
    assert stored.media_type == "video/webm" and (stored.width, stored.height) == (96, 64)
    assert content_path(tmp_path, stored.digest).read_bytes() == webm_payload
    assert stored.digest == hashlib.sha256(webm_payload).digest()
    with Image.open(content_path(tmp_path, stored.digest, True)) as preview:
        assert preview.format == "WEBP" and max(preview.size) <= 400
    with pytest.raises(MediaInputError) as rejected:
        store_media(BytesIO(b"\x1a\x45\xdf\xa3broken"), tmp_path)
    assert rejected.value.code == "invalid_video" and str(tmp_path) not in str(rejected.value)


def run(database, scenario):
    async def execute():
        engine = create_async_engine(database.replace("postgresql:", "postgresql+asyncpg:"))
        try:
            await scenario(async_sessionmaker(engine, expire_on_commit=False))
        finally:
            await engine.dispose()
    asyncio.run(execute())


def test_replay_and_repeated_collection_positions(database, tmp_path):
    stored, payload = image(tmp_path)
    assert hashlib.sha256(payload).digest() == stored.digest
    assert content_path(tmp_path, stored.digest).read_bytes() == payload
    async def scenario(sessions):
        async with sessions.begin() as db:
            collection_key = uuid4()
            c = await service.create_collection(db, 1, "test", collection_key)
            assert c == await service.create_collection(db, 1, "test", collection_key)
        key = uuid4()
        async with sessions.begin() as db:
            first = await service.register(db, 1, key, stored, "one.png", c["id"])
        async with sessions.begin() as db:
            replay = await service.register(db, 1, key, stored, "one.png", c["id"])
            second = await service.register(db, 1, uuid4(), stored, "one.png", c["id"])
            assert replay["replayed"] and replay["entry_id"] == first["entry_id"]
            assert second["asset_id"] == first["asset_id"] and second["entry_id"] != first["entry_id"]
            assert second["reused_original"]
            assert [e["position"] for e in (await service.entries(db, 1, c["id"]))["items"]] == [1, 2]
            assert (await db.execute(text("SELECT count(*) FROM collector_next.blob"))).scalar() == 1
    run(database, scenario)


def test_conflicts_and_owner_boundaries(database, tmp_path):
    stored, _ = image(tmp_path)
    async def scenario(sessions):
        async with sessions.begin() as db:
            c = await service.create_collection(db, 1, "private")
            key = uuid4()
            first = await service.register(db, 1, key, stored, "a.png", c["id"])
        with pytest.raises(service.Conflict):
            async with sessions.begin() as db:
                await service.register(db, 1, key, stored, "different.png", c["id"])
        with pytest.raises(service.NotFound):
            async with sessions.begin() as db:
                await service.register(db, 2, uuid4(), stored, "a.png", c["id"])
        with pytest.raises(service.NotFound):
            async with sessions.begin() as db:
                await service.original(db, 2, first["asset_id"])
        async with sessions.begin() as db:
            assert not (await service.list_media(db, 2))["items"]
    run(database, scenario)


def test_concurrent_requests_append_without_losing_positions(database, tmp_path):
    stored, _ = image(tmp_path)
    async def scenario(sessions):
        async with sessions.begin() as db:
            c = await service.create_collection(db, 1, "parallel")
        async def submit(key):
            async with sessions.begin() as db:
                return await service.register(db, 1, key, stored, "a.png", c["id"])
        key = uuid4()
        results = await asyncio.gather(submit(key), submit(key), *[submit(uuid4()) for _ in range(5)])
        assert sum(r["replayed"] for r in results) == 1
        async with sessions.begin() as db:
            assert [r["position"] for r in (await service.entries(db, 1, c["id"]))["items"]] == list(range(1, 7))
            assert len((await service.list_media(db, 1))["items"]) == 1
    run(database, scenario)


def test_cursor_order_and_commit_failure_resume(database, tmp_path):
    one, _ = image(tmp_path, "red")
    two, _ = image(tmp_path, "blue")
    async def scenario(sessions):
        key = uuid4()
        with pytest.raises(RuntimeError):
            async with sessions.begin() as db:
                await service.register(db, 1, key, one, "a.png", None)
                raise RuntimeError("simulate stop before commit")
        async with sessions.begin() as db:
            a = await service.register(db, 1, key, one, "a.png", None)
            b = await service.register(db, 1, uuid4(), two, "b.png", None)
        async with sessions.begin() as db:
            page = await service.list_media(db, 1, limit=1)
            assert page["items"][0]["id"] == b["asset_id"]
            next_page = await service.list_media(db, 1, after=page["next_cursor"], limit=1)
            assert next_page["items"][0]["id"] == a["asset_id"] and next_page["next_cursor"] is None
    run(database, scenario)


def test_bad_images_and_corrupt_original_never_get_accepted(tmp_path):
    with pytest.raises(MediaInputError) as failure:
        store_media(BytesIO(b"not an image"), tmp_path)
    assert failure.value.code == "unrecognized_image"
    assert str(tmp_path) not in str(failure.value)
    stored, payload = image(tmp_path)
    content_path(tmp_path, stored.digest).write_bytes(b"corruption")
    with pytest.raises(ValueError, match="検証"):
        store_media(BytesIO(payload), tmp_path)
    assert not list((tmp_path / "staging").iterdir())


def test_snapshot_restores_metadata_and_originals(database, tmp_path):
    root, dest = tmp_path / "source", tmp_path / "backup"
    stored, payload = image(root)
    async def scenario(sessions):
        async with sessions.begin() as db:
            c = await service.create_collection(db, 1, "restore")
            first = await service.register(db, 1, uuid4(), stored, "a.png", c["id"], "Book/chapter1/a.png")
            await service.register(db, 1, uuid4(), stored, "a.png", c["id"], "Book/chapter2/a.png")
            await label_service.apply(db, 1, uuid4(), [(first['asset_id'], 0)], ['restored'], [])
    run(database, scenario)
    snapshot = backup(database, root, dest, Path("/"))
    manifest = json.loads((snapshot / "manifest.json").read_text())
    assert len(manifest["originals"]) == 1
    assert (snapshot / manifest["originals"][0]["path"]).read_bytes() == payload
    from scripts.collector_next_verify import verify
    assert verify(snapshot)['current_labels'] == 1
    # Restore the dump into the private DB after removing source metadata.
    subprocess.run(["psql", "-X", "-v", "ON_ERROR_STOP=1", "-c", "DROP SCHEMA collector_next CASCADE"],
                   env=pg_environment(database), check=True, capture_output=True)
    subprocess.run(["pg_restore", "--exit-on-error", "--no-owner", "--no-acl", "-d", "postgres", str(snapshot / "database.dump")],
                   env=pg_environment(database), check=True, capture_output=True)
    async def restored(sessions):
        async with sessions.begin() as db:
            c = (await service.collections(db, 1))[0]
            assert [e["relative_path"] for e in (await service.entries(db, 1, c["id"]))["items"]] == ["Book/chapter1/a.png", "Book/chapter2/a.png"]
            assert all(e['tags'] == ['restored'] for e in (await service.entries(db, 1, c["id"]))['items'])
            assert (await db.execute(text("SELECT count(*) FROM collector_next.receipt"))).scalar() == 2
    run(database, restored)
    # Fail closed when the configured removable mount is missing.
    with pytest.raises(RuntimeError, match="マウント"):
        backup(database, root, dest, tmp_path / "absent-mount")


def test_http_adapter_feature_gate_upload_and_safe_mode(database, tmp_path, monkeypatch, webm_payload):
    from types import SimpleNamespace
    from fastapi import FastAPI
    import httpx
    from api.collector import router as adapter
    from api.auth import get_current_user
    from api.db.session import get_db
    monkeypatch.setenv("KURA_COLLECTOR_NEXT", "1")
    monkeypatch.setenv("KURA_COLLECTOR_NEXT_ROOT", str(tmp_path / "files"))
    monkeypatch.setenv("KURA_DB_ROLE", "canonical")
    _, payload = image(tmp_path / "fixture")
    async def scenario(sessions):
        monkeypatch.setattr(adapter, "async_session", sessions)
        app = FastAPI()
        app.include_router(adapter.router)
        async def db_dependency():
            async with sessions() as db:
                yield db
        app.dependency_overrides[get_db] = db_dependency
        app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(user_id=1)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            rejected = await client.post("/api/collector-next/upload", files={"file": ("bad.jpg", b"not an image")},
                                        data={"request_key": str(uuid4())})
            assert rejected.status_code == 422
            assert rejected.json()["detail"]["retryable"] is False
            assert rejected.json()["detail"]["code"] == "unrecognized_image"
            assert "staging" not in rejected.text and str(tmp_path) not in rejected.text
            invalid_path = await client.post("/api/collector-next/upload", files={"file": ("a.png", payload)},
                                            data={"request_key": str(uuid4()), "relative_path": "../a.png"})
            assert invalid_path.status_code == 422 and invalid_path.json()["detail"]["code"] == "invalid_relative_path"
            response = await client.post("/api/collector-next/upload", files={"file": ("a.png", payload)},
                                         data={"request_key": str(uuid4())})
            assert response.status_code == 200, response.text
            asset = response.json()["asset_id"]
            assert (await client.get(f"/api/collector-next/media/{asset}/original")).content == payload
            assert len((await client.get("/api/collector-next/media")).json()["items"]) == 1
            video = await client.post("/api/collector-next/upload", files={"file": ("movie.webm", webm_payload, "video/webm")},
                                      data={"request_key": str(uuid4())})
            assert video.status_code == 200, video.text
            video_id = video.json()["asset_id"]
            playback = await client.get(f"/api/collector-next/media/{video_id}/playback", headers={"Range": "bytes=0-31"})
            assert playback.status_code == 206 and playback.content == webm_payload[:32]
            assert playback.headers["content-type"] == "video/webm" and "content-disposition" not in playback.headers
            assert (await client.get(f"/api/collector-next/media/{video_id}/original")).content == webm_payload
            label_key = str(uuid4())
            command = {'request_key': label_key, 'targets': [{'asset_id': asset, 'revision': 0}, {'asset_id': video_id, 'revision': 0}], 'add': ['http-test']}
            applied = await client.post('/api/collector-next/labels/change', json=command)
            assert applied.status_code == 200, applied.text
            assert (await client.get(f'/api/collector-next/label-operations/{label_key}')).json()['observations'] == 2
            assert len((await client.get(f'/api/collector-next/media/{asset}/label-history')).json()) == 1
            assert len((await client.get('/api/collector-next/media?tags=http-test&kind=video')).json()['items']) == 1
            stale = await client.post('/api/collector-next/labels/change', json={**command, 'request_key': str(uuid4())})
            assert stale.status_code == 409 and stale.json()['detail']['retryable'] is False
            client.cookies.set("kura_safe", "1")
            assert not (await client.get("/api/collector-next/media")).json()["items"]
            assert (await client.get(f"/api/collector-next/media/{asset}/thumbnail")).status_code == 404
            assert (await client.get(f"/api/collector-next/media/{video_id}/playback")).status_code == 404
            monkeypatch.setenv("KURA_DB_ROLE", "replica")
            assert (await client.get("/api/collector-next/status")).status_code == 503
    run(database, scenario)


def test_deep_cursor_reads_only_returned_rows(database):
    async def scenario(sessions):
        async with sessions.begin() as db:
            await db.execute(text("""
                INSERT INTO collector_next.blob(sha256,byte_size)
                SELECT sha256(i::text::bytea),1 FROM generate_series(1,100000) i
            """))
            await db.execute(text("INSERT INTO collector_next.asset(owner_id,blob_id) SELECT 1,id FROM collector_next.blob"))
            await db.execute(text("""
                INSERT INTO collector_next.media_current(asset_id,owner_id,original_name,media_type,width,height)
                SELECT id,1,'benchmark.png','image/png',1,1 FROM collector_next.asset
            """))
            await db.execute(text("ANALYZE collector_next.media_current"))
            async def explain(suffix):
                return (await db.execute(text("EXPLAIN (ANALYZE,BUFFERS,FORMAT JSON) "
                    "SELECT asset_id,original_name,media_type,width,height FROM collector_next.media_current "
                    "WHERE owner_id=1 " + suffix))).scalar_one()[0]
            shallow = await explain("ORDER BY asset_id DESC LIMIT 40")
            deep = await explain("AND asset_id<20000 ORDER BY asset_id DESC LIMIT 40")
            offset = await explain("ORDER BY asset_id DESC OFFSET 80000 LIMIT 40")
            def scan_rows(node):
                if "Scan" in node["Node Type"]:
                    return node["Actual Rows"] + node.get("Rows Removed by Filter", 0)
                return sum(scan_rows(child) for child in node.get("Plans", []))
            assert scan_rows(shallow["Plan"]) == 40
            assert scan_rows(deep["Plan"]) == 40
            assert scan_rows(offset["Plan"]) == 80040
            print("100k SQL comparison:", json.dumps({name: {"scan_rows": scan_rows(plan["Plan"]),
                  "execution_ms": plan["Execution Time"]} for name, plan in
                  [("shallow", shallow), ("deep_cursor", deep), ("offset_80000", offset)]}))
    run(database, scenario)


def test_alembic_installs_only_the_module_namespace(database):
    subprocess.run(["psql", "-X", "-v", "ON_ERROR_STOP=1", "-c", """
        DROP SCHEMA collector_next CASCADE;
        CREATE TABLE public.kura_alembic_version(version_num varchar(32) PRIMARY KEY);
        INSERT INTO public.kura_alembic_version VALUES('0167');
    """], env=pg_environment(database), check=True, capture_output=True)
    env = {**os.environ, "DATABASE_URL": database.replace("postgresql:", "postgresql+asyncpg:")}
    result = subprocess.run([sys.executable, "-m", "alembic", "upgrade", "0170"],
                            cwd=REPO, env=env, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    async def scenario(sessions):
        async with sessions.begin() as db:
            assert (await db.execute(text("SELECT version FROM collector_next.schema_version"))).scalar_one() == 3
            assert (await db.execute(text("SELECT version_num FROM public.kura_alembic_version"))).scalar_one() == '0170'
            assert (await db.execute(text("SELECT count(*) FROM pg_publication_tables WHERE schemaname='collector_next'"))).scalar_one() == 0
    run(database, scenario)


def test_folder_paths_are_part_of_receipt_identity(database, tmp_path):
    stored, _ = image(tmp_path)
    async def scenario(sessions):
        async with sessions.begin() as db:
            c = await service.create_collection(db, 1, "folder")
            key = uuid4()
            first = await service.register(db, 1, key, stored, "a.png", c["id"], "本/第1章/a.png")
            replay = await service.register(db, 1, key, stored, "a.png", c["id"], "本/第1章/a.png")
            second = await service.register(db, 1, uuid4(), stored, "a.png", c["id"], "本/第2章/a.png")
            assert replay["replayed"] and first["entry_id"] == replay["entry_id"]
            assert first["asset_id"] == second["asset_id"] and first["entry_id"] != second["entry_id"]
        with pytest.raises(service.Conflict):
            async with sessions.begin() as db:
                await service.register(db, 1, key, stored, "a.png", c["id"], "本/第2章/a.png")
        async with sessions.begin() as db:
            assert [e["relative_path"] for e in (await service.entries(db, 1, c["id"]))["items"]] == ["本/第1章/a.png", "本/第2章/a.png"]
    run(database, scenario)


@pytest.mark.parametrize("path", ["/a.png", "../a.png", "Book/../a.png", "Book//a.png", "Book/./a.png", "C:/a.png", "Book\\a.png", "Book/b.png", "Book/\x00/a.png"])
def test_folder_path_cannot_be_absolute_or_escape(path):
    with pytest.raises(service.InvalidPath):
        service.validate_relative_path(path, "a.png")


def test_labels_are_replayable_comparisons_not_destructive_tag_rows(database, tmp_path):
    stored, _ = image(tmp_path)
    async def scenario(sessions):
        async with sessions.begin() as db:
            asset = (await service.register(db, 1, uuid4(), stored, "a.png", None))["asset_id"]
        key = uuid4()
        async with sessions.begin() as db:
            first = await label_service.apply(db, 1, key, [(asset, 0)], ["旅行", "Favorite"], [])
            assert first["observations"] == 2 and first["resolver_version"] == 'collector-labels-v1'
        async with sessions.begin() as db:
            replay = await label_service.apply(db, 1, key, [(asset, 0)], ["favorite", "旅行"], [])
            assert replay["replayed"] and replay["targets"] == first["targets"]
            page = await service.list_media(db, 1, tags=["ＦＡＶＯＲＩＴＥ", "旅行"])
            assert page["items"][0]["tags"] == ["Favorite", "旅行"]
            assert page["items"][0]["tag_revision"] == 1
        async with sessions.begin() as db:
            await label_service.apply(db, 1, uuid4(), [(asset, 1)], ["旅行"], [])
        async with sessions.begin() as db:
            await label_service.apply(db, 1, uuid4(), [(asset, 2)], [], ["旅行"])
            assert not (await service.list_media(db, 1, tags=["旅行"]))["items"]
            assert len((await service.list_media(db, 1, tags=["favorite"]))["items"]) == 1
            assert (await db.execute(text("SELECT positive_count FROM collector_next.tag_usage u JOIN collector_next.tag t ON t.id=u.tag_id WHERE t.name='旅行'"))).scalar_one() == 0
            assert len(await label_service.history(db, 1, asset)) == 4
            events = [dict(r) for r in (await db.execute(text("SELECT * FROM collector_next.label_observation ORDER BY id DESC"))).mappings()]
            winners = fold_observations(events)
            actual = {(r["asset_id"], r["tag_id"]): (r["observation_id"], r["present"]) for r in
                      (await db.execute(text("SELECT * FROM collector_next.label_head"))).mappings()}
            assert actual == {k: (v["id"], v["present"]) for k, v in winners.items()}
            assert all(e["based_on_id"] is not None for e in events if e["id"] > 2)
        from sqlalchemy.exc import DBAPIError
        for sql in ["UPDATE collector_next.label_observation SET present=false", "DELETE FROM collector_next.label_operation", "TRUNCATE collector_next.label_observation CASCADE"]:
            with pytest.raises(DBAPIError, match="append-only"):
                async with sessions.begin() as db:
                    await db.execute(text(sql))
    run(database, scenario)


def test_label_conflicts_and_owner_checks_are_atomic(database, tmp_path):
    stored, _ = image(tmp_path)
    async def scenario(sessions):
        async with sessions.begin() as db:
            asset = (await service.register(db, 1, uuid4(), stored, "a.png", None))["asset_id"]
            key = uuid4()
            await label_service.apply(db, 1, key, [(asset, 0)], ["kept"], [])
        with pytest.raises(service.Conflict):
            async with sessions.begin() as db:
                await label_service.apply(db, 1, key, [(asset, 0)], ["different"], [])
        with pytest.raises(service.Conflict):
            async with sessions.begin() as db:
                await label_service.apply(db, 1, uuid4(), [(asset, 0)], ["stale"], [])
        with pytest.raises(service.NotFound):
            async with sessions.begin() as db:
                await label_service.apply(db, 2, uuid4(), [(asset, 1)], ["private"], [])
        with pytest.raises(service.NotFound):
            async with sessions.begin() as db:
                await label_service.operation(db, 2, key)
        async with sessions.begin() as db:
            assert not (await service.list_media(db, 2, tags=["kept"]))["items"]
            assert (await db.execute(text("SELECT count(*) FROM collector_next.label_operation"))).scalar_one() == 1
            assert (await db.execute(text("SELECT count(*) FROM collector_next.label_observation"))).scalar_one() == 1
    run(database, scenario)


def test_concurrent_label_commands_preserve_counts_and_reject_stale_edits(database, tmp_path):
    red, _ = image(tmp_path, "red")
    blue, _ = image(tmp_path, "blue")
    async def scenario(sessions):
        async with sessions.begin() as db:
            a = (await service.register(db, 1, uuid4(), red, "a.png", None))["asset_id"]
            b = (await service.register(db, 1, uuid4(), blue, "b.png", None))["asset_id"]
        async def command(asset, revision, name, key=None):
            try:
                async with sessions.begin() as db:
                    return await label_service.apply(db, 1, key or uuid4(), [(asset, revision)], [name], [])
            except service.Conflict:
                return "conflict"
        await asyncio.gather(command(a, 0, "shared"), command(b, 0, "shared"))
        async with sessions.begin() as db:
            assert (await db.execute(text("SELECT positive_count FROM collector_next.tag_usage"))).scalar_one() == 2
        results = await asyncio.gather(command(a, 1, "left"), command(a, 1, "right"))
        assert results.count("conflict") == 1
        key = uuid4()
        replay = await asyncio.gather(command(b, 1, "extra", key), command(b, 1, "extra", key))
        assert sum(r["replayed"] for r in replay) == 1
        async with sessions.begin() as db:
            assert {r["id"]: r["tag_revision"] for r in (await service.list_media(db, 1))["items"]} == {a: 2, b: 2}
    run(database, scenario)


@pytest.mark.parametrize("raw,expected", [("  Ｆａｖｏｒｉｔｅ  ", ("Favorite", "favorite")), ("夏の  旅行", ("夏の 旅行", "夏の 旅行"))])
def test_label_names_are_normalized_once(raw, expected):
    assert normalize_label(raw) == expected


@pytest.mark.parametrize("raw", ["", " ", "a,b", "a，b", "a、b", "bad\nlabel", "a" * 101, "bad\x00label"])
def test_invalid_label_names_are_rejected(raw):
    with pytest.raises(ValueError):
        normalize_label(raw)


def test_label_projection_can_be_rebuilt_from_evidence(database, tmp_path):
    stored, _ = image(tmp_path)
    async def scenario(sessions):
        async with sessions.begin() as db:
            asset = (await service.register(db, 1, uuid4(), stored, "a.png", None))["asset_id"]
            await label_service.apply(db, 1, uuid4(), [(asset, 0)], ["kept", "removed"], [])
            await label_service.apply(db, 1, uuid4(), [(asset, 1)], [], ["removed"])
        async with sessions.begin() as db:
            await db.execute(text("DELETE FROM collector_next.label_head"))
            await db.execute(text("DELETE FROM collector_next.asset_label_state"))
            await db.execute(text("UPDATE collector_next.tag_usage SET positive_count=999"))
        async with sessions.begin() as db:
            assert await label_service.rebuild_asset(db, 1, asset) == {"asset_id": asset, "revision": 2, "labels": 1}
            page = await service.list_media(db, 1, tags=["kept"])
            assert page["items"][0]["tags"] == ["kept"] and page["items"][0]["tag_revision"] == 2
            assert not (await service.list_media(db, 1, tags=["removed"]))["items"]
            assert (await db.execute(text("SELECT count(*) FROM collector_next.label_observation"))).scalar_one() == 3
    run(database, scenario)


def test_label_search_handles_media_kinds_and_repeated_collection_positions(database, tmp_path, webm_payload):
    still, _ = image(tmp_path)
    video = store_media(BytesIO(webm_payload), tmp_path)
    async def scenario(sessions):
        async with sessions.begin() as db:
            c = await service.create_collection(db, 1, "mixed")
            a = (await service.register(db, 1, uuid4(), still, "a.png", c["id"]))["asset_id"]
            b = (await service.register(db, 1, uuid4(), video, "b.webm", c["id"]))["asset_id"]
            await service.register(db, 1, uuid4(), still, "again.png", c["id"])
            await label_service.apply(db, 1, uuid4(), [(a, 0), (b, 0)], ["common"], [])
            await label_service.apply(db, 1, uuid4(), [(a, 1)], ["still"], [])
        async with sessions.begin() as db:
            assert [r["id"] for r in (await service.list_media(db, 1, tags=["common"], kind="video"))["items"]] == [b]
            assert [r["position"] for r in (await service.entries(db, 1, c["id"], tags=["common", "still"]))["items"]] == [1, 3]
            first = await service.entries(db, 1, c["id"], limit=1, tags=["still"], kind="image")
            second = await service.entries(db, 1, c["id"], after=first["next_cursor"], limit=1, tags=["still"], kind="image")
            assert second["items"][0]["position"] == 3 and second["next_cursor"] is None
            assert not (await service.list_media(db, 1, tags=["unknown"]))["items"]
    run(database, scenario)


def test_sparse_tag_search_uses_current_postings_not_full_media_or_history(database):
    async def scenario(sessions):
        async with sessions.begin() as db:
            await db.execute(text("INSERT INTO collector_next.blob(sha256,byte_size) SELECT sha256(i::text::bytea),1 FROM generate_series(1,10000) i"))
            await db.execute(text("INSERT INTO collector_next.asset(owner_id,blob_id) SELECT 1,id FROM collector_next.blob"))
            await db.execute(text("""INSERT INTO collector_next.media_current(asset_id,owner_id,original_name,media_type,width,height)
                SELECT id,1,'benchmark.png','image/png',1,1 FROM collector_next.asset"""))
            for start in range(1, 10001, 100):
                await label_service.apply(db, 1, uuid4(), [(i, 0) for i in range(start, start + 100)], ["common"], [])
            await label_service.apply(db, 1, uuid4(), [(i, 1) for i in range(100, 10001, 100)], ["rare"], [])
            for table in ['media_current','label_head','tag','tag_usage']:
                await db.execute(text('ANALYZE collector_next.' + table))
        plans = []
        async with sessions.begin() as db:
            class Trace:
                async def execute(self, statement, params=None):
                    if 'media_tag_current pivot' in str(statement):
                        plans.append((await db.execute(text('EXPLAIN (ANALYZE,BUFFERS,FORMAT JSON) ' + str(statement)), params)).scalar_one()[0])
                    return await db.execute(statement, params or {})
            result = await service.list_media(Trace(), 1, tags=["common", "rare"])
            assert [row['id'] for row in result['items']] == list(range(10000, 6000, -100))
        def scanned(node):
            current = (node['Actual Rows'] + node.get('Rows Removed by Filter', 0)) * node['Actual Loops'] if 'Scan' in node['Node Type'] else 0
            return current + sum(scanned(child) for child in node.get('Plans', []))
        print('Sparse label search:', json.dumps({'scan_rows': scanned(plans[0]['Plan']), 'execution_ms': plans[0]['Execution Time']}))
        assert scanned(plans[0]['Plan']) < 1000
    run(database, scenario)


def test_bulk_tag_work_is_batched_and_bounded(database):
    async def scenario(sessions):
        async with sessions.begin() as db:
            await db.execute(text("INSERT INTO collector_next.blob(sha256,byte_size) SELECT sha256(i::text::bytea),1 FROM generate_series(1,100) i"))
            await db.execute(text("INSERT INTO collector_next.asset(owner_id,blob_id) SELECT 1,id FROM collector_next.blob"))
            await db.execute(text("""INSERT INTO collector_next.media_current(asset_id,owner_id,original_name,media_type,width,height)
                SELECT id,1,'benchmark.png','image/png',1,1 FROM collector_next.asset"""))
            class Trace:
                calls = 0
                async def execute(self, statement, params=None):
                    self.calls += 1
                    return await db.execute(statement, params or {})
            trace = Trace()
            started = time.perf_counter()
            result = await label_service.apply(trace, 1, uuid4(), [(i, 0) for i in range(1,101)], [f'tag-{i}' for i in range(16)], [])
            elapsed = (time.perf_counter() - started) * 1000
            assert result['observations'] == 1600 and len(result['targets']) == 100
            assert trace.calls < 40
            assert (await db.execute(text("SELECT min(positive_count) FROM collector_next.tag_usage"))).scalar_one() == 100
            print('Bulk label command:', json.dumps({'assets':100,'tags':16,'observations':1600,'sql_statements':trace.calls,'elapsed_ms':round(elapsed,3)}))
    run(database, scenario)


def test_collection_export_preserves_positions_names_and_label_evidence(database, tmp_path):
    from api.collector import export
    from zipfile import ZipFile
    red, red_bytes = image(tmp_path)
    blue, blue_bytes = image(tmp_path, 'blue')
    async def scenario(sessions):
        async with sessions.begin() as db:
            c = await service.create_collection(db, 1, '作品名')
            a = await service.register(db, 1, uuid4(), red, 'a.png', c['id'], 'root/a.png')
            await service.register(db, 1, uuid4(), blue, 'b.png', c['id'], 'root/sub/b.png')
            await service.register(db, 1, uuid4(), red, 'again.png', c['id'], 'root/again.png')
            await label_service.apply(db, 1, uuid4(), [(a['asset_id'], 0)], ['旅行', '未整理'], [])
            await label_service.apply(db, 1, uuid4(), [(a['asset_id'], 1)], [], ['未整理'])
        with pytest.raises(service.NotFound):
            async with sessions.begin() as db:
                await export.snapshot(db, 2, c['id'])
        async with sessions.begin() as db:
            manifest = await export.snapshot(db, 1, c['id'])
        entries = manifest['collections'][0]['entries']
        assert [e['position'] for e in entries] == [1, 2, 3]
        assert entries[0]['original'] == entries[2]['original'] != entries[1]['original']
        assert [e['relative_path'] for e in entries] == ['root/a.png', 'root/sub/b.png', 'root/again.png']
        assert [e['original_name'] for e in entries] == ['a.png', 'b.png', 'again.png']
        assert len(manifest['originals']) == 2
        evidence = manifest['evidence']
        negative = next(e for e in evidence if not e['present'])
        assert negative['tag'] == '未整理' and negative['prior_present'] is True
        assert negative['based_on'] in {e['id'] for e in evidence if e['present']}
        assert negative['producer']['kind'] == 'account'
        current = next(p for p in manifest['projection'] if p['original'] == entries[0]['original'])
        assert current['revision'] == 2
        assert {p['tag'] for p in current['heads'] if p['present']} == {'旅行'}
        assert all(h['evidence'] in {e['id'] for e in evidence} for h in current['heads'])
        # Subsequent edits don't mutate the captured export or its historical references.
        async with sessions.begin() as db:
            await label_service.apply(db, 1, uuid4(), [(a['asset_id'], 2)], ['後から追加'], [])
            await service.register(db, 1, uuid4(), blue, 'later.png', c['id'])
        archive, size = export.build_archive(tmp_path, manifest)
        try:
            from scripts.collector_exchange_verify import verify
            assert verify(archive)['entries'] == 3
            archive.seek(0)
            assert os.fstat(archive.fileno()).st_nlink == 0  # no abandoned named ZIP on crash
            assert size == os.fstat(archive.fileno()).st_size
            with ZipFile(archive) as z:
                assert len(z.namelist()) == 3
                exported = json.loads(z.read('manifest.json'))
                assert exported == manifest and len(exported['collections'][0]['entries']) == 3
                for original in exported['originals']:
                    payload = z.read(original['path'])
                    assert len(payload) == original['bytes']
                    assert hashlib.sha256(payload).hexdigest() == original['sha256']
                    assert payload in (red_bytes, blue_bytes)
                assert '後から追加' not in z.read('manifest.json').decode()
        finally:
            archive.close()
    run(database, scenario)


def test_export_limits_disk_budget_and_corrupt_originals(database, tmp_path, monkeypatch):
    from api.collector import export
    from api.collector.storage import StorageIntegrityError
    stored, _ = image(tmp_path)
    async def scenario(sessions):
        async with sessions.begin() as db:
            c = await service.create_collection(db, 1, 'limits')
            await service.register(db, 1, uuid4(), stored, '../input.png', c['id'])
            await service.register(db, 1, uuid4(), stored, '../input.png', c['id'])
        with monkeypatch.context() as m:
            m.setattr(export, 'MAX_ENTRIES', 1)
            with pytest.raises(export.ExportLimit):
                async with sessions.begin() as db:
                    await export.snapshot(db, 1, c['id'])
        with monkeypatch.context() as m:
            m.setattr(export, 'MAX_BYTES', 1)
            with pytest.raises(export.ExportLimit):
                async with sessions.begin() as db:
                    await export.snapshot(db, 1, c['id'])
        async with sessions.begin() as db:
            manifest = await export.snapshot(db, 1, c['id'])
        with monkeypatch.context() as m:
            m.setattr(export.shutil, 'disk_usage', lambda _: type('Usage', (), {'free': 0})())
            with pytest.raises(export.ExportLimit):
                export.build_archive(tmp_path, manifest)
        # Input names never become archive paths, even if legacy metadata is hostile.
        archive, _ = export.build_archive(tmp_path, manifest)
        from zipfile import ZipFile
        with archive, ZipFile(archive) as z:
            assert all('..' not in name and not name.startswith('/') for name in z.namelist())
        path = content_path(tmp_path, stored.digest)
        original = path.read_bytes()
        path.write_bytes(b'x' * len(original))  # same size but wrong digest
        with pytest.raises(StorageIntegrityError):
            export.build_archive(tmp_path, manifest)
        path.unlink()
        with pytest.raises(FileNotFoundError):
            export.build_archive(tmp_path, manifest)
        assert list((tmp_path / 'staging').iterdir()) == []
    run(database, scenario)


def test_export_http_auth_safe_errors_and_resource_lifetime(tmp_path, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from types import SimpleNamespace
    from io import BytesIO
    from zipfile import ZipFile
    import api.collector.router as adapter
    from api.collector import export
    stored, _ = image(tmp_path)
    manifest = {'originals': [{'path': 'originals/' + stored.digest.hex(), 'sha256': stored.digest.hex(),
                              'bytes': stored.size, 'id': 'sample'}], 'export_id': str(uuid4()),
                'format': 'kura-collector-exchange', 'format_version': '0.1.0',
                'collections': [{'entries': [{'id': '1', 'position': 1, 'original': 'sample'}]}],
                'evidence': [], 'projection': [{'original': 'sample', 'heads': []}],
                'results': [{'entry': '1', 'status': 'succeeded'}]}
    seen = []
    async def snapshot(db, owner, collection):
        assert owner == 7
        if collection == 404:
            raise service.NotFound('missing')
        if collection == 422:
            raise export.ExportLimit('budget')
        return manifest
    actual_build = export.build_archive
    def build(root, data):
        archive, size = actual_build(root, data)
        seen.append(archive)
        return archive, size
    class DB:
        async def rollback(self):
            pass
    async def database():
        yield DB()
    monkeypatch.setenv('KURA_COLLECTOR_NEXT', '1')
    monkeypatch.setenv('KURA_COLLECTOR_NEXT_ROOT', str(tmp_path))
    monkeypatch.setattr(export, 'snapshot', snapshot)
    monkeypatch.setattr(export, 'build_archive', build)
    app = FastAPI()
    app.include_router(adapter.router)
    from api.errors import KuraError
    from fastapi.responses import JSONResponse
    @app.exception_handler(KuraError)
    async def error_handler(request, exc):
        return JSONResponse({'detail': exc.body()}, status_code=exc.status)
    app.dependency_overrides[adapter.get_db] = database
    with TestClient(app) as client:
        assert client.get('/api/collector-next/collections/1/export').status_code == 401
        app.dependency_overrides[adapter.get_current_user] = lambda: SimpleNamespace(user_id=7)
        assert client.get('/api/collector-next/collections/1/export?safe=1').status_code == 404
        assert client.get('/api/collector-next/collections/404/export').status_code == 404
        assert client.get('/api/collector-next/collections/422/export').status_code == 422
        result = client.get('/api/collector-next/collections/1/export')
        assert result.status_code == 200 and result.headers['content-type'] == 'application/zip'
        assert result.headers['content-length'] == str(len(result.content))
        assert result.headers['cache-control'] == 'private, no-store'
        assert 'attachment' in result.headers['content-disposition']
        with ZipFile(BytesIO(result.content)) as archive:
            assert archive.read('originals/' + stored.digest.hex()) == content_path(tmp_path, stored.digest).read_bytes()
        assert seen[-1].closed and adapter._export_slots._value == 1
        content_path(tmp_path, stored.digest).unlink()
        result = client.get('/api/collector-next/collections/1/export')
        assert result.status_code == 503 and str(tmp_path) not in result.text
        assert adapter._export_slots._value == 1


def test_export_cancelled_build_keeps_slot_until_worker_closes(monkeypatch, tmp_path):
    import threading
    from types import SimpleNamespace
    import api.collector.router as adapter
    from api.collector import export
    started, finish = threading.Event(), threading.Event()
    archive = BytesIO(b'archive')
    def build(root, manifest):
        started.set()
        assert finish.wait(3)
        return archive, 7
    async def snapshot(db, owner, collection):
        return {'export_id': str(uuid4())}
    class DB:
        async def rollback(self):
            pass
    monkeypatch.setattr(export, 'snapshot', snapshot)
    monkeypatch.setattr(export, 'build_archive', build)
    monkeypatch.setattr(adapter, 'root', lambda: tmp_path)
    async def scenario():
        monkeypatch.setattr(adapter, '_export_slots', asyncio.Semaphore(1))
        request = SimpleNamespace(cookies={}, query_params={})
        task = asyncio.create_task(adapter.export_collection(1, request, SimpleNamespace(user_id=1), DB()))
        assert await asyncio.to_thread(started.wait, 2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert adapter._export_slots._value == 0
        finish.set()
        for _ in range(100):
            if archive.closed:
                break
            await asyncio.sleep(.01)
        assert archive.closed and adapter._export_slots._value == 1
    asyncio.run(scenario())


def test_legacy_bridge_keyset_ties_safe_ads_and_blob_separation(postgres):
    from api.collector.adapters import legacy
    async def scenario(sessions):
        async with sessions.begin() as db:
            await db.execute(text('DROP SCHEMA IF EXISTS legacy_bridge_test CASCADE'))
            await db.execute(text('CREATE SCHEMA legacy_bridge_test'))
            await db.execute(text('SET LOCAL search_path=legacy_bridge_test'))
            for ddl in [
                'CREATE TABLE pool(id integer PRIMARY KEY,version integer)',
                'CREATE TABLE pool_name(pool_name_id integer,pool_id integer,name text,ord integer)',
                'CREATE TABLE post(id integer PRIMARY KEY,version integer,type text,safety text,status text,original_filename text,"mime-type" text,image_width integer,image_height integer,sha256 bytea,media_location text,data bytea,thumbnail bytea)',
                'CREATE TABLE pool_post(pool_id integer,post_id integer,ord integer,PRIMARY KEY(pool_id,post_id))',
                'CREATE INDEX entry_cursor ON pool_post(pool_id,ord,post_id)',
                'CREATE TABLE post_tag(post_id integer,tag_id integer)',
                'CREATE TABLE tag_name(tag_id integer,name text)',
            ]:
                await db.execute(text(ddl))
            await db.execute(text('INSERT INTO pool VALUES(7,1),(8,1)'))
            await db.execute(text("INSERT INTO pool_name VALUES(1,7,'old work',0)"))
            await db.execute(text('''INSERT INTO post SELECT i,1,'image',
                CASE WHEN i % 3=0 THEN 'safe' WHEN i % 3=1 THEN 'unsafe' ELSE NULL END,
                CASE WHEN i IN(5,8) THEN 'corrupt' ELSE 'ok' END,'image.png','image/png',80,60,NULL,'pg',
                decode('1122','hex'),decode('3344','hex') FROM generate_series(1,125) i'''))
            await db.execute(text('INSERT INTO pool_post SELECT 7,i,(i-2)/3 FROM generate_series(1,124) i'))
            await db.execute(text('INSERT INTO pool_post VALUES(8,125,0)'))
            await db.execute(text("INSERT INTO tag_name VALUES(1,'advertisement'),(1,'ad_alias')"))
            await db.execute(text('INSERT INTO post_tag VALUES(6,1),(9,1)'))
        async with sessions.begin() as db:
            await db.execute(text('SET LOCAL search_path=legacy_bridge_test'))
            cursor, found = None, []
            while True:
                page = await legacy.entries(db, 7, False, cursor, 11)
                assert len(page['items']) <= 11
                assert all('payload' not in r and 'data' not in r for r in page['items'])
                found.extend(r['id'] for r in page['items'])
                cursor = page['next_cursor']
                if cursor is None:
                    break
            assert found == [i for i in range(1,125) if i not in (5,6,8,9)]
            assert len(found) == len(set(found))
            safe = await legacy.entries(db, 7, True, limit=100)
            assert [r['id'] for r in safe['items']] == [i for i in range(1,125) if i % 3 == 0 and i not in (6,9)]
            assert (await legacy.media(db,7,3,True,'thumbnail'))['payload'] == bytes.fromhex('3344')
            assert (await legacy.media(db,7,3,True,'preview'))['payload'] == bytes.fromhex('1122')
            for post in (1,2,6,9,125):
                with pytest.raises(service.NotFound):
                    await legacy.media(db,7,post,True,'preview')
            with pytest.raises(service.NotFound):
                await legacy.entries(db,404,False)
        from fastapi import FastAPI
        from fastapi.responses import JSONResponse
        from api.errors import KuraError
        from types import SimpleNamespace
        import httpx
        from api.collector import router as adapter
        app = FastAPI()
        app.include_router(adapter.router)
        @app.exception_handler(KuraError)
        async def error_handler(request, exc):
            return JSONResponse({'detail': exc.body()}, status_code=exc.status)
        async def database():
            async with sessions() as db:
                await db.execute(text('SET LOCAL search_path=legacy_bridge_test'))
                yield db
        app.dependency_overrides[adapter.get_db] = database
        app.dependency_overrides[adapter.enabled] = lambda: None
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
            assert (await client.get('/api/collector-next/legacy/pools/7/entries')).status_code == 401
            app.dependency_overrides[adapter.get_current_user] = lambda: SimpleNamespace(user_id=1)
            assert (await client.get('/api/collector-next/legacy/pools/7/entries?after_order=0')).status_code == 422
            result = await client.get('/api/collector-next/legacy/pools/7/entries?safe=1&limit=100')
            assert result.status_code == 200 and [r['id'] for r in result.json()['items']] == [i for i in range(1,125) if i % 3 == 0 and i not in (6,9)]
            assert (await client.get('/api/collector-next/legacy/pools/7/media/1/preview?safe=1')).status_code == 404
            assert (await client.get('/api/collector-next/legacy/pools/7/media/125/preview')).status_code == 404
            result = await client.get('/api/collector-next/legacy/pools/7/media/3/thumbnail?safe=1')
            assert result.content == bytes.fromhex('3344') and result.headers['cache-control'] == 'private, no-store'
        async with sessions.begin() as db:
            await db.execute(text('DROP SCHEMA legacy_bridge_test CASCADE'))
    run(postgres, scenario)


def test_safe_gallery_index_preserves_exact_set_and_handles_invalid_retry(postgres):
    import importlib.util
    source = schema_history.migration('0171_safe_gallery_read_index.py')
    spec = importlib.util.spec_from_file_location('safe_gallery_migration', source)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    async def scenario(sessions):
        # Private test cluster only; real Alembic concurrent DDL outside a transaction.
        engine = create_async_engine(postgres.replace('postgresql:', 'postgresql+asyncpg:'))
        try:
            async with engine.connect() as raw:
                db = await raw.execution_options(isolation_level='AUTOCOMMIT')
                await db.execute(text('CREATE TABLE public.post(id bigint PRIMARY KEY,file_size bigint,status text,safety text)'))
                await db.execute(text("""INSERT INTO public.post SELECT i,i*10,
                    CASE WHEN i%7=0 THEN 'corrupt' ELSE 'ok' END,
                    CASE WHEN i%97=0 THEN 'safe' WHEN i%3=0 THEN NULL ELSE 'unsafe' END
                    FROM generate_series(1,100000) i"""))
                before = (await db.execute(text("SELECT count(*),sum(file_size) FROM public.post WHERE status='ok' AND safety='safe'"))).one()
                from alembic.migration import MigrationContext
                from alembic.operations import Operations
                def upgrade(connection):
                    context = MigrationContext.configure(connection)
                    with context.begin_transaction(), Operations.context(context):
                        migration.upgrade()
                async def apply():
                    await db.rollback()
                    await db.run_sync(upgrade)
                    await db.execution_options(isolation_level='AUTOCOMMIT')
                await apply()
                await apply()  # valid index replay
                after = (await db.execute(text("SELECT count(*),sum(file_size) FROM public.post WHERE status='ok' AND safety='safe'"))).one()
                assert before == after
                rows = (await db.execute(text("SELECT id FROM public.post WHERE status='ok' AND safety='safe' ORDER BY id DESC LIMIT 40"))).scalars().all()
                assert rows == [i for i in range(100000,0,-1) if i%97==0 and i%7!=0][:40]
                await db.execute(text('VACUUM ANALYZE public.post'))
                plan = (await db.execute(text("EXPLAIN(FORMAT JSON) SELECT count(*),sum(file_size) FROM public.post WHERE status='ok' AND safety='safe'"))).scalar()
                assert migration.INDEX_NAME in json.dumps(plan)
                # A concurrently-created invalid index from interrupted uniqueness
                # validation is replaced safely instead of IF NOT EXISTS skipping it.
                await db.execute(text(f'DROP INDEX public.{migration.INDEX_NAME}'))
                with pytest.raises(Exception):
                    await db.execute(text(f'CREATE UNIQUE INDEX CONCURRENTLY {migration.INDEX_NAME} ON public.post(status)'))
                assert (await db.execute(text('SELECT indisvalid FROM pg_index WHERE indexrelid=to_regclass(:name)'), {'name':'public.'+migration.INDEX_NAME})).scalar() is False
                await apply()
                assert (await db.execute(text('SELECT indisvalid FROM pg_index WHERE indexrelid=to_regclass(:name)'), {'name':'public.'+migration.INDEX_NAME})).scalar() is True
                await db.execute(text('DROP TABLE public.post'))
        finally:
            await engine.dispose()
    run(postgres, scenario)


def test_tag_browser_sidebar_matches_old_choices_without_fanout(postgres):
    from api.services.tag_browser import sidebar
    from api.routers.tags import search_tags
    from api.db.models import Base, Tag, TagCategory, TagName, TagLocalization
    from sqlalchemy import event
    async def scenario(sessions):
        async with sessions.begin() as db:
            await db.execute(text('CREATE SCHEMA sidebar_test'))
            await db.execute(text('SET LOCAL search_path=sidebar_test'))
            connection = await db.connection()
            await connection.run_sync(lambda conn: Base.metadata.create_all(conn,
                tables=[m.__table__ for m in (Tag,TagCategory,TagName,TagLocalization)]))
            await db.execute(text('''INSERT INTO tag_category(id,name,color,"default","order",version)
                VALUES(1,'general','#aaa',true,0,1),(2,'artist','#fff',false,1,1),(3,'empty','#999',false,2,1)'''))
            await db.execute(text("""INSERT INTO tag(id,category_id,version,creation_time,post_count,description)
                SELECT i,CASE WHEN i<=60 THEN 1 ELSE 2 END,1,now(),i/3,repeat('unused',2000)
                FROM generate_series(1,80) i"""))
            await db.execute(text("INSERT INTO tag_name(tag_id,name,ord) SELECT i,'tag_'||i,0 FROM generate_series(1,80) i"))
            await db.execute(text("INSERT INTO tag_name(tag_id,name,ord) VALUES(60,'last_alias',1)"))
            await db.execute(text("INSERT INTO tag_localization(tag_id,locale,name) VALUES(60,'ja','共通名'),(59,'ja','共通名')"))
            statements=[]
            listener=lambda conn,cursor,sql,parameters,context,executemany:statements.append(sql)
            event.listen(connection.sync_connection,'before_cursor_execute',listener)
            result = await sidebar(db)
            event.remove(connection.sync_connection,'before_cursor_execute',listener)
            assert len(statements)==1
            assert 'count(' not in statements[0].lower() and 'post_tag' not in statements[0]
            assert [c['name'] for c in result['categories']]==['general','artist','empty']
            assert result['tagsByCat']['empty']==[]
            for category in ('general','artist'):
                old = await search_tags(localize=True,query='',category=category,offset=0,limit=40,
                                        sort='usage',thumbnails=False,db=db)
                choices=result['tagsByCat'][category]
                fields=set(choices[0])
                assert choices==[{k:v for k,v in t.items() if k in fields} for t in old.results]
                assert len(choices)<=40 and all('description' not in c and 'thumbnailUrl' not in c for c in choices)
            await db.execute(text('DROP SCHEMA sidebar_test CASCADE'))
    run(postgres,scenario)


def test_durable_import_replay_rollback_ownership_and_changed_bytes(database,tmp_path):
    from api.collector import import_service as imports
    stored,payload=image(tmp_path)
    other,_=image(tmp_path,'blue')
    key=uuid4();request=uuid4()
    manifest=[dict(request_key=request,original_name='a.png',relative_path='Book/a.png',byte_size=len(payload),sha256=stored.digest.hex()),
              dict(request_key=uuid4(),original_name='large.png',relative_path='Book/large.png',byte_size=64*1024*1024+1,sha256=None)]
    async def scenario(sessions):
        async with sessions.begin() as db:
            first=await imports.plan(db,1,key,None,manifest)
            assert first['rejected_count']==1 and first['remaining_count']==1
        # New session models a process restart; plan and folder mapping survive.
        async with sessions.begin() as db:
            replay=await imports.plan(db,1,key,None,manifest)
            rows=(await imports.items(db,1,key,0,1))
            item=rows['items'][0];assert rows['next_cursor']==1
            assert replay['request_key']==first['request_key']
            assert len(await service.collections(db,1))==1
            assert (await imports.items(db,1,key,1,1))['items'][0]['state']=='rejected'
        with pytest.raises(service.NotFound):
            async with sessions.begin() as db:await imports.operation(db,2,key)
        with pytest.raises(service.Conflict):
            async with sessions.begin() as db:await imports.plan(db,1,key,None,[dict(manifest[0],sha256=other.digest.hex())])
        with pytest.raises(service.Conflict):
            async with sessions.begin() as db:await imports.register(db,1,key,request,other,'a.png',item['collection_id'],'Book/a.png')
        # Crash after registering but before transaction commit: both receipt and counter roll back.
        with pytest.raises(RuntimeError):
            async with sessions.begin() as db:
                await imports.register(db,1,key,request,stored,'a.png',item['collection_id'],'Book/a.png')
                raise RuntimeError('crash before commit')
        async with sessions.begin() as db:
            assert (await imports.operation(db,1,key))['registered_count']==0
            assert (await db.execute(text('SELECT count(*) FROM collector_next.receipt'))).scalar_one()==0
        async def send():
            async with sessions.begin() as db:return await imports.register(db,1,key,request,stored,'a.png',item['collection_id'],'Book/a.png')
        sent=await asyncio.gather(send(),send())
        assert sent[0]['asset_id']==sent[1]['asset_id'] and sent[0]['entry_id']==sent[1]['entry_id']
        async with sessions.begin() as db:
            done=await imports.operation(db,1,key)
            assert done['registered_count']==1 and done['state']=='needs_attention' and done['remaining_count']==0
            assert len((await service.entries(db,1,item['collection_id']))['items'])==1
            await imports.reject(db,1,key,request,'invalid','must not downgrade success')
            assert (await imports.operation(db,1,key))['rejected_count']==1
    run(database,scenario)


def test_http_import_records_input_failure_and_recovers_after_restart(database,tmp_path,monkeypatch):
    from api.collector import router as adapter
    from fastapi import FastAPI
    from fastapi.responses import JSONResponse
    from api.errors import KuraError
    from types import SimpleNamespace
    import httpx
    stored,payload=image(tmp_path/'fixture');bad=b'not an image';key=uuid4();requests=[uuid4(),uuid4()]
    monkeypatch.setenv('KURA_COLLECTOR_NEXT','1');monkeypatch.setenv('KURA_DB_ROLE','canonical')
    monkeypatch.setenv('KURA_COLLECTOR_NEXT_ROOT',str(tmp_path/'store'))
    async def scenario(sessions):
        monkeypatch.setattr(adapter,'async_session',sessions)
        def application():
            app=FastAPI();app.include_router(adapter.router)
            async def db_dependency():
                async with sessions() as db:yield db
            app.dependency_overrides[adapter.get_db]=db_dependency
            app.dependency_overrides[adapter.get_current_user]=lambda:SimpleNamespace(user_id=1)
            @app.exception_handler(KuraError)
            async def error(request,exc):return JSONResponse(status_code=exc.status,content={'detail':exc.body()})
            return app
        plan={'request_key':str(key),'items':[dict(request_key=str(r),original_name=name,byte_size=len(data),sha256=hashlib.sha256(data).hexdigest()) for r,name,data in zip(requests,['bad.png','good.png'],[bad,payload])]}
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=application()),base_url='http://test') as client:
            created=await client.post('/api/collector-next/imports',json=plan);assert created.status_code==201,created.text
            rejected=await client.post('/api/collector-next/upload',data={'request_key':str(requests[0]),'operation_key':str(key)},files={'file':('bad.png',bad)})
            assert rejected.status_code==422,rejected.text
            changed=await client.post('/api/collector-next/upload',data={'request_key':str(requests[1]),'operation_key':str(key)},files={'file':('good.png',bad)})
            # Invalid bytes with a different hash must not permanently reject the planned original.
            assert changed.status_code in (409,422)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=application()),base_url='http://test') as client:
            rows=(await client.get(f'/api/collector-next/imports/{key}/items')).json()['items']
            assert rows[0]['state']=='rejected' and rows[0]['error_code']=='unrecognized_image'
            result=await client.post('/api/collector-next/upload',data={'request_key':str(requests[1]),'operation_key':str(key)},files={'file':('good.png',payload)})
            assert result.status_code==200,result.text
            again=await client.post('/api/collector-next/upload',data={'request_key':str(requests[1]),'operation_key':str(key)},files={'file':('good.png',payload)})
            assert again.status_code==200 and again.json()['replayed']
            summary=(await client.get(f'/api/collector-next/imports/{key}')).json()
            assert summary['registered_count']==1 and summary['rejected_count']==1 and summary['remaining_count']==0
    run(database,scenario)


def test_import_reservations_preserve_page_order_with_partial_reselection(database,tmp_path):
    from api.collector import import_service as imports
    stored,payload=image(tmp_path)
    async def scenario(sessions):
        key=uuid4();requests=[uuid4(),uuid4()]
        async with sessions.begin() as db:
            collection=await service.create_collection(db,1,'book')
            await imports.plan(db,1,key,collection['id'],[dict(request_key=r,original_name=name,byte_size=len(payload),sha256=stored.digest.hex()) for r,name in zip(requests,['1.png','2.png'])])
        async with sessions.begin() as db:
            await imports.register(db,1,key,requests[1],stored,'2.png',collection['id'],None)
            await service.register(db,1,uuid4(),stored,'later.png',collection['id'])
        with pytest.raises(service.Conflict):
            async with sessions.begin() as db:
                await service.register(db,1,requests[0],stored,'changed.png',collection['id'])
        async with sessions.begin() as db:
            await imports.register(db,1,key,requests[0],stored,'1.png',collection['id'],None)
            entries=(await service.entries(db,1,collection['id']))['items']
            assert [(e['position'],e['relative_path'] or e['original_name']) for e in entries]==[(1,'1.png'),(2,'2.png'),(3,'later.png')]
            assert (await imports.operation(db,1,key))['state']=='completed'
            assert (await imports.recent(db,2))['items']==[]
            # Earlier operations remain accessible without OFFSET or all-history reads.
            for _ in range(21):
                await imports.plan(db,1,uuid4(),None,[dict(request_key=uuid4(),original_name='x.png',byte_size=len(payload),sha256=stored.digest.hex())])
            first=await imports.recent(db,1)
            assert len(first['items'])==20 and first['next_cursor']
            second=await imports.recent(db,1,first['next_cursor'])
            assert len(second['items'])==2 and second['next_cursor'] is None
            assert set(r['request_key'] for r in first['items']).isdisjoint(r['request_key'] for r in second['items'])
    run(database,scenario)


def test_import_operation_migration_is_canonical_only(database):
    subprocess.run(['psql','-X','-v','ON_ERROR_STOP=1','-c',"""
        DROP TABLE collector_next.import_item,collector_next.import_operation;
        ALTER TABLE collector_next.schema_version DROP CONSTRAINT schema_version_version_check;
        UPDATE collector_next.schema_version SET version=3;
        ALTER TABLE collector_next.schema_version ADD CHECK(version=3);
        CREATE TABLE IF NOT EXISTS public.kura_alembic_version(version_num varchar(32) PRIMARY KEY);
        DELETE FROM public.kura_alembic_version; INSERT INTO public.kura_alembic_version VALUES('0171');
    """],env=pg_environment(database),check=True,capture_output=True)
    result=subprocess.run([sys.executable,'-m','alembic','upgrade','0172'],cwd=REPO,
        env={**os.environ,'DATABASE_URL':database.replace('postgresql:','postgresql+asyncpg:')},capture_output=True,text=True)
    assert result.returncode==0,result.stderr
    async def scenario(sessions):
        async with sessions.begin() as db:
            assert (await db.execute(text('SELECT version FROM collector_next.schema_version'))).scalar_one()==4
            assert (await db.execute(text("SELECT count(*) FROM pg_publication_tables WHERE schemaname='collector_next'"))).scalar_one()==0
    run(database,scenario)


def test_snapshot_preserves_pending_import_plan_and_receipt_progress(database,tmp_path):
    from api.collector import import_service as imports
    stored,payload=image(tmp_path/'source');key=uuid4()
    async def scenario(sessions):
        async with sessions.begin() as db:
            manifest=[dict(request_key=uuid4(),original_name=name,relative_path=f'Book/{name}',byte_size=len(payload),sha256=stored.digest.hex()) for name in ('1.png','2.png')]
            await imports.plan(db,1,key,None,manifest)
            first=(await imports.items(db,1,key))['items'][0]
            await imports.register(db,1,key,UUID(first['request_key']) if isinstance(first['request_key'],str) else first['request_key'],stored,'1.png',first['collection_id'],'Book/1.png')
    run(database,scenario)
    snapshot=backup(database,tmp_path/'source',tmp_path/'backup',Path('/'))
    from scripts.collector_next_verify import verify
    verified=verify(snapshot)
    assert verified['import_operations']==1 and verified['import_items']==2 and verified['receipts']==1


def test_import_plan_10000_items_keeps_planning_queries_bounded(database,tmp_path):
    from api.collector import import_service as imports
    from sqlalchemy import event
    stored,payload=image(tmp_path);key=uuid4()
    async def scenario(sessions):
        statements=[]
        engine=sessions.kw['bind']
        def record(conn,cursor,statement,parameters,context,many):statements.append(statement)
        event.listen(engine.sync_engine,'before_cursor_execute',record)
        try:
            async with sessions.begin() as db:
                await imports.plan(db,1,key,None,[dict(request_key=uuid4(),original_name=f'{n}.png',relative_path=f'Book/{n}.png',byte_size=len(payload),sha256=stored.digest.hex()) for n in range(10000)])
                assert len(statements)<=12,len(statements)
                statements.clear()
                page=await imports.items(db,1,key,9990,20)
                assert len(page['items'])==10 and page['next_cursor'] is None
                assert len(statements)==2 and all('OFFSET' not in s for s in statements)
        finally:event.remove(engine.sync_engine,'before_cursor_execute',record)
    run(database,scenario)


# Exchange restoration uses only this fixture's private temporary PostgreSQL.
def reset_exchange_destination(database):
    env = pg_environment(database)
    subprocess.run(['psql', '-X', '-v', 'ON_ERROR_STOP=1', '-c',
                    'DROP SCHEMA collector_next CASCADE'], env=env, check=True, capture_output=True)
    for filename in ['schema.sql', 'migrations/0002_import_paths.sql',
                     'migrations/0003_labels.sql', 'migrations/0004_import_operations.sql', 'migrations/0005_tag_summary.sql',
                     'migrations/0006_label_producer_origin.sql']:
        subprocess.run(['psql', '-X', '-v', 'ON_ERROR_STOP=1', '-f',
                        str(REPO / 'api/collector' / filename)], env=env, check=True, capture_output=True)


def exchange_fixture(database, root):
    from api.collector import export
    stored, payload = image(root)
    blue, _ = image(root, 'blue')
    saved = {}
    async def source(sessions):
        async with sessions.begin() as db:
            # Leave unrelated source identities outside the package namespace.
            await service.create_collection(db, 1, 'not exported')
            a = await service.register(db, 1, uuid4(), stored, 'original.png', None)
            c = await service.create_collection(db, 1, '旅行の記録')
            await service.register(db, 1, uuid4(), stored, 'page.png', c['id'], 'trip/page.png')
            await service.register(db, 1, uuid4(), blue, 'blue.png', c['id'], 'trip/sub/blue.png')
            await service.register(db, 1, uuid4(), stored, 'again.png', c['id'], 'trip/again.png')
            await db.execute(text('UPDATE collector_next.collection_entry SET position=position+10 WHERE collection_id=:c'), {'c': c['id']})
            await db.execute(text('UPDATE collector_next.collection_entry SET position=(position-10)*2 WHERE collection_id=:c'), {'c': c['id']})
            await label_service.apply(db, 1, uuid4(), [(a['asset_id'], 0)], ['旅行', '未整理'], [])
            await label_service.apply(db, 1, uuid4(), [(a['asset_id'], 1)], [], ['未整理'])
            await label_service.apply(db, 1, uuid4(), [(a['asset_id'], 2)], ['旅行'], [])
        async with sessions.begin() as db:
            saved['manifest'] = await export.snapshot(db, 1, c['id'])
        archive, _ = export.build_archive(root, saved['manifest'])
        with archive:
            saved['bytes'] = archive.read()
    run(database, source)
    return saved


def exchange_semantics(manifest):
    # Database integer IDs are deliberately local; UUIDs and evidence meaning survive.
    m = json.loads(json.dumps(manifest))
    for c in m['collections']:
        c.pop('id')
        for e in c['entries']:
            e.pop('id')
    events = sorted(m['evidence'], key=lambda e: int(e['id']))
    ids = {e['id']: str(i) for i, e in enumerate(events)}
    for e in events:
        e['id'] = ids[e['id']]
        if e['based_on'] is not None:
            e['based_on'] = ids[e['based_on']]
    for p in m['projection']:
        for h in p['heads']:
            h['evidence'] = ids[h['evidence']]
    return {'originals': sorted(m['originals'], key=lambda o: o['id']),
            'collections': m['collections'], 'evidence': events,
            'projection': sorted(m['projection'], key=lambda p: p['original'])}


def test_exchange_restore_round_trip_into_empty_namespace(database, tmp_path):
    from api.collector import export, restore
    saved = exchange_fixture(database, tmp_path / 'source')
    reset_exchange_destination(database)
    package = tmp_path / 'exchange.zip'
    package.write_bytes(saved['bytes'])
    root = tmp_path / 'destination'
    async def target(sessions):
        async with sessions.begin() as db:
            result = await restore.restore(db, root, package, 7)
        async with sessions.begin() as db:
            cs = await service.collections(db, 7)
            assert len(cs) == 1 and not await service.collections(db, 1)
            c = cs[0]
            rows = (await service.entries(db, 7, c['id']))['items']
            assert [r['position'] for r in rows] == [2, 4, 6]
            assert rows[0]['id'] == rows[2]['id'] != rows[1]['id']
            assert rows[0]['tags'] == ['旅行']
        async with sessions.begin() as db:
            restored = await export.snapshot(db, 7, c['id'])
        assert exchange_semantics(restored) == exchange_semantics(saved['manifest'])
        for original in restored['originals']:
            p = content_path(root, bytes.fromhex(original['sha256']))
            assert p.stat().st_size == original['bytes']
            assert hashlib.sha256(p.read_bytes()).hexdigest() == original['sha256']
        # Rebuild from preserved evidence and then perform an ordinary local edit.
        async with sessions.begin() as db:
            rebuilt = await label_service.rebuild_asset(db, 7, rows[0]['id'])
            assert rebuilt['revision'] == 3
            await label_service.apply(db, 7, uuid4(), [(rows[0]['id'], 3)], ['復元後'], [])
    run(database, target)


def test_exchange_restore_refuses_existing_data_before_cas_write(database, tmp_path):
    from api.collector import restore
    saved = exchange_fixture(database, tmp_path / 'source')
    package = tmp_path / 'exchange.zip'
    package.write_bytes(saved['bytes'])
    root = tmp_path / 'destination'
    async def target(sessions):
        with pytest.raises((ValueError, service.Conflict)):
            async with sessions.begin() as db:
                await restore.restore(db, root, package, 7)
        async with sessions.begin() as db:
            assert len(await service.collections(db, 1)) == 2
            assert not await service.collections(db, 7)
    run(database, target)
    assert not list(root.glob('originals/**/*'))


@pytest.mark.parametrize('damage', ['hash', 'path', 'revision', 'normalized_name', 'boolean', 'producer', 'timezone'])
def test_exchange_restore_rejects_invalid_package_without_receipts(database, tmp_path, damage):
    from api.collector import restore
    from zipfile import ZipFile, ZIP_STORED
    saved = exchange_fixture(database, tmp_path / 'source')
    reset_exchange_destination(database)
    manifest = saved['manifest']
    if damage == 'path':
        manifest['collections'][0]['entries'][0]['relative_path'] = '../page.png'
    elif damage == 'revision':
        next(p for p in manifest['projection'] if p['revision'])['revision'] += 1
    elif damage == 'normalized_name':
        manifest['evidence'][0]['normalized_name'] = 'forged'
    elif damage == 'boolean':
        manifest['evidence'][0]['present'] = 'false'
    elif damage == 'producer':
        manifest['evidence'][0]['producer']['kind'] = 'unknown'
    elif damage == 'timezone':
        manifest['evidence'][0]['observed_at'] = '2026-10-18T12:00:00'
    package = tmp_path / 'bad.zip'
    with ZipFile(BytesIO(saved['bytes'])) as source, ZipFile(package, 'w', ZIP_STORED) as out:
        for name in source.namelist():
            data = json.dumps(manifest, ensure_ascii=False).encode() if name == 'manifest.json' else source.read(name)
            if damage == 'hash' and name.startswith('originals/'):
                data = b'x' * len(data)
            out.writestr(name, data)
    root = tmp_path / 'destination'
    async def target(sessions):
        with pytest.raises((ValueError, service.Conflict)):
            async with sessions.begin() as db:
                await restore.restore(db, root, package, 7)
        async with sessions.begin() as db:
            assert (await db.execute(text('SELECT count(*) FROM collector_next.receipt'))).scalar() == 0
            assert (await db.execute(text('SELECT count(*) FROM collector_next.collection'))).scalar() == 0
    run(database, target)
    assert not list(root.glob('originals/**/*'))


def test_exchange_restore_empty_collection(database, tmp_path):
    from api.collector import export, restore
    package = tmp_path / 'empty.zip'
    async def source(sessions):
        async with sessions.begin() as db:
            c = await service.create_collection(db, 1, '空の作品')
        async with sessions.begin() as db:
            manifest = await export.snapshot(db, 1, c['id'])
        archive, _ = export.build_archive(tmp_path / 'source', manifest)
        with archive:
            package.write_bytes(archive.read())
    run(database, source)
    reset_exchange_destination(database)
    async def target(sessions):
        async with sessions.begin() as db:
            await restore.restore(db, tmp_path / 'destination', package, 7)
        async with sessions.begin() as db:
            cs = await service.collections(db, 7)
            assert len(cs) == 1 and cs[0]['title'] == '空の作品'
            assert not (await service.entries(db, 7, cs[0]['id']))['items']
    run(database, target)


def test_exchange_restore_webm(database, tmp_path, webm_payload):
    from api.collector import export, restore
    package = tmp_path / 'video.zip'
    source_root, target_root = tmp_path / 'source', tmp_path / 'target'
    stored = store_media(BytesIO(webm_payload), source_root)
    async def source(sessions):
        async with sessions.begin() as db:
            c = await service.create_collection(db, 1, '動画')
            await service.register(db, 1, uuid4(), stored, 'movie.webm', c['id'])
        async with sessions.begin() as db:
            manifest = await export.snapshot(db, 1, c['id'])
        archive, _ = export.build_archive(source_root, manifest)
        with archive:
            package.write_bytes(archive.read())
    run(database, source)
    reset_exchange_destination(database)
    async def target(sessions):
        async with sessions.begin() as db:
            await restore.restore(db, target_root, package, 7)
        async with sessions.begin() as db:
            rows = (await service.list_media(db, 7))['items']
            assert len(rows) == 1 and rows[0]['media_type'] == 'video/webm'
    run(database, target)
    assert content_path(target_root, stored.digest).read_bytes() == webm_payload
    with Image.open(content_path(target_root, stored.digest, True)) as preview:
        assert preview.format == 'WEBP'


def test_exchange_restore_rolls_back_metadata_and_reuses_cas(database, tmp_path):
    from api.collector import restore
    from sqlalchemy import event
    saved = exchange_fixture(database, tmp_path / 'source')
    reset_exchange_destination(database)
    package = tmp_path / 'exchange.zip'
    package.write_bytes(saved['bytes'])
    root = tmp_path / 'target'
    async def target(sessions):
        engine = sessions.kw['bind']
        def fail_after_asset(conn, cursor, statement, params, context, executemany):
            if 'INSERT INTO collector_next.label_observation' in statement:
                raise RuntimeError('simulated process failure during metadata insertion')
        event.listen(engine.sync_engine, 'before_cursor_execute', fail_after_asset)
        try:
            with pytest.raises(RuntimeError):
                async with sessions.begin() as db:
                    await restore.restore(db, root, package, 7)
        finally:
            event.remove(engine.sync_engine, 'before_cursor_execute', fail_after_asset)
        async with sessions.begin() as db:
            for table in ['collection', 'receipt', 'asset', 'blob', 'label_operation', 'label_observation']:
                assert (await db.execute(text(f'SELECT count(*) FROM collector_next.{table}'))).scalar() == 0
        async with sessions.begin() as db:
            await restore.restore(db, root, package, 7)
        async with sessions.begin() as db:
            assert len(await service.collections(db, 7)) == 1
    run(database, target)


def test_exchange_restore_concurrent_empty_target_admits_one(database, tmp_path):
    from api.collector import restore
    saved = exchange_fixture(database, tmp_path / 'source')
    reset_exchange_destination(database)
    package = tmp_path / 'exchange.zip'
    package.write_bytes(saved['bytes'])
    async def target(sessions):
        async def attempt():
            async with sessions.begin() as db:
                return await restore.restore(db, tmp_path / 'target', package, 7)
        results = await asyncio.gather(attempt(), attempt(), return_exceptions=True)
        assert sum(not isinstance(r, BaseException) for r in results) == 1
        async with sessions.begin() as db:
            assert len(await service.collections(db, 7)) == 1
            assert (await db.execute(text('SELECT count(*) FROM collector_next.receipt'))).scalar() == 3
    run(database, target)


def test_exchange_restore_cli_explicit_destination(database, tmp_path):
    saved = exchange_fixture(database, tmp_path / 'source')
    reset_exchange_destination(database)
    package = tmp_path / 'exchange.zip'
    package.write_bytes(saved['bytes'])
    command = [sys.executable, '-m', 'scripts.collector_exchange_restore',
               '--database', database, '--root', str(tmp_path / 'target'), '--owner', '7', str(package)]
    env_command = command.copy()
    env_command[3:5] = ['--database-env', 'COLLECTOR_RESTORE_TEST_DATABASE']
    result = subprocess.run(env_command, env={**os.environ, 'COLLECTOR_RESTORE_TEST_DATABASE': database},
                            cwd=REPO, text=True, capture_output=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
    assert isinstance(json.loads(result.stdout), dict)
    again = subprocess.run(command, cwd=REPO, text=True, capture_output=True, timeout=30)
    assert again.returncode != 0
    assert database not in again.stdout + again.stderr


def test_exchange_restore_equal_future_timestamps_and_later_edit(database, tmp_path):
    from api.collector import restore
    from zipfile import ZipFile, ZIP_STORED
    saved = exchange_fixture(database, tmp_path / 'source')
    reset_exchange_destination(database)
    manifest = saved['manifest']
    for e in manifest['evidence']:
        e['observed_at'] = '2099-01-01T00:00:00+00:00'
    # Archive evidence order is not an insertion instruction.
    manifest['evidence'].reverse()
    package = tmp_path / 'future.zip'
    with ZipFile(BytesIO(saved['bytes'])) as source, ZipFile(package, 'w', ZIP_STORED) as out:
        for name in source.namelist():
            out.writestr(name, json.dumps(manifest).encode() if name == 'manifest.json' else source.read(name))
    async def target(sessions):
        async with sessions.begin() as db:
            await restore.restore(db, tmp_path / 'target', package, 7)
        async with sessions.begin() as db:
            cs = await service.collections(db, 7)
            rows = (await service.entries(db, 7, cs[0]['id']))['items']
            asset = rows[0]['id']
            assert rows[0]['tags'] == ['旅行']
            await label_service.apply(db, 7, uuid4(), [(asset, 3)], [], ['旅行'])
        async with sessions.begin() as db:
            rows = (await service.entries(db, 7, cs[0]['id']))['items']
            assert rows[0]['tags'] == [] and rows[0]['tag_revision'] == 4
    run(database, target)


@pytest.mark.parametrize('damage', ['compression', 'duplicate_key', 'large_directory', 'extra_member', 'zip64_override'])
def test_exchange_restore_bounds_archive_parser_before_database(database, tmp_path, damage):
    from api.collector import restore
    from zipfile import ZipFile, ZIP_STORED, ZIP_DEFLATED
    import struct
    saved = exchange_fixture(database, tmp_path / 'source')
    data = BytesIO()
    with ZipFile(BytesIO(saved['bytes'])) as source, ZipFile(data, 'w', ZIP_DEFLATED if damage == 'compression' else ZIP_STORED) as out:
        for name in source.namelist():
            payload = source.read(name)
            if damage == 'duplicate_key' and name == 'manifest.json':
                payload = payload.replace(b'"format":', b'"format":"wrong","format":', 1)
            out.writestr(name, payload)
        if damage == 'extra_member':
            out.writestr('../../outside', b'not an original')
    raw = bytearray(data.getvalue())
    end = raw.rfind(b'PK\x05\x06')
    if damage == 'large_directory':
        struct.pack_into('<L', raw, end + 12, 0xFFFFFFFF)
    if damage == 'zip64_override':
        # A malicious locator must not override the already-bounded directory size.
        fake = struct.pack('<4sQ2H2L4Q', b'PK\x06\x06', 44, 45, 45, 0, 0, 1, 1, end - 1, 1)
        locator = struct.pack('<4sLQL', b'PK\x06\x07', 0, end, 1)
        raw[end:end] = fake + locator
    with pytest.raises(restore.RestoreError):
        restore.prepare(BytesIO(raw))


def test_exchange_restore_accepts_exporter_zip64_end_records(database, tmp_path, monkeypatch):
    from api.collector import restore
    import zipfile
    saved = exchange_fixture(database, tmp_path / 'source')
    result = BytesIO()
    with zipfile.ZipFile(BytesIO(saved['bytes'])) as source:
        with monkeypatch.context() as patch:
            patch.setattr(zipfile, 'ZIP64_LIMIT', 0)
            with zipfile.ZipFile(result, 'w', zipfile.ZIP_STORED) as out:
                for name in source.namelist():
                    out.writestr(name, source.read(name))
    assert b'PK\x06\x06' in result.getvalue()
    assert len(restore.prepare(BytesIO(result.getvalue())).originals) == 2


def test_exchange_restore_rechecks_target_after_copy_without_blocking_writer(database, tmp_path, monkeypatch):
    from api.collector import restore
    import threading
    saved = exchange_fixture(database, tmp_path / 'source')
    reset_exchange_destination(database)
    package = tmp_path / 'exchange.zip'
    package.write_bytes(saved['bytes'])
    entered, release = threading.Event(), threading.Event()
    actual = restore.store_originals
    def pause_copy(*args):
        entered.set()
        if not release.wait(10):
            raise RuntimeError('test copy barrier timed out')
        return actual(*args)
    monkeypatch.setattr(restore, 'store_originals', pause_copy)
    async def target(sessions):
        async def restoring():
            async with sessions.begin() as db:
                await restore.restore(db, tmp_path / 'target', package, 7)
        async def writer():
            async with sessions.begin() as db:
                await service.create_collection(db, 9, 'concurrent writer')
        task = asyncio.create_task(restoring())
        try:
            assert await asyncio.to_thread(entered.wait, 5)
            await asyncio.wait_for(writer(), 2)
        finally:
            release.set()
        with pytest.raises(restore.NamespaceNotEmpty):
            await task
        async with sessions.begin() as db:
            assert len(await service.collections(db, 9)) == 1
            assert not await service.collections(db, 7)
            assert (await db.execute(text('SELECT count(*) FROM collector_next.asset'))).scalar() == 0
    run(database, target)


def test_exchange_restore_metadata_is_batched_for_100_assets(database, tmp_path):
    from api.collector import export, restore
    source_root = tmp_path / 'source'
    media = [image(source_root, f'#{i:02x}3366')[0] for i in range(100)]
    package = tmp_path / 'batch.zip'
    async def source(sessions):
        async with sessions.begin() as db:
            c = await service.create_collection(db, 1, 'batch')
            ids = []
            for i, stored in enumerate(media):
                r = await service.register(db, 1, uuid4(), stored, f'{i}.png', c['id'])
                ids.append((r['asset_id'], 0))
            await label_service.apply(db, 1, uuid4(), ids, [f'tag-{i:02d}' for i in range(16)], [])
        async with sessions.begin() as db:
            manifest = await export.snapshot(db, 1, c['id'])
        archive, _ = export.build_archive(source_root, manifest)
        with archive:
            package.write_bytes(archive.read())
    run(database, source)
    reset_exchange_destination(database)
    async def target(sessions):
        async with sessions.begin() as db:
            class Trace:
                calls = 0
                async def execute(self, statement, params=None):
                    self.calls += 1
                    return await db.execute(statement, params or {})
            trace = Trace()
            started = time.perf_counter()
            result = await restore.restore(trace, tmp_path / 'target', package, 7)
            elapsed = (time.perf_counter() - started) * 1000
            assert result['originals'] == 100 and result['label_observations'] == 1600
            assert trace.calls < 60
            print('Exchange restore:', json.dumps({'assets': 100, 'observations': 1600,
                  'sql_statements': trace.calls, 'elapsed_ms': round(elapsed, 3)}))
    run(database, target)


def _producers(database):
    async def read(sessions):
        async with sessions.begin() as db:
            return sorted(tuple(r) for r in (await db.execute(text(
                'SELECT owner_id,reference,version,origin FROM collector_next.label_producer'))).all())
    out = {}
    async def go(sessions):
        out['rows'] = await read(sessions)
    run(database, go)
    return out['rows']


def _restore_into(database, tmp_path, data: bytes, owner: int, name: str):
    from api.collector import restore
    package = tmp_path / f'{name}.zip'
    package.write_bytes(data)
    async def target(sessions):
        async with sessions.begin() as db:
            await restore.restore(db, tmp_path / name, package, owner)
    run(database, target)


def test_restored_evidence_keeps_its_library_apart_from_local_edits(database, tmp_path):
    """#152: library A's account 1 and library B's account 1 are not one producer. A package from
    A restored into B for account 1, then edited in B by account 1, gives two producers; B's
    export carries both origins and a restore into C keeps them apart."""
    from api.collector import export
    saved = exchange_fixture(database, tmp_path / 'source')            # library A
    a_id = saved['manifest']['source_library']
    assert saved['manifest']['format_version'] == '0.2.0'
    assert {e['producer']['origin'] for e in saved['manifest']['evidence']} == {a_id}
    reset_exchange_destination(database)                                # library B
    _restore_into(database, tmp_path, saved['bytes'], 1, 'b')
    out = {}
    async def edit_in_b(sessions):
        async with sessions.begin() as db:
            c = (await service.collections(db, 1))[0]
            rows = (await service.entries(db, 1, c['id']))['items']
            await label_service.rebuild_asset(db, 1, rows[0]['id'])
            await label_service.apply(db, 1, uuid4(), [(rows[0]['id'], 3)], ['Bで追加'], [])
        async with sessions.begin() as db:
            out['manifest'] = await export.snapshot(db, 1, c['id'])
        archive, _ = export.build_archive(tmp_path / 'b', out['manifest'])
        with archive:
            out['bytes'] = archive.read()
    run(database, edit_in_b)
    producers = _producers(database)
    assert producers == sorted([(1, '1', 'collector-label-api-v1', a_id), (1, '1', 'collector-label-api-v1', '')])
    b_id = out['manifest']['source_library']
    assert b_id != a_id
    origins = {e['producer']['origin'] for e in out['manifest']['evidence']}
    assert origins == {a_id, b_id}                                      # A's evidence still says A
    reset_exchange_destination(database)                                # library C
    _restore_into(database, tmp_path, out['bytes'], 1, 'c')
    assert {p[3] for p in _producers(database)} == {a_id, b_id}         # neither became C's own


def test_a_package_that_does_not_say_its_library_stays_unknown(database, tmp_path):
    from api.collector import export
    saved = exchange_fixture(database, tmp_path / 'source')
    old = json.loads(json.dumps(saved['manifest']))
    old['format_version'] = '0.1.0'
    old.pop('source_library')
    for e in old['evidence']:
        e['producer'].pop('origin')
    archive, _ = export.build_archive(tmp_path / 'source', old)
    with archive:
        data = archive.read()
    reset_exchange_destination(database)
    _restore_into(database, tmp_path, data, 1, 'b')
    assert {p[3] for p in _producers(database)} == {f"unknown:{old['export_id']}"}   # not this library's own


def test_a_package_restored_into_the_library_it_came_from_is_its_own_again(database, tmp_path):
    saved = exchange_fixture(database, tmp_path / 'source')
    a_id = saved['manifest']['source_library']
    reset_exchange_destination(database)
    async def same_library(sessions):
        async with sessions.begin() as db:
            await db.execute(text('UPDATE collector_next.library SET id=:i'), {'i': a_id})
    run(database, same_library)
    _restore_into(database, tmp_path, saved['bytes'], 1, 'home')
    assert {p[3] for p in _producers(database)} == {''}
