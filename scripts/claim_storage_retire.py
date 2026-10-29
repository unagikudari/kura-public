#!/usr/bin/env python3
"""Retire a checkpoint only after real normalized restore proof and observed idle readers."""
import argparse
import asyncio
import json
from pathlib import Path
from sqlalchemy.ext.asyncio import AsyncSession,create_async_engine
from api.config import DATABASE_URL
from api.claim_storage.retirement import retire
from scripts.claim_storage_activate import verify_backup


async def run(observation,started):
    engine=create_async_engine(DATABASE_URL,pool_size=1,max_overflow=0)
    try:
        async with AsyncSession(engine) as db:
            async with db.begin():return await retire(db,observation,started)
    finally:await engine.dispose()


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--backup',type=Path,required=True)
    p.add_argument('--observation',type=Path,required=True)
    args=p.parse_args()
    manifest=verify_backup(args.backup,require_restore=True,expected='normalized')
    proof=json.loads(args.backup.with_suffix('.restore-check.json').read_text())
    if proof.get('reopened_after_clean_shutdown') is not True:raise ValueError('Clean restore reopen is required')
    observation=json.loads(args.observation.read_text())
    print(json.dumps(asyncio.run(run(observation,manifest['started_at']))))


if __name__=='__main__':main()
