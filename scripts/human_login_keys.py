#!/usr/bin/env python3
"""Private canonical operator entry. Never accept keys or DSNs in argv."""
from __future__ import annotations
import argparse
import asyncio
import json
import os
from pathlib import Path
import secrets
import stat
import sys
from contextlib import contextmanager
from datetime import datetime, timezone

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.human_login_source import environment


def arguments():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--environment", type=Path, default=Path.home() / "kura/api/env")
    sub = p.add_subparsers(dest="operation", required=True)
    for operation in ("issue", "rotate", "revoke"):
        q = sub.add_parser(operation)
        q.add_argument("--user-id", type=int, required=True)
        if operation != "issue":
            q.add_argument("--predecessor-file", type=Path, required=True,
                           help="Owned private file containing the exact old key digest, not its plaintext")
        if operation != "revoke":
            q.add_argument("--output", type=Path, required=True,
                           help="Exclusive new owned 0600 key file; never stdout")
    q = sub.add_parser("import")
    q.add_argument("--cut-inventory", type=Path, required=True)
    q.add_argument("--receipt", type=Path, required=True, help="Exclusive new owned0600 cut receipt; pending state survives uncertain outcomes")
    q.add_argument("--apply", action="store_true", help="Without this, validate under custody and roll back target writes")
    q.add_argument("--staged-environment", type=Path)
    q.add_argument("--api-unit")
    q.add_argument("--api-url", help="Explicit loopback HTTP origin, no credentials or redirects")
    q.add_argument("--activation-key-file", type=Path)
    q.add_argument("--revision", help="Exact deployed40hex source commit")
    q.add_argument("--activation-timeout", type=float, help="Finite whole activation deadline in seconds")
    args=p.parse_args()
    if args.operation=="import" and args.apply:
        required=('staged_environment','api_unit','api_url','activation_key_file','revision','activation_timeout')
        if any(getattr(args,name) is None for name in required):
            p.error("--apply requires the declared activation unit/config/probe/key/revision/deadline inputs")
    return args


def private_json(path):
    return json.loads(private_text(path))


def private_text(path):
    st = path.lstat()
    if (not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid() or st.st_mode & 0o077):
        raise RuntimeError("private_input_required")
    return path.read_text()


def stage_key(path):
    # No named plaintext exists until exclusive creation succeeds. fsync before
    # SQL so a failed filesystem operation cannot leave an undelivered active key.
    raw = secrets.token_hex(32)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, "w") as f:
            f.write(raw + "\n"); f.flush(); os.fsync(f.fileno())
        d = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(d)
        finally:
            os.close(d)
    except BaseException:
        path.unlink(missing_ok=True)
        raise
    return raw


@contextmanager
def private_receipt(path):
    """Reserve once; atomically preserve the last durable complete observation."""
    from api.human_login_activation import write_private
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    os.close(fd)
    retained = {}
    def record(value):
        nonlocal retained
        next_value = {**retained, **value, "updated_at": datetime.now(timezone.utc).isoformat()}
        data = (json.dumps(next_value, sort_keys=True) + "\n").encode()
        write_private(path, data, exclusive=False)
        retained = next_value
    record({"operation": "import", "state": "pending", "started_at": datetime.now(timezone.utc).isoformat()})
    yield record


async def run(args):
    config = environment(args.environment)
    if config.get("KURA_DB_ROLE", "canonical").strip().lower() != "canonical":
        raise RuntimeError("canonical_required")
    if not config.get("DATABASE_URL"):
        raise RuntimeError("canonical_database_not_configured")
    # Read declared node data. Ambient environment cannot redirect the operator.
    os.environ["KURA_DB_ROLE"] = config.get("KURA_DB_ROLE", "canonical")
    os.environ["KURA_HUMAN_LOGIN_MODE"] = config.get("KURA_HUMAN_LOGIN_MODE", "active")
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
    from api.human_login import change_key, import_keys, key_digest
    from api.human_login_cut import SourceCut
    url = config["DATABASE_URL"].replace("postgresql://", "postgresql+asyncpg://", 1)
    engine = create_async_engine(url, hide_parameters=True)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        if args.operation == "import":
            if os.environ["KURA_HUMAN_LOGIN_MODE"] != "bootstrap":
                raise RuntimeError("bootstrap_closed")
            async with factory() as db:
                users = [dict(row) for row in (await db.execute(text(
                    'SELECT id,email,rank,broker_trust_tier FROM public."user"'))).mappings()]
            with private_receipt(args.receipt) as record:
                activation=None
                if args.apply:
                    from api.human_login_activation import Activation
                    activation=Activation(environment_file=args.environment,
                        staged_environment=args.staged_environment,unit=args.api_unit,
                        api_url=args.api_url,key_file=args.activation_key_file,
                        revision=args.revision,timeout=args.activation_timeout,
                        input_fd=sys.stdin.fileno(),record=record)
                with SourceCut(private_json(args.cut_inventory), users,
                               deadline=activation.deadline if activation else None) as cut:
                    record({"source_cut":cut.receipt()})
                    try:
                        async with factory() as db:
                            state = await import_keys(db, cut.keys, custody=cut, dry_run=not args.apply,
                                                      activation=activation)
                            target = db.info["human_login_import_receipt"]
                        active_unit=activation.verify_committed() if activation else None
                        record({"operation": "import", "state": state,
                                "eligible_count": len(cut.keys), "source_cut": cut.receipt(),
                                "target": target, "active_unit":active_unit,
                                "completed_at": datetime.now(timezone.utc).isoformat()})
                        print(json.dumps({"operation": "import", "state": state, "eligible_count": len(cut.keys)}))
                    except BaseException as exc:
                        if activation is not None:
                            if activation.commit_started:
                                outcome='committed_probe_unconfirmed' if activation.committed else 'commit_uncertain'
                            else:
                                try:
                                    activation.abort_before_commit()
                                    outcome='aborted_blocked' if activation.prepared else 'aborted_before_cut'
                                except BaseException:
                                    outcome='abort_blocked_unproven'
                            target = activation.target
                            if target is not None:
                                certainty = ('committed' if activation.committed else
                                             'unknown' if activation.commit_started else 'not_committed')
                                target = {**target, "outcome": certainty}
                            record({"operation":"import","state":outcome,
                                    "target":target,
                                    "reason":exc.reason if hasattr(exc,'reason') else type(exc).__name__})
                        raise

        else:
            predecessor = private_text(args.predecessor_file).strip() if args.operation != "issue" else None
            raw = None
            try:
                if args.operation != "revoke":
                    raw = stage_key(args.output)
                async with factory() as db:
                    await change_key(db, user_id=args.user_id, operation=args.operation,
                                     predecessor=predecessor, replacement=key_digest(raw) if raw else None)
            except BaseException:
                # COMMIT transport failure may have committed: retain private output
                # for operator reconciliation, never delete its sole recoverable copy.
                if raw is not None:
                    print("Operation not confirmed; private output retained for reconciliation", file=sys.stderr)
                raise
            print(json.dumps({"operation": args.operation, "state": "committed"}))
    finally:
        await engine.dispose()


def main():
    args = arguments()  # --help never reads config or DB
    try:
        asyncio.run(run(args))
    except Exception as exc:
        # Never stringify driver failures, which can carry credential parameters.
        from api.human_login import KeyRefused
        print(json.dumps({"error": exc.reason if isinstance(exc, KeyRefused) else "operation_refused"}), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
