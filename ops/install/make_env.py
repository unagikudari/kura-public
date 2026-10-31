#!/usr/bin/env python3
# Description: write a new install's api/env and web/.env.server from the examples, with every
#              secret generated (docs/notes/fresh-install-baseline.md)
# Effects: creates the two files (0600); refuses if either exists
"""First step of a fresh install.

    python3 ops/install/make_env.py --origin https://kura.example.org --data-dir /srv/kura-data
    python3 ops/install/make_env.py --docker --origin http://localhost:3000

--docker writes the same two files for compose.yaml (the database at `db`, the data at /data in
the API container, the web reaching the API at `api`), and a third, .env, with what compose
itself reads: the PostgreSQL superuser's password, the kura role's (the same one DATABASE_URL
holds), the origin the web is built for, and your user and group ids (the tools service writes
files as you).

Generated, never typed: the database password (in DATABASE_URL), KURA_FS_NAME_SALT (kura#61:
the API refuses a salt shorter than 32 characters while no file is named with it),
KURA_API_KEY and the web's KURA_SESSION_SECRET. Nothing is printed: the values exist only in
the two files. Joining an existing deployment is not this: copy that node's env instead.
"""
from __future__ import annotations

import argparse
import os
import re
import secrets
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]


def fill(template: str, values: dict[str, str], drop: set[str]) -> str:
    out, seen = [], set()
    for line in template.splitlines(keepends=True):
        key = re.match(r"^([A-Z_][A-Z0-9_]*)=", line)
        if key and key.group(1) in drop:
            continue
        if key and key.group(1) in values:
            line = f"{key.group(1)}={values[key.group(1)]}\n"
            seen.add(key.group(1))
        out.append(line)
    out += [f"{k}={v}\n" for k, v in values.items() if k not in seen]
    return "".join(out)


def write_private(path: Path, text: str) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(text)


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--origin", required=True, help="the URL people open kura at, e.g. https://kura.example.org")
    p.add_argument("--data-dir", type=Path, help="where originals and renditions are kept (host install)")
    p.add_argument("--docker", action="store_true", help="settings for compose.yaml, and its .env")
    p.add_argument("--db-host", default="127.0.0.1")
    p.add_argument("--db-port", type=int, default=5432)
    p.add_argument("--db-name", default="kura")
    p.add_argument("--root", type=Path, default=REPO, help="where api/env and web/.env.server are written")
    a = p.parse_args()
    if a.docker:
        a.data_dir, a.db_host, a.db_port, a.db_name = Path("/data"), "db", 5432, "kura"
    elif a.data_dir is None:
        p.error("--data-dir is required (or --docker)")
    api_env, web_env, compose_env = a.root / "api/env", a.root / "web/.env.server", a.root / ".env"
    for path in (api_env, web_env, *([compose_env] if a.docker else [])):
        if path.exists():
            raise SystemExit(f"{path} exists; this writes a new install's settings only")
    if not re.fullmatch(r"https?://[^\s/?#]+", a.origin.rstrip("/")):
        raise SystemExit("--origin is a scheme and host (and port), nothing else")
    data_dir = a.data_dir if a.docker else a.data_dir.resolve()
    token = lambda: secrets.token_urlsafe(32)      # 43 characters, [A-Za-z0-9_-]
    password = token()
    write_private(api_env, fill((REPO / "api/env.example").read_text(), {
        "DATABASE_URL": f"postgresql+asyncpg://kura:{password}@{a.db_host}:{a.db_port}/{a.db_name}",
        "DATA_DIR": str(data_dir),
        "KURA_FS_NAME_SALT": token(),
        "KURA_API_KEY": token(),
        "KURA_URL": "http://127.0.0.1:8001",
        "KURA_CANONICAL_URL": a.origin.rstrip("/"),
    }, drop={"KURA_SECRET", "KURA_USER", "KURA_TOKEN"}))
    write_private(web_env, fill((REPO / "web/.env.server.example").read_text(), {
        "ORIGIN": a.origin.rstrip("/"),
        "KURA_SESSION_SECRET": token(),
        **({"INTERNAL_API_URL": "http://api:8001"} if a.docker else {}),
    }, drop=set()))
    if a.docker:
        write_private(compose_env, f"POSTGRES_PASSWORD={token()}\nKURA_DB_PASSWORD={password}\n"
                                   f"ORIGIN={a.origin.rstrip('/')}\n"
                                   # compose.yaml's tools service writes files as you
                                   f"KURA_UID={os.getuid()}\nKURA_GID={os.getgid()}\n")
        print(f"wrote {api_env}, {web_env} and {compose_env} (0600); next: docker compose up -d --build")
        return 0
    data_dir.mkdir(parents=True, exist_ok=True)
    print(f"wrote {api_env} and {web_env} (0600); next: ~/.local/share/kura-api/venv/bin/python ops/install/database.py create")
    return 0


if __name__ == "__main__":
    sys.exit(main())
