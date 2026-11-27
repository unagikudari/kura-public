"""Run the node snapshot's SQL against temporary tables, never fleet state."""
import ast
import os
from pathlib import Path

import pytest


def test_node_snapshot_keeps_missing_stale_and_unregistered_workers():
    dsn = os.environ.get('KURA_TEST_DSN')
    if not dsn:
        pytest.skip('KURA_TEST_DSN required')
    import psycopg2
    tree = ast.parse((Path(__file__).resolve().parents[1] / 'api/routers/pools.py').read_text())
    helper = next(n for n in tree.body if isinstance(n, ast.AsyncFunctionDef) and n.name == '_dashboard_nodes')
    queries = [ast.literal_eval(n.args[0]) for n in ast.walk(helper)
               if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == 'text']
    db = psycopg2.connect(dsn)
    try:
        with db, db.cursor() as cur:
            cur.execute('SET LOCAL search_path TO pg_temp')
            cur.execute('CREATE TEMP TABLE tagger_expected_worker (host text, description text) ON COMMIT DROP')
            cur.execute('CREATE TEMP TABLE tagger_node_state (host text, release_id int, verified bool, detail text, reported_at timestamptz) ON COMMIT DROP')
            cur.executemany('INSERT INTO tagger_expected_worker VALUES (%s,%s)', [(h,'worker') for h in ('fresh','stale','dead','missing')])
            cur.executemany("INSERT INTO tagger_node_state VALUES (%s,42,true,'detail',now() - %s * interval '1 minute')", [('fresh',1),('stale',5),('dead',60),('extra',2)])
            cur.execute(queries[0]); rows = cur.fetchall()
            assert {r[0]:r[5] for r in rows} == {'fresh':'active','stale':'stale','dead':'dead','missing':'missing'}
            assert rows[-1][0] == 'missing' and rows[-1][1:5] == (None,None,None,None)
            cur.execute(queries[1]); assert [r[0] for r in cur.fetchall()] == ['extra']
            cur.execute('DELETE FROM tagger_expected_worker')
            cur.execute(queries[0]); assert cur.fetchall() == []
            cur.execute(queries[1]); assert len(cur.fetchall()) == 4
    finally:
        db.close()
