"""Unit tests for api/tagger_core/artifact_store.py
(plan:tagger-release-is-the-whole-ensemble rev16).

Pure filesystem tests -- no DB, no network, no torch. The store's whole promise
is "the filename is the identity", so most of these tests are adversarial:
they corrupt bytes, lie about hashes, and interrupt writes, and check the store
refuses rather than returns something plausible.
"""
import hashlib
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from api.tagger_core import artifact_store as store_mod  # noqa: E402
from api.tagger_core.artifact_store import (  # noqa: E402
    ArtifactStore,
    ArtifactStoreError,
    ArtifactVerifyError,
    ingest_file,
)

DATA = b"the whole ensemble, not one model file"
SHA = hashlib.sha256(DATA).hexdigest()
OTHER = b"a different artifact"
OTHER_SHA = hashlib.sha256(OTHER).hexdigest()


@pytest.fixture()
def store(tmp_path):
    return ArtifactStore(tmp_path / "artifacts")


# ── identity ─────────────────────────────────────────────────────────────

def test_path_for_rejects_non_sha(store):
    for bad in ("", "abc", "z" * 64, SHA[:-1], SHA.upper()):
        with pytest.raises(ValueError):
            store.path_for(bad)


def test_put_then_get_roundtrips(store):
    p = store.put_bytes(DATA, SHA)
    assert p.name == SHA
    assert p.read_bytes() == DATA
    assert store.get(SHA) == p
    assert store.have(SHA)


def test_put_rejects_wrong_expected_sha(store):
    with pytest.raises(ArtifactVerifyError):
        store.put_bytes(DATA, OTHER_SHA)
    # nothing was filed under the lie
    assert not store.path_for(OTHER_SHA).exists()
    assert not store.path_for(SHA).exists()


def test_put_is_idempotent_and_does_not_rewrite(store):
    p = store.put_bytes(DATA, SHA)
    mtime = p.stat().st_mtime_ns
    again = store.put_bytes(DATA, SHA)
    assert again == p
    assert p.stat().st_mtime_ns == mtime


# ── tamper detection: the point of the whole module ──────────────────────

def test_have_is_false_for_drifted_bytes(store):
    p = store.put_bytes(DATA, SHA)
    p.write_bytes(DATA + b"!")          # an admin edits the file in place
    assert not store.have(SHA), "drifted bytes must not count as present"


def test_get_refuses_drifted_bytes_instead_of_returning_them(store):
    p = store.put_bytes(DATA, SHA)
    p.write_bytes(b"swapped for something else entirely")
    with pytest.raises(ArtifactVerifyError):
        store.get(SHA)


def test_get_does_not_silently_refetch_over_drifted_bytes(store):
    """A drifted file is a tamper signal, not a cache miss. Refetching would
    paper over it and lose the evidence."""
    p = store.put_bytes(DATA, SHA)
    p.write_bytes(b"tampered")
    calls = []

    def fetch(sha):
        calls.append(sha)
        return DATA

    with pytest.raises(ArtifactVerifyError):
        store.get(SHA, fetch)
    assert calls == [], "must not fetch; the local copy failing verify is the finding"


def test_verify_all_reports_drift(store):
    store.put_bytes(DATA, SHA)
    store.put_bytes(OTHER, OTHER_SHA)
    assert store.verify_all() == []

    store.path_for(SHA).write_bytes(b"tampered")
    found = store.verify_all()
    assert [c.sha256 for c in found] == [SHA]
    assert found[0].actual_sha256 == hashlib.sha256(b"tampered").hexdigest()
    assert found[0].path == store.path_for(SHA)


def test_verify_all_is_empty_on_missing_root(store):
    assert store.verify_all() == []


def test_verify_all_negative_control_would_pass_a_broken_check(store):
    """Guard against a verify_all() that always returns []: with two artifacts
    stored and both corrupted, it must report exactly two."""
    store.put_bytes(DATA, SHA)
    store.put_bytes(OTHER, OTHER_SHA)
    store.path_for(SHA).write_bytes(b"x")
    store.path_for(OTHER_SHA).write_bytes(b"y")
    assert len(store.verify_all()) == 2


# ── fail-closed fetch ────────────────────────────────────────────────────

def test_get_fetches_once_when_absent(store):
    calls = []

    def fetch(sha):
        calls.append(sha)
        return DATA

    p = store.get(SHA, fetch)
    assert p.read_bytes() == DATA
    assert calls == [SHA]
    store.get(SHA, fetch)          # second call is served locally
    assert calls == [SHA]


def test_get_raises_when_absent_and_no_fetch(store):
    with pytest.raises(ArtifactStoreError):
        store.get(SHA)


def test_get_raises_when_fetch_fails(store):
    def fetch(sha):
        raise OSError("network down")

    with pytest.raises(ArtifactStoreError):
        store.get(SHA, fetch)
    assert not store.path_for(SHA).exists()


def test_get_refuses_fetched_bytes_that_do_not_match(store):
    """A server that hands back the wrong bytes must not poison the store."""
    def fetch(sha):
        return b"not what was asked for"

    with pytest.raises(ArtifactVerifyError):
        store.get(SHA, fetch)
    assert not store.path_for(SHA).exists()


# ── partial writes ───────────────────────────────────────────────────────

def test_failed_write_leaves_no_partial(store, monkeypatch):
    real = store_mod.sha256_file
    calls = {"n": 0}

    def flaky(path):
        calls["n"] += 1
        return "0" * 64 if calls["n"] == 1 else real(path)

    monkeypatch.setattr(store_mod, "sha256_file", flaky)
    with pytest.raises(ArtifactVerifyError):
        store.put_bytes(DATA, SHA)
    leftovers = [p.name for p in store.root.iterdir()] if store.root.is_dir() else []
    assert leftovers == [], f"partial left behind: {leftovers}"


def test_sweep_partials_removes_only_partials(store):
    store.put_bytes(DATA, SHA)
    (store.root / ".partial-deadbeef").write_bytes(b"junk")
    assert store.sweep_partials() == 1
    assert store.have(SHA)


# ── iteration / accounting ───────────────────────────────────────────────

def test_iter_lists_only_valid_shas(store):
    store.put_bytes(DATA, SHA)
    store.put_bytes(OTHER, OTHER_SHA)
    (store.root / "README").write_bytes(b"not an artifact")
    (store.root / ".partial-x").write_bytes(b"junk")
    assert sorted(store) == sorted([SHA, OTHER_SHA])


def test_usage_bytes_counts_only_artifacts(store):
    store.put_bytes(DATA, SHA)
    (store.root / "README").write_bytes(b"x" * 1000)
    assert store.usage_bytes() == len(DATA)


# ── ingest (the migration path) ──────────────────────────────────────────

def test_ingest_file_copies_and_returns_sha(store, tmp_path):
    src = tmp_path / "blob.bin"
    src.write_bytes(DATA)
    sha = ingest_file(store, src)
    assert sha == SHA
    assert store.have(SHA)
    assert src.exists(), "copy mode must leave the source alone"


def test_ingest_file_move_removes_source(store, tmp_path):
    src = tmp_path / "blob.bin"
    src.write_bytes(DATA)
    sha = ingest_file(store, src, move=True)
    assert sha == SHA
    assert store.have(SHA)
    assert not src.exists()


def test_ingest_file_is_idempotent(store, tmp_path):
    src = tmp_path / "blob.bin"
    src.write_bytes(DATA)
    assert ingest_file(store, src) == SHA
    assert ingest_file(store, src) == SHA
    assert sorted(store) == [SHA]


def test_ingest_file_move_is_idempotent_when_already_present(store, tmp_path):
    store.put_bytes(DATA, SHA)
    src = tmp_path / "blob.bin"
    src.write_bytes(DATA)
    assert ingest_file(store, src, move=True) == SHA
    assert not src.exists()
    assert store.have(SHA)


@pytest.mark.parametrize('other_ingest', [False, True])
def test_simultaneous_writers_own_their_partials(store, tmp_path, monkeypatch, other_ingest):
    from concurrent.futures import ThreadPoolExecutor
    import threading
    barrier = threading.Barrier(2)
    partials = []
    real = store_mod.sha256_file
    def synchronized(path):
        digest = real(path)
        if path.name.startswith('.partial-'):
            partials.append(path)
            barrier.wait(timeout=3)  # both bytes verified before either rename
        return digest
    monkeypatch.setattr(store_mod, 'sha256_file', synchronized)
    src = tmp_path / 'source'; src.write_bytes(DATA)
    with ThreadPoolExecutor(2) as pool:
        first = pool.submit(store.put_bytes, DATA, SHA)
        if other_ingest:
            second = pool.submit(ingest_file, store, src)
        else:
            second = pool.submit(store.put_bytes, DATA, SHA)
        first.result(timeout=5); second.result(timeout=5)
    assert len(set(partials)) == 2
    assert store.have(SHA)
    assert not list(store.root.glob('.partial-*'))
