"""Preserve scoped legacy evidence verbatim, without resolving organizational intent."""
import json
from sqlalchemy import text

# Identifiers are fixed here, never supplied by callers. Order is the source PK.
TABLES = {
    'post_tag_pin': 'post_id,tag_id',
    'post_tag_audit': 'id',
    'post_tag_actor_claim': 'post_id,tag_id,actor_id',
}


async def capture(db, post_ids, available_tables, *, max_rows=250000, max_bytes=64*1024*1024):
    if not 1 <= max_rows <= 1000000 or not 1 <= max_bytes <= 128*1024*1024:
        raise ValueError('Invalid provenance budget')
    evidence = {}
    row_count = byte_count = 0
    for table, order in TABLES.items():
        if table not in available_tables:
            continue
        rows = []
        # Stream bounded batches: a large pool must not materialize an unbounded
        # history before the limit can be checked. JSON preserves all columns,
        # including historical names and future additive provenance fields.
        result = await db.stream(text(f'''
            SELECT to_jsonb(e)::text FROM public.{table} e
            WHERE post_id=ANY(CAST(:ids AS integer[]))
            ORDER BY {order} LIMIT :lim
        '''), {'ids': post_ids, 'lim': max_rows-row_count+1},
            execution_options={'yield_per': 100})
        try:
            async for raw in result.scalars():
                row_count += 1
                byte_count += len(raw.encode('utf-8'))
                if row_count > max_rows:
                    raise ValueError('Provenance row budget exceeded; inspection is incomplete')
                if byte_count > max_bytes:
                    raise ValueError('Provenance byte budget exceeded; inspection is incomplete')
                rows.append(json.loads(raw))
        finally:
            await result.close()
        evidence[table] = rows
    return {
        'format': 'kura-legacy-provenance-v1',
        'scope': 'all evidence rows for collection post IDs, including absent tags',
        'semantics': 'source records only; not account labels or verified scene facts',
        'tables': evidence,
        'missing_tables': sorted(set(TABLES)-set(evidence)),
        'summary': {'rows': row_count, 'source_json_bytes': byte_count,
                    'table_rows': {name: len(rows) for name, rows in evidence.items()}},
    }
