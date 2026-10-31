#!/usr/bin/env python3
"""Compare claim text vs integer references in a private, disposable PostgreSQL 18."""
import argparse
import asyncio
from datetime import datetime
import json
from pathlib import Path
import subprocess
import tempfile
import time
import asyncpg
from api.collector.adapters.legacy_evidence import verify_snapshot
from scripts.collector_legacy_evidence import MAX_INPUT_BYTES


async def measure(socket, snapshot, *, port=65442, user='probe'):
    rows = verify_snapshot(snapshot).get('post_tag_actor_claim', [])
    if not rows:
        raise ValueError('No source claims to compare')
    actors = sorted({row['actor_id'] for row in rows})
    ids = {actor: i+1 for i, actor in enumerate(actors)}
    conn = await asyncpg.connect(user=user, database='postgres', host=str(socket), port=port)
    try:
        await conn.execute('''
          CREATE TABLE old_claim(post_id int,tag_id int,actor_id text,claimed_at timestamptz NOT NULL,
              PRIMARY KEY(post_id,tag_id,actor_id));
          CREATE INDEX old_actor ON old_claim(post_id,actor_id);
          CREATE TABLE source_claimant(id bigint PRIMARY KEY,actor_id text NOT NULL UNIQUE);
          CREATE TABLE new_claim(post_id int,tag_id int,claimant_id bigint REFERENCES source_claimant,
              claimed_at timestamptz NOT NULL,PRIMARY KEY(post_id,tag_id,claimant_id));
          CREATE INDEX new_actor ON new_claim(post_id,claimant_id);
        ''')
        old = [(r['post_id'],r['tag_id'],r['actor_id'],datetime.fromisoformat(r['claimed_at'])) for r in rows]
        new = [(p,t,ids[a],when) for p,t,a,when in old]
        measurements = {}
        for name in ('old', 'new'):
            before = await conn.fetchval('SELECT pg_current_wal_insert_lsn()::text')
            started = time.perf_counter()
            async with conn.transaction():
                if name == 'new':
                    await conn.copy_records_to_table('source_claimant', records=[(ids[a],a) for a in actors])
                await conn.copy_records_to_table(name+'_claim', records=old if name == 'old' else new)
            elapsed = (time.perf_counter()-started)*1000
            wal = await conn.fetchval('SELECT pg_wal_lsn_diff(pg_current_wal_insert_lsn(),$1::text::pg_lsn)::bigint',before)
            await conn.execute('VACUUM (ANALYZE) '+name+'_claim')
            if name == 'new':
                await conn.execute('VACUUM (ANALYZE) source_claimant')
            relations = [name+'_claim'] + (['source_claimant'] if name == 'new' else [])
            sizes = [await conn.fetchval('SELECT pg_total_relation_size($1::regclass)',r) for r in relations]
            measurements[name] = {'total_relation_bytes':sum(sizes),'insert_wal_bytes':wal,'insert_ms':round(elapsed,3)}
        different = await conn.fetchval('''
          WITH expanded AS (SELECT n.post_id,n.tag_id,a.actor_id,n.claimed_at
              FROM new_claim n JOIN source_claimant a ON a.id=n.claimant_id),
          left_diff AS (SELECT * FROM old_claim EXCEPT ALL SELECT * FROM expanded),
          right_diff AS (SELECT * FROM expanded EXCEPT ALL SELECT * FROM old_claim)
          SELECT (SELECT count(*) FROM left_diff)+(SELECT count(*) FROM right_diff)
        ''')
        if different:
            raise ValueError('Normalized claim round trip differs')
        # Exact per-post/per-actor self-retraction candidates; do not measure
        # a faster query with different semantics or a missing actor filter.
        post, actor = old[0][0], old[0][2]
        queries = {
          'old': 'SELECT tag_id FROM old_claim WHERE post_id=$1 AND actor_id=$2 ORDER BY tag_id',
          'new': 'SELECT tag_id FROM new_claim WHERE post_id=$1 AND claimant_id=(SELECT id FROM source_claimant WHERE actor_id=$2) ORDER BY tag_id',
        }
        results = {}
        for name, query in queries.items():
            results[name] = [r['tag_id'] for r in await conn.fetch(query,post,actor)]
            plan = json.loads(await conn.fetchval('EXPLAIN (ANALYZE,BUFFERS,FORMAT JSON) '+query,post,actor))[0]
            measurements[name]['warm_lookup_execution_ms'] = plan['Execution Time']
            measurements[name]['warm_lookup_shared_hit_blocks'] = plan['Plan'].get('Shared Hit Blocks',0)
        if results['old'] != results['new']:
            raise ValueError('Actor-scoped query differs')
        return {'format':'kura-claim-dictionary-probe-v1',
                'source_snapshot_sha256':snapshot['snapshot_sha256'],
                'rows':len(rows),'source_claimants':len(actors),'roundtrip_differences':different,
                'actor_scoped_result_match':True,'measurements':measurements,
                'limitations':'Private PostgreSQL18, local filesystem, one load per layout and one warm lookup; not a production migration, minimum-spec guarantee or full workload benchmark.'}
    finally:
        await conn.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--snapshot',type=Path,required=True)
    parser.add_argument('--work-root',type=Path,required=True,help='Explicit local filesystem for disposable database')
    parser.add_argument('--output',type=Path,required=True)
    args = parser.parse_args()
    if args.output.exists():
        parser.error('Output exists')
    with args.snapshot.open('rb') as source:
        raw = source.read(MAX_INPUT_BYTES+1)
    if len(raw)>MAX_INPUT_BYTES:
        parser.error('Snapshot exceeds input budget')
    snapshot = json.loads(raw)
    verify_snapshot(snapshot)
    binary = Path('/usr/lib/postgresql/18/bin')
    args.work_root.mkdir(parents=True,exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='claim-probe-',dir=args.work_root) as temporary:
        root = Path(temporary); socket=root/'socket';socket.mkdir()
        def command(*parts):
            subprocess.run([str(binary/parts[0]),*map(str,parts[1:])],check=True,capture_output=True)
        command('initdb','-D',root/'data','-A','trust','-U','probe','--no-locale')
        command('pg_ctl','-D',root/'data','-l',root/'log','-o',f"-k {socket} -p 65442 -c listen_addresses='' -c shared_buffers=32MB -c max_connections=10",'-w','start')
        try:
            result=asyncio.run(measure(socket,snapshot))
        finally:
            command('pg_ctl','-D',root/'data','-m','immediate','-w','stop')
    args.output.parent.mkdir(parents=True,exist_ok=True)
    # Exclusive mode, aggregate values only. No actor names or image filenames.
    with args.output.open('x') as output:
        json.dump(result,output,indent=2);output.write('\n')
    print(json.dumps(result))


if __name__=='__main__':
    main()
