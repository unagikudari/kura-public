"""The archive on another host (docs/notes/kura-on-nodeb-plan.md, rev 8): nodeb offers, primary
reads and places with the same verified half as a local run, nodeb reads the object back and
only then relinks under its locks. One process plays both nodes; the post-row checks and the
archive mount are stubbed (the local-run tests cover them)."""
from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image, ImageSequence

from api.services import files as F
from scripts import original_archive as A


@pytest.fixture(autouse=True)
def _declared_plan_is_the_tests(monkeypatch):
    """Writers run only the plan tagger/deploy/nodes.json declares (tested on its own below);
    these tests build their own plans."""
    monkeypatch.setattr(A, "current_plan", lambda path=None: Path(path))

ARCHIVE_ID = str(uuid.uuid4())


def png(colour="blue"):
    b = io.BytesIO(); Image.new("RGB", (8, 8), colour).save(b, format="PNG"); return b.getvalue()


@pytest.fixture
def nodes(tmp_path, monkeypatch):
    # nodeb: the source
    source = tmp_path / "nodeb" / "posts"; source.mkdir(parents=True)
    monkeypatch.setattr(F, "DATA_DIR", source.parent); monkeypatch.setattr(F, "POSTS_DIR", "posts")
    data = png(); one = source / "1_0123456789abcdef.png"; two = source / "1_fedcba9876543210.png"
    one.write_bytes(data); os.link(one, two); st = one.stat()
    archive_root = tmp_path / "primary" / "kura-original-archive"; archive_root.mkdir(parents=True)
    (archive_root / ".archive-id").write_text(ARCHIVE_ID)
    monkeypatch.setattr(A, "ORIGINAL_ARCHIVE_ROOT", archive_root)
    monkeypatch.setattr("api.config.ARCHIVE_ID", ARCHIVE_ID)
    plan_path = tmp_path / "nodeb" / "state" / "archive-plan.sqlite"; plan_path.parent.mkdir(parents=True)
    plan = A.connect_plan(plan_path)
    plan.executescript("""CREATE TABLE meta (header text);
      CREATE TABLE objects (dev int,ino int,pid int,ext text,size int,mtime int,nlink int,allocated int,
        status text DEFAULT 'unclassified',reason text,sha text,md5 text,archive_sha text,PRIMARY KEY(dev,ino));
      CREATE TABLE aliases (name text PRIMARY KEY,dev int,ino int);""")
    guard = A.SourceGuard(source, archive_root, ARCHIVE_ID)
    plan.execute("INSERT INTO meta VALUES (?)", (json.dumps(guard.header()),))
    plan.execute("INSERT INTO objects VALUES (?,?,?,?,?,?,?,?,'pending',NULL,?,?,NULL)",
                 (st.st_dev, st.st_ino, 1, "png", st.st_size, st.st_mtime_ns, 2, 0,
                  hashlib.sha256(data).hexdigest(), hashlib.md5(data).hexdigest()))
    for n in (one, two):
        plan.execute("INSERT INTO aliases VALUES (?,?,?)", (n.name, st.st_dev, st.st_ino))
    plan.commit(); plan.close()

    # the database checks are the local run's (tested there): accept here
    monkeypatch.setattr(A, "locked_eligible_post", lambda db, row, names: {"type": "image", "view": None})

    class _PG:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def cursor(self, **k): return contextlib.nullcontext(None)
        def close(self): pass
    monkeypatch.setattr(A.psycopg2, "connect", lambda *a, **k: _PG())

    # primary: the archive, no real mount
    class Guard:
        root = archive_root; source = None; identity = ARCHIVE_ID
        def check(self): pass
    monkeypatch.setattr(A, "MountGuard", lambda mount, root, source, expected=None: Guard())
    monkeypatch.setattr(A.shutil, "disk_usage", lambda p: SimpleNamespace(free=10 ** 15))

    def read_back(o, digest):   # what kura-archive-objects would serve: the placed object
        obj = archive_root / "objects" / "sha256" / digest[:2] / digest
        assert hashlib.sha256(obj.read_bytes()).hexdigest() == digest
    monkeypatch.setattr(A, "_read_back", read_back)
    monkeypatch.setattr(A, "remote_attestation", lambda: (lambda: None))

    calls = []

    def transport(command, stdout=None):   # ssh → the forced command on nodeb
        calls.append(command)
        try:
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                A.remote_dispatch(command, plan_path, out=stdout)
            return 0, buf.getvalue()
        except Exception as exc:
            calls.append(f"ERROR {type(exc).__name__}: {exc}")
            return 3, ""
    return SimpleNamespace(source=source, one=one, two=two, data=data, root=archive_root,
                           plan=plan_path, transport=transport, calls=calls)


def _offers(n):
    plan = A.connect_plan(n.plan)
    return [dict(r) for r in plan.execute("SELECT * FROM offers")]


def test_an_original_is_archived_on_the_other_host_and_relinked(nodes):
    n = nodes
    out = A.run_remote("/m", n.root, n.transport, expected_archive_id=ARCHIVE_ID)
    assert out == {"done": 1, "refused": 0}
    digest = hashlib.sha256(n.data).hexdigest()
    target = n.root / "objects" / "sha256" / digest[:2] / digest
    assert target.read_bytes() == n.data
    for name in (n.one, n.two):
        assert name.is_symlink() and os.readlink(name) == str(target)
    [o] = _offers(n)
    assert o["state"] == "committed" and o["archive_sha"] == digest
    assert A.remote_commit(n.plan, o["offer_id"], digest) == "already committed"
    with pytest.raises(RuntimeError, match="another digest"):
        A.remote_commit(n.plan, o["offer_id"], "0" * 64)


def test_a_source_changed_during_the_read_is_discarded(nodes, monkeypatch):
    """The signature differs at the check after streaming: remote-read exits non-zero and primary
    discards what it staged; nothing is committed and the alias stays a file."""
    n = nodes
    real_read = A.remote_read
    def changing_read(plan_path, offer_id, out):
        real_sig = A.signature
        calls = []
        def sig(st):
            calls.append(1)
            s = real_sig(st)
            return s if len(calls) == 1 else (s[0], s[1], s[2], s[3] + 1)
        monkeypatch.setattr(A, "signature", sig)
        try:
            return real_read(plan_path, offer_id, out)
        finally:
            monkeypatch.setattr(A, "signature", real_sig)
    monkeypatch.setattr(A, "remote_read", changing_read)
    out = A.run_remote("/m", n.root, n.transport)
    assert out == {"done": 0, "refused": 1}
    assert not n.one.is_symlink() and n.one.read_bytes() == n.data
    assert not any("remote-commit" in c for c in n.calls)
    assert _offers(n)[0]["state"] == "refused"
    assert not list((n.root / "staging").glob("*"))      # the staged bytes were discarded


def test_a_failed_read_back_switches_nothing_and_the_retry_completes(nodes, monkeypatch):
    n = nodes
    monkeypatch.setattr(A, "_read_back", lambda o, d: (_ for _ in ()).throw(RuntimeError("archive away")))
    assert A.run_remote("/m", n.root, n.transport) == {"done": 0, "refused": 1}
    assert not n.one.is_symlink()
    assert _offers(n)[0]["state"] == "read"            # still open: offered again next run
    monkeypatch.undo  # noqa: B018 - restore below explicitly
    digest = hashlib.sha256(n.data).hexdigest()
    monkeypatch.setattr(A, "_read_back", lambda o, d: None)
    assert A.run_remote("/m", n.root, n.transport) == {"done": 1, "refused": 0}
    assert n.one.is_symlink() and n.two.is_symlink()


def test_a_stop_mid_switch_converges_through_the_runner(nodes, monkeypatch):
    """Codex 1.270.0: after a stop between the switch and the committed record, the next run
    read the offer again, found only links and refused it. The digest is now recorded first
    and the runner resumes with a commit."""
    n = nodes
    real = A.os.replace
    count = []
    def once(src, dst):
        count.append(dst)
        if len(count) == 2:
            raise OSError("stopped mid-switch")
        return real(src, dst)
    monkeypatch.setattr(A.os, "replace", once)
    assert A.run_remote("/m", n.root, n.transport)["done"] == 0
    assert n.one.is_symlink() != n.two.is_symlink()     # half switched
    assert _offers(n)[0]["state"] == "committing"
    monkeypatch.setattr(A.os, "replace", real)
    assert A.run_remote("/m", n.root, n.transport) == {"done": 1, "refused": 0}
    assert n.one.is_symlink() and n.two.is_symlink()
    assert _offers(n)[0]["state"] == "committed"


def test_a_terminal_state_never_goes_back(nodes):
    n = nodes
    A.run_remote("/m", n.root, n.transport)
    [o] = _offers(n)
    A._refuse(A.connect_plan(n.plan), o["offer_id"], "late refusal")
    assert _offers(n)[0]["state"] == "committed"


def test_the_read_back_never_uses_the_cache(nodes, tmp_path, monkeypatch):
    """Codex 1.270.0: original_bytes answered from the cache while the archive was down. The
    module's own _read_back (the fixture stubs it) must fail with the archive unreachable even
    though a verified copy sits in the cache."""
    from api import config
    from api.services import originals
    n = nodes
    monkeypatch.setattr(config, "ARCHIVE_OBJECTS_URL", "http://127.0.0.1:9")    # nothing listens
    monkeypatch.setattr(config, "ARCHIVE_ID", ARCHIVE_ID)
    monkeypatch.setattr(config, "ORIGINAL_CACHE_DIR", tmp_path / "cache")
    digest = hashlib.sha256(n.data).hexdigest()
    loc = originals.Location("cas", Path(""), sha=digest)
    ident = originals.Identity(digest, None, len(n.data))
    cached = originals._cache_path(loc, ident); cached.parent.mkdir(parents=True); cached.write_bytes(n.data)
    with pytest.raises(RuntimeError, match="Read-back failed"):
        _module_read_back()({"sha": digest, "md5": None, "size": len(n.data)}, digest)


def _module_read_back():
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "original_archive_fresh", Path(__file__).resolve().parents[1] / "scripts" / "original_archive.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod._read_back


def test_workers_that_do_not_read_remotely_block_the_switch(monkeypatch):
    import contextlib as cl
    import scripts.media_replication_gate as gate
    from api import config
    monkeypatch.setenv("KURA_LOCAL_URL", "http://127.0.0.1:8001")
    monkeypatch.setattr(config, "ARCHIVE_ID", ARCHIVE_ID)
    monkeypatch.setattr(gate, "_serving_worker_pids", lambda unit, port: {11, 12})
    import scripts.kura_http_key as key
    monkeypatch.setattr(key, "install_api_key_opener", lambda: None)
    answers = iter([{"pid": 11, "original_reader": {"remote": True, "archive_id": ARCHIVE_ID}},
                    {"pid": 12, "original_reader": {"remote": False, "archive_id": None}}])
    monkeypatch.setattr(A.urllib.request, "urlopen",
                        lambda url, timeout: cl.nullcontext(io.BytesIO(json.dumps(next(answers)).encode())))
    with pytest.raises(RuntimeError, match="does not read archived originals"):
        A.remote_attestation()


def test_the_forced_command_runs_with_the_nodes_environment(tmp_path):
    """Codex 1.270.0: an ssh forced command inherits no EnvironmentFile."""
    import subprocess
    import sys
    env_file = tmp_path / "api.env"
    env_file.write_text("DATA_DIR=/srv/kura-data\n"
                        "DATABASE_URL=postgresql+asyncpg://kura:secret@127.0.0.1:5433/kura\n"
                        "KURA_ORIGINAL_ARCHIVE_ID=" + ARCHIVE_ID + "\n")
    archive_env = tmp_path / "archive.env"
    archive_env.write_text("")
    declaration = tmp_path / "nodes.json"; declaration.write_text('{"nodes": {}}')
    script = Path(__file__).resolve().parents[1] / "scripts" / "original_archive_dispatch.sh"
    out = subprocess.run([str(script)], capture_output=True, text=True, timeout=120,
                         env={"PATH": "/usr/bin:/bin", "HOME": str(tmp_path), "SSH_ORIGINAL_COMMAND": "remote-status",
                              "KURA_API_ENV": str(env_file), "KURA_ARCHIVE_ENV": str(archive_env),
                              "KURA_NODES_DECLARATION": str(declaration),
                              "KURA_PYTHON": sys.executable})
    assert out.returncode == 0, out.stderr[-1500:]
    status = json.loads(out.stdout.strip().splitlines()[-1])
    assert status["data_dir"] == "/srv/kura-data" and status["db_port"] == 5433
    assert status["archive_id"] == ARCHIVE_ID and "secret" not in out.stdout


def test_the_forced_command_accepts_only_its_three_commands(nodes):
    for bad in ("rm -rf /", "remote-offer --limit 9999", "remote-read --offer ../x",
                "remote-commit --offer x --sha y", "remote-offer", "remote-status --x"):
        with pytest.raises(RuntimeError):
            A.remote_dispatch(bad, nodes.plan, out=io.BytesIO())


def test_an_md5_only_offer_is_read_back_against_its_md5(nodes, monkeypatch):
    """Codex 1.270.1: the requested digest was passed as the SHA-256, so the MD5 was never
    checked and other bytes of the same size could be committed."""
    from api.services import originals
    seen = []
    monkeypatch.setattr(originals, "fetch_fresh", lambda loc, ident: seen.append(ident) or b"")
    o = {"sha": None, "md5": "0" * 32, "size": 10}
    with pytest.raises(RuntimeError):
        _module_read_back()(o, "a" * 64)            # b"" does not hash to the digest
    assert seen and seen[0].sha256 is None and seen[0].md5 == "0" * 32


def test_a_concurrent_commit_cannot_rebind_the_digest(nodes, monkeypatch):
    n = nodes
    plan = A.connect_plan(n.plan)
    digest = hashlib.sha256(n.data).hexdigest()
    real_commit = A.remote_commit
    # Stop after the read, then let "another process" bind the offer to another digest
    # between our snapshot and our update.
    monkeypatch.setattr(A, "remote_commit", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("held")))
    A.run_remote("/m", n.root, n.transport)
    monkeypatch.setattr(A, "remote_commit", real_commit)
    [o] = _offers(n)
    assert o["state"] == "read"
    def racing_read_back(offer, d):
        with plan:
            plan.execute("UPDATE offers SET state='committing',archive_sha=? WHERE offer_id=?", ("b" * 64, o["offer_id"]))
    with pytest.raises(RuntimeError, match="concurrently"):
        A.remote_commit(n.plan, o["offer_id"], digest, read_back=racing_read_back)
    assert _offers(n)[0]["archive_sha"] == "b" * 64     # the other binding stands
    with pytest.raises(RuntimeError, match="another digest"):
        A.remote_commit(n.plan, o["offer_id"], digest)


def test_a_permanent_failure_before_any_switch_closes_the_offer(nodes, monkeypatch):
    """Codex 1.270.1: a post deleted or replaced after the read left the offer 'committing'
    forever, and such offers filled the limit."""
    n = nodes
    calls = []
    def gone_after_offer(db, row, names):        # eligible when offered, deleted by the commit
        calls.append(1)
        if len(calls) > 1:
            raise RuntimeError("Post metadata changed")
        return {"type": "image", "view": None}
    monkeypatch.setattr(A, "locked_eligible_post", gone_after_offer)
    assert A.run_remote("/m", n.root, n.transport) == {"done": 0, "refused": 1}
    [o] = _offers(n)
    assert o["state"] == "refused" and o["reason"].startswith("before switch")
    assert not n.one.is_symlink() and not n.two.is_symlink()
    assert A.remote_offer(n.plan, 10) == []             # not offered again as it is


def test_a_commit_that_cannot_get_the_lock_does_not_refuse_a_running_one(nodes, monkeypatch):
    """Codex 1.270.2: a second commit of the same offer and digest failed to take the source
    lock, saw no alias switched yet and refused the offer under the first one."""
    import fcntl
    n = nodes
    real_commit = A.remote_commit
    monkeypatch.setattr(A, "remote_commit", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("held")))
    A.run_remote("/m", n.root, n.transport)
    monkeypatch.setattr(A, "remote_commit", real_commit)
    [o] = _offers(n)
    digest = hashlib.sha256(n.data).hexdigest()
    lock_path = n.plan.parent / ".archive-remote.lock"
    with lock_path.open("a") as held:                       # the first commit holds the lock
        fcntl.flock(held, fcntl.LOCK_EX)
        with pytest.raises(OSError):
            A.remote_commit(n.plan, o["offer_id"], digest)
    assert _offers(n)[0]["state"] == "committing"           # not refused
    assert A.remote_commit(n.plan, o["offer_id"], digest) == "committed"
    assert n.one.is_symlink() and n.two.is_symlink()


def _apng():
    frames = [Image.new("RGB", (8, 8), c) for c in ("red", "blue")]
    b = io.BytesIO(); frames[0].save(b, format="PNG", save_all=True, append_images=frames[1:]); return b.getvalue()


def _make_animated(n):
    data = _apng()
    for name in (n.one, n.two):
        name.unlink()
    n.one.write_bytes(data); os.link(n.one, n.two); st = n.one.stat()
    plan = A.connect_plan(n.plan)
    with plan:
        plan.execute("UPDATE objects SET dev=?,ino=?,size=?,mtime=?,sha=?,md5=?",
                     (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns,
                      hashlib.sha256(data).hexdigest(), hashlib.md5(data).hexdigest()))
        plan.execute("UPDATE aliases SET dev=?,ino=?", (st.st_dev, st.st_ino))
    return data


def test_an_animated_original_is_never_offered(nodes):
    """Rehearsal 2026-10-25: the 20 largest pending originals were animated WebP; the archive
    node refused each and they came back every run."""
    _make_animated(nodes)
    assert A.remote_offer(nodes.plan, 10) == []
    plan = A.connect_plan(nodes.plan)
    assert plan.execute("SELECT status FROM objects").fetchone()[0] == "refused"


def test_the_archive_node_closes_an_offer_it_finds_animated(nodes, monkeypatch):
    n = nodes
    A.remote_offer(n.plan, 10)                       # offered while it was a still image
    _make_animated(n)
    plan = A.connect_plan(n.plan)
    with plan:                                       # the offer now describes the animated file
        st = n.one.stat(); data = n.one.read_bytes()
        plan.execute("UPDATE offers SET dev=?,ino=?,size=?,mtime=?,sha=?,md5=?",
                     (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns,
                      hashlib.sha256(data).hexdigest(), hashlib.md5(data).hexdigest()))
    assert A.run_remote("/m", n.root, n.transport) == {"done": 0, "refused": 1}
    assert any(c.startswith("remote-refuse") and c.endswith("--reason animated") for c in n.calls)
    assert _offers(n)[0]["state"] == "refused"
    assert A.remote_offer(n.plan, 10) == []
    assert not n.one.is_symlink()


def test_remote_refuse_accepts_only_known_reasons_and_open_offers(nodes):
    n = nodes
    A.run_remote("/m", n.root, n.transport)
    [o] = _offers(n)
    with pytest.raises(RuntimeError):
        A.remote_dispatch(f"remote-refuse --offer {o['offer_id']} --reason because", n.plan)
    assert A.remote_refuse(n.plan, o["offer_id"], "animated").startswith("unchanged")
    assert _offers(n)[0]["state"] == "committed"


def _animation_post(original):
    """A post of type animation whose view was made by the ingest path's own encoder."""
    from api.services.media_materialize import animated_webp_bytes
    with Image.open(io.BytesIO(original)) as im:
        return {"type": "animation", "view": animated_webp_bytes(im)}


def test_an_animation_whose_view_keeps_its_motion_is_archived_on_the_other_host(nodes, monkeypatch):
    """kura-on-nodeb step 4 remedies: an animated original leaves only behind a view that keeps
    its motion; the offer says so and the archive node accepts it."""
    n = nodes
    post = _animation_post(_make_animated(n))
    monkeypatch.setattr(A, "locked_eligible_post", lambda db, row, names: post)
    [offer] = A.remote_offer(n.plan, 10)
    assert offer["ptype"] == "animation"
    assert A.run_remote("/m", n.root, n.transport) == {"done": 1, "refused": 0}
    assert n.one.is_symlink() and n.two.is_symlink()


def test_an_animation_whose_view_lost_its_timing_stays_local(nodes, monkeypatch):
    n = nodes
    original = _make_animated(n)
    with Image.open(io.BytesIO(original)) as im:
        frames = [f.convert("RGB") for f in ImageSequence.Iterator(im)]
    bad = io.BytesIO()
    frames[0].save(bad, "WEBP", save_all=True, append_images=frames[1:], duration=999, loop=0)
    monkeypatch.setattr(A, "locked_eligible_post", lambda db, row, names: {"type": "animation", "view": bad.getvalue()})
    assert A.remote_offer(n.plan, 10) == []
    plan = A.connect_plan(n.plan)
    assert plan.execute("SELECT reason FROM objects").fetchone()[0].startswith("Animated view does not keep")


def test_a_view_changed_after_the_offer_stops_the_switch(nodes, monkeypatch):
    """Codex 1.276.0: the motion check ran only at the offer; the commit must check the view as
    it is at the switch, under the locks."""
    n = nodes
    original = _make_animated(n)
    good = _animation_post(original)
    with Image.open(io.BytesIO(original)) as im:
        frames = [f.convert("RGB") for f in ImageSequence.Iterator(im)]
    bad = io.BytesIO()
    frames[0].save(bad, "WEBP", save_all=True, append_images=frames[1:], duration=999, loop=0)
    calls = []
    def eligible(db, row, names):            # the view is replaced between offer and commit
        calls.append(1)
        return good if len(calls) == 1 else {"type": "animation", "view": bad.getvalue()}
    monkeypatch.setattr(A, "locked_eligible_post", eligible)
    assert A.run_remote("/m", n.root, n.transport)["done"] == 0
    [o] = _offers(n)
    assert o["state"] == "refused" and "does not keep" in o["reason"]
    assert not n.one.is_symlink() and not n.two.is_symlink()


def _declare(tmp_path, monkeypatch, plan, digest=None):
    import socket
    node = socket.gethostname().split(".")[0]
    d = tmp_path / "nodes.json"
    d.write_text(json.dumps({"nodes": {node: {"original_archive": {
        "plan": str(plan), "header_sha256": digest or A._header_digest(plan)}}}}))
    monkeypatch.setenv("KURA_NODES_DECLARATION", str(d))


def test_writers_run_only_the_declared_plan(nodes, tmp_path, monkeypatch):
    """2026-10-25: a plan taken before the post repairs was still the one a node ran; the plan
    a node runs is declared in tagger/deploy/nodes.json, and writers refuse any other."""
    monkeypatch.undo()                                   # the real current_plan
    other = tmp_path / "other.sqlite"; other.write_bytes(nodes.plan.read_bytes())
    _declare(tmp_path, monkeypatch, nodes.plan)
    assert A.current_plan() == nodes.plan and A.current_plan(nodes.plan) == nodes.plan
    for call in (lambda: A.run_plan(other), lambda: A.remote_offer(other),
                 lambda: A.remote_refuse(other, "x", "y")):
        with pytest.raises(RuntimeError, match="not the declared plan"):
            call()
    assert A.any_plan(other) == other                   # readers take any plan


def test_a_plan_re_made_at_the_declared_path_is_refused(nodes, tmp_path, monkeypatch):
    monkeypatch.undo()
    _declare(tmp_path, monkeypatch, nodes.plan, digest="0" * 64)
    with pytest.raises(RuntimeError, match="header differs"):
        A.current_plan()


def test_a_node_without_a_declaration_runs_no_plan(tmp_path, monkeypatch):
    d = tmp_path / "nodes.json"; d.write_text('{"nodes": {}}')
    monkeypatch.setenv("KURA_NODES_DECLARATION", str(d))
    with pytest.raises(RuntimeError, match="no original archive plan is declared"):
        A.declared_plan()
