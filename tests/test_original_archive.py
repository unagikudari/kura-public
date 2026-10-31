"""Original bytes remain immutable through offload, replacement and deletion."""
import hashlib
import io
import json
import os
from pathlib import Path
from types import SimpleNamespace
import pytest
from PIL import Image

from api.services import files as F
from scripts import original_archive as A


@pytest.fixture(autouse=True)
def _declared_plan_is_the_tests(monkeypatch):
    """Writers run only the plan tagger/deploy/nodes.json declares (tested on its own below);
    these tests build their own plans."""
    monkeypatch.setattr(A, "current_plan", lambda path=None: Path(path))


def png():
    b=io.BytesIO();Image.new('RGB',(8,8),'blue').save(b,format='PNG');return b.getvalue()


@pytest.fixture
def fixture(tmp_path,monkeypatch):
    source=tmp_path/'local'/'posts';source.mkdir(parents=True)
    root=tmp_path/'external';root.mkdir()
    monkeypatch.setattr(F,'DATA_DIR',source.parent);monkeypatch.setattr(F,'POSTS_DIR','posts')
    class Guard:
        def check(self):pass
    guard=Guard();guard.source=source;guard.root=root
    data=png();one=source/'1_0123456789abcdef.png';two=source/'1_fedcba9876543210.png'
    one.write_bytes(data);os.link(one,two);st=one.stat()
    row=dict(pid=1,dev=st.st_dev,ino=st.st_ino,size=st.st_size,mtime=st.st_mtime_ns,nlink=2,sha=hashlib.sha256(data).hexdigest(),md5=hashlib.md5(data).hexdigest())
    return SimpleNamespace(guard=guard,row=row,names=[one.name,two.name],one=one,two=two,data=data)


def test_verified_move_replaces_all_hardlinks_and_keeps_exact_original(fixture):
    f=fixture
    target,digest=A.verified_copy(f.one,f.guard,f.row)
    f.row['archive_sha']=digest
    A.switch_aliases(f.guard,f.row,f.names,target)
    assert f.one.is_symlink() and f.two.is_symlink()
    assert f.one.resolve()==f.two.resolve()==target and target.read_bytes()==f.data
    assert F.verify_names(f.one,f.two)
    # A normal replacement detaches BOTH aliases rather than mutating the CAS.
    F.write_all_names([f.one,f.two],b'replacement')
    assert not f.one.is_symlink() and os.path.samefile(f.one,f.two)
    assert f.one.read_bytes()==b'replacement' and target.read_bytes()==f.data
    F.delete_all_names([f.one,f.two]);assert target.read_bytes()==f.data


def test_deleting_archived_aliases_does_not_delete_cas_even_without_config(fixture,monkeypatch):
    f=fixture;target,digest=A.verified_copy(f.one,f.guard,f.row);f.row['archive_sha']=digest
    A.switch_aliases(f.guard,f.row,f.names,target)
    monkeypatch.setattr(F,'ORIGINAL_ARCHIVE_ROOT',None)
    F.delete_all_names([f.one,f.two])
    assert not f.one.exists() and not f.two.exists() and target.read_bytes()==f.data


def test_wrong_source_identity_never_switches_names(fixture):
    f=fixture;f.row['sha']='0'*64
    with pytest.raises(RuntimeError,match='SHA256 mismatch'):A.verified_copy(f.one,f.guard,f.row)
    assert not f.one.is_symlink() and f.one.read_bytes()==f.data


def test_changed_inode_refuses_alias_switch(fixture):
    f=fixture;target,digest=A.verified_copy(f.one,f.guard,f.row);f.row['archive_sha']=digest
    f.one.unlink();f.one.write_bytes(b'newer-original')
    with pytest.raises(RuntimeError,match='inode changed'):A.switch_aliases(f.guard,f.row,f.names,target)
    assert f.one.read_bytes()==b'newer-original'


def test_interrupted_alias_switch_is_resumable(fixture,monkeypatch):
    f=fixture;target,digest=A.verified_copy(f.one,f.guard,f.row);f.row['archive_sha']=digest
    original=A.os.replace;calls=[]
    def fail_second(src,dst):
        calls.append(dst)
        if len(calls)==2:raise OSError('simulated interruption')
        return original(src,dst)
    monkeypatch.setattr(A.os,'replace',fail_second)
    with pytest.raises(OSError):A.switch_aliases(f.guard,f.row,f.names,target)
    monkeypatch.setattr(A.os,'replace',original)
    A.switch_aliases(f.guard,f.row,f.names,target)
    assert f.one.is_symlink() and f.two.is_symlink() and target.read_bytes()==f.data


def test_destination_corruption_is_not_overwritten(fixture):
    f=fixture;digest=f.row['sha'];target=f.guard.root/'objects'/'sha256'/digest[:2]/digest
    target.parent.mkdir(parents=True);target.write_bytes(b'bad')
    with pytest.raises(RuntimeError,match='matching file'):A.verified_copy(f.one,f.guard,f.row)
    assert target.read_bytes()==b'bad' and not f.one.is_symlink()


def test_missing_or_same_filesystem_mount_is_refused(tmp_path):
    root=tmp_path/'external';source=tmp_path/'source';source.mkdir()
    with pytest.raises(RuntimeError):A.MountGuard(tmp_path,root,source)
    assert not root.exists()


def test_only_valid_static_webp_derivatives_pass():
    b=io.BytesIO();Image.new('RGB',(8,8),'red').save(b,format='WEBP')
    A.viewable(b.getvalue(),b.getvalue())
    with pytest.raises(Exception):A.viewable(b'broken',b.getvalue())
    with pytest.raises(RuntimeError):A.viewable(png(),b.getvalue())


def test_extra_unobserved_hardlink_refuses_reclaim(fixture,tmp_path):
    f=fixture;os.link(f.one,tmp_path/'hidden-alias')
    with pytest.raises(RuntimeError,match='Unobserved'):A.check_aliases(f.guard,f.row,f.names)


@pytest.mark.parametrize('interrupted',[False,True])
def test_real_database_inventory_and_move_are_idempotent(fixture,monkeypatch,database_url,interrupted):
    import psycopg2
    from psycopg2.extensions import make_dsn
    from uuid import uuid4
    f=fixture;schema='archive_'+uuid4().hex
    base=database_url.replace('+asyncpg','')
    admin=psycopg2.connect(base);admin.autocommit=True
    with admin.cursor() as q:q.execute('CREATE SCHEMA '+schema)
    dsn=make_dsn(base,options='-csearch_path='+schema)
    b=io.BytesIO();Image.new('RGB',(8,8),'blue').save(b,format='WEBP');webp=b.getvalue()
    try:
        with psycopg2.connect(dsn) as pg:
            with pg.cursor() as q:
                q.execute('''CREATE TABLE post(id int primary key,sha256 bytea,checksum_md5 text,checksum text,
                  "mime-type" text,file_size bigint,data bytea,thumbnail bytea,status text,type text,media_location text)''')
                # 0177: display bytes may be rendition files instead of the columns.
                q.execute('CREATE TABLE post_rendition(post_id int,kind text,sha256 bytea)')
                q.execute('CREATE TABLE rendition_object(sha256 bytea,mime text,byte_size bigint,width int,height int)')
                q.execute("INSERT INTO post VALUES(1,%s,%s,%s,'image/png',%s,%s,%s,'ok','image','pg')",(bytes.fromhex(f.row['sha']),f.row['md5'],f.row['md5'],len(f.data),webp,webp))
        monkeypatch.setattr(A,'DATABASE_URL_SYNC',dsn);monkeypatch.setattr(A,'ORIGINAL_ARCHIVE_ROOT',f.guard.root)
        monkeypatch.setattr(F,'names',lambda *args:[f.one,f.two])
        f.guard.header=lambda:dict(root=str(f.guard.root),source=str(f.guard.source),mount=str(f.guard.root))
        plan=f.guard.root/'plans'/'test.sqlite'
        before=A.inventory(plan,f.guard);assert before['pending']['files']==1
        monkeypatch.setattr(A,'MountGuard',lambda *args:f.guard)
        monkeypatch.setattr(A,'runtime_attestation',lambda:lambda:None)
        if interrupted:
            original=A.os.replace;count=[0]
            def fail_once(src,dst):
                count[0]+=1
                if count[0]==2:raise OSError('interruption')
                return original(src,dst)
            monkeypatch.setattr(A.os,'replace',fail_once)
            halfway=A.run_plan(plan,limit=1,seconds=20);assert halfway['verified']['files']==1
            monkeypatch.setattr(A.os,'replace',original)
        after=A.run_plan(plan,limit=1,seconds=20);assert after['done']['files']==1
        assert f.one.read_bytes()==f.data and f.one.is_symlink()
        assert A.run_plan(plan,limit=1,seconds=20)==after
        restored=A.restore(plan,1);assert restored['restored']['files']==1
        assert not f.one.is_symlink() and os.path.samefile(f.one,f.two) and f.one.read_bytes()==f.data
        with psycopg2.connect(dsn) as pg:
            with pg.cursor() as q:
                q.execute('SELECT data,sha256 FROM post WHERE id=1');data,sha=q.fetchone()
                assert bytes(data)==webp and bytes(sha).hex()==f.row['sha']
    finally:
        with admin.cursor() as q:q.execute('DROP SCHEMA '+schema+' CASCADE')
        admin.close()


from test_upload_original_identity import database_url  # noqa:F401


def test_offline_cas_pointer_is_detached_without_writing_to_missing_mount(fixture):
    f=fixture
    f.one.unlink();f.two.unlink()
    offline=f.guard.root/'unmounted'/'objects'/'sha256'/f.row['sha'][:2]/f.row['sha']
    f.one.symlink_to(offline);f.two.symlink_to(offline)
    F.write_all_names([f.one,f.two],b'new local bytes')
    assert f.one.read_bytes()==b'new local bytes' and os.path.samefile(f.one,f.two)
    assert not (f.guard.root/'unmounted').exists()


@pytest.mark.parametrize('db_mime,eligible',[('image/webp',True),('image/png',True)])
@pytest.mark.parametrize('representation',['legacy','rendition','corrupt','missing'])
def test_a_png_original_of_a_webp_display_row_is_eligible(fixture,monkeypatch,database_url,db_mime,eligible,representation):
    # post."mime-type" names the display WebP for pg rows. The 2026-10-19 plan
    # excluded every .jpg/.png original of such rows as not_original_names
    # because the expected names carried a .webp extension.
    import psycopg2
    from psycopg2.extensions import make_dsn
    from uuid import uuid4
    f=fixture;schema='archive_'+uuid4().hex
    base=database_url.replace('+asyncpg','')
    admin=psycopg2.connect(base);admin.autocommit=True
    with admin.cursor() as q:q.execute('CREATE SCHEMA '+schema)
    dsn=make_dsn(base,options='-csearch_path='+schema)
    b=io.BytesIO();Image.new('RGB',(8,8),'blue').save(b,format='WEBP');webp=b.getvalue()
    monkeypatch.setattr(A.renditions,'ROOT',f.guard.source.parent/'renditions')
    stored=A.renditions.store(webp) if representation!='legacy' else None
    try:
        with psycopg2.connect(dsn) as pg:
            with pg.cursor() as q:
                q.execute('''CREATE TABLE post(id int primary key,sha256 bytea,checksum_md5 text,checksum text,
                  "mime-type" text,file_size bigint,data bytea,thumbnail bytea,status text,type text,media_location text)''')
                q.execute('CREATE TABLE post_rendition(post_id int,kind text,sha256 bytea)')
                q.execute('CREATE TABLE rendition_object(sha256 bytea PRIMARY KEY,mime text,byte_size bigint,width int,height int)')
                if stored:
                    q.execute('INSERT INTO rendition_object VALUES(%s,%s,%s,%s,%s)',
                              (stored.sha256,stored.mime,stored.byte_size,stored.width,stored.height))
                    q.execute("INSERT INTO post_rendition VALUES(1,'view',%s),(1,'thumb',%s)",
                              (stored.sha256,stored.sha256))
                q.execute("INSERT INTO post VALUES(1,%s,%s,%s,%s,%s,%s,%s,'ok','image','pg')",
                          (bytes.fromhex(f.row['sha']),f.row['md5'],f.row['md5'],db_mime,len(f.data),
                           None if stored else webp,None if stored else webp))
        monkeypatch.setattr(A,'DATABASE_URL_SYNC',dsn);monkeypatch.setattr(A,'ORIGINAL_ARCHIVE_ROOT',f.guard.root)
        ext={'image/webp':'.webp','image/png':'.png'}
        # The real derivation: extension from the mime, hash part from the salt.
        monkeypatch.setattr(F,'names',lambda kind,pid,mime=None:[p.with_suffix(ext[mime]) for p in (f.one,f.two)])
        f.guard.header=lambda:dict(root=str(f.guard.root),source=str(f.guard.source),mount=str(f.guard.root))
        plan=f.guard.root/'plans'/'mime.sqlite'
        out=A.inventory(plan,f.guard)
        assert out['pending']['files']==(1 if eligible else 0)
        monkeypatch.setattr(A,'MountGuard',lambda *args:f.guard)
        monkeypatch.setattr(A,'runtime_attestation',lambda:lambda:None)
        if representation=='corrupt':stored.path.write_bytes(b'bad rendition')
        if representation=='missing':stored.path.unlink()
        if representation in ('corrupt','missing'):
            assert A.run_plan(plan,limit=1,seconds=20)['refused']['files']==1
            assert not f.one.is_symlink() and f.one.read_bytes()==f.data
            assert not f.two.is_symlink() and f.two.read_bytes()==f.data
            return
        assert A.run_plan(plan,limit=1,seconds=20)['done']['files']==1
        assert f.one.is_symlink() and f.one.read_bytes()==f.data
        assert A.restore(plan,1)['restored']['files']==1
        assert not f.one.is_symlink() and f.one.read_bytes()==f.data
        assert not f.one.with_suffix('.webp').exists()
    finally:
        with admin.cursor() as q:q.execute('DROP SCHEMA '+schema+' CASCADE')
        admin.close()


def test_the_motion_check_lets_an_animated_original_go_only_behind_a_moving_view():
    from PIL import ImageSequence
    from api.services.media_materialize import animated_webp_bytes
    frames = [Image.new("RGB", (8, 8), c) for c in ("red", "blue", "green")]
    b = io.BytesIO(); frames[0].save(b, "GIF", save_all=True, append_images=frames[1:], duration=[40, 80, 120], loop=0)
    anim = io.BytesIO(b.getvalue())
    view = animated_webp_bytes(Image.open(io.BytesIO(b.getvalue())))
    A.check_motion(anim, {"type": "animation"}, view)                      # keeps its motion
    with pytest.raises(RuntimeError, match="motion-preserving policy"):
        A.check_motion(anim, {"type": "image"}, view)                      # frozen to frame 0
    still = io.BytesIO(); frames[0].save(still, "WEBP")
    with pytest.raises(RuntimeError, match="does not keep"):
        A.check_motion(anim, {"type": "animation"}, still.getvalue())
    png = io.BytesIO(); Image.new("RGB", (8, 8)).save(png, "PNG")
    A.check_motion(png, {"type": "image"}, None)                            # a still image
    with pytest.raises(RuntimeError, match="still original"):
        A.check_motion(png, {"type": "animation"}, view)


def test_a_motion_answer_is_reused_only_for_the_same_two_contents(tmp_path, monkeypatch):
    from api.services.media_materialize import animated_webp_bytes
    frames = [Image.new("RGB", (8, 8), c) for c in ("red", "blue")]
    b = io.BytesIO(); frames[0].save(b, "GIF", save_all=True, append_images=frames[1:], duration=[40, 80], loop=0)
    f = tmp_path / "a.gif"; f.write_bytes(b.getvalue())
    view = animated_webp_bytes(Image.open(f))
    calls = []
    real = A.motion_signature
    monkeypatch.setattr(A, "motion_signature", lambda im: calls.append(1) or real(im))
    monkeypatch.setattr(A, "_MOTION", {})
    A.check_motion(f, {"type": "animation"}, view); A.check_motion(f, {"type": "animation"}, view)
    assert len(calls) == 2                                                 # one decode of each, once
    still = io.BytesIO(); frames[0].save(still, "WEBP")
    with pytest.raises(RuntimeError, match="does not keep"):               # a changed view is checked anew
        A.check_motion(f, {"type": "animation"}, still.getvalue())
    assert len(calls) == 4
    entry = A.motion_entry(str(f), view)                                   # what a worker returns
    assert entry == ((hashlib.sha256(f.read_bytes()).hexdigest(), hashlib.sha256(view).hexdigest()), True)


def test_the_view_check_follows_the_post_type():
    from api.services.media_materialize import animated_webp_bytes
    frames = [Image.new("RGB", (8, 8), c) for c in ("red", "blue")]
    b = io.BytesIO(); frames[0].save(b, "GIF", save_all=True, append_images=frames[1:], duration=50, loop=0)
    moving = animated_webp_bytes(Image.open(io.BytesIO(b.getvalue())))
    still = io.BytesIO(); frames[0].save(still, "WEBP"); still = still.getvalue()
    A.viewable(moving, still, "animation")
    A.viewable(still, still, "image")
    with pytest.raises(RuntimeError):A.viewable(moving, still, "image")
    with pytest.raises(RuntimeError):A.viewable(still, still, "animation")
    with pytest.raises(RuntimeError):A.viewable(moving, moving, "animation")   # the thumbnail stays still


def test_display_bytes_are_read_in_the_order_the_api_serves_them(tmp_path, monkeypatch):
    """Codex 1.276.0: the archive checked the legacy column while the API serves the rendition."""
    import hashlib
    rendition = io.BytesIO(); Image.new("RGB", (4, 4), "blue").save(rendition, "WEBP"); rendition = rendition.getvalue()
    path = tmp_path / "r.webp"; path.write_bytes(rendition)
    sha = hashlib.sha256(rendition).digest()
    monkeypatch.setattr(A.renditions, "path_for", lambda s, m: path)
    class DB:
        def execute(self, *a): pass
        def fetchone(self): return {"sha256": sha, "mime": "image/webp"}
    legacy = b"stale legacy bytes"
    assert A._display_blobs(DB(), {"id": 1, "data": legacy, "thumbnail": legacy}) == [rendition, rendition]
    class NoRendition(DB):
        def fetchone(self): return None
    assert A._display_blobs(NoRendition(), {"id": 1, "data": legacy, "thumbnail": legacy}) == [legacy, legacy]


def _orphan_setup(f, monkeypatch, tmp_path, present=frozenset({1})):
    import contextlib
    gif = io.BytesIO()
    frames = [Image.new('RGB', (8, 8), c) for c in ('red', 'blue')]
    frames[0].save(gif, 'GIF', save_all=True, append_images=frames[1:], duration=50, loop=0)
    orphan = f.guard.source / '2_00000000000000aa.gif'; orphan.write_bytes(gif.getvalue())
    os.link(orphan, f.guard.source / '2_00000000000000bb.gif')
    class Cur:
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def execute(self, sql, params): self.params = params
        def __iter__(self): return iter([(i,) for i in self.params[0] if i in present])
        def fetchone(self): return (1,) if self.params[0] in present else None
    class PG:
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def cursor(self): return Cur()
        def rollback(self): pass
        def close(self): pass
    monkeypatch.setattr(A.psycopg2, 'connect', lambda *a, **k: PG())
    monkeypatch.setattr(A, 'MountGuard', lambda mount, root, source: f.guard)
    monkeypatch.setattr(A, 'archive_lock', lambda guard: contextlib.nullcontext())
    monkeypatch.setattr(F, 'content_lock', lambda pid: contextlib.nullcontext())
    return orphan, f.guard.root / 'orphans' / 'manifest.jsonl'


def _sealed():
    import contextlib
    return contextlib.nullcontext(), 'dsn', {'PGPASSWORD': 'x'}


def test_orphans_are_copied_into_the_cas_with_a_manifest_and_left_in_place(fixture, monkeypatch, tmp_path):
    f = fixture; orphan, manifest = _orphan_setup(f, monkeypatch, tmp_path)
    out = A.orphans('/m', f.guard.root, f.guard.source)
    assert out == {'files': 2, 'copied': 1, 'removed': 0, 'skipped': 0}       # two names, one inode
    lines = [json.loads(l) for l in manifest.read_text().splitlines()]
    assert {l['name'] for l in lines} == {'2_00000000000000aa.gif', '2_00000000000000bb.gif'}
    digest = hashlib.sha256(orphan.read_bytes()).hexdigest()
    assert (f.guard.root / 'objects' / 'sha256' / digest[:2] / digest).read_bytes() == orphan.read_bytes()
    assert orphan.exists() and f.one.exists()                                   # nothing removed, post 1 untouched


def test_removal_needs_a_proven_seal(fixture, monkeypatch, tmp_path):
    f = fixture; orphan, manifest = _orphan_setup(f, monkeypatch, tmp_path)
    with pytest.raises(RuntimeError, match='needs a seal'):                     # this module has none
        A.orphans('/m', f.guard.root, f.guard.source, remove=True)
    def unsealed(): raise RuntimeError('kura is not sealed: kura can still connect')
    with pytest.raises(RuntimeError, match='not sealed'):
        A.orphans('/m', f.guard.root, f.guard.source, remove=True, seal=unsealed)
    assert orphan.exists()
    out = A.orphans('/m', f.guard.root, f.guard.source, remove=True, seal=_sealed)
    assert out['removed'] == 2 and not orphan.exists() and f.one.exists()


def test_a_file_changed_between_its_names_is_copied_again(fixture, monkeypatch, tmp_path):
    f = fixture; orphan, manifest = _orphan_setup(f, monkeypatch, tmp_path)
    real_copy = A.verified_copy
    def copy_then_change(path, guard, row, **kw):
        out = real_copy(path, guard, row, **kw)
        if len(calls) == 0:
            calls.append(1)
            data = orphan.read_bytes(); orphan.write_bytes(data[:-1] + bytes([data[-1] ^ 1]))   # same size, new bytes
        return out
    calls = []
    monkeypatch.setattr(A, 'verified_copy', copy_then_change)
    out = A.orphans('/m', f.guard.root, f.guard.source)
    lines = [json.loads(l) for l in manifest.read_text().splitlines()]
    assert out['copied'] == 2 and lines[0]['sha256'] != lines[1]['sha256']


def test_a_file_replaced_between_reads_cannot_file_one_answer_under_another_key(tmp_path, monkeypatch):
    """Codex 1.280.2: the digest and the decode come from one read of the original."""
    from api.services.media_materialize import animated_webp_bytes
    frames = [Image.new("RGB", (8, 8), c) for c in ("red", "blue")]
    a = io.BytesIO(); frames[0].save(a, "GIF", save_all=True, append_images=frames[1:], duration=[40, 80], loop=0)
    b = io.BytesIO(); frames[0].save(b, "GIF", save_all=True, append_images=frames[1:], duration=[400, 800], loop=0)
    f = tmp_path / "a.gif"; f.write_bytes(a.getvalue())
    view = animated_webp_bytes(Image.open(io.BytesIO(a.getvalue())))
    real_open = A.Image.open
    def swap_then_open(fp, *args, **kw):
        f.write_bytes(b.getvalue())                    # replaced after the read, before the decode
        return real_open(fp, *args, **kw)
    monkeypatch.setattr(A.Image, "open", swap_then_open)
    key, same = A.motion_entry(str(f), view)
    assert key[0] == hashlib.sha256(a.getvalue()).hexdigest() and same    # A's key, A's answer


@pytest.mark.parametrize('cut', ['after_primary', 'before_ledger', 'changed_local'])
def test_a_restore_cut_off_partway_is_finished_by_running_it_again(fixture, monkeypatch, database_url, cut):
    """#151: a restore cut off after the primary name, or after every name but before its
    ledger row, is finished by the next restore; a local file whose bytes are not the original
    (same size) is refused and left as it is."""
    import psycopg2
    from psycopg2.extensions import make_dsn
    from uuid import uuid4
    f = fixture; schema = 'archive_' + uuid4().hex
    base = database_url.replace('+asyncpg', '')
    admin = psycopg2.connect(base); admin.autocommit = True
    with admin.cursor() as q: q.execute('CREATE SCHEMA ' + schema)
    dsn = make_dsn(base, options='-csearch_path=' + schema)
    b = io.BytesIO(); Image.new('RGB', (8, 8), 'blue').save(b, format='WEBP'); webp = b.getvalue()
    try:
        with psycopg2.connect(dsn) as pg:
            with pg.cursor() as q:
                q.execute('''CREATE TABLE post(id int primary key,sha256 bytea,checksum_md5 text,checksum text,
                  "mime-type" text,file_size bigint,data bytea,thumbnail bytea,status text,type text,media_location text)''')
                q.execute('CREATE TABLE post_rendition(post_id int,kind text,sha256 bytea)')
                q.execute('CREATE TABLE rendition_object(sha256 bytea,mime text,byte_size bigint,width int,height int)')
                q.execute("INSERT INTO post VALUES(1,%s,%s,%s,'image/png',%s,%s,%s,'ok','image','pg')",
                          (bytes.fromhex(f.row['sha']), f.row['md5'], f.row['md5'], len(f.data), webp, webp))
        monkeypatch.setattr(A, 'DATABASE_URL_SYNC', dsn); monkeypatch.setattr(A, 'ORIGINAL_ARCHIVE_ROOT', f.guard.root)
        monkeypatch.setattr(F, 'names', lambda *args: [f.one, f.two])
        f.guard.header = lambda: dict(root=str(f.guard.root), source=str(f.guard.source), mount=str(f.guard.root))
        plan = f.guard.root / 'plans' / 'test.sqlite'
        A.inventory(plan, f.guard)
        monkeypatch.setattr(A, 'MountGuard', lambda *args: f.guard)
        monkeypatch.setattr(A, 'runtime_attestation', lambda: lambda: None)
        assert A.run_plan(plan, limit=1, seconds=20)['done']['files'] == 1
        assert f.one.is_symlink() and f.two.is_symlink()
        if cut == 'changed_local':
            f.one.unlink(); f.one.write_bytes(bytes(len(f.data)))           # same size, other bytes
            with pytest.raises(RuntimeError, match='Active original names changed'):
                A.restore(plan, 1)
            assert f.one.read_bytes() == bytes(len(f.data)) and f.two.is_symlink()
            return
        if cut == 'after_primary':
            real = F.link_secondary
            def cut_here(*a, **k): raise OSError('cut after the primary name')
            monkeypatch.setattr(F, 'link_secondary', cut_here)
        else:
            real = A.fsync_dir
            def cut_here(*a, **k): raise OSError('cut before the ledger row')
            monkeypatch.setattr(A, 'fsync_dir', cut_here)
        with pytest.raises(OSError):
            A.restore(plan, 1)
        links = sorted(p.is_symlink() for p in (f.one, f.two))
        # cut after the primary: one name restored, one still the archive; cut before the
        # ledger: both restored (the plan's alias order decides which name is the primary)
        assert links == ([False, True] if cut == 'after_primary' else [False, False])
        assert all(p.read_bytes() == f.data for p in (f.one, f.two))
        monkeypatch.setattr(F if cut == 'after_primary' else A, 'link_secondary' if cut == 'after_primary' else 'fsync_dir', real)
        restored = A.restore(plan, 1)
        assert restored['restored']['files'] == 1
        assert not f.one.is_symlink() and os.path.samefile(f.one, f.two) and f.one.read_bytes() == f.data
    finally:
        with admin.cursor() as q: q.execute('DROP SCHEMA ' + schema + ' CASCADE')
        admin.close()
