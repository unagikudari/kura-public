"""Bounded worker-reported execution facts, not an attestation or qualification.

This restricted JSON format uses ASCII strings and integers (no floats), making
sorted compact JSON a deterministic encoding across the API and Python workers.
No timestamps, paths, credentials or arbitrary diagnostic text are permitted.
Claim actor/source identity is deliberately outside this contract.
"""
import hashlib
import json
import re

MAX_BYTES = 4096
PROVIDERS = {'CPUExecutionProvider', 'ROCMExecutionProvider', 'CUDAExecutionProvider'}
KEYS = {'schema', 'host', 'profile', 'release_id', 'component_source', 'providers',
        'ort_version', 'pillow_version', 'numpy_version', 'intra_op_threads',
        'inter_op_threads', 'execution_mode', 'device_id', 'gpu_mem_limit',
        'arena_extend_strategy', 'qualification_ref'}


class ReceiptRefused(ValueError):
    pass


def require(value, reason):
    if not value:
        raise ReceiptRefused(reason)


def _text(value, pattern):
    return isinstance(value, str) and re.fullmatch(pattern, value, re.ASCII) is not None


def canonical(body):
    require(type(body) is dict and set(body) == KEYS, 'execution_receipt_shape')
    require(body['schema'] == 'kura.character-execution/1', 'execution_receipt_schema')
    require(_text(body['host'], r'[A-Za-z0-9][A-Za-z0-9_.-]{0,62}'), 'execution_receipt_host')
    require(body['profile'] in ('full', 'wd14'), 'execution_receipt_profile')
    require(type(body['release_id']) is int and body['release_id'] > 0, 'execution_receipt_release')
    require(_text(body['component_source'], r'cl_tagger_v2=[0-9a-f]{64}'), 'execution_receipt_source')
    providers = body['providers']
    require(type(providers) is list and 1 <= len(providers) <= 2
            and all(type(p) is str and p in PROVIDERS for p in providers)
            and len(set(providers)) == len(providers), 'execution_receipt_providers')
    require(len(providers) == 1 or (providers[0] != 'CPUExecutionProvider'
            and providers[1] == 'CPUExecutionProvider'), 'execution_receipt_provider_order')
    for name in ('ort_version', 'pillow_version', 'numpy_version'):
        require(_text(body[name], r'[0-9][A-Za-z0-9.+_-]{0,63}'), 'execution_receipt_version')
    require(type(body['intra_op_threads']) is int and 1 <= body['intra_op_threads'] <= 16
            and type(body['inter_op_threads']) is int and body['inter_op_threads'] == 1
            and body['execution_mode'] == 'sequential', 'execution_receipt_threads')
    if providers[0] == 'CPUExecutionProvider':
        require(all(body[k] is None for k in ('device_id','gpu_mem_limit','arena_extend_strategy')),
                'execution_receipt_cpu_options')
    else:
        require(type(body['device_id']) is int and 0 <= body['device_id'] < 16
                and type(body['gpu_mem_limit']) is int and 256*1024**2 <= body['gpu_mem_limit'] <= 16*1024**3
                and body['arena_extend_strategy'] == 'kSameAsRequested', 'execution_receipt_gpu_options')
    require(body['qualification_ref'] is None or _text(body['qualification_ref'], r'[0-9a-f]{64}'),
            'execution_receipt_qualification_ref')
    wire = json.dumps(body, sort_keys=True, separators=(',', ':'), ensure_ascii=True, allow_nan=False).encode('ascii')
    require(len(wire) <= MAX_BYTES, 'execution_receipt_size')
    return wire


def make(body):
    wire = canonical(body)
    return {'receipt_sha256':hashlib.sha256(wire).hexdigest(), 'body':json.loads(wire)}


def verify(packet, *, host, profile, release_id):
    require(type(packet) is dict and set(packet) == {'receipt_sha256','body'}, 'execution_receipt_packet')
    expected = make(packet['body'])
    require(_text(packet['receipt_sha256'], r'[0-9a-f]{64}')
            and packet['receipt_sha256'] == expected['receipt_sha256'], 'execution_receipt_digest')
    body = expected['body']
    require(body['host'] == host and body['profile'] == profile
            and type(release_id) is int and body['release_id'] == release_id, 'execution_receipt_context')
    return expected


def bind_completion(packet, *, host, profile, release_id, heartbeat_receipt):
    result = verify(packet, host=host, profile=profile, release_id=release_id)
    require(heartbeat_receipt == result['receipt_sha256'], 'execution_receipt_heartbeat_mismatch')
    return result


def from_loaded(state, *, host, profile, release_id):
    """Only facts captured from the actual initialized CL session; no ambient env."""
    body = dict(state['execution'])
    body.update(schema='kura.character-execution/1', host=host, profile=profile,
                release_id=release_id, component_source=state['source'], qualification_ref=None)
    return make(body)


def weakest_provider(primary, packet):
    if packet is None:
        return primary
    providers = packet['body']['providers']
    # Compare component primary providers; registered CPU fallback is reported in the full receipt.
    return primary if providers[0] == primary else 'CPUExecutionProvider'
