"""A stranger's install, end to end, on an empty disposable PostgreSQL 18 (never DATABASE_URL).

docs/notes/fresh-install-baseline.md: make_env → database.py create (baseline, then the
privileged upgrade) → the API started as the unprivileged `kura` through its lifespan →
create_admin → sign in with the key from the file → an authenticated request. Locally the
private PG18 fixture; CI supplies KURA_FRESH_INSTALL_TEST_DSN (a superuser).
"""
import base64
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
import urllib.request
from uuid import uuid4

import psycopg2
import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from test_collector_next import postgres  # noqa:F401

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


@pytest.fixture
def cluster(request):
    """(superuser psql argv, a URL template for the new database's `kura`, superuser URL)."""
    dsn = os.environ.get("KURA_FRESH_INSTALL_TEST_DSN")
    if not dsn:
        if os.environ.get("GITHUB_ACTIONS"):
            pytest.fail("Fresh-install CI must configure its dedicated PostgreSQL service")
        dsn = request.getfixturevalue("postgres")
    url = make_url(dsn).set(drivername="postgresql")
    with psycopg2.connect(url.render_as_string(hide_password=False)) as admin:
        with admin.cursor() as cursor:
            cursor.execute("SHOW server_version_num")
            assert int(cursor.fetchone()[0]) // 10000 == 18, "Fresh-install contract requires PostgreSQL 18"
    host = url.query.get("host") or url.host or "127.0.0.1"
    port = str(url.query.get("port") or url.port or 5432)
    psql = ["psql", "-h", host, "-p", port, "-U", url.username]
    return {"psql": psql, "host": host, "port": port, "superuser_url": url, "password": url.password,
            "user": url.username}


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _install(cluster, tmp_path):
    name = "kura_fresh_" + uuid4().hex[:12]
    root = tmp_path / "install"
    (root / "api").mkdir(parents=True)
    (root / "web").mkdir()
    subprocess.run([sys.executable, str(ROOT / "ops/install/make_env.py"), "--origin", "http://127.0.0.1:1",
                    "--data-dir", str(tmp_path / "data"), "--db-name", name, "--root", str(root)],
                   check=True, capture_output=True, text=True)
    env_file = root / "api/env"
    assert oct(env_file.stat().st_mode & 0o777) == "0o600"
    # The generated URL names 127.0.0.1:5432; point it at the test cluster, keeping the password.
    lines = env_file.read_text().splitlines()
    db_line = next(l for l in lines if l.startswith("DATABASE_URL="))
    password = db_line.split("://kura:", 1)[1].split("@", 1)[0]
    url = f"postgresql+asyncpg://kura:{password}@/{name}?host={cluster['host']}&port={cluster['port']}"
    env_file.write_text("\n".join(f"DATABASE_URL={url}" if l.startswith("DATABASE_URL=") else l for l in lines) + "\n")
    # The private cluster's superuser is this OS user: psql and the proxy run without sudo.
    env = {**os.environ, "KURA_INSTALL_PSQL": json.dumps(cluster["psql"]),
           "KURA_INSTALL_PROXY": json.dumps([sys.executable]), "KURA_INSTALL_SUPERUSER": cluster["user"],
           "KURA_INSTALL_SS": "[]"}
    if cluster["password"]:
        env["PGPASSWORD"] = cluster["password"]
    run = subprocess.run([sys.executable, str(ROOT / "ops/install/database.py"), "create", "--environment", str(env_file)],
                         cwd=ROOT, env=env, capture_output=True, text=True, timeout=600)
    assert run.returncode == 0, (run.stdout + run.stderr)[-6000:]
    settings = {k: v for k, v in (l.split("=", 1) for l in env_file.read_text().splitlines()
                                  if "=" in l and not l.startswith("#"))}
    return name, env_file, settings


def test_a_stranger_installs_starts_and_signs_in(cluster, tmp_path):
    name, env_file, settings = _install(cluster, tmp_path)
    port = _free_port()
    api_env = {"PATH": os.environ["PATH"], "HOME": str(tmp_path), **settings}
    api = subprocess.Popen([sys.executable, "-m", "uvicorn", "api.main:app", "--host", "127.0.0.1", "--port", str(port)],
                           cwd=ROOT, env=api_env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    base = f"http://127.0.0.1:{port}"
    try:
        for _ in range(120):
            try:
                urllib.request.urlopen(base + "/api/build", timeout=2)
                break
            except OSError:
                assert api.poll() is None, api.stdout.read()[-6000:]
                time.sleep(0.5)
        else:
            pytest.fail("the API did not answer within 60 s")
        key_file = tmp_path / "admin.key"
        run = subprocess.run([sys.executable, str(ROOT / "scripts/create_admin.py"), "--environment", str(env_file),
                              "create", "--name", "admin", "--email", "admin@example.invalid", "--output", str(key_file)],
                             cwd=ROOT, capture_output=True, text=True, timeout=120)
        assert run.returncode == 0, run.stderr
        assert key_file.read_text().strip() not in run.stdout + run.stderr      # never printed
        assert oct(key_file.stat().st_mode & 0o777) == "0o600"
        login = urllib.request.Request(base + "/api/auth/login", method="POST",
                                       headers={"Content-Type": "application/json"},
                                       data=json.dumps({"email": "admin@example.invalid",
                                                        "key": key_file.read_text().strip()}).encode())
        session = json.load(urllib.request.urlopen(login, timeout=10))
        auth = base64.b64encode(f"{session['username']}:{session['token']}".encode()).decode()
        whoami = urllib.request.Request(base + "/api/auth/whoami", headers={"Authorization": "Token " + auth})
        assert json.load(urllib.request.urlopen(whoami, timeout=10))["rank"] == "administrator"
        # The first image, added the way the web page adds it (drop or paste → POST /api/posts),
        # is in the list at once: a new install has no cull timer to make it visible later.
        posted = _post_image(base, auth)
        assert posted["http"] == 200
        listing = urllib.request.Request(base + "/api/posts?limit=40", headers={"Authorization": "Token " + auth})
        assert [p["id"] for p in json.load(urllib.request.urlopen(listing, timeout=10))["results"]] == [posted["id"]]
        moving = _post_image(base, auth, animated=True)                  # #149: kept as an animation
        assert moving["http"] == 200 and moving["type"] == "animation"
        # A second first-admin is refused: later keys go through change_key.
        again = subprocess.run([sys.executable, str(ROOT / "scripts/create_admin.py"), "--environment", str(env_file),
                                "create", "--name", "other", "--email", "o@example.invalid",
                                "--output", str(tmp_path / "other.key")], cwd=ROOT, capture_output=True, text=True)
        assert again.returncode == 2 and "administrator_exists" in again.stderr
        # A lost key is recovered: the old one stops working, the new one signs in.
        recovered = tmp_path / "recovered.key"
        run = subprocess.run([sys.executable, str(ROOT / "scripts/create_admin.py"), "--environment", str(env_file),
                              "recover", "--user-id", "1", "--output", str(recovered)],
                             cwd=ROOT, capture_output=True, text=True, timeout=120)
        assert run.returncode == 0, run.stderr
        old = urllib.request.Request(base + "/api/auth/login", method="POST", headers={"Content-Type": "application/json"},
                                     data=json.dumps({"email": "admin@example.invalid",
                                                      "key": key_file.read_text().strip()}).encode())
        with pytest.raises(urllib.error.HTTPError):
            urllib.request.urlopen(old, timeout=10)
    finally:
        api.terminate()
        api.wait(timeout=30)


def _post_image(base, auth, animated=False):
    import io
    from PIL import Image
    image = io.BytesIO()
    if animated:
        Image.new("RGB", (64, 48), "red").save(image, format="WEBP", save_all=True,
                                               append_images=[Image.new("RGB", (64, 48), "blue")])
    else:
        Image.new("RGB", (64, 48), (40, 90, 160)).save(image, format="PNG")
    boundary = uuid4().hex
    body = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"first.{'webp' if animated else 'png'}\"\r\n"
            f"Content-Type: image/{'webp' if animated else 'png'}\r\n\r\n").encode() + image.getvalue() + f"\r\n--{boundary}--\r\n".encode()
    request = urllib.request.Request(base + "/api/posts?localize=true", method="POST", data=body,
                                     headers={"Authorization": "Token " + auth,
                                              "Content-Type": f"multipart/form-data; boundary={boundary}"})
    with urllib.request.urlopen(request, timeout=30) as response:
        return {**json.load(response), "http": response.status}


def test_owner_ddl_only_on_the_privileged_path_and_kura_stays_unprivileged(cluster, tmp_path):
    from api.alembic.owner import as_owner
    name, env_file, settings = _install(cluster, tmp_path)
    su = cluster["superuser_url"].set(drivername="postgresql+psycopg2", database=name)
    fn = "public.tagger_governed_claim_auto_tag(integer, integer)"

    class Op:
        def __init__(self, bind): self.bind = bind
        def get_bind(self): return self.bind

    with create_engine(su).connect() as c:                          # superuser-authenticated: the upgrade path
        c.execute(text("SET SESSION AUTHORIZATION kura"))
        definition = c.execute(text(f"SELECT pg_get_functiondef('{fn}'::regprocedure)")).scalar()
        as_owner(Op(c), "kura_reconciler", definition)               # CREATE OR REPLACE as the owner
        assert tuple(c.execute(text("SELECT session_user, current_user")).one()) == ("kura", "kura")
        c.execute(text("SELECT * FROM tagger_governed_claim_auto_tag(1, 60)")).all()   # a governed call as kura
        owner = c.execute(text(f"SELECT pg_get_userbyid(proowner) FROM pg_proc WHERE oid = '{fn}'::regprocedure")).scalar()
        member = c.execute(text("SELECT pg_has_role('kura', 'kura_reconciler', 'MEMBER')")).scalar()
        c.rollback()
    assert owner == "kura_reconciler" and member is False
    # A plain startup connection (authenticated as kura) cannot take the owner's identity.
    kura_url = make_url(settings["DATABASE_URL"]).set(drivername="postgresql+psycopg2")
    with create_engine(kura_url).connect() as c:
        with pytest.raises(RuntimeError, match="database.py upgrade"):
            as_owner(Op(c), "kura_reconciler", definition)


def _superuser(cluster, sql, db="postgres"):
    with psycopg2.connect(cluster["superuser_url"].set(database=db).render_as_string(hide_password=False)) as c:
        c.autocommit = True
        with c.cursor() as cur:
            cur.execute(sql)
            return cur.fetchall() if cur.description else None


def test_a_second_install_never_changes_the_existing_kura_password(cluster, tmp_path):
    """codex 1.282.0 point 4: `create` once reset the cluster-wide kura password."""
    _install(cluster, tmp_path / "one")
    before = _superuser(cluster, "SELECT rolpassword FROM pg_authid WHERE rolname = 'kura'")
    _install(cluster, tmp_path / "two")           # a new env, a new generated password
    assert _superuser(cluster, "SELECT rolpassword FROM pg_authid WHERE rolname = 'kura'") == before
    assert before[0][0].startswith("SCRAM-SHA-256$")          # only a verifier was ever sent


def test_a_remote_database_url_is_refused_before_anything_is_written(tmp_path):
    """codex 1.282.0 point 3: the superuser steps use this machine's socket."""
    sys.path.insert(0, str(ROOT / "ops/install"))
    import database
    env = tmp_path / "env"
    env.write_text("DATABASE_URL=postgresql+asyncpg://kura:x@10.1.2.3:5432/kura\n")
    with pytest.raises(SystemExit, match="own machine"):
        database.settings(env)


def test_the_superuser_socket_is_private_and_ends_with_its_parent(tmp_path):
    """codex 1.282.0 points 1-2: no role, no password; nothing outlives the upgrade."""
    import stat
    listen = tmp_path / "listen"
    listen.mkdir(mode=0o700)
    proxy = subprocess.Popen([sys.executable, str(ROOT / "ops/install/superuser_proxy.py"), "--listen-dir", str(listen),
                              "--port", "5999", "--target", str(tmp_path / "nothing"), "--owner-uid", str(os.getuid())],
                             stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    assert proxy.stdout.readline().strip() == "ready"
    sock = listen / ".s.PGSQL.5999"
    assert stat.S_IMODE(sock.stat().st_mode) == 0o600 and sock.stat().st_uid == os.getuid()
    proxy.stdin.close()                                        # the parent ends
    assert proxy.wait(timeout=10) == 0 and not sock.exists()
    other = tmp_path / "open"
    other.mkdir(mode=0o755)
    refused = subprocess.run([sys.executable, str(ROOT / "ops/install/superuser_proxy.py"), "--listen-dir", str(other),
                              "--port", "5999", "--target", "x", "--owner-uid", str(os.getuid())],
                             stdin=subprocess.DEVNULL, capture_output=True, text=True)
    assert refused.returncode != 0 and "0700" in refused.stderr


def test_the_runtime_role_check_sees_indirect_membership_and_superuser(cluster, tmp_path):
    """codex 1.282.0 point 5: kura -> mid -> kura_reconciler passed a direct-edge check."""
    from api.main import RUNTIME_ROLE_PRIVILEGES
    _install(cluster, tmp_path)
    kura = cluster["superuser_url"].set(username="kura", password=None, database="postgres")

    def check():
        with psycopg2.connect(kura.render_as_string(hide_password=False)) as c, c.cursor() as cur:
            cur.execute(RUNTIME_ROLE_PRIVILEGES)
            return cur.fetchone()
    assert check() == (False, "")
    _superuser(cluster, "CREATE ROLE kura_test_mid NOLOGIN; GRANT kura_reconciler TO kura_test_mid; GRANT kura_test_mid TO kura")
    try:
        assert check() == (False, "kura_reconciler")
    finally:
        _superuser(cluster, "REVOKE kura_test_mid FROM kura; DROP ROLE kura_test_mid")
    _superuser(cluster, "ALTER ROLE kura SUPERUSER")
    try:
        assert check()[0] is True
    finally:
        _superuser(cluster, "ALTER ROLE kura NOSUPERUSER")


def test_a_url_naming_another_local_cluster_writes_nothing_to_either(cluster, tmp_path):
    """codex 1.282.0 re-review: create wrote roles and a database before checking the server.
    Two clusters on one machine: the superuser path reaches A, DATABASE_URL names B."""
    binary = Path("/usr/lib/postgresql/18/bin")
    other = tmp_path / "b"
    (other / "socket").mkdir(parents=True)
    subprocess.run([str(binary / "initdb"), "-D", str(other / "data"), "-A", "trust", "-U", "b_admin", "--no-locale"],
                   check=True, capture_output=True)
    subprocess.run([str(binary / "pg_ctl"), "-D", str(other / "data"), "-l", str(other / "log"), "-o",
                    f"-k {other / 'socket'} -p 65441 -c listen_addresses=''", "-w", "start"], check=True, capture_output=True)
    try:
        name = "kura_wrong_" + uuid4().hex[:8]
        env_file = tmp_path / "env"
        env_file.write_text(f"DATABASE_URL=postgresql+asyncpg://kura:pw@/{name}?host={other / 'socket'}&port=65441\n")
        os.chmod(env_file, 0o600)
        roles_a = _superuser(cluster, "SELECT count(*) FROM pg_roles")
        run = subprocess.run([sys.executable, str(ROOT / "ops/install/database.py"), "create", "--environment", str(env_file)],
                             cwd=ROOT, env={**os.environ, "KURA_INSTALL_PSQL": json.dumps(cluster["psql"]), "KURA_INSTALL_SS": "[]"},
                             capture_output=True, text=True, timeout=120)
        assert run.returncode != 0 and "nothing was written" in run.stderr
        assert _superuser(cluster, "SELECT count(*) FROM pg_roles") == roles_a
        assert not _superuser(cluster, f"SELECT 1 FROM pg_database WHERE datname = '{name}'")
        b = subprocess.run(["psql", "-h", str(other / "socket"), "-p", "65441", "-U", "b_admin", "-d", "postgres", "-XAtc",
                            f"SELECT count(*) FROM pg_roles WHERE rolname LIKE 'kura%' UNION ALL "
                            f"SELECT count(*) FROM pg_database WHERE datname = '{name}'"],
                           capture_output=True, text=True, check=True).stdout.split()
        assert b == ["0", "0"]
    finally:
        subprocess.run([str(binary / "pg_ctl"), "-D", str(other / "data"), "-m", "immediate", "-w", "stop"], capture_output=True)


def _start(tmp_path, tag, port, listen):
    binary = Path("/usr/lib/postgresql/18/bin")
    root = tmp_path / tag
    (root / "socket").mkdir(parents=True)
    subprocess.run([str(binary / "initdb"), "-D", str(root / "data"), "-A", "trust", "-U", f"{tag}_admin", "--no-locale"],
                   check=True, capture_output=True)
    subprocess.run([str(binary / "pg_ctl"), "-D", str(root / "data"), "-l", str(root / "log"), "-o",
                    f"-k {root / 'socket'} -p {port} -c listen_addresses='{listen}'", "-w", "start"],
                   check=True, capture_output=True)
    return root


def _stop(root):
    subprocess.run(["/usr/lib/postgresql/18/bin/pg_ctl", "-D", str(root / "data"), "-m", "immediate", "-w", "stop"],
                   capture_output=True)


@pytest.mark.parametrize("a_listen", ["127.0.0.1", "*"])
def test_two_servers_on_one_port_but_different_addresses_are_told_apart_before_writing(tmp_path, a_listen):
    """codex 1.282.0 re-review 2: A on 127.0.0.1:P and B on [::1]:P share a port; a URL naming
    [::1]:P must not let the superuser path (reaching A) write anything."""
    port = 65442
    # b first: with "*" a is configured for ::1 too, fails to bind it, and starts anyway (codex
    # re-review 3: configured intent is not the endpoint).
    b = _start(tmp_path, "b", port, "::1")
    a = _start(tmp_path, "a", port, a_listen)
    try:
        psql_a = ["psql", "-h", str(a / "socket"), "-p", str(port), "-U", "a_admin"]
        name = "kura_v6_" + uuid4().hex[:8]
        env_file = tmp_path / "env"
        env_file.write_text(f"DATABASE_URL=postgresql+asyncpg://kura:pw@[::1]:{port}/{name}\n")
        os.chmod(env_file, 0o600)
        run = subprocess.run([sys.executable, str(ROOT / "ops/install/database.py"), "create", "--environment", str(env_file)],
                             cwd=ROOT, env={**os.environ, "KURA_INSTALL_PSQL": json.dumps(psql_a), "KURA_INSTALL_SS": "[]"},
                             capture_output=True, text=True, timeout=120)
        assert run.returncode != 0 and "nothing was written" in run.stderr, run.stderr
        for root, user in ((a, "a_admin"), (b, "b_admin")):
            out = subprocess.run(["psql", "-h", str(root / "socket"), "-p", str(port), "-U", user, "-d", "postgres", "-XAtc",
                                  f"SELECT count(*) FROM pg_roles WHERE rolname LIKE 'kura%' UNION ALL "
                                  f"SELECT count(*) FROM pg_database WHERE datname = '{name}'"],
                                 capture_output=True, text=True, check=True).stdout.split()
            assert out == ["0", "0"]
        # The address A does hold passes the same check.
        sys.path.insert(0, str(ROOT / "ops/install"))
        import database
        env_file.write_text(f"DATABASE_URL=postgresql+asyncpg://kura:pw@127.0.0.1:{port}/{name}\n")
        os.environ.update(KURA_INSTALL_PSQL=json.dumps(psql_a), KURA_INSTALL_SS="[]")
        try:
            database.target_is_the_url(database.settings(env_file))
        finally:
            del os.environ["KURA_INSTALL_PSQL"], os.environ["KURA_INSTALL_SS"]
    finally:
        _stop(a)
        _stop(b)
