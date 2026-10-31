#!/usr/bin/env python3
"""Restore a collector exchange ZIP into a completely EMPTY collector_next namespace.

CLI only, one transaction: any failure rolls back all metadata. Originals already
written to CAS remain as verified, reusable orphans. Output is one JSON object;
failures name an error code and manifest location, never the DSN or paths.
"""
import argparse
import asyncio
import json
import os
from pathlib import Path
import sys

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

from api.collector.restore import RestoreError, restore
from api.collector.storage import StorageIntegrityError


def owner_id(value):
    if not value.isdigit() or not 0 < int(value) <= 2**31 - 1:
        raise argparse.ArgumentTypeError('owner must be a positive integer')
    return int(value)


def database_url(value):
    # ArgumentTypeError keeps argparse from echoing the rejected value (a DSN).
    scheme, separator, rest = value.partition('://')
    if not separator or scheme not in ('postgresql', 'postgres', 'postgresql+asyncpg'):
        raise argparse.ArgumentTypeError('expected a postgresql:// URL')
    return 'postgresql+asyncpg://' + rest


async def run(args):
    engine = create_async_engine(args.database, poolclass=NullPool)
    try:
        async with AsyncSession(engine) as db:
            async with db.begin():
                return await restore(db, args.root, args.archive, args.owner)
    finally:
        await engine.dispose()


def fail(code, message):
    print(json.dumps({'restored': False, 'error': code, 'message': message}, ensure_ascii=False))
    sys.exit(1)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument('--database', type=database_url, help='target DB URL (no default)')
    target.add_argument('--database-env', metavar='NAME', help='environment variable containing the target DB URL')
    parser.add_argument('--root', type=Path, required=True, help='collector CAS root')
    parser.add_argument('--owner', type=owner_id, required=True, help='target owner user id')
    parser.add_argument('archive', type=Path)
    args = parser.parse_args()
    if args.database_env:
        try:
            args.database = database_url(os.environ[args.database_env])
        except (KeyError, argparse.ArgumentTypeError):
            fail('database_configuration', 'target database environment variable is missing or invalid')
    try:
        result = asyncio.run(run(args))
    except RestoreError as exc:
        fail(exc.code, str(exc))
    except StorageIntegrityError:
        fail('storage_integrity', 'stored original failed verification; check the collector root')
    except OSError:
        fail('io_unavailable', 'archive, storage or database connection is unavailable')
    except SQLAlchemyError:
        fail('database_rejected', 'database rejected the restore; nothing was committed')
    except Exception as exc:  # noqa: BLE001 - never echo messages that may carry paths
        fail('restore_failed', type(exc).__name__)
    print(json.dumps(result, ensure_ascii=False))


if __name__ == '__main__':
    main()
