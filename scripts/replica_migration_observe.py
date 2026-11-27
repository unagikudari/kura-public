"""Compare actual canonical and replica ledgers with committed migrations.

Run on a replica with its local api/env and the normal Kura canonical DB
configuration. Output is suitable for the journal. This command never stamps
a ledger, applies DDL, or sends a retired broker signal.
"""
from __future__ import annotations

import argparse
import json
import re
from contextlib import closing
from pathlib import Path

from scripts.canonical_migration_committed_check import REPO, committed_revisions, fetch_origin
from ops.install.database import settings
from tagger.kura_db import canonical_dsn

REVISION = re.compile(r'[0-9]{4}')


def observe(canonical: str, replica: str, committed: set[str]) -> dict:
    if not committed or not all(REVISION.fullmatch(x) for x in (canonical, replica)):
        raise ValueError('unreadable migration ledger or committed revisions')
    direction = 'aligned'
    if canonical > replica:
        direction = 'canonical_ahead'
    elif replica > canonical:
        direction = 'replica_ahead'
    return {
        'canonical': canonical,
        'replica': replica,
        'repository': max(committed),
        'replica_vs_canonical': direction,
        'canonical_in_repository': canonical in committed,
    }


def read_head(**connection) -> str:
    import psycopg2

    with closing(psycopg2.connect(connect_timeout=3, **connection)) as db:
        db.set_session(readonly=True)
        with db.cursor() as cursor:
            cursor.execute("SET LOCAL statement_timeout = '2s'")
            cursor.execute("SELECT version_num FROM public.kura_alembic_version")
            rows = cursor.fetchall()
    if len(rows) != 1:
        raise ValueError('expected exactly one ledger revision')
    return rows[0][0]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--environment', type=Path, default=REPO / 'api/env')
    parser.add_argument('--verified-main-ref',
                        help='Exact already verified main SHA; skips fetching locally')
    args = parser.parse_args()
    side = 'repository'
    try:
        if args.verified_main_ref:
            if not re.fullmatch(r'[0-9a-f]{40}', args.verified_main_ref):
                raise ValueError('expected a full Git commit SHA')
            committed = committed_revisions(REPO, args.verified_main_ref)
        else:
            fetch_origin(REPO)
            committed = committed_revisions(REPO)
        side = 'replica'
        local = settings(args.environment)
        replica = read_head(host=local['host'], port=local['port'],
                            user=local['user'], dbname=local['name'], password=local['password'])
        side = 'canonical'
        canonical = read_head(dsn=canonical_dsn())
        side = 'comparison'
        result = observe(canonical, replica, committed)
    except (Exception, SystemExit) as error:
        # Exceptions from a connection or Git may include configuration details.
        print(json.dumps({'status': 'unobserved', 'source': side,
                          'error_type': type(error).__name__}))
        return 2
    print(json.dumps(result))
    return int(result['replica_vs_canonical'] != 'aligned' or not result['canonical_in_repository'])


if __name__ == '__main__':
    raise SystemExit(main())
