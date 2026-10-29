"""kura#61 PR-A — one owner for on-disk names; two names per file during the migration.

Pinned here, in the order the reviews earned them:
  * a file written during the migration has BOTH names and they are one inode;
  * the other name is attached by link-to-tmp + rename, so a regeneration (the
    other name already exists) neither fails with EEXIST nor leaves a window
    without the name, and a crash residue is recoverable;
  * deleting a post removes EVERY name (a leftover hard link keeps a deleted post
    reachable);
  * both transition salts at once is refused — by the builders and by the boot guard;
  * the salt/hmac is referenced only where the names are derived (AST, not grep);
  * the migration script's audit is a count over the disk that moves monotonically.
"""
from __future__ import annotations

import ast
import importlib
import os
import pathlib
import sys

import pytest

_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))


@pytest.fixture(autouse=True)
def _leave_the_modules_as_found(monkeypatch):
    """Every test here reloads config/hashing/files under a patched env. Put them back
    under the real env afterwards, or the rest of the suite runs against a tmp DATA_DIR."""
    yield
    monkeypatch.undo()
    import api.config, api.services.hashing, api.services.files
    for m in (api.config, api.services.hashing, api.services.files):
        importlib.reload(m)


def _reload(monkeypatch, tmp_path, salt="old-salt", nxt="", legacy=""):
    monkeypatch.setenv("KURA_FS_NAME_SALT", salt)
    monkeypatch.setenv("KURA_FS_NAME_SALT_NEXT", nxt)
    monkeypatch.setenv("KURA_FS_NAME_SALT_LEGACY", legacy)
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    import api.config as config
    importlib.reload(config)
    import api.services.hashing as hashing
    importlib.reload(hashing)
    import api.services.files as files
    importlib.reload(files)
    return files, hashing


# ── names ────────────────────────────────────────────────────────────────────

def test_outside_the_migration_a_file_has_one_name(monkeypatch, tmp_path):
    files, _ = _reload(monkeypatch, tmp_path)
    assert len(files.names("content", 7, "image/png")) == 1
    assert files.names("content", 7, "image/png")[0] == files.content_path(7, "image/png")


def test_in_phase_a_the_reader_name_is_the_old_salt_and_the_other_is_the_next(monkeypatch, tmp_path):
    files, hashing = _reload(monkeypatch, tmp_path, salt="old", nxt="new")
    a, b = files.names("thumbnail", 42)
    assert a.name == f"42_{hashing._hash_with('old', 42)}.jpg"
    assert b.name == f"42_{hashing._hash_with('new', 42)}.jpg"
    assert files.thumbnail_path(42) == a, "URLs keep coming from the reader's salt"


def test_in_phase_b_the_reader_name_is_the_new_salt_and_the_other_is_legacy(monkeypatch, tmp_path):
    files, hashing = _reload(monkeypatch, tmp_path, salt="new", legacy="old")
    a, b = files.names("content", 42, "video/mp4")
    assert a.name.startswith(f"42_{hashing._hash_with('new', 42)}") and b.name.startswith(f"42_{hashing._hash_with('old', 42)}")


def test_both_transition_salts_at_once_is_refused_by_the_builders(monkeypatch, tmp_path):
    files, hashing = _reload(monkeypatch, tmp_path, salt="a", nxt="b", legacy="c")
    with pytest.raises(hashing.SaltStateError):
        files.names("content", 1, "image/png")


# ── writes: both names, one inode, no window ─────────────────────────────────

def test_a_write_during_the_migration_lands_under_both_names_as_one_inode(monkeypatch, tmp_path):
    files, _ = _reload(monkeypatch, tmp_path, salt="old", nxt="new")
    paths = files.names("content", 9, "image/png")
    files.write_all_names(paths, b"PNG-bytes")
    assert all(p.exists() for p in paths)
    assert os.stat(paths[0]).st_ino == os.stat(paths[1]).st_ino
    assert paths[1].read_bytes() == b"PNG-bytes"


def test_regeneration_over_an_existing_other_name_does_not_fail_and_ends_on_one_inode(monkeypatch, tmp_path):
    """The EEXIST case: the other name already exists (a previous generation). `os.link`
    alone would raise; unlink+link would open a window. link-to-tmp + rename does neither."""
    files, _ = _reload(monkeypatch, tmp_path, salt="old", nxt="new")
    paths = files.names("thumbnail", 5)
    files.write_all_names(paths, b"v1")
    files.write_all_names(paths, b"v2")                      # regenerate
    assert paths[0].read_bytes() == b"v2" and paths[1].read_bytes() == b"v2"
    assert os.stat(paths[0]).st_ino == os.stat(paths[1]).st_ino
    assert not list(paths[1].parent.glob("*.tmp.*")), "no residue on the happy path"


def test_the_other_name_is_never_absent_while_it_is_replaced(monkeypatch, tmp_path):
    """Observe from the outside: at no call boundary inside link_secondary is the
    secondary name missing. Pin by replacing os.replace with one that checks."""
    files, _ = _reload(monkeypatch, tmp_path, salt="old", nxt="new")
    paths = files.names("thumbnail", 6)
    files.write_all_names(paths, b"v1")
    seen = []
    real_replace = os.replace
    def spying_replace(src, dst):
        seen.append(pathlib.Path(dst).exists())      # the name exists right up to the rename
        real_replace(src, dst)
    monkeypatch.setattr(files.os, "replace", spying_replace)
    files.write_all_names(paths, b"v2")
    assert seen and all(seen), "the secondary name was absent at some point"


def test_a_crash_between_link_and_rename_leaves_only_a_tmp_that_audit_reports(monkeypatch, tmp_path):
    files, _ = _reload(monkeypatch, tmp_path, salt="old", nxt="new")
    paths = files.names("thumbnail", 8)
    files.write_all_names(paths, b"v1")
    def crash(src, dst):
        raise OSError("simulated power loss")
    monkeypatch.setattr(files.os, "replace", crash)
    with pytest.raises(OSError):
        files.link_secondary(paths[0], paths[1])
    monkeypatch.undo()
    assert paths[1].exists() and paths[1].read_bytes() == b"v1", "the previous name survived the crash"
    # and the residue, if any, is visible to the audit and swept by the next link
    files2, _ = _reload(monkeypatch, tmp_path, salt="old", nxt="new")
    files2.link_secondary(paths[0], paths[1])
    assert not list(paths[1].parent.glob("*.tmp.*"))


# ── delete: every name ───────────────────────────────────────────────────────

def test_deleting_removes_every_name_so_the_inode_is_freed(monkeypatch, tmp_path):
    files, _ = _reload(monkeypatch, tmp_path, salt="old", nxt="new")
    c = files.names("content", 3, "image/jpeg"); t = files.names("thumbnail", 3)
    files.write_all_names(c, b"jpg"); files.write_all_names(t, b"thumb")
    n = files.delete_all_names(c + t)
    assert n == 4 and not any(p.exists() for p in c + t)


def test_delete_tolerates_a_name_that_is_already_gone(monkeypatch, tmp_path):
    files, _ = _reload(monkeypatch, tmp_path)
    assert files.delete_all_names(files.names("content", 99, "image/png")) == 0


# ── the boot guard refuses an undecidable state ──────────────────────────────

def test_the_boot_guard_refuses_both_transition_salts(monkeypatch, tmp_path):
    _reload(monkeypatch, tmp_path, salt="a", nxt="b", legacy="c")
    # No reload of api.main: reloading re-creates the app and re-mounts /data, and
    # that mount then shows up as an unaccounted served surface in a LATER test
    # (measured: test_refguard_covers_every_served_surface went red only inside the
    # full suite). The guard reads the hashing module at call time, so the reloaded
    # hashing is what it sees.
    import api.main as main
    with pytest.raises(RuntimeError) as e:
        main._check_fs_name_salt()
    assert "both set" in str(e.value)


# ── one owner of names (AST, not grep) ───────────────────────────────────────

_ALLOWED_SALT_REFS = {"api/config.py", "api/services/hashing.py", "api/services/files.py", "api/main.py"}
_ALLOWED_DIR_BUILDERS = {"api/services/files.py", "api/config.py"}


def _py_files():
    for sub in ("api", "scripts"):
        for f in (_ROOT / sub).rglob("*.py"):
            if "alembic" in f.parts or "legacy" in f.parts:   # scripts/legacy/ is retired code
                continue
            yield f


def test_the_salt_is_read_only_where_names_are_derived():
    """`FS_NAME_SALT*` and `hmac` appear in the config, the hash helper and the boot
    guard — nowhere else. A router that reads the salt is a router that can build a
    name (round 2/3 of the plan review: `_legacy_fs_content_url` was exactly that)."""
    offenders = []
    for f in _py_files():
        rel = str(f.relative_to(_ROOT))
        tree = ast.parse(f.read_text(encoding="utf-8"), filename=rel)
        for node in ast.walk(tree):
            name = getattr(node, "id", None) or getattr(node, "attr", None)
            if name and (name.startswith("FS_NAME_SALT") or name == "hmac") and rel not in _ALLOWED_SALT_REFS:
                offenders.append(f"{rel}:{node.lineno}:{name}")
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                names = [a.name for a in node.names]
                if ("hmac" in names) and rel not in _ALLOWED_SALT_REFS:
                    offenders.append(f"{rel}:{node.lineno}:import hmac")
    assert offenders == [], offenders


def test_no_router_or_script_assembles_a_content_path_by_hand():
    """`POSTS_DIR` / `THUMBNAILS_DIR` / `CUSTOM_THUMBNAILS_DIR` joined into a path outside
    files.py is a name built by hand — the exact shape the four posts.py sites had."""
    offenders = []
    for f in _py_files():
        rel = str(f.relative_to(_ROOT))
        if rel in _ALLOWED_DIR_BUILDERS:
            continue
        tree = ast.parse(f.read_text(encoding="utf-8"), filename=rel)
        for node in ast.walk(tree):
            if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
                for side in (node.left, node.right):
                    n = getattr(side, "id", None) or getattr(side, "attr", None)
                    if n in ("POSTS_DIR", "THUMBNAILS_DIR", "CUSTOM_THUMBNAILS_DIR", "POSTS_PATH", "THUMBNAIL_DIR"):
                        offenders.append(f"{rel}:{node.lineno}:{n}")
    assert offenders == [], offenders


# ── the migration script ─────────────────────────────────────────────────────

def test_backfill_adds_the_other_name_and_audit_is_monotone(monkeypatch, tmp_path):
    files, hashing = _reload(monkeypatch, tmp_path, salt="old", nxt="new")
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    import scripts.fs_salt_migrate as mig
    importlib.reload(mig)
    # three legacy-only files, one already dual, one with an unknown salt
    for pid in (1, 2, 3):
        p = files._name("content", pid, hashing._hash_with("old", pid), "image/png"); p.parent.mkdir(parents=True, exist_ok=True); p.write_bytes(b"x")
    files.write_all_names(files.names("content", 4, "image/png"), b"dual")
    (tmp_path / "posts" / "5_0123456789abcdef.png").write_bytes(b"?")
    before = mig.audit(("content",))["content"]
    assert (before["reader_only"], before["reader_and_other"], before["unknown_salt"]) == (3, 1, 1)
    out = mig.backfill(("content",), batch=0, sleep=0, dry_run=False)
    assert out["linked"] == 3 and out["already"] == 1
    after = mig.audit(("content",))["content"]
    assert (after["reader_only"], after["reader_and_other"]) == (0, 4)
    assert mig.backfill(("content",), batch=0, sleep=0, dry_run=False)["linked"] == 0, "idempotent"


def test_cleanup_refuses_outside_phase_b_and_never_unlinks_the_last_name(monkeypatch, tmp_path):
    files, hashing = _reload(monkeypatch, tmp_path, salt="old", nxt="new")
    import scripts.fs_salt_migrate as mig
    importlib.reload(mig)
    with pytest.raises(SystemExit):
        mig.cleanup(("content",), 0, 0, False)
    files, hashing = _reload(monkeypatch, tmp_path, salt="new", legacy="old")
    importlib.reload(mig)
    files.write_all_names(files.names("content", 1, "image/png"), b"both")      # both names
    lone = files._name("content", 2, hashing._hash_with("old", 2), "image/png"); lone.write_bytes(b"only-legacy")
    out = mig.cleanup(("content",), 0, 0, False)
    assert out == {"unlinked": 1, "kept_as_only_name": 1, "dry_run": False}
    assert lone.exists(), "the last name of a file is never unlinked"


def test_tally_decides_phase_c_from_first_party_requests_or_the_bounded_fallback(monkeypatch, tmp_path):
    files, hashing = _reload(monkeypatch, tmp_path, salt="new", legacy="old")
    import scripts.fs_salt_migrate as mig
    importlib.reload(mig)
    import datetime as dt
    legacy = hashing._hash_with("old", 10); today = dt.date(2026, 9, 12)
    line = lambda day, ua, ref: (f'1.2.3.4 - - [{day.strftime("%d/%b/%Y")}:10:00:00 +0900] "GET /data/posts/10_{legacy}.png HTTP/1.1" 200 5 "{ref}" "{ua}"\n')
    # Every decision below is now reached only through the coverage proof: the
    # canary must come back out of the log, and the log must span the window.
    # Without both, `start` is "unknown" whatever the counts say — see
    # tests/test_tally_proves_it_observed_the_content_path.py for that half.
    canary = {"ua": "kura-fs-salt-tally-canary/1", "status": 404}
    # the instrument predates the window here; its own clause is pinned in
    # tests/test_tally_proves_it_observed_the_content_path.py
    instrumented = today - dt.timedelta(days=60)
    span = "".join(f'1.2.3.4 - - [{(today - dt.timedelta(days=i)).strftime("%d/%b/%Y")}:09:00:00 +0900]'
                   f' "GET / HTTP/1.1" 200 5 "-" "Mozilla/5.0"\n' for i in range(7))
    seen = line(today, canary["ua"], "-")
    log = tmp_path / "access.log"
    log.write_text(span + seen + line(today, "Mozilla/5.0 Googlebot", "-") + line(today - dt.timedelta(days=1), "Mozilla/5.0", "https://kura.example/post/10"))
    out = mig.tally(log, 7, "kura.example", None, 30, 10, now=today, canary=canary, instrumented_since=instrumented)
    assert out["start"] == "no" and out["cond_first_party_zero"] is False
    out = mig.tally(log, 7, "kura.example", dt.date(2026, 8, 1), 30, 10, now=today, canary=canary, instrumented_since=instrumented)
    assert out["cond_bounded_fallback"] is True and out["start"] == "yes"
    bots_only = tmp_path / "bots.log"; bots_only.write_text(span + seen + line(today, "bingbot/2.0", "-"))
    assert mig.tally(bots_only, 7, "kura.example", None, 30, 10, now=today, canary=canary, instrumented_since=instrumented)["start"] == "yes"


# ── PR-A2: a name may be a symlink; the property is "same file", not "one inode" ─
# Measured on the canonical node 2026-09-05: posts regular=521,352 symlink=738,965
# (into a second filesystem) broken=0; generated-thumbnails regular=1,026,472 symlink=0.
# PR-A's hard link failed with EXDEV on every symlinked post, and its tmp+replace
# write would have replaced the symlink with a regular file on the first filesystem.

import errno  # noqa: E402
import tempfile  # noqa: E402


def _symlinked_post(files, hashing, tmp_path, pid=11, mime="image/png"):
    """A post whose reader's name is a symlink into another directory (the HDD stand-in)."""
    reader = files.names("content", pid, mime)[0]
    reader.parent.mkdir(parents=True, exist_ok=True)
    hdd = tmp_path / "hdd" / "posts"; hdd.mkdir(parents=True, exist_ok=True)
    target = hdd / reader.name; target.write_bytes(b"on the other disk")
    reader.symlink_to(target)
    return reader, target


def test_a_write_through_a_symlinked_name_lands_on_the_other_disk_and_keeps_the_symlink(monkeypatch, tmp_path):
    files, hashing = _reload(monkeypatch, tmp_path, salt="old")
    reader, target = _symlinked_post(files, hashing, tmp_path)
    files.write_all_names([reader], b"replaced")
    assert reader.is_symlink(), "the name must stay a symlink — PR-A replaced it with a regular file"
    assert target.read_bytes() == b"replaced" and reader.read_bytes() == b"replaced"
    assert not list(target.parent.glob("*.tmp.*"))


def test_the_other_name_of_a_symlinked_post_is_a_symlink_to_the_same_file(monkeypatch, tmp_path):
    files, hashing = _reload(monkeypatch, tmp_path, salt="old", nxt="new")
    reader, target = _symlinked_post(files, hashing, tmp_path)
    other = files.names("content", 11, "image/png")[1]
    files.link_secondary(reader, other)
    assert other.is_symlink() and files.same_file(reader, other)
    assert os.path.realpath(other) == str(target)


def test_a_hard_link_that_cannot_cross_devices_is_refused_and_leaves_no_name(monkeypatch, tmp_path):
    """Round 1 (both seats): the old fallback made the new name a symlink TO the old name;
    Phase C unlinking the old name would then have deleted the file. Fail closed instead."""
    files, _ = _reload(monkeypatch, tmp_path, salt="old", nxt="new")
    paths = files.names("thumbnail", 12)
    paths[0].parent.mkdir(parents=True, exist_ok=True); paths[0].write_bytes(b"regular file")
    def exdev(src, dst, *a, **k):
        raise OSError(errno.EXDEV, "Invalid cross-device link")
    monkeypatch.setattr(files.os, "link", exdev)
    with pytest.raises(files.CannotLinkAcrossDevices):
        files.link_secondary(paths[0], paths[1])
    assert not os.path.lexists(paths[1]) and not list(paths[0].parent.glob("*.tmp.*"))
    assert paths[0].read_bytes() == b"regular file"


def test_regeneration_of_a_symlinked_post_keeps_both_names_on_the_same_file(monkeypatch, tmp_path):
    files, hashing = _reload(monkeypatch, tmp_path, salt="old", nxt="new")
    reader, target = _symlinked_post(files, hashing, tmp_path)
    paths = files.names("content", 11, "image/png")
    files.write_all_names(paths, b"v2"); files.write_all_names(paths, b"v3")
    assert files.same_file(*paths) and target.read_bytes() == b"v3"
    assert reader.is_symlink() and paths[1].is_symlink()


def test_deleting_a_symlinked_post_removes_both_names_and_the_file_behind_them(monkeypatch, tmp_path):
    files, hashing = _reload(monkeypatch, tmp_path, salt="old", nxt="new")
    reader, target = _symlinked_post(files, hashing, tmp_path)
    paths = files.names("content", 11, "image/png"); files.link_secondary(reader, paths[1])
    n = files.delete_all_names(paths)
    assert n == 3 and not os.path.lexists(reader) and not os.path.lexists(paths[1]) and not target.exists()


def test_audit_counts_symlink_names_and_backfill_links_them_by_property(monkeypatch, tmp_path):
    files, hashing = _reload(monkeypatch, tmp_path, salt="old", nxt="new")
    import scripts.fs_salt_migrate as mig
    importlib.reload(mig)
    _symlinked_post(files, hashing, tmp_path, pid=21)
    reg = files._name("content", 22, hashing._hash_with("old", 22), "image/png"); reg.write_bytes(b"regular")
    before = mig.audit(("content",))["content"]
    assert before["reader_only"] == 2 and before["symlink_names"] == 1 and before["broken_symlinks"] == 0
    out = mig.backfill(("content",), batch=0, sleep=0, dry_run=False)
    assert out["linked"] == 2 and out["diverged"] == 0
    after = mig.audit(("content",))["content"]
    assert after["reader_and_other"] == 2 and after["diverged"] == 0
    assert mig.backfill(("content",), batch=0, sleep=0, dry_run=False)["linked"] == 0


def test_two_names_that_are_two_files_are_reported_as_diverged_and_never_overwritten(monkeypatch, tmp_path):
    files, hashing = _reload(monkeypatch, tmp_path, salt="old", nxt="new")
    import scripts.fs_salt_migrate as mig
    importlib.reload(mig)
    a, b = files.names("content", 31, "image/png")
    a.parent.mkdir(parents=True, exist_ok=True); a.write_bytes(b"A"); b.write_bytes(b"B")
    out = mig.backfill(("content",), batch=0, sleep=0, dry_run=False)
    assert out["diverged"] == 1 and out["linked"] == 0 and b.read_bytes() == b"B"
    assert mig.audit(("content",))["content"]["diverged"] == 1


# ── round 2: the rev 6 predicate (reachability AND independence) is the one check ─

def test_verify_names_accepts_hard_links_and_twin_symlinks_and_rejects_chains(tmp_path):
    from api.services import files
    d = tmp_path / "d"; d.mkdir(); hdd = tmp_path / "hdd"; hdd.mkdir()
    # (a) two hard links
    a = d / "a"; a.write_bytes(b"x"); b = d / "b"; os.link(a, b)
    assert files.verify_names(a, b) and files.verify_names(b, a)
    # (b) two symlinks to one file on the other disk
    t = hdd / "t"; t.write_bytes(b"y"); s1 = d / "s1"; s1.symlink_to(t); s2 = d / "s2"; s2.symlink_to(t)
    assert files.verify_names(s1, s2) and files.verify_names(s2, s1)
    # (c) new -> old, absolute: reachable, NOT independent
    old = d / "old"; old.write_bytes(b"z"); new_abs = d / "new_abs"; new_abs.symlink_to(old)
    assert files.same_file(old, new_abs) and not files.verify_names(old, new_abs)
    # (d) new -> old, relative: the stored string is "old", not an absolute path
    new_rel = d / "new_rel"; os.symlink("old", new_rel)
    assert files.same_file(old, new_rel) and not files.verify_names(old, new_rel)
    # (e) unrelated files, and a broken link
    other = d / "other"; other.write_bytes(b"w")
    assert not files.verify_names(a, other)
    broken = d / "broken"; broken.symlink_to(hdd / "missing")
    assert not files.verify_names(a, broken) and not files.verify_names(broken, a)


def test_cleanup_keeps_a_legacy_name_that_the_reader_is_a_symlink_to(monkeypatch, tmp_path):
    """The round-1 defect, as a fixture: legacy = the regular file, reader = symlink -> legacy.
    same_file says yes; unlinking legacy would delete the file. verify_names says no."""
    files, hashing = _reload(monkeypatch, tmp_path, salt="new", legacy="old")
    import scripts.fs_salt_migrate as mig
    importlib.reload(mig)
    reader, legacy = files.names("content", 41, "image/png")
    legacy.parent.mkdir(parents=True, exist_ok=True); legacy.write_bytes(b"the only copy")
    reader.symlink_to(legacy)
    assert files.same_file(legacy, reader)
    out = mig.cleanup(("content",), 0, 0, False)
    assert out["unlinked"] == 0 and out["kept_as_only_name"] == 1
    assert legacy.exists() and reader.read_bytes() == b"the only copy"
    assert mig.audit(("content",))["content"]["chained"] == 1


def test_a_failed_write_through_a_symlink_leaves_no_tmp_on_the_other_disk(monkeypatch, tmp_path):
    files, hashing = _reload(monkeypatch, tmp_path, salt="old")
    reader, target = _symlinked_post(files, hashing, tmp_path)
    def boom(*a, **k):
        raise OSError(errno.ENOSPC, "No space left on device")
    monkeypatch.setattr(files.Path, "write_bytes", boom)
    with pytest.raises(OSError):
        files.write_all_names([reader], b"new bytes")
    monkeypatch.undo()
    assert not list(target.parent.glob("*.tmp.*")), "a .tmp on the second filesystem is invisible to audit"
    assert reader.is_symlink() and target.read_bytes() == b"on the other disk"


def test_a_broken_second_name_is_repaired_by_backfill_and_counted_as_broken_not_diverged(monkeypatch, tmp_path):
    files, hashing = _reload(monkeypatch, tmp_path, salt="old", nxt="new")
    import scripts.fs_salt_migrate as mig
    importlib.reload(mig)
    reader, target = _symlinked_post(files, hashing, tmp_path, pid=51)
    other = files.names("content", 51, "image/png")[1]
    other.symlink_to(tmp_path / "hdd" / "posts" / "gone.png")          # a second name whose target is missing
    before = mig.audit(("content",))["content"]
    assert before["broken_symlinks"] == 1 and before["broken_pair"] == 1 and before["diverged"] == 0
    out = mig.backfill(("content",), batch=0, sleep=0, dry_run=False)
    assert out["repaired"] == 1 and out["linked"] == 0 and out["diverged"] == 0
    assert files.verify_names(reader, other) and other.read_bytes() == b"on the other disk"
    after = mig.audit(("content",))["content"]
    assert after["reader_and_other"] == 1 and after["broken_pair"] == 0


def test_a_relative_symlink_primary_gets_a_second_name_that_resolves_from_its_own_directory(monkeypatch, tmp_path):
    files, hashing = _reload(monkeypatch, tmp_path, salt="old", nxt="new")
    reader, other = files.names("content", 61, "image/png")
    reader.parent.mkdir(parents=True, exist_ok=True)
    hdd = tmp_path / "hdd" / "posts"; hdd.mkdir(parents=True, exist_ok=True)
    target = hdd / reader.name; target.write_bytes(b"rel")
    os.symlink(os.path.relpath(target, reader.parent), reader)             # "../hdd/posts/<name>"
    assert not os.path.isabs(os.readlink(reader))
    files.link_secondary(reader, other)
    assert not os.path.isabs(os.readlink(other)), "a relative primary gets a relative second name"
    assert files.verify_names(reader, other) and files.verify_names(other, reader)
    files.write_all_names([reader, other], b"rel v2")
    assert target.read_bytes() == b"rel v2" and reader.is_symlink() and other.is_symlink()


def test_backfill_counts_a_regular_file_it_cannot_link_across_devices_and_moves_on(monkeypatch, tmp_path):
    files, hashing = _reload(monkeypatch, tmp_path, salt="old", nxt="new")
    import scripts.fs_salt_migrate as mig
    importlib.reload(mig)
    reg = files._name("content", 71, hashing._hash_with("old", 71), "image/png")
    reg.parent.mkdir(parents=True, exist_ok=True); reg.write_bytes(b"regular")
    def exdev(src, dst, *a, **k):
        raise OSError(errno.EXDEV, "Invalid cross-device link")
    monkeypatch.setattr(files.os, "link", exdev)
    out = mig.backfill(("content",), batch=0, sleep=0, dry_run=False)
    assert out["refused"] == 1 and out["linked"] == 0
    assert mig.audit(("content",))["content"]["reader_only"] == 1


_HOME_DEV = os.stat(os.path.expanduser("~")).st_dev
_TMP_DEV = os.stat(tempfile.gettempdir()).st_dev


@pytest.mark.skipif(_HOME_DEV == _TMP_DEV, reason="needs two devices (tmp and home on one filesystem here)")
def test_on_two_real_devices_a_symlinked_post_is_written_named_and_deleted_across_them(monkeypatch, tmp_path):
    """Rev 6 fixture (b), for real: the name lives on the tmp device, the file on the home
    device. No monkeypatching of os.link — a hard link here would really raise EXDEV."""
    files, hashing = _reload(monkeypatch, tmp_path, salt="old", nxt="new")
    other_disk = pathlib.Path(tempfile.mkdtemp(prefix="kura-61-", dir=os.path.expanduser("~")))
    try:
        reader, other = files.names("content", 81, "image/png")
        reader.parent.mkdir(parents=True, exist_ok=True)
        target = other_disk / reader.name; target.write_bytes(b"far away")
        reader.symlink_to(target)
        assert os.stat(reader.parent).st_dev != os.stat(target).st_dev
        with pytest.raises(OSError) as ei:
            os.link(target, reader.parent / "hardlink-attempt")
        assert ei.value.errno == errno.EXDEV, "the premise of this test: the two dirs are on different devices"
        files.write_all_names([reader, other], b"written across")
        assert reader.is_symlink() and other.is_symlink()
        assert files.verify_names(reader, other) and files.verify_names(other, reader)
        assert target.read_bytes() == b"written across" and os.stat(other).st_dev == os.stat(target).st_dev
        assert files.delete_all_names([reader, other]) == 3 and not target.exists()
    finally:
        import shutil
        shutil.rmtree(other_disk, ignore_errors=True)


def test_a_reader_name_that_is_a_broken_symlink_is_counted_not_duplicated(monkeypatch, tmp_path):
    files, hashing = _reload(monkeypatch, tmp_path, salt="old", nxt="new")
    import scripts.fs_salt_migrate as mig
    importlib.reload(mig)
    reader, other = files.names("content", 91, "image/png")
    reader.parent.mkdir(parents=True, exist_ok=True); reader.symlink_to(tmp_path / "hdd" / "posts" / "vanished.png")
    out = mig.backfill(("content",), batch=0, sleep=0, dry_run=False)
    assert out["broken_reader"] == 1 and out["linked"] == 0 and out["refused"] == 0
    assert not os.path.lexists(other), "a broken reader must not be copied into a broken second name"


# ── PR-A3: the audit counts name pairs, one migration at a time, whole-tree guard ──

def test_a_post_with_two_reader_names_of_different_extensions_is_two_files_not_a_divergence(monkeypatch, tmp_path):
    """Measured on the canonical node 2026-09-05: 1,392 posts carry a `.jpg` original
    behind a symlink AND a local `.webp`, both under the reader's salt. Keyed by post
    id alone the audit compared the `.jpg` against the `.webp`'s second name and
    reported `diverged` — a defect of the audit, not of the disk."""
    files, hashing = _reload(monkeypatch, tmp_path, salt="old", nxt="new")
    import scripts.fs_salt_migrate as mig
    importlib.reload(mig)
    reader_jpg, target = _symlinked_post(files, hashing, tmp_path, pid=101, mime="image/jpeg")
    reader_webp = files._name("content", 101, hashing._hash_with("old", 101), "image/webp"); reader_webp.write_bytes(b"webp local")
    files.link_secondary(reader_webp, files._name("content", 101, hashing._hash_with("new", 101), "image/webp"))
    c = mig.audit(("content",))["content"]
    assert (c["reader_and_other"], c["reader_only"], c["diverged"], c["chained"]) == (1, 1, 0, 0)
    out = mig.backfill(("content",), batch=0, sleep=0, dry_run=False)
    assert out["linked"] == 1 and out["diverged"] == 0
    c = mig.audit(("content",))["content"]
    assert (c["reader_and_other"], c["reader_only"], c["diverged"]) == (2, 0, 0)


def test_a_second_mutating_run_refuses_while_the_first_holds_the_lock(monkeypatch, tmp_path):
    files, hashing = _reload(monkeypatch, tmp_path, salt="old", nxt="new")
    import scripts.fs_salt_migrate as mig
    importlib.reload(mig)
    with mig._OneMigrationAtATime("backfill"):
        with pytest.raises(SystemExit) as ei:
            with mig._OneMigrationAtATime("cleanup"):
                pass
        assert "refusing to run cleanup concurrently" in str(ei.value)
        assert "pid" in (tmp_path / ".fs_salt_migrate.lock").read_text()
    with mig._OneMigrationAtATime("backfill"):      # released: the next run may take it
        pass


def test_the_guard_command_reports_what_the_next_restart_would_decide(monkeypatch, tmp_path):
    files, hashing = _reload(monkeypatch, tmp_path, salt="old", nxt="new")
    import scripts.fs_salt_migrate as mig
    importlib.reload(mig)
    for pid in (1, 2, 3):
        p = files._name("content", pid, hashing._hash_with("old", pid), "image/png"); p.parent.mkdir(parents=True, exist_ok=True); p.write_bytes(b"x")
    assert mig.guard(None)["verdict"] == "ok"
    mig.backfill(("content",), batch=0, sleep=0, dry_run=False)
    g = mig.guard(None)
    assert g["verdict"] == "ok" and g["checked"] == 6 and g["matched_reader"] == 3 and g["next_restart_boots"]
    # a third salt's name is still refused, and the whole tree sees it where a sample may not
    (tmp_path / "posts" / "9_0123456789abcdef.png").write_bytes(b"?")
    assert mig.guard(None)["verdict"] == "mismatch" and mig.guard(None)["next_restart_boots"] is False
