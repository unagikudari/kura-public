#!/usr/bin/env python3
"""Let this machine's tagger worker in: a token for it, and its host on the expected-worker list.

    python3 scripts/tagger_worker_setup.py --user admin --output tagger/env

--output is what the worker unit (systemd/user/kura-tagger.service) loads: KURA_URL, KURA_USER
and KURA_TOKEN, written as an exclusive new 0600 file and fsynced before any SQL, never to
stdout. Then one transaction adds the token to user_token (no expiry; disable the row to revoke
it) and the host to tagger_expected_worker — the API refuses a claim from a host it does not
expect (409). The host defaults to this machine's name; the file names it, so the worker reports
the host that was added. A host already on the list under another profile is refused before
anything is written: its claims would be refused (409) with a token that works. A failure after SQL started keeps the file: the commit may
have landed, and the file is then the only copy of the token.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.human_login_source import environment  # noqa: E402


def arguments():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--environment", type=Path, default=Path(__file__).resolve().parents[1] / "api/env")
    p.add_argument("--user", required=True, help="the kura user the worker acts as")
    p.add_argument("--output", type=Path, required=True, help="Exclusive new owned 0600 file; never stdout")
    p.add_argument("--host", default=os.environ.get("KURA_AUTOTAG_HOST") or os.uname().nodename)
    p.add_argument("--url", default="http://127.0.0.1:8001", help="the API as the worker reaches it")
    return p.parse_args()


#: The profile kura-tagger-worker.service runs (TAGGER_PROFILE=wd14).
PROFILE = "wd14"


async def profile_check(c, host: str) -> None:
    from sqlalchemy import text
    row = (await c.execute(text("SELECT profile FROM tagger_expected_worker WHERE host = :h"),
                           {"h": host})).one_or_none()
    if row is not None and row.profile != PROFILE:   # NULL too: the claim guard refuses it
        raise LookupError(f"host_has_profile_{row.profile}")


def stage(path: Path, text: str) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, "w") as f:
            f.write(text); f.flush(); os.fsync(f.fileno())
        d = os.open(path.resolve().parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(d)
        finally:
            os.close(d)
    except BaseException:
        path.unlink(missing_ok=True)
        raise


async def run(args) -> dict:
    config = environment(args.environment)
    if not config.get("DATABASE_URL"):
        raise RuntimeError("database_not_configured")
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine
    url = config["DATABASE_URL"].replace("postgresql://", "postgresql+asyncpg://", 1)
    engine = create_async_engine(url, hide_parameters=True)
    try:
        async with engine.connect() as c:          # refuse before the file exists
            user_id = (await c.execute(text('SELECT id FROM "user" WHERE name = :n'), {"n": args.user})).scalar()
            if user_id is None:
                raise LookupError("no_such_user")
            await profile_check(c, args.host)
        token = str(uuid.uuid4())                   # user_token.token is varchar(36)
        stage(args.output, f"KURA_URL={args.url}\nKURA_USER={args.user}\nKURA_TOKEN={token}\n"
                           f"KURA_AUTOTAG_HOST={args.host}\n")
        try:
            async with engine.begin() as c:
                now = datetime.now(timezone.utc).replace(tzinfo=None)
                await c.execute(text("INSERT INTO user_token (user_id, token, note, enabled, creation_time, version) "
                                     "VALUES (:u, :t, :note, true, :now, 1)"),
                                {"u": user_id, "t": token, "note": f"tagger worker {args.host}"[:128], "now": now})
                added = (await c.execute(text(
                    "INSERT INTO tagger_expected_worker (host, description, added_by, profile) "
                    "VALUES (:h, 'tagger worker (scripts/tagger_worker_setup.py)', :by, :p) "
                    "ON CONFLICT (host) DO NOTHING RETURNING host"),
                    {"h": args.host, "by": args.user, "p": PROFILE})).scalar() is not None
                await profile_check(c, args.host)    # a row added since the first check
        except LookupError:                         # refused inside the transaction: rolled back
            args.output.unlink(missing_ok=True)
            raise
        except BaseException:
            print("Operation not confirmed; private output retained for reconciliation", file=sys.stderr)
            raise
        return {"state": "committed", "user": args.user, "host": args.host, "host_added": added,
                "env_file": str(args.output)}
    finally:
        await engine.dispose()


def main() -> int:
    args = arguments()
    try:
        print(json.dumps(asyncio.run(run(args))))
    except Exception as exc:
        # Never stringify driver failures, which can carry credential parameters.
        print(json.dumps({"error": str(exc) if isinstance(exc, LookupError) else type(exc).__name__}),
              file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
