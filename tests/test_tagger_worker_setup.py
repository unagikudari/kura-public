"""scripts/tagger_worker_setup.py lets a new install's tagger worker in: the token it writes
authenticates, and the host is on tagger_expected_worker (a claim from any other host is 409).
"""
import json
import os
import subprocess
import sys

import pytest

from full_schema import ROOT, cluster, load


@pytest.fixture(scope="module")
def pg():
    with cluster() as c:
        yield c


def _psql(url, sql):
    from urllib.parse import parse_qs, urlsplit
    u = urlsplit(url); q = parse_qs(u.query)
    out = subprocess.run(["/usr/lib/postgresql/18/bin/psql", "-h", q["host"][0], "-p", q["port"][0],
                          "-U", "postgres", "-d", u.path.lstrip("/"), "-XAtq", "-c", sql],
                         check=True, capture_output=True, text=True)
    return out.stdout.strip()


def _run(tmp_path, url, *extra):
    env_file = tmp_path / "api.env"
    env_file.write_text(f"DATABASE_URL={url}\n"); env_file.chmod(0o600)
    return subprocess.run([sys.executable, str(ROOT / "scripts/tagger_worker_setup.py"), "--environment",
                           str(env_file), *extra], capture_output=True, text=True,
                          env={**os.environ, "PYTHONPATH": str(ROOT)})


def test_the_worker_gets_a_token_and_its_host_is_expected(pg, tmp_path):
    url = load(*pg)
    _psql(url, "INSERT INTO \"user\" (id, name, rank, creation_time, avatar_style, version, password_revision) "
               "VALUES (1, 'admin', 'administrator', now(), 'gravatar', 1, 0)")
    out = tmp_path / "tagger.env"
    r = _run(tmp_path, url, "--user", "admin", "--output", str(out), "--host", "box1")
    assert r.returncode == 0, r.stderr
    assert json.loads(r.stdout)["host_added"] is True
    assert oct(out.stat().st_mode & 0o777) == "0o600"
    env = dict(line.split("=", 1) for line in out.read_text().splitlines())
    assert env["KURA_USER"] == "admin" and len(env["KURA_TOKEN"]) == 36
    assert env["KURA_AUTOTAG_HOST"] == "box1"   # what the worker reports is the host that was added
    assert env["KURA_TOKEN"] not in r.stdout + r.stderr
    assert _psql(url, f"SELECT enabled FROM user_token WHERE token = '{env['KURA_TOKEN']}'") == "t"
    assert _psql(url, "SELECT profile FROM tagger_expected_worker WHERE host = 'box1'") == "wd14"


def test_an_unknown_user_writes_no_file(pg, tmp_path):
    url = load(*pg)
    out = tmp_path / "tagger.env"
    r = _run(tmp_path, url, "--user", "nobody", "--output", str(out), "--host", "box2")
    assert r.returncode == 2 and "no_such_user" in r.stderr
    assert not out.exists()
    assert _psql(url, "SELECT count(*) FROM tagger_expected_worker") == "0"


def test_an_existing_file_is_never_overwritten(pg, tmp_path):
    url = load(*pg)
    _psql(url, "INSERT INTO \"user\" (id, name, rank, creation_time, avatar_style, version, password_revision) "
               "VALUES (1, 'admin', 'administrator', now(), 'gravatar', 1, 0)")
    out = tmp_path / "tagger.env"; out.write_text("keep\n")
    r = _run(tmp_path, url, "--user", "admin", "--output", str(out), "--host", "box3")
    assert r.returncode == 2 and out.read_text() == "keep\n"
    assert _psql(url, "SELECT count(*) FROM user_token") == "0"


def test_a_host_listed_under_another_profile_is_refused_before_anything_is_written(pg, tmp_path):
    url = load(*pg)
    _psql(url, "INSERT INTO \"user\" (id, name, rank, creation_time, avatar_style, version, password_revision) "
               "VALUES (1, 'admin', 'administrator', now(), 'gravatar', 1, 0)")
    _psql(url, "INSERT INTO tagger_expected_worker (host, description, profile) VALUES ('box4', 'x', 'full')")
    out = tmp_path / "tagger.env"
    r = _run(tmp_path, url, "--user", "admin", "--output", str(out), "--host", "box4")
    assert r.returncode == 2 and "host_has_profile_full" in r.stderr
    assert not out.exists()
    assert _psql(url, "SELECT count(*) FROM user_token") == "0"


def test_a_host_listed_without_a_profile_is_refused_too(pg, tmp_path):
    url = load(*pg)
    _psql(url, "INSERT INTO \"user\" (id, name, rank, creation_time, avatar_style, version, password_revision) "
               "VALUES (1, 'admin', 'administrator', now(), 'gravatar', 1, 0)")
    _psql(url, "INSERT INTO tagger_expected_worker (host, description) VALUES ('box5', 'x')")
    out = tmp_path / "tagger.env"
    r = _run(tmp_path, url, "--user", "admin", "--output", str(out), "--host", "box5")
    assert r.returncode == 2 and "host_has_profile_None" in r.stderr
    assert not out.exists() and _psql(url, "SELECT count(*) FROM user_token") == "0"
