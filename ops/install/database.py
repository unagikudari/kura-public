#!/usr/bin/env python3
# Description: create a new install's database from the baseline, and upgrade any install's
#              database to head (docs/notes/fresh-install-baseline.md)
# Effects: create — roles, database, extensions, baseline load, then upgrade; upgrade —
#          `alembic upgrade head` over a superuser connection that exists only while it runs
"""Run as the user who owns the checkout; superuser steps go through `sudo` (nothing secret in
any argv: sudo logs argv).

    python3 ops/install/database.py create     # once, after ops/install/make_env.py
    python3 ops/install/database.py upgrade    # after every pull, with the services stopped

The database settings come from api/env's DATABASE_URL, which must name this machine's
PostgreSQL (the superuser steps reach it through its local socket, and both paths are checked
to be the same server before anything is written).

upgrade: some migrations replace objects owned by kura_reconciler. Nothing is granted to the
runtime role `kura` for that, and no role or password is created: ops/install/superuser_proxy.py
runs under sudo as the OS user postgres and forwards a socket that only this user can open;
alembic connects through it (peer authentication: the superuser) and becomes `kura` at once
(SET SESSION AUTHORIZATION, api/alembic/env.py); owner-only DDL switches to the owner and back
(api/alembic/owner.py). The proxy exits when this process ends, however it ends.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

REPO = Path(__file__).resolve().parents[2]
BASELINES = REPO / "api/alembic/baseline"
NAME = re.compile(r"[a-z_][a-z0-9_]*")
LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1"}


def settings(environment: Path) -> dict:
    url = next((l.split("=", 1)[1].strip().strip('"') for l in environment.read_text().splitlines()
                if l.startswith("DATABASE_URL=")), None)
    if not url:
        raise SystemExit("api/env has no DATABASE_URL (run ops/install/make_env.py first)")
    u = urlsplit(url.replace("postgresql+asyncpg://", "postgresql://"))
    query = {k: v[0] for k, v in parse_qs(u.query).items()}
    host = query.get("host") or u.hostname or "127.0.0.1"
    s = {"url": url, "host": host, "port": int(query.get("port") or u.port or 5432),
         "name": u.path.lstrip("/"), "user": unquote(u.username or ""), "password": unquote(u.password or "")}
    if s["user"] != "kura" or not NAME.fullmatch(s["name"]):
        raise SystemExit("DATABASE_URL must name the role kura and a plain database name")
    # The superuser steps use this machine's socket; a remote host would be another server.
    if host not in LOCAL_HOSTS and not host.startswith("/"):
        raise SystemExit(f"DATABASE_URL names {host}: install and upgrade run on the database's own machine")
    return s


def literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def scram_verifier(password: str) -> str:
    """What `psql \\password` sends: the server stores and logs only this, never the password."""
    salt, iterations = secrets.token_bytes(16), 4096
    salted = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, iterations)
    stored = hashlib.sha256(hmac.new(salted, b"Client Key", "sha256").digest()).digest()
    server = hmac.new(salted, b"Server Key", "sha256").digest()
    b64 = lambda b: base64.b64encode(b).decode()
    return f"SCRAM-SHA-256${iterations}:{b64(salt)}${b64(stored)}:{b64(server)}"


def superuser(s: dict, sql: str, db: str = "postgres", capture: bool = False) -> str:
    # KURA_INSTALL_PSQL (a JSON argv) replaces `sudo -u postgres psql -p <port>` — the tests'
    # private cluster; it never carries a secret (the SQL is on stdin).
    # Bound to DATABASE_URL's server: its port, and its socket directory when it names one.
    psql = json.loads(os.environ["KURA_INSTALL_PSQL"]) if os.environ.get("KURA_INSTALL_PSQL") else \
        ["sudo", "-u", "postgres", "psql", "-p", str(s["port"]),
         *(["-h", s["host"]] if s["host"].startswith("/") else [])]
    run = subprocess.run([*psql, "-d", db, "-XAtq", "-v", "ON_ERROR_STOP=1", "-f", "-"],
                         input=sql, text=True, capture_output=capture)
    if run.returncode:
        raise SystemExit(f"psql as postgres failed{': ' + run.stderr.strip()[:400] if capture else ''}")
    return run.stdout if capture else ""


def as_kura(s: dict, sql: str) -> str:
    """The API's own path: its URL, its password (in the environment, not argv)."""
    host = s["host"]
    run = subprocess.run(["psql", f"host={host} port={s['port']} user=kura dbname={s['name']}",
                          "-XAtq", "-v", "ON_ERROR_STOP=1", "-c", sql],
                         env={**os.environ, "PGPASSWORD": s["password"]}, capture_output=True, text=True)
    if run.returncode:
        raise SystemExit(f"cannot connect as kura with api/env's DATABASE_URL: {run.stderr.strip()[:300]}")
    return run.stdout.strip()


SERVER = "SELECT pg_postmaster_start_time()::text || '|' || current_setting('port')"


def same_server(s: dict) -> None:
    """Both paths reach one PostgreSQL: the same start time and port, read by each."""
    su = superuser(s, SERVER, db=s["name"], capture=True).strip()
    mine = as_kura(s, SERVER)
    if su != mine:
        raise SystemExit(f"the superuser steps and DATABASE_URL reach different servers ({su} vs {mine})")


def listeners(s: dict, unix: bool) -> list[tuple[str, int]]:
    """(local address or socket path, pid) of every listening socket on the URL's port, from
    the kernel — what is bound, not what is configured. Under sudo to see other users' pids;
    KURA_INSTALL_SS (a JSON argv prefix) replaces `sudo` in the tests."""
    prefix = json.loads(os.environ["KURA_INSTALL_SS"]) if os.environ.get("KURA_INSTALL_SS") else ["sudo"]
    args = ["ss", "-Hlxp"] if unix else ["ss", "-Hltnp", f"sport = :{s['port']}"]
    out = subprocess.run([*prefix, *args], capture_output=True, text=True, check=True).stdout
    found = []
    for line in out.splitlines():
        pid = re.search(r"pid=(\d+)", line)
        cols = line.split()
        if pid and len(cols) >= 5:
            found.append((cols[4] if unix else cols[3].rsplit(":", 1)[0].strip("[]"), int(pid.group(1))))
    return found


def target_is_the_url(s: dict) -> None:
    """Before any write: every endpoint DATABASE_URL can reach is a socket held by the very
    server the superuser path reached (its postmaster pid, from postmaster.pid). Configuration
    is not evidence — a server configured for `*` or `localhost` starts even when another holds
    ::1 on its port — so this reads the kernel's listening sockets. `same_server` confirms it
    again through kura's own login once kura exists."""
    postmaster = int(superuser(s, "SELECT split_part(pg_read_file('postmaster.pid'), E'\\n', 1)",
                               capture=True).strip())
    if s["host"].startswith("/"):
        path = f"{s['host'].rstrip('/')}/.s.PGSQL.{s['port']}"
        held = {pid for addr, pid in listeners(s, unix=True) if addr == path}
        reach = [path]
    else:
        import socket as _socket
        reach = sorted({ai[4][0] for ai in _socket.getaddrinfo(s["host"], s["port"], type=_socket.SOCK_STREAM)})
        bound = listeners(s, unix=False)
        held = set()
        for ip in reach:
            wildcard = "0.0.0.0" if ":" not in ip else "::"
            pids = {pid for addr, pid in bound if addr in (ip, wildcard, "*")}
            held |= pids or {None}
    if held != {postmaster}:
        raise SystemExit(f"DATABASE_URL's endpoints {reach} are not all held by the server the superuser path "
                         f"reached (postmaster {postmaster}; listeners {sorted(map(str, held))}); nothing was written")


def create(s: dict) -> None:
    db = s["name"]
    target_is_the_url(s)
    if superuser(s, f"SELECT 1 FROM pg_database WHERE datname = {literal(db)}", capture=True).strip():
        tables = superuser(s, "SELECT count(*) FROM pg_tables WHERE schemaname = 'public'", db=db, capture=True)
        if int(tables.strip() or 0):
            raise SystemExit(f"database {db} exists and has tables; create is for a new install only")
    missing = superuser(s, "SELECT string_agg(n, ', ') FROM unnest(ARRAY['vector','pg_trgm','fuzzystrmatch']) n "
                           "WHERE n NOT IN (SELECT name FROM pg_available_extensions)", capture=True).strip()
    if missing:
        raise SystemExit(f"PostgreSQL lacks the extensions {missing} (pgvector: postgresql-18-pgvector)")
    existing = superuser(s, "SELECT rolname FROM pg_roles WHERE rolname IN ('kura', 'kura_reconciler', 'kura_audit')",
                         capture=True).split()
    # An existing kura serves something already: its password is never changed here; api/env
    # must hold it (checked by connecting, below).
    sql = "" if "kura" in existing else f"CREATE ROLE kura LOGIN PASSWORD {literal(scram_verifier(s['password']))};\n"
    sql += "".join(f"CREATE ROLE {r} NOLOGIN;\n" for r in ("kura_reconciler", "kura_audit") if r not in existing)
    if sql:
        superuser(s, sql)
    if not superuser(s, f"SELECT 1 FROM pg_database WHERE datname = {literal(db)}", capture=True).strip():
        superuser(s, f'CREATE DATABASE "{db}" OWNER kura TEMPLATE template0 ENCODING \'UTF8\'')
    same_server(s)
    superuser(s, "CREATE EXTENSION IF NOT EXISTS vector; CREATE EXTENSION IF NOT EXISTS pg_trgm; "
                 "CREATE EXTENSION IF NOT EXISTS fuzzystrmatch;", db=db)
    baseline = sorted(BASELINES.glob("*.sql"))[-1]
    superuser(s, baseline.read_text(), db=db)
    print(f"loaded {baseline.name} into {db}")
    upgrade(s)


def upgrade(s: dict) -> None:
    target_is_the_url(s)
    same_server(s)
    socket_dir = superuser(s, "SELECT split_part(current_setting('unix_socket_directories'), ',', 1)",
                           capture=True).strip()
    listen = Path(tempfile.mkdtemp(prefix="kura-upgrade-"))           # 0700, this user's
    # KURA_INSTALL_PROXY (a JSON argv prefix) replaces `sudo python3` and the drop to postgres —
    # the tests' private cluster, whose superuser is the test's own OS user.
    test_prefix = json.loads(os.environ["KURA_INSTALL_PROXY"]) if os.environ.get("KURA_INSTALL_PROXY") else None
    proxy = subprocess.Popen([*(test_prefix if test_prefix is not None else ["sudo", "python3"]),
                              str(REPO / "ops/install/superuser_proxy.py"), "--listen-dir", str(listen),
                              "--port", str(s["port"]), "--target", f"{socket_dir}/.s.PGSQL.{s['port']}",
                              "--owner-uid", str(os.getuid()),
                              *([] if test_prefix is not None else ["--as-user", "postgres"])],
                             stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    try:
        if proxy.stdout.readline().strip() != "ready":
            raise SystemExit("the superuser socket did not start")
        superuser_name = os.environ.get("KURA_INSTALL_SUPERUSER", "postgres")
        env = {**os.environ, "KURA_MIGRATE_SESSION_AUTHORIZATION": "kura",
               "DATABASE_URL": f"postgresql+asyncpg://{superuser_name}@/{s['name']}?host={listen}&port={s['port']}"}
        run = subprocess.run([sys.executable, "-m", "alembic", "upgrade", "head"], cwd=REPO, env=env)
    finally:
        proxy.stdin.close()                       # the proxy's lifeline
        try:
            proxy.wait(timeout=30)
        except subprocess.TimeoutExpired:
            proxy.kill()
        shutil.rmtree(listen, ignore_errors=True)
    if run.returncode:
        raise SystemExit("alembic upgrade head failed")
    print("upgraded to head")


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("operation", choices=["create", "upgrade"])
    p.add_argument("--environment", type=Path, default=REPO / "api/env")
    a = p.parse_args()
    (create if a.operation == "create" else upgrade)(settings(a.environment))
    return 0


if __name__ == "__main__":
    sys.exit(main())
