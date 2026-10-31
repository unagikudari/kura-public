"""Unit tests for tagger/release_client.py (plan kura-tagger-registry rev2 Step 5).

Pure-logic tests -- fetch_meta_fn/fetch_data_fn/loader_fn are all fakes, no
network or torch required. sleep_fn/rand_fn are also injected so tests run
instantly instead of actually sleeping through backoff.
"""
import hashlib
import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from tagger import release_client as rc  # noqa: E402


def _noop_sleep(_):
    pass


def _zero_rand(_lo, _hi):
    return 0.0


def _ok_loader(model_path, csv_path):
    return True, "ok"


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# ── happy path ───────────────────────────────────────────────────────────────

def test_pull_writes_and_activates_on_first_success(tmp_path):
    model_bytes, csv_bytes = b"model-v1", b"csv-v1"
    meta = {"id": 7, "model_sha256": _sha(model_bytes)}

    result = rc.pull(
        fetch_meta_fn=lambda: meta,
        fetch_data_fn=lambda rid, part: model_bytes if part == "model" else csv_bytes,
        cache_dir=tmp_path, loader_fn=_ok_loader,
        sleep_fn=_noop_sleep, rand_fn=_zero_rand,
    )

    assert result.release_id == 7
    assert result.fallback is False
    assert result.model_path.read_bytes() == model_bytes
    assert result.csv_path.read_bytes() == csv_bytes
    assert not (tmp_path / ".partial" / "wd14-fixed.pt").exists()  # renamed away
    saved_meta = json.loads((tmp_path / "active" / "meta.json").read_text())
    assert saved_meta["release_id"] == 7


def test_pull_skips_redownload_when_sha256_unchanged(tmp_path):
    model_bytes, csv_bytes = b"model-v1", b"csv-v1"
    meta = {"id": 7, "model_sha256": _sha(model_bytes)}
    rc.pull(fetch_meta_fn=lambda: meta,
            fetch_data_fn=lambda rid, part: model_bytes if part == "model" else csv_bytes,
            cache_dir=tmp_path, loader_fn=_ok_loader, sleep_fn=_noop_sleep, rand_fn=_zero_rand)

    calls = []

    def counting_fetch_data(rid, part):
        calls.append((rid, part))
        raise AssertionError("should not be called -- content-addressed cache hit expected")

    result = rc.pull(fetch_meta_fn=lambda: meta, fetch_data_fn=counting_fetch_data,
                      cache_dir=tmp_path, loader_fn=_ok_loader,
                      sleep_fn=_noop_sleep, rand_fn=_zero_rand)
    assert calls == []
    assert result.release_id == 7
    assert result.fallback is False


# ── fail-closed: sha256 mismatch ────────────────────────────────────────────

def test_pull_falls_back_to_previous_verified_release_on_sha_mismatch(tmp_path):
    good_model, good_csv = b"model-v1", b"csv-v1"
    good_meta = {"id": 1, "model_sha256": _sha(good_model)}
    rc.pull(fetch_meta_fn=lambda: good_meta,
            fetch_data_fn=lambda rid, part: good_model if part == "model" else good_csv,
            cache_dir=tmp_path, loader_fn=_ok_loader, sleep_fn=_noop_sleep, rand_fn=_zero_rand)

    bad_model = b"corrupted-bytes"
    bad_meta = {"id": 2, "model_sha256": "0" * 64}  # deliberately wrong hash

    result = rc.pull(
        fetch_meta_fn=lambda: bad_meta,
        fetch_data_fn=lambda rid, part: bad_model if part == "model" else b"whatever",
        cache_dir=tmp_path, loader_fn=_ok_loader,
        sleep_fn=_noop_sleep, rand_fn=_zero_rand,
    )
    assert result.fallback is True
    assert result.release_id == 1  # the OLD, still-good release -- never clobbered
    assert result.model_path.read_bytes() == good_model  # active/ untouched by the bad pull


def test_pull_refuses_to_start_when_first_ever_pull_is_corrupt(tmp_path):
    bad_meta = {"id": 1, "model_sha256": "0" * 64}
    with pytest.raises(rc.ReleaseClientError, match="refusing to start"):
        rc.pull(fetch_meta_fn=lambda: bad_meta,
                fetch_data_fn=lambda rid, part: b"corrupted", cache_dir=tmp_path,
                loader_fn=_ok_loader, sleep_fn=_noop_sleep, rand_fn=_zero_rand)
    # No partial junk left mid-cache, and definitely nothing marked active.
    assert not (tmp_path / "active" / "wd14-fixed.pt").exists()


# ── fail-closed: load+selftest rejects even a checksum-correct file ─────────

def test_pull_falls_back_when_loader_selftest_fails_despite_correct_sha256(tmp_path):
    good_model, good_csv = b"model-v1", b"csv-v1"
    good_meta = {"id": 1, "model_sha256": _sha(good_model)}
    rc.pull(fetch_meta_fn=lambda: good_meta,
            fetch_data_fn=lambda rid, part: good_model if part == "model" else good_csv,
            cache_dir=tmp_path, loader_fn=_ok_loader, sleep_fn=_noop_sleep, rand_fn=_zero_rand)

    new_model = b"bit-perfect-but-wont-load"
    new_meta = {"id": 2, "model_sha256": _sha(new_model)}  # checksum is CORRECT

    def failing_loader(model_path, csv_path):
        return False, "simulated architecture mismatch"

    result = rc.pull(fetch_meta_fn=lambda: new_meta,
                      fetch_data_fn=lambda rid, part: new_model if part == "model" else b"csv",
                      cache_dir=tmp_path, loader_fn=failing_loader,
                      sleep_fn=_noop_sleep, rand_fn=_zero_rand)
    assert result.fallback is True
    assert result.release_id == 1
    assert result.model_path.read_bytes() == good_model


def test_pull_refuses_to_start_when_selftest_fails_and_nothing_cached(tmp_path):
    meta = {"id": 1, "model_sha256": _sha(b"whatever")}

    def failing_loader(model_path, csv_path):
        return False, "no good"

    with pytest.raises(rc.ReleaseClientError, match="refusing to start"):
        rc.pull(fetch_meta_fn=lambda: meta, fetch_data_fn=lambda rid, part: b"whatever",
                cache_dir=tmp_path, loader_fn=failing_loader,
                sleep_fn=_noop_sleep, rand_fn=_zero_rand)


# ── transient transport failures: retried with backoff, not treated as a
#    verify failure (must not fall back / give up on the first hiccup) ─────

def test_pull_retries_transient_fetch_errors_then_succeeds():
    model_bytes, csv_bytes = b"model-v1", b"csv-v1"
    meta = {"id": 3, "model_sha256": _sha(model_bytes)}
    calls = {"n": 0}
    sleeps = []

    def flaky_fetch_meta():
        calls["n"] += 1
        if calls["n"] < 3:
            raise ConnectionError("simulated network blip")
        return meta

    import tempfile
    with tempfile.TemporaryDirectory() as td:
        result = rc.pull(
            fetch_meta_fn=flaky_fetch_meta,
            fetch_data_fn=lambda rid, part: model_bytes if part == "model" else csv_bytes,
            cache_dir=Path(td), loader_fn=_ok_loader, max_retries=5,
            sleep_fn=lambda s: sleeps.append(s), rand_fn=_zero_rand,
        )
    assert calls["n"] == 3
    assert len(sleeps) == 2  # slept after the 2 failed attempts, not after success
    assert result.release_id == 3


def test_sleep_backoff_is_bounded_and_jittered_within_cap():
    seen = []
    rc._sleep_backoff(attempt=0, base=2.0, sleep_fn=seen.append, rand_fn=lambda lo, hi: hi)
    rc._sleep_backoff(attempt=10, base=2.0, sleep_fn=seen.append, rand_fn=lambda lo, hi: hi)
    assert seen[0] == pytest.approx(2.0)     # base * 2^0
    assert seen[1] == pytest.approx(60.0)    # capped, would otherwise be 2*2^10
