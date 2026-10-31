"""Lossless migration artifact: dictionary references, never invented scene facts.

This is an offline source representation, not the collector's canonical schema.
An attribution identifies exact source fields; it is NOT a verified producer version.
"""
import hashlib
import json

ATTRIBUTION_FIELDS = {
    'post_tag_pin': ('pinned_by',),
    'post_tag_audit': ('actor_type', 'actor_id', 'source', 'host', 'release_id'),
    'post_tag_actor_claim': ('actor_id',),
}
REFERENCE = '__source_attribution_id'


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False,
                      separators=(',', ':'), allow_nan=False).encode('utf-8')


def fingerprint(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def verify_snapshot(snapshot):
    if not isinstance(snapshot, dict):
        raise ValueError('Invalid source snapshot')
    content = {k: v for k, v in snapshot.items() if k != 'snapshot_sha256'}
    if snapshot.get('format') != 'kura-legacy-read-audit-v2' or fingerprint(content) != snapshot.get('snapshot_sha256'):
        raise ValueError('Unsupported or damaged source snapshot')
    provenance = snapshot.get('provenance', {})
    if not isinstance(provenance, dict) or provenance.get('format') != 'kura-legacy-provenance-v1':
        raise ValueError('Missing source provenance')
    tables = provenance.get('tables', {})
    if not isinstance(tables, dict) or set(tables) - set(ATTRIBUTION_FIELDS):
        raise ValueError('Unknown provenance table')
    if any(not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows) for rows in tables.values()):
        raise ValueError('Invalid source rows')
    if sum(len(rows) for rows in tables.values()) > 1000000:
        raise ValueError('Evidence row budget exceeded')
    return tables


def compact(snapshot):
    tables = verify_snapshot(snapshot)
    # Deterministic IDs depend on exact fields, including missing vs NULL. Do
    # not infer kind/version from actor strings, hostnames, or release handles.
    identities = set()
    for name, rows in tables.items():
        for row in rows:
            if REFERENCE in row:
                raise ValueError('Reserved source column')
            identities.add(canonical({k: row[k] for k in ATTRIBUTION_FIELDS[name] if k in row}))
    identities = sorted(identities)
    ids = {key: index + 1 for index, key in enumerate(identities)}
    encoded = {}
    for name, rows in sorted(tables.items()):
        columns = sorted({key for row in rows for key in row} - set(ATTRIBUTION_FIELDS[name]))
        # Columns must be uniform within a source table; missing fields must
        # never silently become NULL during transposition.
        shapes = {tuple(sorted(row)) for row in rows}
        if len(shapes) > 1:
            raise ValueError('Inconsistent source columns')
        values = []
        for row in rows:
            attribution = canonical({k: row[k] for k in ATTRIBUTION_FIELDS[name] if k in row})
            values.append([row[key] for key in columns] + [ids[attribution]])
        encoded[name] = {'columns': columns + [REFERENCE], 'rows': values}
    metadata = {k: v for k, v in snapshot.items() if k not in ('snapshot_sha256', 'provenance')}
    metadata['provenance'] = {k: v for k, v in snapshot['provenance'].items() if k != 'tables'}
    payload = {'format': 'kura-legacy-evidence-package-v1',
               'source_snapshot_sha256': snapshot['snapshot_sha256'],
               'source_metadata': metadata,
               'attributions': [json.loads(key) for key in identities], 'tables': encoded,
               'readiness': {'ready_for_import': False, 'provenance_converted': False,
                             'interpretation': 'exact source fields; producer versions and scene meaning unresolved'}}
    payload['summary'] = {'source_rows': sum(len(rows) for rows in tables.values()),
                          'attributions': len(identities),
                          'source_evidence_json_bytes': len(canonical(tables)),
                          'normalized_evidence_json_bytes': len(canonical({'attributions': payload['attributions'], 'tables': encoded}))}
    payload['package_sha256'] = fingerprint(payload)
    # Verify the same complete source digest, including negative pins,
    # historical names, NULLs, unknown additive columns, row order and metadata.
    restore(payload)
    return payload


def restore(package):
    body = {k: v for k, v in package.items() if k != 'package_sha256'}
    if package.get('format') != 'kura-legacy-evidence-package-v1' or fingerprint(body) != package.get('package_sha256'):
        raise ValueError('Unsupported or damaged evidence package')
    tables = {}
    for name, table in package['tables'].items():
        if name not in ATTRIBUTION_FIELDS:
            raise ValueError('Unknown provenance table')
        columns = table['columns']
        if len(set(columns)) != len(columns) or columns.count(REFERENCE) != 1:
            raise ValueError('Invalid source columns')
        rows = []
        for values in table['rows']:
            if len(values) != len(columns):
                raise ValueError('Invalid source row')
            row = dict(zip(columns, values))
            reference = row.pop(REFERENCE)
            if type(reference) is not int or not 1 <= reference <= len(package['attributions']):
                raise ValueError('Invalid source attribution reference')
            fields = package['attributions'][reference-1]
            if set(fields) - set(ATTRIBUTION_FIELDS[name]) or set(fields) & set(row):
                raise ValueError('Incompatible source attribution')
            row.update(fields)
            rows.append(row)
        tables[name] = rows
    source = dict(package['source_metadata'])
    source['provenance'] = {**source['provenance'], 'tables': tables}
    source['snapshot_sha256'] = package['source_snapshot_sha256']
    verify_snapshot(source)
    return source
