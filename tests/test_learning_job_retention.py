import asyncio
import json
import subprocess
import sys

from tagger import learning_job_retention as r


def _job(root, key, event, *, tag="t"):
    job = root / key
    (job / "dataset" / "images").mkdir(parents=True)
    (job / "dataset" / "images" / "1.bin").write_bytes(b"x" * 1000)
    (job / "code").mkdir()
    (job / "code" / "a.py").write_text("print()")
    (job / "baseline.pt").write_bytes(b"b" * 500)
    (job / "received").mkdir()
    (job / "received" / "result.json.bundle.pt").write_bytes(b"model")
    (job / "snapshot.json").write_text("{}")
    (job / "inputs.json").write_text(json.dumps({"tag": tag, "event": event}))
    return job


class _DB:
    def __init__(self, terminal):
        self.terminal = terminal
        self.params = None

    async def execute(self, _sql, params):
        self.params = params
        return [(e,) for e in params["ids"] if e in self.terminal]


def test_only_terminal_jobs_lose_their_working_copies(tmp_path):
    done = _job(tmp_path, r.job_key("t", 7), 7)
    running = _job(tmp_path, r.job_key("t", 8), 8)
    (tmp_path / "not-a-job").mkdir()
    db = _DB({7})
    summary = asyncio.run(r.prune_finished(db, tmp_path))
    assert db.params["terminal"] == ["registered", "refused", "settled"]
    assert summary["jobs"] == 1 and summary["bytes"] >= 1500 and summary["busy"] == 0
    assert not (done / "dataset").exists() and not (done / "baseline.pt").exists() and not (done / "code").exists()
    assert (done / "received" / "result.json.bundle.pt").read_bytes() == b"model"
    assert (done / "snapshot.json").exists() and (done / "inputs.json").exists()
    assert json.loads((done / "pruned.json").read_text())["removed"] == ["baseline.pt", "code", "dataset"]
    assert (running / "dataset").exists() and (running / "baseline.pt").exists()


def test_a_job_whose_controller_holds_the_lock_is_left_alone(tmp_path):
    import fcntl

    job = _job(tmp_path, "c" * 24, 9)
    with open(job / "controller.lock", "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        assert r.prune_dir(job) is None
    assert (job / "dataset").exists()


def test_jobs_without_readable_inputs_are_not_touched(tmp_path):
    job = _job(tmp_path, r.job_key("t", 10), 10)
    (job / "inputs.json").write_text("{broken")
    summary = asyncio.run(r.prune_finished(_DB({10}), tmp_path))
    assert summary["jobs"] == 0 and (job / "dataset").exists()


def test_worker_script_keeps_results_and_skips_locked_jobs(tmp_path):
    import fcntl

    for key in ("e" * 24, "f" * 24):
        job = tmp_path / key
        (job / "dataset").mkdir(parents=True)
        (job / "dataset" / "x").write_bytes(b"x" * 100)
        (job / "baseline.pt").write_bytes(b"b")
        (job / "result").mkdir()
        (job / "result" / "result.json.bundle.pt").write_bytes(b"m")
    with open(tmp_path / ("f" * 24) / "job.lock", "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        out = subprocess.check_output([sys.executable, "-c", r.REMOTE_SCRIPT, str(tmp_path), "e" * 24, "f" * 24,
                                       "0" * 24], text=True)
    assert json.loads(out) == {"jobs": 1, "bytes": 101, "failed": []}
    assert not (tmp_path / ("e" * 24) / "dataset").exists()
    assert (tmp_path / ("e" * 24) / "result" / "result.json.bundle.pt").exists()
    assert (tmp_path / ("f" * 24) / "dataset").exists()


def test_worker_keys_are_validated(monkeypatch):
    calls = []
    monkeypatch.setattr("tagger.remote_learning.ssh", lambda w, a, timeout: calls.append(a) or '{"jobs":0,"bytes":0}')
    assert r.prune_worker({"python": "p", "root": "/r"}, ["../../etc"]) == {"jobs": 0, "bytes": 0}
    assert not calls
    r.prune_worker({"python": "p", "root": "/r"}, ["a" * 24, "bad"])
    assert calls[0][-1] == "a" * 24 and "bad" not in calls[0]


def test_a_directory_not_named_after_its_job_is_not_touched(tmp_path):
    job = _job(tmp_path, "9" * 24, 7)  # copied/renamed: name is not job_key("t", 7)
    assert r.identity(job) is None
    assert asyncio.run(r.prune_finished(_DB({7}), tmp_path))["jobs"] == 0 and (job / "dataset").exists()


def test_one_failing_directory_does_not_stop_the_others(tmp_path, monkeypatch):
    first, second = _job(tmp_path, r.job_key("t", 1), 1), _job(tmp_path, r.job_key("t", 2), 2)
    real = r.prune_dir

    def flaky(job_dir, *a):
        if job_dir == first:
            raise PermissionError("read-only")
        return real(job_dir, *a)
    monkeypatch.setattr(r, "prune_dir", flaky)
    summary = asyncio.run(r.prune_finished(_DB({1, 2}), tmp_path))
    assert summary["failed"][0][:2] == [first.name, "PermissionError"]
    assert summary["jobs"] == 1 and not (second / "dataset").exists()
