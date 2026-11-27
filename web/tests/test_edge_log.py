"""edge-log.py: route collapsing, timing summary and rotation, on synthetic logs only."""
import gzip
import importlib.util
import json
import os
import signal
from pathlib import Path

spec = importlib.util.spec_from_file_location("edge_log", Path(__file__).parents[1] / "scripts/edge-log.py")
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)

UA_BROWSER = "Mozilla/5.0 (iPad; CPU OS 18_0 like Mac OS X)"
UA_WORKER = "python-requests/2.34.2"


def line(path, rt, urt="0.010", status="200", method="GET", ua=UA_BROWSER, size="100"):
    return "\t".join(["2026-11-07T05:00:00+09:00", status, method, path, rt, urt, size,
                      "HTTP/3.0", "192.0.2.1", ua]) + "\n"


def test_routes_collapse_ids_hashes_and_files():
    assert m.route("/api/post/328250/content") == "/api/post/:n/content"
    assert m.route("/pool/9172") == "/pool/:n"
    assert m.route("/api/post/99ff36988557fa0dce71cee71da424d3b5591cad") == "/api/post/:h"
    assert m.route("/_app/immutable/chunks/app.DkX9.js") == "/_app/immutable/chunks/:file"
    assert m.route("/data/posts/12/34/abc.webp") == "/data/posts/*"
    assert m.route("/") == "/"
    assert m.route("/api/posts?query=a") == "/api/posts"


def test_summary_splits_browser_and_worker_and_reports_percentiles(tmp_path):
    log = tmp_path / "access.log"
    rows = [line(f"/api/post/{i}/content", f"{(i + 1) / 1000:.3f}") for i in range(100)]
    rows += [line("/api/internal/tagging/claim", "0.200", method="POST", ua=UA_WORKER) for _ in range(5)]
    rows += [line("/api/posts", "1.000", urt="0.900, 0.050", status="502")]
    rows += ["127.0.0.1 - - [07/Nov/2026] \"GET / HTTP/1.1\" 200 1 \"-\" \"x\"\n"]  # old combined format
    log.write_text("".join(rows))
    rep = m.summarise(log)
    assert rep["requests"] == 106 and rep["unparsed_lines"] == 1
    by = {(r["client"], r["route"]): r for r in rep["routes"]}
    content = by[("browser", "GET /api/post/:n/content")]
    assert content["count"] == 100 and content["p50_ms"] == 50 and content["p95_ms"] == 95 and content["max_ms"] == 100
    assert by[("worker", "POST /api/internal/tagging/claim")]["count"] == 5
    retried = by[("browser", "GET /api/posts")]
    assert retried["origin_p95_ms"] == 950 and retried["errors_5xx"] == 1
    assert "GET /api/post/:n/content" in m.text(rep)


def test_summary_reads_gzip(tmp_path):
    p = tmp_path / "a.log.gz"
    with gzip.open(p, "wt") as fh:
        fh.write(line("/", "0.005", urt="-"))
    rep = m.summarise(p)
    assert rep["requests"] == 1 and rep["routes"][0]["origin_p95_ms"] is None


def edge_dir(tmp_path, monkeypatch, lines=30):
    edge, state = tmp_path / "edge", tmp_path / "state"
    (edge / "logs/archive").mkdir(parents=True)
    (edge / "tmp").mkdir()
    (edge / "tmp/nginx.pid").write_text("4242\n")
    (edge / "logs/access.log").write_text(line("/pool/1", "0.030") * lines)
    monkeypatch.setattr(m, "EDGE", edge)
    monkeypatch.setattr(m, "STATE", state)
    monkeypatch.setattr(m.time, "sleep", lambda s: None)
    monkeypatch.setattr(m, "nginx_pids", lambda master: [master, 4243])
    return edge, state


def nginx(monkeypatch, edge, holding):
    """A fake nginx: SIGUSR1 makes it open a fresh access.log; `holding` decides whether
    a worker still has the old inode open (a worker that has not reopened yet)."""
    sent = []

    def kill(pid, sig):
        sent.append((pid, sig))
        (edge / "logs/access.log").write_text("")

    monkeypatch.setattr(m.os, "kill", kill)
    monkeypatch.setattr(m, "holders", lambda path, pids: [4243] if holding() else [])
    return sent


def test_rotate_archives_reports_signals_and_prunes(tmp_path, monkeypatch):
    edge, state = edge_dir(tmp_path, monkeypatch)
    old = edge / "logs/archive/access-20200101-000000.log.gz"
    old.write_bytes(gzip.compress(b""))
    os.utime(old, (0, 0))
    sent = nginx(monkeypatch, edge, lambda: False)
    assert m.rotate(14) == 0
    assert sent == [(4242, signal.SIGUSR1)]
    archives = sorted((edge / "logs/archive").glob("access-*.log.gz"))
    assert len(archives) == 1 and not old.exists()
    assert gzip.decompress(archives[0].read_bytes()).decode().count("\n") == 30
    latest = json.loads((state / "reports/latest.json").read_text())
    assert latest["requests"] == 30 and latest["routes"][0]["route"] == "GET /pool/:n"
    assert not list((edge / "logs/archive").glob("*.log")) and not list((edge / "logs/archive").glob(".*"))


def test_rotate_keeps_the_log_while_a_worker_still_writes_and_collects_it_later(tmp_path, monkeypatch):
    edge, state = edge_dir(tmp_path, monkeypatch, lines=2)
    still = {"open": True}
    nginx(monkeypatch, edge, lambda: still["open"])
    monkeypatch.setattr(m.time, "monotonic", iter(range(0, 10**6, 100)).__next__)  # time runs out at once
    assert m.rotate(14, timeout=1) == 1
    plain = list((edge / "logs/archive").glob("access-*.log"))
    assert len(plain) == 1 and not list((edge / "logs/archive").glob("*.gz"))
    with open(plain[0], "a") as fh:            # the late worker's write lands in the kept file
        fh.write(line("/pool/2", "0.040"))
    still["open"] = False
    (edge / "logs/access.log").write_text("")  # nothing new to rotate this time
    assert m.rotate(14) == 0
    gz = list((edge / "logs/archive").glob("access-*.log.gz"))
    assert len(gz) == 1 and gzip.decompress(gz[0].read_bytes()).decode().count("\n") == 3
    assert not list((edge / "logs/archive").glob("access-*.log"))


def test_rotate_keeps_the_log_when_writers_cannot_be_read(tmp_path, monkeypatch):
    edge, state = edge_dir(tmp_path, monkeypatch, lines=2)
    nginx(monkeypatch, edge, lambda: False)
    monkeypatch.setattr(m, "master_pid", lambda: (_ for _ in ()).throw(ValueError("pid file")))
    monkeypatch.setattr(m.os, "kill", lambda *a: None)
    monkeypatch.setattr(m.time, "monotonic", iter(range(0, 10**6, 100)).__next__)
    (edge / "logs/archive/access-20261107-000000.log").write_text(line("/", "0.001"))
    (edge / "logs/access.log").write_text("")
    assert m.rotate(14, timeout=1) == 1
    assert (edge / "logs/archive/access-20261107-000000.log").exists()


def test_a_second_rotate_waits_for_the_first(tmp_path, monkeypatch):
    edge, state = edge_dir(tmp_path, monkeypatch)
    state.mkdir()
    import fcntl
    with open(state / "rotate.lock", "w") as held:
        fcntl.flock(held, fcntl.LOCK_EX)
        monkeypatch.setattr(m.os, "kill", lambda *a: (_ for _ in ()).throw(AssertionError("must not signal")))
        assert m.rotate(14) == 75
    assert (edge / "logs/access.log").read_text().count("\n") == 30


def test_archive_names_never_overwrite(tmp_path):
    (tmp_path / "access-20261107-040000.log.gz").write_bytes(b"x")
    (tmp_path / "access-20261107-040000-1.log").write_text("y")
    p = m.unique(tmp_path, "access-20261107-040000", ".log")
    assert p.name == "access-20261107-040000-2.log"


def test_holders_finds_an_open_inode(tmp_path):
    f = tmp_path / "x.log"
    f.write_text("")
    with open(f, "a"):
        assert m.holders(f, [os.getpid()]) == [os.getpid()]
    assert m.holders(f, [os.getpid()]) == []


def test_unreadable_descriptors_are_not_released(tmp_path, monkeypatch):
    f = tmp_path / "x.log"
    f.write_text("")
    real_iterdir = Path.iterdir

    def iterdir(self):
        if str(self).startswith("/proc/") and str(self).endswith("/fd"):
            raise PermissionError(13, "Permission denied", str(self))
        return real_iterdir(self)

    monkeypatch.setattr(Path, "iterdir", iterdir)
    with open(f, "a"):
        try:
            m.holders(f, [os.getpid()])
        except PermissionError:
            pass
        else:
            raise AssertionError("an unreadable fd table must not read as released")
    monkeypatch.setattr(m, "master_pid", lambda: os.getpid())
    monkeypatch.setattr(m, "nginx_pids", lambda master: [master])
    monkeypatch.setattr(m.time, "monotonic", iter(range(0, 10**6, 100)).__next__)
    monkeypatch.setattr(m.time, "sleep", lambda s: None)
    assert m.released(f, timeout=1) == [-1]


def test_an_exited_process_holds_nothing_but_a_missing_master_is_unknown(tmp_path):
    f = tmp_path / "x.log"
    f.write_text("")
    assert m.holders(f, [2**22 + 12345]) == []            # no such pid: exited
    try:
        m.nginx_pids(2**22 + 12345)
    except ProcessLookupError:
        pass
    else:
        raise AssertionError("a missing master must not read as no writers")


def test_rotate_keeps_the_log_when_the_fd_table_is_unreadable(tmp_path, monkeypatch):
    edge, state = edge_dir(tmp_path, monkeypatch, lines=2)
    nginx(monkeypatch, edge, lambda: False)
    monkeypatch.setattr(m, "holders", lambda path, pids: (_ for _ in ()).throw(PermissionError(13, "denied")))
    monkeypatch.setattr(m.time, "monotonic", iter(range(0, 10**6, 100)).__next__)
    assert m.rotate(14, timeout=1) == 1
    assert len(list((edge / "logs/archive").glob("access-*.log"))) == 1
    assert not list((edge / "logs/archive").glob("*.gz"))


def test_rotate_does_nothing_on_empty_log(tmp_path, monkeypatch):
    edge, state = edge_dir(tmp_path, monkeypatch)
    (edge / "logs/access.log").write_text("")
    monkeypatch.setattr(m.os, "kill", lambda *a: (_ for _ in ()).throw(AssertionError("must not signal")))
    assert m.rotate(14) == 0


def test_template_logs_timing_without_query_strings():
    conf = (Path(__file__).parents[1] / "ops/edge-nginx.conf").read_text()
    assert "access_log logs/access.log kura_timed;" in conf
    fmt = next(l for l in conf.splitlines() if "log_format kura_timed" in l)
    assert "$request_time" in fmt and "$upstream_response_time" in fmt
    assert "$uri" in fmt and "$request_uri" not in fmt and "$args" not in fmt and "$query_string" not in fmt
    assert fmt.count("\\t") == m.FIELDS - 1
