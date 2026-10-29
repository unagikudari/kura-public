#!/usr/bin/env python3
"""Inspect one shared legacy collection without copying images or changing rows."""
import argparse,asyncio,json,os
from pathlib import Path
import tempfile
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import create_async_engine,AsyncSession
from api.config import DATABASE_URL
from api.collector.adapters.legacy_audit import inspect_pool


async def inspect(args):
    engine=create_async_engine(DATABASE_URL,pool_size=1,max_overflow=0)
    try:
        async with AsyncSession(engine) as db:
            await db.execute(text('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY'))
            await db.execute(text("SET LOCAL timezone='UTC'"))
            await db.execute(text("SET LOCAL statement_timeout='15s'"))
            await db.execute(text("SET LOCAL lock_timeout='2s'"))
            return await inspect_pool(db,args.pool,safe=args.safe,max_entries=args.max_entries,max_memberships=args.max_memberships,
                include_provenance=args.include_provenance,max_provenance_rows=args.max_provenance_rows,
                max_provenance_bytes=args.max_provenance_bytes)
    finally:await engine.dispose()


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--pool',type=int,required=True);p.add_argument('--safe',action='store_true')
    p.add_argument('--max-entries',type=int,default=2000);p.add_argument('--max-memberships',type=int,default=250000)
    p.add_argument('--include-provenance',action='store_true')
    p.add_argument('--max-provenance-rows',type=int,default=250000)
    p.add_argument('--max-provenance-bytes',type=int,default=64*1024*1024)
    p.add_argument('--output',type=Path,required=True);args=p.parse_args()
    target=args.output.resolve()
    if target.exists():p.error('Output exists; choose a new inspection file')
    try:result=asyncio.run(inspect(args))
    except ValueError as exc:p.error(str(exc))
    except DBAPIError:p.error('Database read failed or timed out; no inspection was published')
    target.parent.mkdir(parents=True,exist_ok=True)
    temporary=None
    try:
        with tempfile.NamedTemporaryFile(mode='w',encoding='utf-8',dir=target.parent,delete=False) as out:
            temporary=Path(out.name);os.fchmod(out.fileno(),0o600)
            json.dump(result,out,ensure_ascii=False,indent=2);out.write('\n');out.flush();os.fsync(out.fileno())
        # Hard-link publishes without overwriting another process's audit file.
        os.link(temporary,target)
    finally:
        if temporary is not None:temporary.unlink(missing_ok=True)
    print(json.dumps({'output':str(target),'snapshot_sha256':result['snapshot_sha256'],**result['summary'],'ready_for_import':False}))

if __name__=='__main__':main()
