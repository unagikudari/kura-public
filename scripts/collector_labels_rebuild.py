#!/usr/bin/env python3
"""Rebuild one collector asset's label projections; immutable evidence is retained."""
import argparse
import asyncio
import json
from api.db.session import async_session, engine
from api.collector.label_service import rebuild_asset


async def main(owner, asset):
    try:
        async with async_session() as db:
            async with db.begin():
                result = await rebuild_asset(db, owner, asset)
        print(json.dumps(result))
    finally:
        await engine.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--owner", type=int, required=True)
    parser.add_argument("--asset", type=int, required=True)
    args = parser.parse_args()
    asyncio.run(main(args.owner, args.asset))
