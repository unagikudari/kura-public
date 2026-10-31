"""The missing-media invariant (#56) counts displayable posts whose thumbnail is nowhere, and
it never reports holding without having looked: an unreadable database or a file that cannot
be checked is "not observed". (How the release gate reads it is tested with the gate, which
is release machinery and not in the public tree.)
"""
from __future__ import annotations

import socket
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))
import media_invariant as MI  # noqa: E402


@pytest.fixture(scope="module")
def dsn():
    bindir = Path(subprocess.check_output(["pg_config", "--bindir"], text=True).strip())
    if not (bindir / "initdb").exists():
        pytest.skip("needs PostgreSQL's initdb")
    with tempfile.TemporaryDirectory(prefix="kura56-") as root:
        subprocess.run([str(bindir / "initdb"), "-D", f"{root}/db", "-A", "trust", "-U", "tester"],
                       check=True, capture_output=True)
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        ctl = [str(bindir / "pg_ctl"), "-D", f"{root}/db"]
        subprocess.run([*ctl, "-l", f"{root}/log", "-o", f"-h 127.0.0.1 -p {port} -k {root} -F", "-w", "start"],
                       check=True, capture_output=True)
        try:
            url = f"host=127.0.0.1 port={port} user=tester dbname=postgres"
            import psycopg2
            with psycopg2.connect(url) as pg, pg.cursor() as cur:
                cur.execute("CREATE TABLE post (id int PRIMARY KEY, status text, thumbnail bytea)")
                cur.execute("CREATE TABLE post_rendition (post_id int, kind text)")
                cur.execute("""INSERT INTO post VALUES (1,'ok',NULL), (2,'ok','\\x00'), (3,'ok',NULL),
                               (4,'ok',NULL), (5,'corrupt',NULL), (6,'ok',NULL)""")
                cur.execute("INSERT INTO post_rendition VALUES (3,'thumb')")
            yield url
        finally:
            subprocess.run([*ctl, "-m", "immediate", "-w", "stop"], check=True, capture_output=True)


def test_a_displayable_post_with_no_thumbnail_anywhere_is_counted(dsn, monkeypatch, tmp_path):
    # 1: nothing stored, no file → missing; 2: in the row; 3: a rendition; 4: only the legacy
    # file (a regular file); 5: not displayable (corrupt); 6: a DIRECTORY under the
    # thumbnail's name → missing, not a thumbnail
    from api.services import files
    monkeypatch.setattr(files, "names", lambda kind, pid: [tmp_path / f"{pid}.jpg"])
    (tmp_path / "4.jpg").write_bytes(b"jpeg")
    (tmp_path / "6.jpg").mkdir()
    out = MI.measure(dsn)
    assert out == {"observed": True, "checked": 3, "missing": 2, "sample": [1, 6]}


def test_a_file_that_cannot_be_checked_is_not_observed(dsn, monkeypatch, tmp_path):
    from api.services import files
    monkeypatch.setattr(files, "names", lambda kind, pid: [tmp_path / f"{pid}.jpg"])
    real = MI.os.stat

    def broken(path, *a, **k):
        if str(path).startswith(str(tmp_path)):
            raise PermissionError(13, "Permission denied")
        return real(path, *a, **k)
    monkeypatch.setattr(MI.os, "stat", broken)
    out = MI.measure(dsn)
    assert out["observed"] is False and "PermissionError" in out["error"]


def test_an_unreadable_database_is_not_observed():
    out = MI.measure("host=127.0.0.1 port=9 user=nobody dbname=none connect_timeout=2")
    assert out["observed"] is False and "error" in out
