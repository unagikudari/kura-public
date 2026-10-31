"""Execute the dashboard's actual SQL on temp-only synthetic audit rows.

Set KURA_TEST_DSN to a dedicated test database. No production tables are read.
"""
import ast
import os
from pathlib import Path

import pytest


def test_human_churn_uses_actor_not_operation_name():
    dsn = os.environ.get('KURA_TEST_DSN')
    if not dsn:
        pytest.skip('KURA_TEST_DSN required for PostgreSQL aggregation regression')
    import psycopg2
    tree = ast.parse((Path(__file__).resolve().parents[1] / 'api/routers/pools.py').read_text())
    assignment = next(n for n in ast.walk(tree) if isinstance(n, ast.Assign)
                      and any(isinstance(t, ast.Name) and t.id == 'tag_churn' for t in n.targets))
    sql = next(ast.literal_eval(n.args[0]) for n in ast.walk(assignment)
               if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == 'text')
    with psycopg2.connect(dsn) as db:
        with db.cursor() as cur:
            cur.execute('SET LOCAL search_path TO pg_temp')
            cur.execute('CREATE TEMP TABLE post_tag_audit (source text, actor_type text, added_tags text[], removed_tags text[], created_at timestamptz) ON COMMIT DROP')
            rows = [('manual', 'human', ['smegma'], [], 0),
                    ('bulk_remove:smegma', 'human', [], ['smegma'], 0),
                    ('future_operation', 'human', [], ['smegma'], 0),
                    ('manual', 'script', [], ['smegma'], 0),
                    ('manual', None, [], ['smegma'], 0),
                    ('auto_tag', 'script', ['smegma'], ['smegma'], 0),
                    ('bulk_remove:smegma', 'human', [], ['smegma'], 8)]
            cur.executemany("INSERT INTO post_tag_audit VALUES (%s,%s,%s,%s,now() - %s * interval '1 day')", rows)
            cur.execute(sql)
            assert cur.fetchall() == [('smegma', 1, 1, 1, 2)]
