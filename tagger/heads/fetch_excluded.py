#!/usr/bin/env python3
"""Atomically refresh a local exclusion cache from an explicit read-only source.

Both PostgreSQL DSNs must be provided by the operator. This module has no import
side effects and never assumes a host, database, username or password.
"""
from __future__ import annotations

from contextlib import closing
import os
import sys

SOURCE_SQL = """
SELECT pp.post_id
FROM pool_post pp JOIN pool p ON p.id = pp.pool_id
WHERE p.verify_status = 'excluded'
UNION
SELECT pt.post_id
FROM post_tag pt JOIN tag_name tn ON tn.tag_id = pt.tag_id
WHERE tn.name = 'tagger-exclude'
"""
# Serialize before reading the source: an older snapshot cannot overwrite a
# newer refresh merely because its network transfer finished later.
LOCK_ID = 98432117
BATCH_SIZE = 2000


def required_dsns(environ=None):
    env = os.environ if environ is None else environ
    values = []
    for key in ('KURA_EXCLUDE_SOURCE_DSN', 'KURA_EXCLUDE_TARGET_DSN'):
        value = env.get(key, '').strip()
        if not value:
            raise ValueError(f'{key} is required; configure the source and local cache explicitly')
        values.append(value)
    return tuple(values)


def refresh(source_dsn, target_dsn, *, connect=None):
    """Return the verified row count; any failure preserves the previous cache.

    Read the source with a server cursor to bound client memory. The local
    staging table, deletion, insertion, and statistics commit together. MVCC
    readers see the old or new contents, never an empty intermediate table.
    Only the scratch cache is writable, not source library tables.
    """
    import psycopg2
    from psycopg2.extras import execute_values

    connect = connect or psycopg2.connect
    with closing(connect(target_dsn, connect_timeout=10)) as target:
        with target:
            with target.cursor() as dest:
                dest.execute("SET LOCAL statement_timeout = '120s'")
                dest.execute('SELECT pg_advisory_xact_lock(%s)', (LOCK_ID,))
                dest.execute('CREATE TEMP TABLE kura_exclude_snapshot (post_id integer PRIMARY KEY) ON COMMIT DROP')
                with closing(connect(source_dsn, connect_timeout=10)) as source:
                    source.set_session(readonly=True, isolation_level='REPEATABLE READ')
                    with source.cursor() as settings:
                        settings.execute("SET LOCAL statement_timeout = '120s'")
                    with source.cursor(name='kura_exclude_source') as incoming:
                        incoming.itersize = BATCH_SIZE
                        incoming.execute(SOURCE_SQL)
                        count = 0
                        while rows := incoming.fetchmany(BATCH_SIZE):
                            if any(len(row) != 1 or type(row[0]) is not int or not 0 < row[0] <= 2147483647 for row in rows):
                                raise ValueError('source returned an invalid post identifier')
                            execute_values(dest, 'INSERT INTO kura_exclude_snapshot (post_id) VALUES %s', rows)
                            count += len(rows)
                dest.execute('CREATE TABLE IF NOT EXISTS tagger_gt_exclude (post_id integer PRIMARY KEY)')
                dest.execute('ANALYZE kura_exclude_snapshot')
                dest.execute('DELETE FROM tagger_gt_exclude e WHERE NOT EXISTS (SELECT 1 FROM kura_exclude_snapshot s WHERE s.post_id=e.post_id)')
                dest.execute('INSERT INTO tagger_gt_exclude (post_id) SELECT post_id FROM kura_exclude_snapshot ON CONFLICT DO NOTHING')
                dest.execute('SELECT count(*) FROM tagger_gt_exclude')
                if dest.fetchone()[0] != count:
                    raise RuntimeError('exclusion cache count does not match the complete source snapshot')
                dest.execute('ANALYZE tagger_gt_exclude')
        return count


def main():
    try:
        source, target = required_dsns()
        count = refresh(source, target)
    except ValueError:
        print('Exclusion sync refused: configure KURA_EXCLUDE_SOURCE_DSN and KURA_EXCLUDE_TARGET_DSN; source IDs must be valid. Previous cache retained.', file=sys.stderr)
        return 2
    except Exception as exc:
        # Database error text can contain a DSN, hostname or password. Do not
        # echo it into a service log; the exception type identifies the failure.
        print(f'Exclusion sync failed ({type(exc).__name__}); previous cache retained. Check source and target connectivity/schema.', file=sys.stderr)
        return 1
    print(f'tagger_gt_exclude: {count} posts synchronized atomically')
    return 0


if __name__ == '__main__':
    sys.exit(main())
