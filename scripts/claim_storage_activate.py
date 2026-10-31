#!/usr/bin/env python3
"""Explicitly activate verified normalized claims; requires a verified backup artifact."""
import argparse
import asyncio
import hashlib
import json
from pathlib import Path
from sqlalchemy.ext.asyncio import AsyncSession,create_async_engine
from api.config import DATABASE_URL
from api.claim_storage.cutover import activate,rollback


def verify_backup(path,require_restore=False,expected='legacy'):
    manifest=json.loads(path.with_suffix('.manifest.json').read_text())
    with path.open('rb') as archive:
        digest=hashlib.file_digest(archive,'sha256').hexdigest()
    kind='normalized' if manifest.get('kind')=='normalized-claim-storage' else 'legacy'
    valid_kind=(manifest.get('schema')=='claim_storage' if kind=='normalized' else manifest.get('table')=='public.post_tag_actor_claim')
    if expected not in ('any',kind) or not valid_kind or manifest.get('file')!=path.name or not manifest.get('decompression_verified') or manifest.get('bytes')!=path.stat().st_size or manifest.get('sha256')!=digest:
        raise ValueError('Backup artifact verification failed')
    if require_restore:
        proof=json.loads(path.with_suffix('.restore-check.json').read_text())
        if not proof.get('database_restore_verified') or proof.get('archive_sha256')!=digest or proof.get('validated_foreign_keys')!=(3 if kind=='normalized' else 2):
            raise ValueError('Database restore verification is required before cutover')
        if kind=='normalized' and (proof.get('storage_kind')!='normalized' or proof.get('source_phase')!='active' or proof.get('legacy_archive_present') is not False):
            raise ValueError('Normalized backup did not contain an active canonical snapshot')
    return manifest


async def main_async(reverse=False):
    engine=create_async_engine(DATABASE_URL,pool_size=1,max_overflow=0)
    try:
        async with AsyncSession(engine) as db:
            async with db.begin():
                result=await (rollback(db) if reverse else activate(db))
        print(json.dumps(result))
    finally:await engine.dispose()


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--backup',type=Path,required=True)
    parser.add_argument('--rollback',action='store_true',help='Reconcile post-cutover changes before restoring the legacy writer')
    args=parser.parse_args()
    verify_backup(args.backup,require_restore=True)
    asyncio.run(main_async(args.rollback))


if __name__=='__main__':main()
