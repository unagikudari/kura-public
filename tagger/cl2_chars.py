"""CL v2_00 character component. No API writes or redistribution of model bytes.

Threshold is mandatory: qualification does not silently choose a production
operating point. Each node obtains the gated files from the official HF repo.
The legacy PixAI general/copyright loader deliberately remains independent.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path

from .pixai_chars import RequiredCharacterComponentError

REPO = 'cella110n/cl_tagger_v2'
REVISION = 'b57909b8e9c63f71e208a26473e7aabdf45ed6b6'
FILES = {
    'model.onnx': '9cff358bebe593f7187969e952f5338b7258506856bdccc9f6afb391384130e6',
    'model.onnx.data': '281ba888b39c6c9a4009c241d278f8ea87bfb56fed68241a94c588b93cb54e07',
    'model_vocabulary.json': '638f2979e7d711f5403ddb1bbe5054004377239b7a5062b1ea95c4abb1479551',
}
PRODUCER = 'cl_tagger_v2'
# Stable semantic contract, independent of checkout comments or runtime provider.
DERIVER_CONTRACT = hashlib.sha256(b'kura-cl2-rgb-bicubic384-mean0.5-std0.5-sigmoid-character-top0.8-v2').hexdigest()
# A name is kept only when it also scores at least this share of the image's best character score. It is part of
# the derivation (the contract above names it), not an operating point: the threshold stays the one admitted number.
# Measured 2026-11-15 on 9,669 title-labelled images at threshold .45 (kura#149): the title character is found in
# 85.1% with the rule against 86.5% without, and a second, extra name is attached to 10.1% instead of 36.7%.
TOP_RATIO = 0.8
CATEGORIES = {'Character': 4, 'Copyright': 3, 'General': 0, 'Meta': 5, 'Quality': 5, 'Rating': 9}


def require(ok, reason):
    if not ok:
        raise RequiredCharacterComponentError(reason)


def digest(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()


def _unique(pairs):
    result = {}
    for key, value in pairs:
        require(key not in result, 'cl2_duplicate_json_key')
        result[key] = value
    return result


def vocabulary(raw):
    """Validate the complete index mapping; normalization may not merge tags."""
    require(isinstance(raw, dict), 'cl2_vocabulary_invalid')
    count = raw.get('num_tags')
    require(type(count) is int and count > 0, 'cl2_vocabulary_size_invalid')
    indexed = raw.get('idx_to_tag')
    if isinstance(indexed, dict):
        require(set(indexed) == {str(i) for i in range(count)}, 'cl2_vocabulary_indices_invalid')
        names = [indexed[str(i)] for i in range(count)]
    else:
        require(isinstance(indexed, list) and len(indexed) == count, 'cl2_vocabulary_indices_invalid')
        names = indexed
    require(all(isinstance(n, str) and bool(n.strip()) for n in names), 'cl2_vocabulary_name_invalid')
    normalized = [n.replace(' ', '_') for n in names]
    require(len(set(normalized)) == count, 'cl2_vocabulary_normalization_collision')
    categories = raw.get('tag_to_category')
    require(isinstance(categories, dict) and set(categories) == set(names), 'cl2_vocabulary_categories_invalid')
    require(all(c in CATEGORIES for c in categories.values()), 'cl2_vocabulary_category_unknown')
    reverse = raw.get('tag_to_idx')
    require(isinstance(reverse, dict) and set(reverse) == set(names)
            and all(type(reverse[n]) is int and reverse[n] == i for i, n in enumerate(names)),
            'cl2_vocabulary_reverse_invalid')
    require(all(n.strip() == n for n in names if categories[n] in ('Character', 'Copyright')),
            'cl2_named_entity_whitespace_invalid')
    chars = {i: normalized[i] for i, n in enumerate(names) if categories[n] == 'Character'}
    require(bool(chars), 'cl2_character_vocabulary_empty')
    return chars, count


def preprocess(image):
    import numpy as np
    from PIL import Image
    image = image.convert('RGB').resize((384, 384), Image.Resampling.BICUBIC)
    return ((np.asarray(image, dtype=np.float32) / 255 - .5) / .5).transpose(2, 0, 1)[None]


def prediction_vector(session, inputs, count):
    import numpy as np
    try:
        output = np.asarray(session.run(['logits'], {session.get_inputs()[0].name: inputs})[0])
        require(output.shape == (1, count) and np.isfinite(output).all(), 'cl2_logits_invalid')
        logits = output[0].astype(np.float64)
        # Stable sigmoid, including finite logits outside float32 exp range.
        scores = np.exp(-np.logaddexp(0, -logits))
        require(np.isfinite(scores).all(), 'cl2_probabilities_invalid')
        return scores
    except RequiredCharacterComponentError:
        raise
    except Exception as exc:
        raise RequiredCharacterComponentError('cl2_inference_failed') from exc


def validate_threshold(threshold):
    require(type(threshold) in (int, float) and math.isfinite(threshold)
            and 0 < threshold <= 1, 'cl2_threshold_invalid')


def execution_providers(ort):
    """Explicit node choice; never silently downgrade a declared GPU to CPU."""
    primary = os.environ.get('KURA_CL2_PROVIDER', 'CPUExecutionProvider')
    require(primary in ('CPUExecutionProvider', 'ROCMExecutionProvider', 'CUDAExecutionProvider'),
            'cl2_provider_invalid')
    require(primary in ort.get_available_providers(), 'cl2_provider_unavailable')
    return [primary] if primary == 'CPUExecutionProvider' else [primary, 'CPUExecutionProvider']


def create_session(ort, path, options):
    providers = execution_providers(ort)
    provider_options = [{} for _ in providers]
    if providers[0] != 'CPUExecutionProvider':
        raw = os.environ.get('KURA_CL2_GPU_MEM_LIMIT_BYTES', '')
        require(raw.isascii() and raw.isdecimal() and len(raw) <= 12,
                'cl2_gpu_memory_limit_required')
        limit = int(raw)
        require(256 * 1024**2 <= limit <= 16 * 1024**3, 'cl2_gpu_memory_limit_invalid')
        provider_options[0] = {'device_id': '0', 'gpu_mem_limit': str(limit), 'arena_extend_strategy': 'kSameAsRequested'}
        if providers[0] == 'CUDAExecutionProvider' and os.environ.get('KURA_CL2_CUDA_LOW_MEMORY') == '1':
            options.graph_optimization_level = ort.GraphOptimizationLevel.ORT_DISABLE_ALL
            options.add_session_config_entry('session.disable_prepacking', '1')
    session = ort.InferenceSession(str(path), options, providers=providers, provider_options=provider_options)
    require(providers[0] in session.get_providers(), 'cl2_provider_fallback')
    return session


def load(*, threshold, intra_op_threads=None, local_files_only=True, model_dir=None,
         bounded_cpu_memory=False):
    """Load verified local files or obtain directly from HF with an explicit node provider.

    model_dir is for a locally acquired snapshot, not a distribution source.
    All three digests are checked before ONNX Runtime reads external weights.
    """
    validate_threshold(threshold)
    if intra_op_threads is None:
        raw_threads = os.environ.get('KURA_CL2_THREADS', '4')
        require(raw_threads.isascii() and raw_threads.isdecimal() and len(raw_threads) <= 2,
                'cl2_threads_invalid')
        intra_op_threads = int(raw_threads)
    require(type(intra_op_threads) is int and 1 <= intra_op_threads <= 16, 'cl2_threads_invalid')
    try:
        if model_dir is None:
            from huggingface_hub import snapshot_download
            model_dir = Path(snapshot_download(REPO, revision=REVISION,
                allow_patterns=['v2_00/' + n for n in FILES], local_files_only=local_files_only)) / 'v2_00'
        root = Path(model_dir)
        for name, expected in FILES.items():
            require(digest(root / name) == expected, 'cl2_artifact_digest_mismatch')
        chars, count = vocabulary(json.loads((root / 'model_vocabulary.json').read_text(), object_pairs_hook=_unique))
        import onnxruntime as ort
        options = ort.SessionOptions()
        options.intra_op_num_threads = intra_op_threads
        options.inter_op_num_threads = 1
        options.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
        if bounded_cpu_memory and execution_providers(ort) == ['CPUExecutionProvider']:
            from .onnx_memory import bounded_cpu_options
            options = bounded_cpu_options(ort, options, intra_op_threads=intra_op_threads)
        session = create_session(ort, root / 'model.onnx', options)
        require(len(session.get_inputs()) == 1 and session.get_inputs()[0].shape == ['batch_size', 3, 384, 384],
                'cl2_input_contract_invalid')
        require([o.name for o in session.get_outputs()] == ['logits'], 'cl2_output_contract_invalid')
        identity = json.dumps(dict(repository=REPO, revision=REVISION, files=FILES,
            threshold=threshold, deriver_sha256=DERIVER_CONTRACT, preprocess='rgb-bicubic384-mean0.5-std0.5-sigmoid-v1'), sort_keys=True)
        return dict(sess=session, chars=chars, tag_count=count, threshold=threshold,
                    source=PRODUCER + '=' + hashlib.sha256(identity.encode()).hexdigest(),
                    execution=session_facts(ort, session, options))
    except RequiredCharacterComponentError:
        raise
    except Exception as exc:
        raise RequiredCharacterComponentError('cl2_load_failed') from exc


def char_tags(state, image):
    threshold = state['threshold']
    validate_threshold(threshold)
    scores = prediction_vector(state['sess'], preprocess(image), state['tag_count'])
    hits = [(name, float(scores[i])) for i, name in state['chars'].items() if scores[i] >= threshold]
    floor = TOP_RATIO * max((score for _, score in hits), default=0.0)
    return sorted((pair for pair in hits if pair[1] >= floor), key=lambda pair: (-pair[1], pair[0]))


def release_description(*, threshold, evidence):
    """Metadata only, for qualification; never publishes or uploads gated bytes.

    The production publisher must admit this description and its threshold row
    together before a worker is configured to use it.
    """
    validate_threshold(threshold)
    require(isinstance(evidence, str) and len(evidence.strip()) >= 10, 'cl2_threshold_evidence_required')
    params = dict(producer=PRODUCER, repository=REPO, revision=REVISION,
                  category=4, threshold=threshold, threshold_evidence=evidence, distribution='official_hf_per_node')
    return dict(components=[dict(role='character_base', ordinal=i,
        content_sha256=sha, deriver_sha256=DERIVER_CONTRACT,
        params={**params, 'filename':'v2_00/' + name})
        for i,(name,sha) in enumerate(FILES.items())],
        threshold_row=dict(producer=PRODUCER, category=4, threshold=threshold, evidence=evidence))


def session_facts(ort, session, options):
    """Actual session options; a provider registered here is not operator placement proof."""
    import PIL
    import numpy as np
    providers = session.get_providers()
    facts = dict(providers=providers, ort_version=ort.__version__, pillow_version=PIL.__version__,
                 numpy_version=np.__version__, intra_op_threads=options.intra_op_num_threads,
                 inter_op_threads=options.inter_op_num_threads, execution_mode='sequential',
                 device_id=None, gpu_mem_limit=None, arena_extend_strategy=None)
    if providers[0] != 'CPUExecutionProvider':
        actual = session.get_provider_options()[providers[0]]
        facts.update(device_id=int(actual['device_id']), gpu_mem_limit=int(actual['gpu_mem_limit']),
                     arena_extend_strategy=actual['arena_extend_strategy'])
    return facts
