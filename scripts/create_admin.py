#!/usr/bin/env python3
"""A new install's first administrator, and a lost key's recovery (docs/notes/fresh-install-baseline.md).

    create_admin.py create --name admin --email me@example.org --output ~/kura-admin.key
    create_admin.py recover --user-id 1 --output ~/kura-admin.key

The key is written only to --output: an exclusive new 0600 file, fsynced before any SQL
(scripts/human_login_keys.stage_key), never to stdout. Once SQL has started, a failure keeps the
file — a commit whose acknowledgement was lost may have committed, and the file is then the
only copy. `recover` revokes the user's one active key and issues a new one in one transaction
(api.human_login.change_key rotate, its predecessor read from the database).
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.human_login_keys import stage_key  # noqa: E402
from scripts.human_login_source import environment  # noqa: E402


def arguments():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--environment", type=Path, default=Path(__file__).resolve().parents[1] / "api/env")
    sub = p.add_subparsers(dest="operation", required=True)
    q = sub.add_parser("create")
    q.add_argument("--name", required=True)
    q.add_argument("--email", required=True)
    q.add_argument("--output", type=Path, required=True, help="Exclusive new owned 0600 key file; never stdout")
    q = sub.add_parser("recover")
    q.add_argument("--user-id", type=int, required=True)
    q.add_argument("--output", type=Path, required=True, help="Exclusive new owned 0600 key file; never stdout")
    return p.parse_args()


async def run(args) -> dict:
    config = environment(args.environment)
    if not config.get("DATABASE_URL"):
        raise RuntimeError("database_not_configured")
    os.environ["KURA_DB_ROLE"] = config.get("KURA_DB_ROLE", "canonical")
    os.environ["KURA_HUMAN_LOGIN_MODE"] = config.get("KURA_HUMAN_LOGIN_MODE", "active")
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    from api.human_login import active_digest, change_key, create_first_admin, key_digest
    url = config["DATABASE_URL"].replace("postgresql://", "postgresql+asyncpg://", 1)
    engine = create_async_engine(url, hide_parameters=True)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        predecessor = None
        if args.operation == "recover":           # read before the key file exists
            async with factory() as db:
                predecessor = await active_digest(db, args.user_id)
        raw = stage_key(args.output)
        try:
            async with factory() as db:
                if args.operation == "create":
                    user_id = await create_first_admin(db, name=args.name, email=args.email,
                                                       replacement=key_digest(raw))
                else:
                    user_id = args.user_id
                    await change_key(db, user_id=user_id, operation="rotate",
                                     predecessor=predecessor, replacement=key_digest(raw))
        except BaseException:
            print("Operation not confirmed; private output retained for reconciliation", file=sys.stderr)
            raise
        return {"operation": args.operation, "state": "committed", "user_id": user_id, "key_file": str(args.output)}
    finally:
        await engine.dispose()


def main() -> int:
    args = arguments()
    try:
        print(json.dumps(asyncio.run(run(args))))
    except Exception as exc:
        # Never stringify driver failures, which can carry credential parameters.
        from api.human_login import KeyRefused
        print(json.dumps({"error": exc.reason if isinstance(exc, KeyRefused) else type(exc).__name__}),
              file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
