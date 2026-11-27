"""Run merge_pools' actual alias-move SQL on temp-only synthetic rows.

Set KURA_TEST_DSN to a dedicated test database. No production tables are read:
search_path is pg_temp and every table is a temp table dropped at commit.
The merge used to keep only http(s) aliases, so a provider:<folder>/<path> alias
was deleted with the dropped pool (non-HTTP source alias regression, 2026-11-04).
"""
import ast
import os
from pathlib import Path

import pytest


def _alias_sql() -> str:
    tree = ast.parse((Path(__file__).resolve().parents[1] / 'api/routers/pools.py').read_text())
    merge = next(n for n in ast.walk(tree)
                 if isinstance(n, ast.AsyncFunctionDef) and n.name == 'merge_pools')
    for call in ast.walk(merge):
        if isinstance(call, ast.Call) and isinstance(call.func, ast.Name) and call.func.id == 'text':
            sql = ast.literal_eval(call.args[0])
            if sql.startswith('UPDATE pool_url SET pool_id'):
                return sql
    raise AssertionError('alias UPDATE not found in merge_pools')


def test_merge_moves_every_source_alias_and_drops_only_sentinels():
    dsn = os.environ.get('KURA_TEST_DSN')
    if not dsn:
        pytest.skip('KURA_TEST_DSN required for the pool merge alias regression')
    import psycopg2
    sql = _alias_sql().replace(':k', '%(k)s').replace(':f', '%(f)s')
    with psycopg2.connect(dsn) as db:
        with db.cursor() as cur:
            cur.execute('SET LOCAL search_path TO pg_temp')
            cur.execute('CREATE TEMP TABLE pool_url (pool_id int, url text UNIQUE, is_primary bool) ON COMMIT DROP')
            rows = [(1, 'https://example.invalid/keep', True),
                    (1, 'provider:SHARED/x.zip', False),                    # keep already owns it
                    (2, 'https://example.invalid/drop', True),
                    (2, 'provider:EXAMPLE/folder name/2#-JPEG.zip', False),
                    (2, 'manual:pool/2', False),
                    (2, 'legacy:2', False),
                    (2, 'kura-merge:2', False)]
            cur.executemany('INSERT INTO pool_url VALUES (%s,%s,%s)', rows)
            cur.execute(sql, {'k': 1, 'f': 2})
            cur.execute('SELECT pool_id, url, is_primary FROM pool_url ORDER BY pool_id, url')
            assert cur.fetchall() == [
                (1, 'https://example.invalid/drop', False),
                (1, 'https://example.invalid/keep', True),
                (1, 'provider:SHARED/x.zip', False),
                (1, 'provider:EXAMPLE/folder name/2#-JPEG.zip', False),
                (2, 'kura-merge:2', False),
                (2, 'legacy:2', False),
                (2, 'manual:pool/2', False),
            ]
