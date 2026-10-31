"""#150: real upload functions + isolated PostgreSQL and temporary originals.

Only tag/pool domain helpers are replaced; identity queries, transactions,
advisory locks, SHA256 uniqueness, file writes and materialization are real.
MD5 collisions are injected deterministically, not claimed as media exploits.
No production API, database, or media directory is used.
"""
import asyncio
from contextlib import asynccontextmanager
import hashlib
import io
import json
import os
from pathlib import Path
import socket
import subprocess
import tempfile
from types import SimpleNamespace

from fastapi import HTTPException, UploadFile
from PIL import Image
import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from api.db.models import Post, User
from api.routers import upload as U


@pytest.fixture(scope="module")
def database_url():
    configured = os.environ.get("KURA_UPLOAD_TEST_DATABASE_URL")
    if configured:
        assert sa.engine.make_url(configured).database == "kura_upload_test", "test DB must be named kura_upload_test"
        yield configured
        return
    # Local runs get a private disposable cluster, never the application DB.
    bindir = Path(subprocess.check_output(["pg_config", "--bindir"], text=True).strip())
    assert (bindir / "initdb").exists(), "install PostgreSQL or set KURA_UPLOAD_TEST_DATABASE_URL to a disposable DB"
    with tempfile.TemporaryDirectory(prefix="kura150-") as root:
        cluster = Path(root) / "db"
        subprocess.run([str(bindir / "initdb"), "-D", str(cluster), "-A", "trust", "-U", "tester"],
                       check=True, capture_output=True)
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        ctl = [str(bindir / "pg_ctl"), "-D", str(cluster)]
        subprocess.run([*ctl, "-l", str(Path(root) / "postgres.log"), "-o",
                        f"-h 127.0.0.1 -p {port} -k {root} -F", "-w", "start"],
                       check=True, capture_output=True)
        try:
            yield f"postgresql+asyncpg://tester@127.0.0.1:{port}/postgres"
        finally:
            subprocess.run([*ctl, "-m", "immediate", "-w", "stop"], check=True, capture_output=True)


@pytest.fixture
def harness(monkeypatch, tmp_path, database_url):
    from api.services import upload_input
    monkeypatch.setattr(upload_input, "_slots", asyncio.Semaphore(2))
    def names(kind, post_id, mime=None):
        ext = U.MIME_EXT.get(mime, "dat") if kind == "content" else "jpg"
        return [tmp_path / f"{kind}-{post_id}.{ext}"]

    monkeypatch.setattr(U.files, "names", names)
    # Display bytes are rendition files (0177); keep them in this test's tmp dir.
    monkeypatch.setattr(U.renditions, "ROOT", tmp_path / "renditions")

    async def tags(db, post, value, **kwargs):
        # Tags are recorded in a test-only table; post rows carry no tag arrays (0179).
        for name in value.split():
            await db.execute(sa.text("INSERT INTO test_tag_membership VALUES (:id, :t) ON CONFLICT DO NOTHING"),
                             {"id": post.post_id, "t": name})

    async def pool(db, post, name, **kwargs):
        await db.execute(sa.text("INSERT INTO test_pool_membership VALUES (:name, :id) ON CONFLICT DO NOTHING"),
                         {"name": name, "id": post.post_id})

    monkeypatch.setattr(U, "_apply_tags", tags)
    monkeypatch.setattr(U, "_append_to_pool", pool)

    @asynccontextmanager
    async def opened():
        engine = create_async_engine(database_url)
        # Clone just the ORM columns this endpoint reads. Domain FKs/triggers
        # are outside this test; no application migrations touch production.
        metadata = sa.MetaData()
        for model in [User, Post]:
            table = model.__table__.to_metadata(metadata)
            table.constraints = {c for c in table.constraints if not isinstance(c, sa.ForeignKeyConstraint)}
            table.foreign_keys.clear()
            for column in table.columns:
                column.foreign_keys.clear()
        sa.Index("test_unique_sha256", metadata.tables["post"].c.sha256, unique=True,
                 postgresql_where=metadata.tables["post"].c.status == "ok")
        async with engine.begin() as conn:
            await conn.run_sync(metadata.drop_all)
            await conn.run_sync(metadata.create_all)
            await conn.execute(sa.text("CREATE TABLE IF NOT EXISTS test_pool_membership (name text, post_id int, PRIMARY KEY(name, post_id))"))
            await conn.execute(sa.text("TRUNCATE test_pool_membership"))
            await conn.execute(sa.text("CREATE TABLE IF NOT EXISTS test_tag_membership (post_id int, tag text, PRIMARY KEY(post_id, tag))"))
            await conn.execute(sa.text("TRUNCATE test_tag_membership"))
            await conn.execute(sa.text("DROP TABLE IF EXISTS post_rendition, rendition_object"))
            await conn.execute(sa.text("CREATE TABLE rendition_object (sha256 bytea PRIMARY KEY, mime text, "
                                       "byte_size bigint, width int, height int)"))
            await conn.execute(sa.text("CREATE TABLE post_rendition (post_id int, kind text, sha256 bytea, "
                                       "recipe text, attached_at timestamptz DEFAULT now(), PRIMARY KEY(post_id, kind))"))
        sessions = async_sessionmaker(engine, expire_on_commit=False)

        async def send(data, tag="a", pool="pool-a", **kwargs):
            async with sessions() as db:
                values = dict(
                    file=UploadFile(io.BytesIO(data), filename="original.png"),
                    tags=tag, source=f"https://example.test/{tag}", safety="safe",
                    pool=pool, pool_source_url="", collection_url="",
                    artist_tags="", db=db, user=None)
                values.update(kwargs)
                result = await U.upload_post(**values)
                return json.loads(result.body) if hasattr(result, "body") else result

        async def rows():
            async with sessions() as db:
                return (await db.execute(sa.select(Post).order_by(Post.post_id))).unique().scalars().all()

        async def tags_of(post_id):
            async with sessions() as db:
                return [r[0] for r in (await db.execute(sa.text(
                    "SELECT tag FROM test_tag_membership WHERE post_id = :id ORDER BY tag"), {"id": post_id})).all()]

        async def memberships():
            async with sessions() as db:
                return set((await db.execute(sa.text("SELECT name, post_id FROM test_pool_membership"))).all())

        try:
            yield SimpleNamespace(send=send, rows=rows, sessions=sessions, memberships=memberships, names=names,
                                  tags_of=tags_of)
        finally:
            await engine.dispose()
    return opened


def png(color):
    buf = io.BytesIO()
    Image.new("RGB", (8, 8), color).save(buf, "PNG")
    return buf.getvalue()


def collide_md5(monkeypatch):
    monkeypatch.setattr(U, "hashlib", SimpleNamespace(
        md5=lambda _: SimpleNamespace(hexdigest=lambda: "0" * 32),
        sha1=hashlib.sha1, sha256=hashlib.sha256, file_digest=hashlib.file_digest))


def test_md5_collision_keeps_both_originals_and_metadata_separate(harness, monkeypatch):
    collide_md5(monkeypatch)
    async def scenario():
        async with harness() as h:
            a, b = png("red"), png("blue")
            first = await h.send(a)
            second = await h.send(b, "b", "pool-b")
            assert first["id"] != second["id"]
            assert not second["duplicate"]
            rows = await h.rows()
            assert [await h.tags_of(p.post_id) for p in rows] == [["a"], ["b"]]
            assert [p.source for p in rows] == ["https://example.test/a", "https://example.test/b"]
            for post, original in zip(rows, [a, b]):
                assert post.sha256 == hashlib.sha256(original).digest()
                assert h.names("content", post.post_id, "image/png")[0].read_bytes() == original
            assert await h.memberships() == {("pool-a", first["id"]), ("pool-b", second["id"])}
            # The oldest MD5 candidate differs; the later SHA256 match wins.
            again = await h.send(b, "c", "pool-c")
            assert again["duplicate"] and again["id"] == second["id"]
            assert await h.tags_of((await h.rows())[1].post_id) == ["b", "c"]
    asyncio.run(scenario())


def test_identical_reupload_keeps_id_and_additional_requests(harness):
    async def scenario():
        async with harness() as h:
            first = await h.send(png("red"))
            again = await h.send(png("red"), "b", "pool-b")
            assert again["id"] == first["id"] and again["duplicate"]
            assert await h.tags_of((await h.rows())[0].post_id) == ["a", "b"]
            assert await h.memberships() == {("pool-a", first["id"]), ("pool-b", first["id"])}
    asyncio.run(scenario())


@pytest.mark.parametrize("missing", [False, True])
@pytest.mark.parametrize("status", ["ok", "pending"])
def test_legacy_original_is_verified_or_explicitly_held(harness, missing, status):
    async def scenario():
        async with harness() as h:
            data = png("red")
            first = await h.send(data)
            async with h.sessions() as db:
                await db.execute(sa.update(Post).values(sha256=None, status=status))
                await db.commit()
            if missing:
                h.names("content", first["id"], "image/png")[0].unlink()
                with pytest.raises(HTTPException) as exc:
                    await h.send(data, "b", "pool-b")
                assert exc.value.status_code == 409
                assert exc.value.detail["code"] == "original_identity_unverified"
                assert await h.tags_of((await h.rows())[0].post_id) == ["a"]
                assert await h.memberships() == {("pool-a", first["id"])}
            else:
                again = await h.send(data, "b", "pool-b")
                assert again["id"] == first["id"] and again["duplicate"]
                assert (await h.rows())[0].sha256 == hashlib.sha256(data).digest()
    asyncio.run(scenario())


@pytest.mark.parametrize("legacy", [False, True])
def test_pending_collision_does_not_replace_existing_content(harness, monkeypatch, legacy):
    collide_md5(monkeypatch)
    async def scenario():
        async with harness() as h:
            a, b = png("red"), png("blue")
            first = await h.send(a)
            async with h.sessions() as db:
                values = dict(status="pending", data=None)
                if legacy:
                    values["sha256"] = None
                await db.execute(sa.update(Post).values(**values))
                await db.commit()
            second = await h.send(b, "b", "pool-b")
            assert second["id"] != first["id"]
            original = (await h.rows())[0]
            assert original.status == "pending" and await h.tags_of(original.post_id) == ["a"]
            assert h.names("content", first["id"], "image/png")[0].read_bytes() == a
    asyncio.run(scenario())


@pytest.mark.parametrize("conversion_fails", [False, True])
def test_concurrent_identical_uploads_preserve_all_requests(harness, monkeypatch, conversion_fails):
    if conversion_fails:
        async def fail(*args):
            return False
        monkeypatch.setattr(U, "_synchronous_materialize", fail)
    async def scenario():
        async with harness() as h:
            data = png("green")
            results = await asyncio.wait_for(asyncio.gather(*(
                h.send(data, f"tag-{i}", f"pool-{i}") for i in range(4))), timeout=20)
            ids = {r["id"] for r in results}
            assert len(ids) == 1
            assert sum(not r["duplicate"] for r in results) == 1
            rows = await h.rows()
            assert len(rows) == 1 and await h.tags_of(rows[0].post_id) == [f"tag-{i}" for i in range(4)]
            assert await h.memberships() == {(f"pool-{i}", rows[0].post_id) for i in range(4)}
    asyncio.run(scenario())


def test_unreadable_legacy_original_is_not_success(monkeypatch, tmp_path):
    original = tmp_path / "unreadable.png"
    monkeypatch.setattr(U.files, "names", lambda *args: [original])
    def unreadable(*args, **kwargs):
        raise PermissionError("unreadable")
    monkeypatch.setattr(Path, "open", unreadable)
    assert U._legacy_original_sha256(SimpleNamespace(post_id=1, mime_type="image/png")) is None


def test_later_legacy_match_is_not_hidden_by_oldest_collision(harness, monkeypatch):
    collide_md5(monkeypatch)
    async def scenario():
        async with harness() as h:
            a, b = png("red"), png("blue")
            await h.send(a)
            second = await h.send(b, "b", "pool-b")
            async with h.sessions() as db:
                await db.execute(sa.update(Post).values(sha256=None))
                await db.commit()
            again = await h.send(b, "c", "pool-c")
            assert again["id"] == second["id"] and again["duplicate"]
            assert [await h.tags_of(p.post_id) for p in await h.rows()] == [["a"], ["b", "c"]]
    asyncio.run(scenario())


def test_failed_materialization_still_records_identity_and_retries(harness, monkeypatch):
    materialize = U._synchronous_materialize
    async def fail(*args):
        return False
    async def scenario():
        async with harness() as h:
            monkeypatch.setattr(U, "_synchronous_materialize", fail)
            data = png("red")
            first = await h.send(data)
            assert first["status"] == "pending"
            assert (await h.rows())[0].sha256 == hashlib.sha256(data).digest()
            monkeypatch.setattr(U, "_synchronous_materialize", materialize)
            again = await h.send(data, "b", "pool-b")
            assert again["id"] == first["id"] and again["duplicate"] and again["status"] == "ok"
    asyncio.run(scenario())


def test_non_upload_sha256_race_reapplies_losing_request(harness, monkeypatch):
    materialize = U._synchronous_materialize
    async def scenario():
        async with harness() as h:
            winner_id = None
            async def competing_writer(db_, post, data, thumb):
                nonlocal winner_id
                # This writer intentionally bypasses the upload advisory lock.
                async with h.sessions() as db:
                    winner = Post(
                        version=1, creation_time=post.creation_time, safety="safe",
                        type=post.type, checksum=post.checksum, checksum_md5=post.checksum_md5,
                        mime_type=post.mime_type, sha256=hashlib.sha256(data).digest(),
                        status="ok")
                    db.add(winner)
                    await db.flush()
                    winner_id = winner.post_id
                    await db.execute(sa.text("INSERT INTO test_tag_membership VALUES (:id, 'winner')"), {"id": winner_id})
                    h.names("content", winner_id, "image/png")[0].write_bytes(data)
                    await db.commit()
                return await materialize(db_, post, data, thumb)
            monkeypatch.setattr(U, "_synchronous_materialize", competing_writer)
            result = await h.send(png("red"), "loser", "pool-loser")
            assert result["duplicate"] and result["id"] == winner_id
            rows = await h.rows()
            assert len(rows) == 1 and await h.tags_of(rows[0].post_id) == ["loser", "winner"]
            assert await h.memberships() == {("pool-loser", winner_id)}
    asyncio.run(scenario())


def test_ready_sha256_match_wins_over_older_pending_candidate(harness):
    async def scenario():
        async with harness() as h:
            data = png("red")
            first = await h.send(data)
            async with h.sessions() as db:
                oldest = (await db.execute(sa.select(Post))).unique().scalar_one()
                oldest.status = "pending"
                await db.flush()
                ready = Post(
                    version=1, creation_time=oldest.creation_time, safety="safe",
                    type=oldest.type, checksum=oldest.checksum, checksum_md5=oldest.checksum_md5,
                    mime_type=oldest.mime_type, sha256=oldest.sha256,
                    status="ok")
                db.add(ready)
                await db.flush()
                await db.execute(sa.text("INSERT INTO test_tag_membership VALUES (:id, 'ready')"), {"id": ready.post_id})
                await db.commit()
                ready_id = ready.post_id
            again = await h.send(data, "b", "pool-b")
            assert again["duplicate"] and again["id"] == ready_id
            rows = await h.rows()
            assert rows[0].post_id == first["id"] and rows[0].status == "pending"
            assert await h.tags_of(rows[0].post_id) == ["a"]
            assert await h.tags_of(rows[1].post_id) == ["b", "ready"]
    asyncio.run(scenario())


@pytest.mark.parametrize('animation',[False,True])
def test_pg_images_do_not_generate_a_second_filesystem_poster(harness,monkeypatch,animation):
    def redundant(*args):raise AssertionError('image poster was decoded twice')
    monkeypatch.setattr(U,'_generate_thumbnail',redundant)
    data=png('red')
    if animation:
        out=io.BytesIO()
        Image.new('RGB',(8,8),'red').save(out,'GIF',save_all=True,append_images=[Image.new('RGB',(8,8),'blue')],duration=100,loop=0)
        data=out.getvalue()
    async def scenario():
        async with harness() as h:
            result=await h.send(data)
            assert result['status']=='ok'
            async with h.sessions() as db:
                blob,thumb,location=(await db.execute(sa.select(Post.data,Post.thumbnail,Post.media_location).where(Post.post_id==result['id']))).one()
                assert blob is None and thumb is None and location=='pg'  # bytes are rendition files (0177)
                kinds={k:(bytes(sha),mime) for k,sha,mime in (await db.execute(sa.text(
                    "SELECT r.kind,o.sha256,o.mime FROM post_rendition r JOIN rendition_object o USING (sha256) WHERE r.post_id=:id"),
                    {'id':result['id']})).all()}
                assert set(kinds)=={'view','thumb'}
                with Image.open(U.renditions.path_for(*kinds['view'])) as image:
                    assert getattr(image,'n_frames',1)==(2 if animation else 1)
            assert not h.names('thumbnail',result['id'])[0].exists()
    asyncio.run(scenario())


def test_optional_pdf_poster_failure_does_not_publish_black_or_link_missing_alias(harness,monkeypatch):
    monkeypatch.setattr(U,'_generate_thumbnail',lambda *args:False)
    original_names=U.files.names
    monkeypatch.setattr(U.files,'names',lambda *args: original_names(*args)+[original_names(*args)[0].with_name(original_names(*args)[0].name+'.alias')])
    data=b'%PDF-1.4\nminimal fixture body\n'
    async def scenario():
        async with harness() as h:
            result=await h.send(data,file=UploadFile(io.BytesIO(data),filename='document.pdf'))
            assert result['status']=='ok'
            async with h.sessions() as db:
                blob,thumb,location=(await db.execute(sa.select(Post.data,Post.thumbnail,Post.media_location).where(Post.post_id==result['id']))).one()
                assert blob is None and thumb is None and location=='hdd'
    asyncio.run(scenario())


def test_an_extension_records_its_receipt_in_the_upload_transaction(harness):
    from api import extension_api
    async def scenario():
        async with harness() as h:
            seen = []
            async def record(db, ingested):
                seen.append(ingested.duplicate)
                await db.execute(sa.text("INSERT INTO test_tag_membership VALUES (:id, 'receipt') ON CONFLICT DO NOTHING"),
                                 {"id": ingested.post.post_id})
            for tag in ("a", "b"):
                async with h.sessions() as db:
                    result = await extension_api.upload(
                        db, UploadFile(io.BytesIO(png("red")), filename="a.png"), record=record, tags=tag)
            assert seen == [False, True] and result["duplicate"]
            assert await h.tags_of(result["id"]) == ["a", "b", "receipt"]
    asyncio.run(scenario())


def test_a_failing_receipt_rolls_the_upload_back_and_removes_its_files(harness):
    from api import extension_api
    async def scenario():
        async with harness() as h:
            written = []
            async def record(db, ingested):
                written.extend(h.names("content", ingested.post.post_id, "image/png"))
                assert all(path.exists() for path in written)
                raise HTTPException(422, "bad receipt")
            async with h.sessions() as db:
                with pytest.raises(HTTPException):
                    await extension_api.upload(
                        db, UploadFile(io.BytesIO(png("red")), filename="a.png"), record=record)
            assert await h.rows() == [] and written and not any(path.exists() for path in written)
    asyncio.run(scenario())


def test_a_commit_with_an_unknown_outcome_keeps_the_original(harness):
    # the server committed, then the confirmation was lost: the post may be stored
    async def scenario():
        async with harness() as h:
            async with h.sessions() as db:
                commit = db.commit
                async def committed_then_lost():
                    await commit()
                    raise ConnectionResetError("connection lost after COMMIT")
                db.commit = committed_then_lost
                with pytest.raises(ConnectionResetError):
                    await U.store(db, png("red"), filename="a.png", tags="a")
            [post] = await h.rows()
            assert h.names("content", post.post_id, "image/png")[0].read_bytes() == png("red")
    asyncio.run(scenario())
