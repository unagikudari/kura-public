"""Immutable, operator-provided evaluation sets; never labels or model scores.

Separate from pools: a review set must not create new training-group edges.
Files are named by their full byte digest in a dedicated directory. Loading a
set changes neither the archive nor the training/publication state.
"""
import hashlib
import json
import os
from pathlib import Path
import re
import stat
from urllib.parse import urlencode
from api.services.review_tag_contract_v1 import valid as valid_tag


class ReviewQueueRefused(ValueError):
    pass


# provenance kind of a media-bound /2 successor, by the purpose of the /1 request it supersedes
FROZEN_KIND = {'candidate_evaluation': 'frozen_candidate_sample', 'training_support': 'frozen_training_support',
               'fixed_evaluation': 'frozen_review_set', 'production_precision': 'frozen_review_set',
               'boundary_recheck': 'frozen_review_set'}

def _require(ok, reason):
    if not ok:
        raise ReviewQueueRefused(reason)


def _unique(pairs):
    out = {}
    for key, value in pairs:
        _require(key not in out, 'duplicate_key')
        out[key] = value
    return out


def read_queue_blobs(root=None):
    root = Path(root or os.environ.get('KURA_REVIEW_QUEUE_DIR', '~/.local/share/kura/review-queues')).expanduser()
    try:
        directory = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise ReviewQueueRefused('directory_unreadable') from exc
    try:
        names = sorted(os.listdir(directory))
        _require(len(names) <= 20, 'too_many_sets')
        blobs = {}
        for name in names:
            _require(re.fullmatch(r'[a-f0-9]{64}\.json', name), 'filename_invalid')
            try:
                fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
                try:
                    info = os.fstat(fd)
                    _require(stat.S_ISREG(info.st_mode) and info.st_size <= 131072, 'file_invalid')
                    with os.fdopen(fd, 'rb', closefd=False) as handle:
                        raw = handle.read(131073)
                    _require(len(raw) <= 131072, 'file_invalid')
                finally:
                    os.close(fd)
            except OSError as exc:
                raise ReviewQueueRefused('file_unreadable') from exc
            blobs[name] = raw
        return blobs
    finally:
        os.close(directory)


def load_queues(root=None):
    blobs=read_queue_blobs(root)
    return None if blobs is None else parse_queues(blobs)


def parse_queues(blobs, *, canonical=False):
    """One validation contract for immutable file imports and canonical DB rows."""
    _require(isinstance(blobs, dict) and (canonical or len(blobs)<=20), "too_many_sets")
    queues=[]
    for name, raw in sorted(blobs.items()):
        _require(isinstance(name,str) and re.fullmatch(r"[a-f0-9]{64}\.json",name), "filename_invalid")
        _require(isinstance(raw,bytes) and len(raw)<=131072, "file_invalid")
        digest = hashlib.sha256(raw).hexdigest()
        _require(digest + '.json' == name, 'digest_mismatch')
        try:
            item = json.loads(raw, object_pairs_hook=_unique)
        except ReviewQueueRefused:
            raise
        except (ValueError, UnicodeError) as exc:
            raise ReviewQueueRefused('json_invalid') from exc
        _require(isinstance(item, dict), 'shape_invalid')
        direct = item.get('schema') == 'human-review-set/3'
        explicit = direct or item.get('schema') == 'human-review-set/2'
        keys = {'schema', 'tag', 'purpose', 'frame_sha256', 'model_sha256', 'post_ids'}
        _require(set(item) == keys | ({'source_provenance','media_sha256'} if explicit else set()), 'shape_invalid')
        _require(item['schema'] in ('human-review-set/1','human-review-set/2','human-review-set/3'), 'schema_invalid')
        if direct:
            provenance = item['source_provenance']
            _require((item['purpose'], provenance.get('kind') if isinstance(provenance, dict) else None)
                     in (('candidate_evaluation', 'dedicated_candidate'), ('base_comparison', 'base_comparison'))
                     and isinstance(provenance, dict)
                     and set(provenance) == {'kind', 'measurement_sha256', 'snapshot_sha256'}
                     and all(isinstance(provenance[k], str)
                             and re.fullmatch('[a-f0-9]{64}', provenance[k])
                             for k in ('measurement_sha256', 'snapshot_sha256')),
                     'provenance_invalid')
        elif explicit:
            provenance=item['source_provenance']
            support = item['purpose'] == 'training_support'
            # every frozen /1 request may be re-issued as a media-bound /2 successor (owner 2026-11-22: all
            # review sets open in the review view); only a historical recheck stands alone
            superseding = item['purpose'] in FROZEN_KIND
            _require(not support or item['model_sha256'] is None, 'training_support_model_not_applicable')
            _require(superseding or item['purpose'] == 'historical_label_recheck', 'purpose_invalid')
            expected = {'kind','artifact_sha256'} | ({'supersedes_queue_sha256'} if superseding else set())
            if isinstance(provenance, dict) and 'origin' in provenance:
                _require(item['purpose'] == 'historical_label_recheck'
                         and provenance['origin'] == 'lora_findings', 'provenance_invalid')
                expected.add('origin')
            _require(isinstance(provenance,dict) and set(provenance)==expected
                     and provenance['kind'] == FROZEN_KIND.get(item['purpose'], 'historical_human_training')
                     and all(isinstance(provenance[k],str) and re.fullmatch('[a-f0-9]{64}',provenance[k])
                             for k in expected-{'kind', 'origin'}), 'provenance_invalid')
        _require(explicit or item['purpose'] in ('fixed_evaluation', 'production_precision', 'training_support', 'boundary_recheck', 'candidate_evaluation'), 'purpose_invalid')
        tag = item['tag']
        _require(valid_tag(tag), 'tag_invalid')
        for key in ('frame_sha256', 'model_sha256'):
            if key == 'model_sha256' and item['purpose'] in ('training_support','historical_label_recheck') and item[key] is None:
                continue  # A training-support queue may precede any model.
            _require(isinstance(item[key], str) and re.fullmatch(r'[a-f0-9]{64}', item[key]), 'reference_invalid')
        ids = item['post_ids']
        _require(isinstance(ids, list) and 0 < len(ids) <= 500 and all(type(i) is int and 0 < i < 2**31 for i in ids), 'ids_invalid')
        _require(len(set(ids)) == len(ids), 'duplicate_post')
        if explicit:
            hashes=item['media_sha256']
            _require(isinstance(hashes,dict) and set(hashes)=={str(i) for i in ids} and all(isinstance(v,str) and re.fullmatch('[a-f0-9]{64}',v) for v in hashes.values()), 'media_binding_invalid')
        queue = dict(item, queue_sha256=digest)
        if explicit:
            queue['browse_url'] = '/tagger-review/' + digest
            queue['unjudged_url'] = queue['browse_url']
        else:
            query = 'ids:' + ','.join(map(str, ids))
            queue['browse_url'] = '/?' + urlencode({'q': query, 'bulk': '1', 'bulkTag': tag})
            queue['unjudged_url'] = '/?' + urlencode({'q': query + f' -locked:{tag} -removed:{tag}', 'bulk': '1', 'bulkTag': tag})
            _require(max(len(queue[k].encode('ascii')) for k in ('browse_url','unjudged_url')) <= 7500, 'link_too_long')
        queues.append(queue)
    # Keep the original file and score binding, but expose one active request.
    by_ref = {q['queue_sha256']: q for q in queues}
    replaced = set()
    for q in queues:
        if q['schema'] != 'human-review-set/2' or q['purpose'] not in FROZEN_KIND:
            continue
        ref = q['source_provenance']['supersedes_queue_sha256']
        old = by_ref.get(ref)
        _require(old is not None and old['schema']=='human-review-set/1'
                 and old['purpose']==q['purpose'] and ref not in replaced,
                 'supersession_source_invalid')
        _require(all(q[k] == old[k] for k in ('tag','purpose','frame_sha256','model_sha256','post_ids')),
                 'supersession_population_changed')
        old['superseded_by'] = q['queue_sha256']
        replaced.add(ref)
    return queues


def review_lanes(queues, legacy_refs):
    """Project the origin without rewriting an immutable queue or its answers."""
    for queue in queues:
        provenance = queue.get('source_provenance', {})
        findings = queue['purpose'] == 'historical_label_recheck' and (
            provenance.get('origin') == 'lora_findings' or queue['queue_sha256'] in legacy_refs
        )
        queue['review_lane'] = 'lora_findings' if findings else 'lifecycle'


def read_legacy_lora_refs(path=None):
    path = Path(path or os.environ.get(
        'KURA_REVIEW_LORA_ORIGINS_PATH', '~/.local/state/kura/review-lora-origins.json')).expanduser()
    try:
        with path.open('rb') as stream:
            raw = stream.read(65537)
    except FileNotFoundError:
        return set()
    _require(len(raw) <= 65536, 'origin_manifest_invalid')
    refs = json.loads(raw, object_pairs_hook=_unique)
    _require(isinstance(refs, list) and all(
        isinstance(ref, str) and re.fullmatch('[a-f0-9]{64}', ref) for ref in refs
    ), 'origin_manifest_invalid')
    return set(refs)
