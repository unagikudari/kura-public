"""Bounded, read-only collection snapshot and verified portable ZIP.

No full-media buffering, compression, legacy writes, or temporary download registry.
This alpha is synchronous; durable transfer jobs are separate work.
Empty-library restoration lives in restore.py, independently of HTTP export.
"""
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import shutil
import tempfile
from uuid import uuid4
from zipfile import ZipFile, ZIP_STORED

from sqlalchemy import text

from .errors import NotFound
from .storage import content_path, StorageIntegrityError

MAX_ENTRIES = 10_000
MAX_EVIDENCE = 20_000
MAX_BYTES = 2 * 1024**3
MAX_MANIFEST = 16 * 1024**2
RESERVE_BYTES = 256 * 1024**2
CHUNK = 1024**2


class ExportLimit(ValueError):
    pass


async def snapshot(db, owner: int, collection: int):
    # Must be the first statement after authentication's transaction is released.
    await db.execute(text('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY'))
    title = (await db.execute(text('SELECT title FROM collector_next.collection WHERE owner_id=:o AND id=:c'),
                             {'o': owner, 'c': collection})).scalar_one_or_none()
    if title is None:
        raise NotFound('作品が見つかりません。')
    params = {'o': owner, 'c': collection, 'lim': MAX_ENTRIES + 1}
    rows = [dict(r) for r in (await db.execute(text('''
        SELECT e.id,e.position,e.asset_id,a.uuid,b.sha256,b.byte_size,m.media_type,m.width,m.height,m.original_name AS asset_name,
               COALESCE(r.original_name,m.original_name) AS original_name,r.relative_path
        FROM collector_next.collection_entry e
        JOIN collector_next.asset a ON a.id=e.asset_id
        JOIN collector_next.blob b ON b.id=a.blob_id
        JOIN collector_next.media_current m ON m.asset_id=a.id
        LEFT JOIN collector_next.receipt r ON r.entry_id=e.id
        WHERE e.owner_id=:o AND e.collection_id=:c ORDER BY e.position LIMIT :lim
    '''), params)).mappings()]
    if len(rows) > MAX_ENTRIES:
        raise ExportLimit('この書き出しでは作品内10,000件まで対応しています。')
    assets = {}
    entries = []
    for row in rows:
        key = str(row['uuid'])
        assets[row['asset_id']] = {
            'id': key, 'sha256': row['sha256'].hex(), 'bytes': row['byte_size'],
            'path': 'originals/' + row['sha256'].hex(), 'media_type': row['media_type'],
            'width': row['width'], 'height': row['height'], 'original_name': row['asset_name'],
        }
        entries.append({'id': str(row['id']), 'position': row['position'], 'original': key,
                        'original_name': row['original_name'], 'relative_path': row['relative_path']})
    if sum(a['bytes'] for a in assets.values()) > MAX_BYTES:
        raise ExportLimit('この書き出しでは重複を除く原本合計2GiBまで対応しています。')
    ids = list(assets)
    params = {'o': owner, 'ids': ids, 'lim': MAX_EVIDENCE + 1}
    # Evidence names the library it came from (#152): this library for its own producers, the
    # library a restored producer came from for those, so A's and B's stay apart in C as well.
    from .library import library_id
    here = await library_id(db, create=False)        # export reads a snapshot: read only
    evidence = []
    for row in (await db.execute(text('''
        SELECT v.id,v.asset_id,t.name AS tag,t.normalized_name,v.present,v.prior_present,v.based_on_id,
               v.observed_at,v.request_key,p.kind_id,k.name AS producer_kind,p.reference,p.version AS producer_version,p.origin,
               policy.version AS policy,op.created_at AS operation_created_at,
               target.before_revision,target.after_revision
        FROM collector_next.label_observation v
        JOIN collector_next.tag t ON t.id=v.tag_id
        JOIN collector_next.label_producer p ON p.id=v.producer_id
        JOIN collector_next.label_producer_kind k ON k.id=p.kind_id
        JOIN collector_next.label_operation op ON op.owner_id=v.owner_id AND op.request_key=v.request_key
        JOIN collector_next.label_policy policy ON policy.id=op.policy_id
        JOIN collector_next.label_target target ON target.owner_id=v.owner_id
          AND target.request_key=v.request_key AND target.asset_id=v.asset_id
        WHERE v.owner_id=:o AND v.asset_id=ANY(CAST(:ids AS bigint[])) ORDER BY v.asset_id,v.id DESC LIMIT :lim
    '''), params)).mappings():
        evidence.append({'id': str(row['id']), 'original': assets[row['asset_id']]['id'],
            'type': 'organization_label', 'tag': row['tag'], 'normalized_name': row['normalized_name'],
            'present': row['present'], 'prior_present': row['prior_present'],
            'based_on': str(row['based_on_id']) if row['based_on_id'] is not None else None,
            'observed_at': row['observed_at'].isoformat(), 'operation': str(row['request_key']),
            'operation_created_at': row['operation_created_at'].isoformat(),
            'before_revision': row['before_revision'], 'after_revision': row['after_revision'],
            'producer': {'kind': row['producer_kind'], 'reference': row['reference'], 'version': row['producer_version'],
                         'origin': row['origin'] or here},
            'policy': row['policy']})
    if len(evidence) > MAX_EVIDENCE:
        raise ExportLimit('この書き出しではタグ変更根拠20,000件まで対応しています。')
    projection = {aid: {'original': a['id'], 'revision': 0, 'policy': 'collector-labels-v1', 'heads': []}
                  for aid, a in assets.items()}
    for row in (await db.execute(text('''
        SELECT s.asset_id,s.revision,p.version FROM collector_next.asset_label_state s
        JOIN collector_next.label_policy p ON p.id=s.policy_id
        WHERE s.owner_id=:o AND s.asset_id=ANY(CAST(:ids AS bigint[]))
    '''), params)).mappings():
        projection[row['asset_id']].update(revision=row['revision'], policy=row['version'])
    for row in (await db.execute(text('''
        SELECT h.asset_id,t.name,h.present,h.observation_id FROM collector_next.label_head h
        JOIN collector_next.tag t ON t.id=h.tag_id
        WHERE h.owner_id=:o AND h.asset_id=ANY(CAST(:ids AS bigint[])) ORDER BY h.asset_id,t.normalized_name
    '''), params)).mappings():
        projection[row['asset_id']]['heads'].append({'tag': row['name'], 'present': row['present'],
                                                    'evidence': str(row['observation_id'])})
    key = str(uuid4())
    manifest = {'format': 'kura-collector-exchange', 'format_version': '0.2.0', 'export_id': key,
        'source_library': here,
        'created_at': datetime.now(timezone.utc).isoformat(), 'selection_snapshot': key,
        'scope': 'one_collection', 'originals': list(assets.values()),
        'collections': [{'id': str(collection), 'title': title, 'entries': entries}],
        'evidence': evidence, 'projection': list(projection.values()),
        'results': [{'entry': e['id'], 'status': 'succeeded'} for e in entries],
        'capabilities': {'organization_label_evidence': True, 'semantic_scene_evidence': False,
                         'import_supported': True, 'resumable': False}}
    # Enforce a metadata budget before touching export storage.
    encode_manifest(manifest)
    return manifest


def encode_manifest(manifest):
    data = json.dumps(manifest, ensure_ascii=False, separators=(',', ':'), allow_nan=False).encode()
    if len(data) > MAX_MANIFEST:
        raise ExportLimit('書き出しメタデータが16MiBの上限を超えています。')
    return data


def build_archive(root: Path, manifest):
    """Return an open, rewound anonymous tempfile only after complete verification.

    Linux TemporaryFile unlinks immediately: restart/disconnect cannot orphan ZIPs.
    Caller owns close(). All exceptional paths close here.
    """
    metadata = encode_manifest(manifest)
    originals = {a['path']: a for a in manifest['originals']}
    total = sum(a['bytes'] for a in originals.values())
    if total > MAX_BYTES:
        raise ExportLimit('原本合計が2GiBの上限を超えています。')
    temp_dir = root / 'staging'
    temp_dir.mkdir(parents=True, exist_ok=True)
    required = total + len(metadata) + len(originals) * 512 + RESERVE_BYTES
    if shutil.disk_usage(temp_dir).free < required:
        raise ExportLimit('書き出し用の空き容量が不足しています。保存先を確認してください。')
    result = tempfile.TemporaryFile(dir=temp_dir)
    try:
        with ZipFile(result, 'w', compression=ZIP_STORED, allowZip64=True) as archive:
            for name, original in originals.items():
                digest = bytes.fromhex(original['sha256'])
                if name != 'originals/' + digest.hex():
                    raise StorageIntegrityError('原本の書き出し先が不正です。')
                hashed = hashlib.sha256()
                size = 0
                with content_path(root, digest).open('rb') as source, archive.open(name, 'w', force_zip64=True) as target:
                    while chunk := source.read(CHUNK):
                        size += len(chunk)
                        if size > original['bytes']:
                            raise StorageIntegrityError('原本サイズが一致しません。保存先を確認してください。')
                        if shutil.disk_usage(temp_dir).free < RESERVE_BYTES:
                            raise ExportLimit('書き出し中に空き容量が不足しました。')
                        hashed.update(chunk)
                        target.write(chunk)
                if size != original['bytes'] or hashed.digest() != digest:
                    raise StorageIntegrityError('原本の照合に失敗しました。保存先を確認してください。')
            archive.writestr('manifest.json', metadata)
        size = result.tell()
        result.seek(0)
        try:
            verify_archive(result, verify_content=False)  # payload hashes were checked during writing
        except (ValueError, KeyError, TypeError) as exc:
            raise StorageIntegrityError('書き出しメタデータの整合性を確認できません。') from exc
        result.seek(0)
        return result, size
    except BaseException:
        result.close()
        raise


def verify_archive(path, *, verify_content=True):
    with ZipFile(path) as archive:
        infos = archive.infolist()
        names = [i.filename for i in infos]
        if len(names) > MAX_ENTRIES + 1 or len(names) != len(set(names)):
            raise ValueError('Too many or duplicate ZIP members')
        info = archive.getinfo('manifest.json')
        if info.file_size > MAX_MANIFEST:
            raise ValueError('Manifest exceeds metadata budget')
        manifest = json.loads(archive.read(info))
        if manifest.get('format') != 'kura-collector-exchange' or manifest.get('format_version') not in ('0.1.0', '0.2.0'):
            raise ValueError('Unsupported exchange format/version')
        originals = manifest['originals']
        original_ids = {o['id'] for o in originals}
        paths = {o['path'] for o in originals}
        if len(original_ids) != len(originals) or len(paths) != len(originals):
            raise ValueError('Duplicate original identity')
        if set(names) != paths | {'manifest.json'} or sum(o['bytes'] for o in originals) > MAX_BYTES:
            raise ValueError('Unexpected/missing content or size budget exceeded')
        for original in originals:
            digest = bytes.fromhex(original['sha256'])
            if len(digest) != 32 or original['path'] != 'originals/' + digest.hex():
                raise ValueError('Invalid hash-based path')
            info = archive.getinfo(original['path'])
            if info.file_size != original['bytes'] or info.file_size <= 0:
                raise ValueError('Original size mismatch')
            if verify_content:
                hashed = hashlib.sha256()
                with archive.open(info) as stream:
                    while chunk := stream.read(CHUNK):
                        hashed.update(chunk)
                if hashed.digest() != digest:
                    raise ValueError('Original hash mismatch')
        if len(manifest['collections']) != 1:
            raise ValueError('Expected one collection')
        entries = manifest['collections'][0]['entries']
        entry_ids = {e['id'] for e in entries}
        positions = [e['position'] for e in entries]
        if (len(entries) > MAX_ENTRIES or len(entry_ids) != len(entries)
                or positions != sorted(set(positions)) or any(p <= 0 for p in positions)
                or any(e['original'] not in original_ids for e in entries)):
            raise ValueError('Invalid collection positions or references')
        if {e['original'] for e in entries} != original_ids:
            raise ValueError('Unreferenced originals')
        evidence = manifest['evidence']
        if len(evidence) > MAX_EVIDENCE:
            raise ValueError('Evidence budget exceeded')
        by_id = {e['id']: e for e in evidence}
        if len(by_id) != len(evidence):
            raise ValueError('Duplicate evidence identity')
        winners = {}
        for e in evidence:
            if e['original'] not in original_ids or e['type'] != 'organization_label':
                raise ValueError('Unknown evidence type or original')
            if e['based_on'] is not None:
                prior = by_id.get(e['based_on'])
                if (not prior or (prior['original'], prior['normalized_name']) != (e['original'], e['normalized_name'])
                        or prior['present'] != e['prior_present'] or int(prior['id']) >= int(e['id'])):
                    raise ValueError('Invalid evidence correction reference')
            pair = (e['original'], e['tag'])
            rank = (datetime.fromisoformat(e['observed_at']), int(e['id']))
            if pair not in winners or rank > winners[pair][0]:
                winners[pair] = (rank, e)
        projection = manifest['projection']
        if len(projection) != len(originals) or {p['original'] for p in projection} != original_ids:
            raise ValueError('Incomplete projection')
        heads = {}
        for p in projection:
            for h in p['heads']:
                pair = (p['original'], h['tag'])
                if pair in heads or pair not in winners:
                    raise ValueError('Invalid projection head')
                winning = winners[pair][1]
                if h['evidence'] != winning['id'] or h['present'] != winning['present']:
                    raise ValueError('Projection differs from immutable evidence')
                heads[pair] = h
        if set(heads) != set(winners):
            raise ValueError('Missing projection head')
        results = manifest['results']
        if (len(results) != len(entries) or {r['entry'] for r in results} != entry_ids
                or any(r['status'] != 'succeeded' for r in results)):
            raise ValueError('Incomplete output results')
        return {'verified': True, 'originals': len(originals), 'entries': len(entries),
                'label_observations': len(evidence), 'sha256_bytes': sum(o['bytes'] for o in originals)}

