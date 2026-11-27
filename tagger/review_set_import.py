"""One-time migration of frozen review requests; never labels or predictions."""
import argparse
import asyncio
import json
from sqlalchemy import text

async def run(directory,execute):
    from api.db.session import async_session
    from api.services.review_queues import read_queue_blobs,parse_queues
    from api.services.review_set_registry import import_sets
    async with async_session() as db:
        await db.execute(text("SET LOCAL statement_timeout='15s'"))
        await db.execute(text("SET LOCAL lock_timeout='2s'"))
        await db.execute(text("SELECT pg_advisory_xact_lock(hashtextextended('tagger_review_set_registry',0))"))
        incoming = await asyncio.to_thread(read_queue_blobs, directory)
        if incoming is None:
            raise ValueError("review_source_missing")
        parse_queues(incoming)
        result=await import_sets(db,incoming)
        if execute:
            await db.commit()
        else:
            await db.rollback()
    return dict(outcome="succeeded",state="registered" if execute else "prepared",
                reason="immutable_requests_verified",next="none" if execute else "execute",
                sets=result["sets"],inserted=result["inserted"],labels_changed=False)

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory",required=True)
    parser.add_argument("--execute",action="store_true")
    args=parser.parse_args()
    try:
        result=asyncio.run(run(args.directory,args.execute))
    except Exception as exc:
        print(json.dumps(dict(outcome="refused",state="unconfirmed",reason=type(exc).__name__,
                              next="inspect_import_inputs_and_schema")))
        return 1
    print(json.dumps(result))
    return 0

if __name__=="__main__":
    raise SystemExit(main())
