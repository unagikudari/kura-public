"""Bounded restore of an exchange v0.1.0 or v0.2.0 ZIP into a completely empty namespace.

prepare() is synchronous and DB-free: it reads only the size-capped manifest and
validates all metadata before any storage or DB write. restore() runs in the
caller's transaction and never commits: it locks every collector_next table
NOWAIT before metadata insertion. It refuses a non-empty namespace before CAS
writes, streams originals against declared digest/size, then rechecks emptiness
under lock and writes metadata in batches. Evidence is inserted as
recorded (provenance preserved), never replayed through label_service.apply.
Projections are rebuilt from evidence and compared with the archive.

Local integer IDs are remapped; UUIDs, times, positions and revisions are kept.
On rollback, published CAS files remain as verified, reusable orphans.
"""
import asyncio
from collections import defaultdict
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
import hashlib
import json
from pathlib import Path
import re
import shutil
import struct
from uuid import UUID, uuid5
from zipfile import BadZipFile, LargeZipFile, ZipFile, ZIP_STORED

from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from .errors import Conflict, InvalidPath
from .export import MAX_BYTES as MAX_TOTAL_BYTES, MAX_ENTRIES, MAX_EVIDENCE, MAX_MANIFEST, RESERVE_BYTES
from .labels import MAX_CHANGES, MAX_LABELS, MAX_TARGETS, POLICY_VERSION, normalize_label
from .service import validate_relative_path
from .storage import FORMATS, MAX_BYTES as MAX_ORIGINAL_BYTES, MAX_PIXELS, MediaInputError, store_media

SCHEMA_VERSION = 6
TABLES = ('asset', 'asset_label_state', 'blob', 'blob_location', 'collection', 'collection_entry',
          'import_item', 'import_operation', 'label_head', 'label_observation', 'label_operation',
          'label_policy', 'label_producer', 'label_producer_kind', 'label_target', 'media_current',
          'receipt', 'schema_version', 'tag', 'tag_usage', 'library')
# library: this install's identity, made on first use; it is not the restored data
SEEDED = frozenset({'label_policy', 'label_producer_kind', 'schema_version', 'library'})
MEDIA_TYPES = frozenset(FORMATS.values()) | {'video/webm'}
BIGINT = 2**63 - 1
LOCAL_ID = re.compile(r'[1-9][0-9]{0,18}')
SHA256 = re.compile(r'[0-9a-f]{64}')

MANIFEST_KEYS = frozenset({'format', 'format_version', 'export_id', 'created_at', 'selection_snapshot', 'scope',
                           'originals', 'collections', 'evidence', 'projection', 'results', 'capabilities'})
CAPABILITY_KEYS = frozenset({'organization_label_evidence', 'semantic_scene_evidence', 'import_supported', 'resumable'})
ORIGINAL_KEYS = frozenset({'id', 'sha256', 'bytes', 'path', 'media_type', 'width', 'height', 'original_name'})
ENTRY_KEYS = frozenset({'id', 'position', 'original', 'original_name', 'relative_path'})
EVIDENCE_KEYS = frozenset({'id', 'original', 'type', 'tag', 'normalized_name', 'present', 'prior_present', 'based_on',
                           'observed_at', 'operation', 'operation_created_at', 'before_revision', 'after_revision',
                           'producer', 'policy'})
PRODUCER_KEYS = frozenset({'kind', 'reference', 'version'})
# 0.2.0 (#152): each producer says which library it came from, and the manifest which library wrote it
FORMAT_VERSIONS = ('0.1.0', '0.2.0')
ORIGIN = re.compile(r'(unknown:)?[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}')
PROJECTION_KEYS = frozenset({'original', 'revision', 'policy', 'heads'})
HEAD_KEYS = frozenset({'tag', 'present', 'evidence'})


class RestoreError(ValueError):
    """Messages name manifest locations only: no values, DSNs or filesystem paths."""
    code = 'restore_failed'


class ArchiveInvalid(RestoreError):
    code = 'archive_invalid'


class SchemaMismatch(RestoreError):
    code = 'schema_mismatch'


class NamespaceBusy(RestoreError):
    code = 'namespace_busy'


class NamespaceNotEmpty(RestoreError):
    code = 'namespace_not_empty'


class MediaMismatch(RestoreError):
    code = 'media_mismatch'


class ProjectionMismatch(RestoreError):
    code = 'projection_mismatch'


@dataclass(frozen=True)
class RestorePlan:
    export_id: UUID
    title: str
    originals: list      # archive order; validated dicts
    entries: list        # position order
    tags: dict           # normalized key -> display name
    evidence: list       # ascending source id
    operations: dict     # request key -> created/producer/tags/assets
    heads: dict          # (asset uuid, key) -> (source evidence id, present)
    revisions: dict      # asset uuid -> revision


# ---- strict field readers -------------------------------------------------

def _obj(value, keys, where):
    if type(value) is not dict or value.keys() != keys:
        raise ArchiveInvalid(f'{where}: unexpected or missing fields')
    return value


def _list(value, where, maximum):
    if type(value) is not list or len(value) > maximum:
        raise ArchiveInvalid(f'{where}: not a list or over budget')
    return value


def _int(value, where, low, high):
    if type(value) is not int or not low <= value <= high:
        raise ArchiveInvalid(f'{where}: integer out of range')
    return value


def _bool(value, where):
    if type(value) is not bool:
        raise ArchiveInvalid(f'{where}: not a boolean')
    return value


def _str(value, where, low, high):
    if type(value) is not str or not low <= len(value) <= high or '\x00' in value:
        raise ArchiveInvalid(f'{where}: invalid text')
    return value


def _uuid(value, where):
    try:
        parsed = UUID(_str(value, where, 36, 36))
    except ValueError as exc:
        raise ArchiveInvalid(f'{where}: invalid UUID') from exc
    if str(parsed) != value:
        raise ArchiveInvalid(f'{where}: non-canonical UUID')
    return parsed


def _local_id(value, where):
    if type(value) is not str or not LOCAL_ID.fullmatch(value) or int(value) > BIGINT:
        raise ArchiveInvalid(f'{where}: invalid identifier')
    return int(value)


def _time(value, where):
    try:
        parsed = datetime.fromisoformat(_str(value, where, 1, 64))
    except ValueError as exc:
        raise ArchiveInvalid(f'{where}: invalid timestamp') from exc
    if parsed.utcoffset() is None or parsed.isoformat() != value:
        raise ArchiveInvalid(f'{where}: timestamp needs canonical ISO form with offset')
    return parsed


def _name(value, where):
    _str(value, where, 0, 255)
    if '/' in value or '\\' in value:
        raise ArchiveInvalid(f'{where}: invalid file name')
    return value


def _unique_object(pairs):
    result = dict(pairs)
    if len(result) != len(pairs):
        raise ArchiveInvalid('manifest: duplicate key')
    return result


def _no_constant(value):
    raise ArchiveInvalid('manifest: non-finite number')


def _no_float(value):
    raise ArchiveInvalid('manifest: fractional number')


def _decode(data: bytes):
    try:
        return json.loads(data.decode('utf-8'), object_pairs_hook=_unique_object,
                          parse_constant=_no_constant, parse_float=_no_float)
    except RestoreError:
        raise
    except (ValueError, RecursionError) as exc:
        raise ArchiveInvalid('manifest: not strict UTF-8 JSON') from exc


# Bound ZIP central-directory allocation before ZipFile parses it. All archives
# produced by our 2GiB/10,000-entry exporter fit these limits, including ZIP64
# end records used when offsets exceed Python's 2GiB ZIP64 threshold.
@contextmanager
def _archive_source(archive):
    owned = not hasattr(archive, 'read')
    source = open(archive, 'rb') if owned else archive
    try:
        source.seek(0, 2)
        size = source.tell()
        if size > MAX_TOTAL_BYTES + MAX_MANIFEST + (MAX_ENTRIES + 1) * 512 + 66581:
            raise ArchiveInvalid('archive: file exceeds size budget')
        source.seek(max(0, size - 65557))
        tail = source.read(65557)
        end = tail.rfind(b'PK\x05\x06')
        if end < 0 or len(tail) - end < 22:
            raise ArchiveInvalid('archive: missing end record')
        _, disk, directory_disk, count_disk, count, central_size, offset, comment = struct.unpack(
            '<4s4H2LH', tail[end:end + 22])
        end_offset = size - len(tail) + end
        if (disk or directory_disk or count_disk != count or count > MAX_ENTRIES + 1
                or central_size > (MAX_ENTRIES + 1) * 256 + 1024
                or len(tail) != end + 22 + comment
                or not 0 <= end_offset - offset - central_size <= 76):
            raise ArchiveInvalid('archive: unsupported or oversized central directory')
        source.seek(max(0, end_offset - 20))
        locator = source.read(20)
        gap = end_offset - offset - central_size
        if locator.startswith(b'PK\x06\x07'):
            _, locator_disk, record_offset, disks = struct.unpack('<4sLQL', locator)
            if gap != 76 or locator_disk or disks != 1 or record_offset != end_offset - 76:
                raise ArchiveInvalid('archive: unsupported ZIP64 locator')
            source.seek(record_offset)
            record = struct.unpack('<4sQ2H2L4Q', source.read(56))
            if (record[0] != b'PK\x06\x06' or record[1] != 44
                    or record[4:] != (disk, directory_disk, count_disk, count, central_size, offset)):
                raise ArchiveInvalid('archive: ZIP64 directory differs from bounded end record')
        elif gap:
            raise ArchiveInvalid('archive: unexpected records after central directory')
        source.seek(0)
        yield source
    finally:
        if owned:
            source.close()


@contextmanager
def _checked_zip(archive):
    with _archive_source(archive) as source, ZipFile(source) as bundle:
        yield bundle


# ---- preflight ------------------------------------------------------------

def prepare(archive) -> RestorePlan:
    """Read and fully validate metadata. Bounded by the exporter's own limits."""
    try:
        with _checked_zip(archive) as bundle:
            infos = bundle.infolist()
            if len(infos) > MAX_ENTRIES + 1:
                raise ArchiveInvalid('archive: too many members')
            members = {}
            for info in infos:
                if (info.filename in members or info.is_dir() or info.flag_bits & 0x1
                        or info.compress_type != ZIP_STORED):
                    raise ArchiveInvalid('archive: duplicate, directory, encrypted or compressed member')
                members[info.filename] = info
            info = members.get('manifest.json')
            if info is None or info.file_size > MAX_MANIFEST:
                raise ArchiveInvalid('archive: manifest missing or over metadata budget')
            with bundle.open(info) as stream:
                data = stream.read(MAX_MANIFEST + 1)
            if len(data) > MAX_MANIFEST:
                raise ArchiveInvalid('archive: manifest over metadata budget')
            plan = _validate(_decode(data))
            if members.keys() != {o['path'] for o in plan.originals} | {'manifest.json'}:
                raise ArchiveInvalid('archive: unexpected or missing members')
            if any(members[o['path']].file_size != o['bytes'] for o in plan.originals):
                raise ArchiveInvalid('archive: original size differs from manifest')
            return plan
    except (BadZipFile, LargeZipFile) as exc:
        raise ArchiveInvalid('archive: not a readable ZIP') from exc


def _validate(manifest) -> RestorePlan:
    version = manifest.get('format_version') if type(manifest) is dict else None
    _obj(manifest, MANIFEST_KEYS | ({'source_library'} if version == '0.2.0' else frozenset()), 'manifest')
    if (manifest['format'] != 'kura-collector-exchange' or version not in FORMAT_VERSIONS
            or manifest['scope'] != 'one_collection'):
        raise ArchiveInvalid('manifest: unsupported format, version or scope')
    export_id = _uuid(manifest['export_id'], 'export_id')
    if version == '0.2.0':
        _uuid(manifest['source_library'], 'source_library')
    # A 0.1.0 package does not say where its evidence came from: that stays unknown (per
    # package), never taken for this library's own (#152).
    producer_keys = PRODUCER_KEYS | ({'origin'} if version == '0.2.0' else frozenset())
    _uuid(manifest['selection_snapshot'], 'selection_snapshot')
    _time(manifest['created_at'], 'created_at')
    capabilities = _obj(manifest['capabilities'], CAPABILITY_KEYS, 'capabilities')
    for key, value in capabilities.items():
        _bool(value, f'capabilities.{key}')
    if not capabilities['organization_label_evidence'] or capabilities['semantic_scene_evidence']:
        raise ArchiveInvalid('capabilities: unsupported evidence kinds')

    originals, digests, total = {}, set(), 0
    for i, o in enumerate(_list(manifest['originals'], 'originals', MAX_ENTRIES)):
        where = f'originals[{i}]'
        _obj(o, ORIGINAL_KEYS, where)
        key = _uuid(o['id'], where + '.id')
        sha = _str(o['sha256'], where + '.sha256', 64, 64)
        if not SHA256.fullmatch(sha) or o['path'] != 'originals/' + sha:
            raise ArchiveInvalid(f'{where}: invalid hash-based path')
        size = _int(o['bytes'], where + '.bytes', 1, MAX_ORIGINAL_BYTES)
        if _str(o['media_type'], where + '.media_type', 1, 100) not in MEDIA_TYPES:
            raise ArchiveInvalid(f'{where}.media_type: unsupported')
        width = _int(o['width'], where + '.width', 1, MAX_PIXELS)
        height = _int(o['height'], where + '.height', 1, MAX_PIXELS)
        if width * height > MAX_PIXELS:
            raise ArchiveInvalid(f'{where}: pixel limit exceeded')
        if key in originals or sha in digests:
            raise ArchiveInvalid(f'{where}: duplicate original identity')
        digests.add(sha)
        total += size
        originals[key] = {'uuid': key, 'digest': bytes.fromhex(sha), 'path': o['path'], 'bytes': size,
                          'media_type': o['media_type'], 'width': width, 'height': height,
                          'name': _name(o['original_name'], where + '.original_name')}
    if total > MAX_TOTAL_BYTES:
        raise ArchiveInvalid('originals: total size budget exceeded')

    collections = _list(manifest['collections'], 'collections', 1)
    if len(collections) != 1:
        raise ArchiveInvalid('collections: expected exactly one')
    collection = _obj(collections[0], {'id', 'title', 'entries'}, 'collections[0]')
    _local_id(collection['id'], 'collections[0].id')
    title = _str(collection['title'], 'collections[0].title', 1, 200)
    entries, entry_ids, referenced, last = [], set(), set(), 0
    for i, e in enumerate(_list(collection['entries'], 'entries', MAX_ENTRIES)):
        where = f'entries[{i}]'
        _obj(e, ENTRY_KEYS, where)
        entry = _local_id(e['id'], where + '.id')
        position = _int(e['position'], where + '.position', 1, BIGINT)
        if position <= last or entry in entry_ids:
            raise ArchiveInvalid(f'{where}: positions must increase and ids be unique')
        original = _uuid(e['original'], where + '.original')
        if original not in originals:
            raise ArchiveInvalid(f'{where}.original: unknown original')
        name = _name(e['original_name'], where + '.original_name')
        path = e['relative_path']
        if path is not None:
            try:
                validate_relative_path(_str(path, where + '.relative_path', 1, 4096), name)
            except InvalidPath as exc:
                raise ArchiveInvalid(f'{where}.relative_path: invalid or differs from name') from exc
        last = position
        entry_ids.add(entry)
        referenced.add(original)
        entries.append({'id': entry, 'position': position, 'original': original, 'name': name, 'path': path})
    if referenced != originals.keys():
        raise ArchiveInvalid('originals: unreferenced original')

    results = _list(manifest['results'], 'results', MAX_ENTRIES)
    result_ids = set()
    for i, r in enumerate(results):
        _obj(r, {'entry', 'status'}, f'results[{i}]')
        if r['status'] != 'succeeded':
            raise ArchiveInvalid(f'results[{i}]: not succeeded')
        result_ids.add(_local_id(r['entry'], f'results[{i}].entry'))
    if len(results) != len(entries) or result_ids != entry_ids:
        raise ArchiveInvalid('results: incomplete output results')

    tags, evidence = {}, {}
    for i, e in enumerate(_list(manifest['evidence'], 'evidence', MAX_EVIDENCE)):
        where = f'evidence[{i}]'
        _obj(e, EVIDENCE_KEYS, where)
        if e['type'] != 'organization_label' or e['policy'] != POLICY_VERSION:
            raise ArchiveInvalid(f'{where}: unsupported evidence type or policy')
        ident = _local_id(e['id'], where + '.id')
        original = _uuid(e['original'], where + '.original')
        if original not in originals or ident in evidence:
            raise ArchiveInvalid(f'{where}: unknown original or duplicate id')
        try:
            normalized = normalize_label(_str(e['tag'], where + '.tag', 1, 100))
        except ArchiveInvalid:
            raise
        except ValueError as exc:
            raise ArchiveInvalid(f'{where}.tag: invalid label') from exc
        if normalized != (e['tag'], e['normalized_name']):
            raise ArchiveInvalid(f'{where}.tag: not in normalized form')
        if tags.setdefault(normalized[1], normalized[0]) != normalized[0]:
            raise ArchiveInvalid(f'{where}.tag: normalized tag has two display names')
        producer = _obj(e['producer'], producer_keys, where + '.producer')
        origin = producer.get('origin', f'unknown:{export_id}')
        if type(origin) is not str or not ORIGIN.fullmatch(origin):
            raise ArchiveInvalid(f'{where}.producer.origin: not a library id')
        observed = _time(e['observed_at'], where + '.observed_at')
        created = _time(e['operation_created_at'], where + '.operation_created_at')
        before = _int(e['before_revision'], where + '.before_revision', 0, BIGINT - 1)
        if _int(e['after_revision'], where + '.after_revision', 1, BIGINT) != before + 1 or observed < created:
            raise ArchiveInvalid(f'{where}: revision step or operation time is inconsistent')
        evidence[ident] = {
            'id': ident, 'original': original, 'key': normalized[1],
            'present': _bool(e['present'], where + '.present'),
            'prior': _bool(e['prior_present'], where + '.prior_present'),
            'based_on': None if e['based_on'] is None else _local_id(e['based_on'], where + '.based_on'),
            'observed': observed, 'operation': _uuid(e['operation'], where + '.operation'), 'created': created,
            'before': before,
            'producer': (_str(producer['kind'], where + '.producer.kind', 1, 100),
                         _str(producer['reference'], where + '.producer.reference', 1, 1000),
                         _str(producer['version'], where + '.producer.version', 1, 1000),
                         origin)}

    # Replay the writer's invariants in source-id order: linear correction chains,
    # contiguous operations per asset, consecutive revisions, full asset x tag grid.
    ordered = [evidence[i] for i in sorted(evidence)]
    chains, operations, history, visited = {}, {}, defaultdict(list), set()
    for e in ordered:
        where = f'evidence id {e["id"]}'
        pair = e['original'], e['key']
        previous = chains.get(pair)
        if (e['based_on'], e['prior']) != ((None, False) if previous is None else (previous['id'], previous['present'])):
            raise ArchiveInvalid(f'{where}: correction chain is broken')
        chains[pair] = e
        op = operations.setdefault(e['operation'], {'created': e['created'], 'producer': e['producer'],
                                                    'tags': {}, 'assets': {}, 'count': 0})
        target = e['before'], e['observed']
        if (op['created'], op['producer']) != (e['created'], e['producer']) \
                or op['tags'].setdefault(e['key'], e['present']) != e['present'] \
                or op['assets'].setdefault(e['original'], target) != target:
            raise ArchiveInvalid(f'{where}: inconsistent operation grouping')
        op['count'] += 1
        steps = history[e['original']]
        if not steps or steps[-1]['operation'] != e['operation']:
            if (e['original'], e['operation']) in visited:
                raise ArchiveInvalid(f'{where}: operation interleaves on one asset')
            if e['before'] != len(steps) or (steps and steps[-1]['observed'] > e['observed']):
                raise ArchiveInvalid(f'{where}: revisions or times are not consecutive')
            visited.add((e['original'], e['operation']))
            steps.append({'operation': e['operation'], 'observed': e['observed'], 'tags': set()})
        if e['key'] in steps[-1]['tags']:
            raise ArchiveInvalid(f'{where}: tag repeated within an operation')
        steps[-1]['tags'].add(e['key'])
    for key, op in operations.items():
        if (not len(op['assets']) <= MAX_TARGETS or not len(op['tags']) <= MAX_CHANGES
                or op['count'] != len(op['assets']) * len(op['tags'])):
            raise ArchiveInvalid(f'operation {key}: incomplete or oversized asset x tag grid')

    names = {name: key for key, name in tags.items()}
    heads, revisions = {}, {}
    for i, p in enumerate(_list(manifest['projection'], 'projection', MAX_ENTRIES)):
        where = f'projection[{i}]'
        _obj(p, PROJECTION_KEYS, where)
        original = _uuid(p['original'], where + '.original')
        if original not in originals or original in revisions or p['policy'] != POLICY_VERSION:
            raise ArchiveInvalid(f'{where}: unknown or duplicate original, or unsupported policy')
        revisions[original] = _int(p['revision'], where + '.revision', 0, BIGINT)
        if revisions[original] != len(history.get(original, ())):
            raise ArchiveInvalid(f'{where}.revision: differs from evidence operations')
        positive = 0
        for j, h in enumerate(_list(p['heads'], where + '.heads', MAX_EVIDENCE)):
            at = f'{where}.heads[{j}]'
            _obj(h, HEAD_KEYS, at)
            pair = original, names.get(_str(h['tag'], at + '.tag', 1, 100))
            winner = chains.get(pair)
            if (winner is None or pair in heads or _local_id(h['evidence'], at + '.evidence') != winner['id']
                    or _bool(h['present'], at + '.present') != winner['present']):
                raise ArchiveInvalid(f'{at}: differs from immutable evidence')
            heads[pair] = winner['id'], winner['present']
            positive += winner['present']
        if positive > MAX_LABELS:
            raise ArchiveInvalid(f'{where}: more than {MAX_LABELS} labels')
    if revisions.keys() != originals.keys() or heads.keys() != chains.keys():
        raise ArchiveInvalid('projection: incomplete')

    return RestorePlan(export_id, title, list(originals.values()), entries, tags, ordered,
                       operations, heads, revisions)


# ---- payload --------------------------------------------------------------

def store_originals(archive, root: Path, plan: RestorePlan):
    """Stream each original into CAS; nothing is extracted under a member name."""
    root.mkdir(parents=True, exist_ok=True)
    if shutil.disk_usage(root).free < sum(o['bytes'] for o in plan.originals) + RESERVE_BYTES:
        raise RestoreError('storage: not enough free space for originals')
    stored = {}
    try:
        with _checked_zip(archive) as bundle:
            for index, original in enumerate(plan.originals):
                where = f'originals[{index}]'
                if shutil.disk_usage(root).free < original['bytes'] + RESERVE_BYTES + 4 * 1024**2:
                    raise RestoreError('storage: free-space reserve would be exhausted')
                info = bundle.getinfo(original['path'])
                if info.file_size != original['bytes'] or info.compress_type != ZIP_STORED:
                    raise ArchiveInvalid(f'{where}: archive member changed since preflight')
                try:
                    with bundle.open(info) as stream:
                        media = store_media(stream, root, expected_digest=original['digest'],
                                            expected_size=original['bytes'])
                except Conflict as exc:
                    raise ArchiveInvalid(f'{where}: bytes differ from declared SHA-256/size') from exc
                except MediaInputError as exc:
                    raise MediaMismatch(f'{where}: rejected as media ({exc.code})') from exc
                if (media.media_type, media.width, media.height) != (
                        original['media_type'], original['width'], original['height']):
                    raise MediaMismatch(f'{where}: media type or dimensions differ from manifest')
                stored[original['uuid']] = media
    except (BadZipFile, LargeZipFile, KeyError) as exc:
        raise ArchiveInvalid('archive: member unreadable or changed since preflight') from exc
    return stored


# ---- database -------------------------------------------------------------

def _sqlstate(exc):
    orig = exc.orig
    return getattr(orig, 'sqlstate', None) or getattr(getattr(orig, '__cause__', None), 'sqlstate', None)


async def _claim_empty_namespace(db, *, lock=True):
    """Lock all tables in one sorted order, then prove the namespace is empty."""
    if (await db.execute(text('SHOW transaction_isolation'))).scalar_one() != 'read committed':
        raise RestoreError('transaction: restore requires READ COMMITTED')
    tables = set((await db.execute(text("""
        SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
        WHERE n.nspname='collector_next' AND c.relkind IN ('r','p')
    """))).scalars())
    if tables != set(TABLES):
        raise SchemaMismatch(f'schema: collector_next tables differ from schema version {SCHEMA_VERSION}')
    if lock:
        try:
            await db.execute(text('LOCK TABLE ' + ','.join('collector_next.' + t for t in TABLES)
                                  + ' IN EXCLUSIVE MODE NOWAIT'))
        except DBAPIError as exc:
            if _sqlstate(exc) == '55P03':
                raise NamespaceBusy('schema: collector_next is in use; retry when idle') from exc
            raise
    versions = (await db.execute(text('SELECT version FROM collector_next.schema_version'))).scalars().all()
    policies = (await db.execute(text('SELECT id,version FROM collector_next.label_policy'))).all()
    kinds = {name: kind for kind, name in (await db.execute(text('SELECT id,name FROM collector_next.label_producer_kind')))}
    if list(versions) != [SCHEMA_VERSION] or [tuple(p) for p in policies] != [(1, POLICY_VERSION)] or kinds != {'account': 1}:
        raise SchemaMismatch('schema: version, label policy or producer kinds are not the supported seed')
    occupied = (await db.execute(text(' UNION ALL '.join(
        f"SELECT '{t}' WHERE EXISTS(SELECT 1 FROM collector_next.{t})" for t in TABLES if t not in SEEDED)))).scalars().all()
    if occupied:
        raise NamespaceNotEmpty('collector_next is not empty: ' + ','.join(sorted(occupied)))
    return kinds


async def check_destination(db):
    """Read-only preflight for adapters; restore revalidates under its write lock."""
    return await _claim_empty_namespace(db,lock=False)


async def restore(db, root: Path, archive, owner: int):
    """Validate and restore using one open input handle and caller-owned transaction."""
    with _archive_source(archive) as source:
        plan = await asyncio.to_thread(prepare, source)
        return await _restore(db, root, source, owner, plan)


async def _restore(db, root: Path, archive, owner: int, plan: RestorePlan):
    """Restore into the caller's open transaction. The caller commits or rolls back.

    Payload bytes are verified even after strict metadata preflight.
    """
    if type(owner) is not int or not 0 < owner <= 2**31 - 1:
        raise ValueError('owner must be a positive integer')
    kinds = await _claim_empty_namespace(db, lock=False)
    if any(op['producer'][0] not in kinds for op in plan.operations.values()):
        raise SchemaMismatch('schema: archive uses an unknown producer kind')
    # Do not block writers while hashing/decoding. A competing write invalidates
    # this restore at the final locked emptiness check; verified CAS orphans are safe.
    stored = await asyncio.to_thread(store_originals, archive, root, plan)
    await _claim_empty_namespace(db)
    o = {'o': owner}

    originals = plan.originals
    blobs = {bytes(r['sha256']): r['id'] for r in (await db.execute(text("""
        INSERT INTO collector_next.blob(sha256,byte_size)
        SELECT * FROM unnest(CAST(:shas AS bytea[]),CAST(:sizes AS bigint[])) RETURNING id,sha256
    """), {'shas': [x['digest'] for x in originals], 'sizes': [x['bytes'] for x in originals]})).mappings()}
    await db.execute(text("""
        INSERT INTO collector_next.blob_location(blob_id,storage_key)
        SELECT * FROM unnest(CAST(:blobs AS bigint[]),CAST(:keys AS text[]))
    """), {'blobs': [blobs[x['digest']] for x in originals], 'keys': [stored[x['uuid']].key for x in originals]})
    assets = {UUID(str(r['uuid'])): r['id'] for r in (await db.execute(text("""
        INSERT INTO collector_next.asset(uuid,owner_id,blob_id)
        SELECT x.u,:o,x.b FROM unnest(CAST(:uuids AS uuid[]),CAST(:blobs AS bigint[])) x(u,b) RETURNING id,uuid
    """), {**o, 'uuids': [x['uuid'] for x in originals], 'blobs': [blobs[x['digest']] for x in originals]})).mappings()}
    await db.execute(text("""
        INSERT INTO collector_next.media_current(asset_id,owner_id,original_name,media_type,width,height)
        SELECT x.a,:o,x.n,x.m,x.w,x.h FROM unnest(CAST(:assets AS bigint[]),CAST(:names AS text[]),
            CAST(:types AS text[]),CAST(:widths AS integer[]),CAST(:heights AS integer[])) x(a,n,m,w,h)
    """), {**o, 'assets': [assets[x['uuid']] for x in originals], 'names': [x['name'] for x in originals],
           'types': [x['media_type'] for x in originals], 'widths': [x['width'] for x in originals],
           'heights': [x['height'] for x in originals]})

    collection = (await db.execute(text("""
        INSERT INTO collector_next.collection(owner_id,title,request_key) VALUES(:o,:title,:key) RETURNING id
    """), {**o, 'title': plan.title, 'key': uuid5(plan.export_id, 'collection')})).scalar_one()
    entries = plan.entries
    entry_ids = {r['position']: r['id'] for r in (await db.execute(text("""
        INSERT INTO collector_next.collection_entry(owner_id,collection_id,asset_id,position)
        SELECT :o,:c,x.a,x.p FROM unnest(CAST(:assets AS bigint[]),CAST(:positions AS bigint[])) x(a,p)
        ORDER BY x.p RETURNING id,position
    """), {**o, 'c': collection, 'assets': [assets[e['original']] for e in entries],
           'positions': [e['position'] for e in entries]})).mappings()}
    # Receipts carry entry names/paths; fingerprints match service.register for the new ids.
    by_uuid = {x['uuid']: x for x in originals}
    fingerprints = []
    for e in entries:
        identity = [by_uuid[e['original']]['digest'].hex(), by_uuid[e['original']]['bytes'], e['name'], collection]
        if e['path'] is not None:
            identity.append(e['path'])
        fingerprints.append(hashlib.sha256(json.dumps(identity, ensure_ascii=False, separators=(',', ':')).encode()).digest())
    await db.execute(text("""
        INSERT INTO collector_next.receipt(owner_id,request_key,fingerprint,original_name,relative_path,asset_id,entry_id)
        SELECT :o,x.k,x.f,x.n,x.p,x.a,x.e FROM unnest(CAST(:keys AS uuid[]),CAST(:fps AS bytea[]),CAST(:names AS text[]),
            CAST(:paths AS text[]),CAST(:assets AS bigint[]),CAST(:entries AS bigint[])) x(k,f,n,p,a,e)
    """), {**o, 'keys': [uuid5(plan.export_id, f'entry:{e["id"]}') for e in entries], 'fps': fingerprints,
           'names': [e['name'] for e in entries], 'paths': [e['path'] for e in entries],
           'assets': [assets[e['original']] for e in entries], 'entries': [entry_ids[e['position']] for e in entries]})

    keys = sorted(plan.tags)
    tag_ids = {r['normalized_name']: r['id'] for r in (await db.execute(text("""
        INSERT INTO collector_next.tag(owner_id,name,normalized_name)
        SELECT :o,x.n,x.k FROM unnest(CAST(:names AS text[]),CAST(:keys AS text[])) x(n,k)
        ORDER BY x.k COLLATE "C" RETURNING id,normalized_name
    """), {**o, 'names': [plan.tags[k] for k in keys], 'keys': keys})).mappings()}
    producers = sorted({op['producer'] for op in plan.operations.values()})
    # Imported producers keep the library they came from; evidence that came from this very
    # library (a package coming home) is this library's own again (#152).
    from .library import library_id
    here = await library_id(db)
    local = {p: '' if p[3] == here else p[3] for p in producers}
    producer_ids = {(r['kind_id'], r['reference'], r['version'], r['origin']): r['id'] for r in (await db.execute(text("""
        INSERT INTO collector_next.label_producer(owner_id,kind_id,reference,version,origin)
        SELECT :o,x.k,x.r,x.v,x.g FROM unnest(CAST(:kinds AS smallint[]),CAST(:refs AS text[]),CAST(:versions AS text[]),
            CAST(:origins AS text[])) x(k,r,v,g)
        RETURNING id,kind_id,reference,version,origin
    """), {**o, 'kinds': [kinds[p[0]] for p in producers], 'refs': [p[1] for p in producers],
           'versions': [p[2] for p in producers], 'origins': [local[p] for p in producers]})).mappings()}
    producer_of = {p: producer_ids[(kinds[p[0]], p[1], p[2], local[p])] for p in producers}

    ops = sorted(plan.operations.items(), key=lambda item: (item[1]['created'], item[0]))
    op_params = {'keys': [], 'fps': [], 'producers': [], 'assets': [], 'tags': [], 'created': []}
    target_rows = []
    for key, op in ops:
        targets = sorted((assets[a], before) for a, (before, _) in op['assets'].items())
        # Same shape as label_service.apply, over the restored local ids.
        fingerprint = hashlib.sha256(json.dumps([targets, sorted(op['tags'].items())],
                                                ensure_ascii=False, separators=(',', ':')).encode()).digest()
        for column, value in zip(op_params, (key, fingerprint, producer_of[op['producer']],
                                             len(targets), len(op['tags']), op['created'])):
            op_params[column].append(value)
        target_rows += [(key, asset, before) for asset, before in targets]
    await db.execute(text("""
        INSERT INTO collector_next.label_operation(owner_id,request_key,fingerprint,producer_id,asset_count,tag_count,policy_id,created_at)
        SELECT :o,x.k,x.f,x.p,x.a,x.t,1,x.c FROM unnest(CAST(:keys AS uuid[]),CAST(:fps AS bytea[]),CAST(:producers AS bigint[]),
            CAST(:assets AS integer[]),CAST(:tags AS integer[]),CAST(:created AS timestamptz[])) x(k,f,p,a,t,c)
    """), {**o, **op_params})

    evidence = plan.evidence
    # Sorted preallocation keeps source id order, which the (observed_at,id) fold relies on.
    allocated = sorted((await db.execute(text(
        "SELECT nextval(pg_get_serial_sequence('collector_next.label_observation','id')) FROM generate_series(1,:n)"
    ), {'n': len(evidence)})).scalars())
    new_id = {e['id']: allocated[i] for i, e in enumerate(evidence)}
    await db.execute(text("""
        INSERT INTO collector_next.label_observation(id,owner_id,asset_id,tag_id,producer_id,request_key,present,prior_present,based_on_id,observed_at)
        OVERRIDING SYSTEM VALUE
        SELECT x.id,:o,x.a,x.t,x.p,x.k,x.v,x.prior,x.base,x.at
        FROM unnest(CAST(:ids AS bigint[]),CAST(:assets AS bigint[]),CAST(:tags AS bigint[]),CAST(:producers AS bigint[]),
            CAST(:keys AS uuid[]),CAST(:values AS boolean[]),CAST(:priors AS boolean[]),CAST(:bases AS bigint[]),
            CAST(:times AS timestamptz[])) x(id,a,t,p,k,v,prior,base,at)
        ORDER BY x.id
    """), {**o, 'ids': [new_id[e['id']] for e in evidence], 'assets': [assets[e['original']] for e in evidence],
           'tags': [tag_ids[e['key']] for e in evidence], 'producers': [producer_of[e['producer']] for e in evidence],
           'keys': [e['operation'] for e in evidence], 'values': [e['present'] for e in evidence],
           'priors': [e['prior'] for e in evidence],
           'bases': [None if e['based_on'] is None else new_id[e['based_on']] for e in evidence],
           'times': [e['observed'] for e in evidence]})
    await db.execute(text("""
        INSERT INTO collector_next.label_target(owner_id,request_key,asset_id,before_revision,after_revision)
        SELECT :o,x.k,x.a,x.b,x.b+1 FROM unnest(CAST(:keys AS uuid[]),CAST(:assets AS bigint[]),CAST(:befores AS bigint[])) x(k,a,b)
    """), {**o, 'keys': [t[0] for t in target_rows], 'assets': [t[1] for t in target_rows],
           'befores': [t[2] for t in target_rows]})

    # Projections come from evidence alone (namespace was empty: owner scope == restored rows).
    await db.execute(text("""
        INSERT INTO collector_next.label_head(owner_id,asset_id,tag_id,observation_id,present)
        SELECT DISTINCT ON(asset_id,tag_id) owner_id,asset_id,tag_id,id,present
        FROM collector_next.label_observation WHERE owner_id=:o
        ORDER BY asset_id,tag_id,observed_at DESC,id DESC
    """), o)
    await db.execute(text("""
        INSERT INTO collector_next.asset_label_state(asset_id,owner_id,revision,positive_count,last_observed_at,policy_id)
        SELECT e.asset_id,e.owner_id,count(DISTINCT e.request_key),
          (SELECT count(*) FROM collector_next.label_head h WHERE h.owner_id=e.owner_id AND h.asset_id=e.asset_id AND h.present),
          max(e.observed_at),1
        FROM collector_next.label_observation e WHERE e.owner_id=:o GROUP BY e.asset_id,e.owner_id
    """), o)
    from . import tag_summary
    await tag_summary.rebuild(db, owner, list(tag_ids.values()))

    expected_heads = {(assets[a], tag_ids[k]): (new_id[old], present) for (a, k), (old, present) in plan.heads.items()}
    actual_heads = {(r['asset_id'], r['tag_id']): (r['observation_id'], r['present']) for r in (await db.execute(text(
        'SELECT asset_id,tag_id,observation_id,present FROM collector_next.label_head WHERE owner_id=:o'), o)).mappings()}
    if actual_heads != expected_heads:
        raise ProjectionMismatch('projection: rebuilt heads differ from archive heads')
    expected_revisions = {assets[a]: r for a, r in plan.revisions.items() if r}
    actual_revisions = {r['asset_id']: r['revision'] for r in (await db.execute(text(
        'SELECT asset_id,revision FROM collector_next.asset_label_state WHERE owner_id=:o'), o)).mappings()}
    if actual_revisions != expected_revisions:
        raise ProjectionMismatch('projection: rebuilt revisions differ from archive revisions')

    return {'restored': True, 'export_id': str(plan.export_id), 'owner': owner, 'collection_id': collection,
            'originals': len(originals), 'entries': len(entries), 'tags': len(keys),
            'label_operations': len(ops), 'label_observations': len(evidence),
            'sha256_bytes': sum(x['bytes'] for x in originals)}
