"""Read-only comparison of frozen scores with current explicit human pins.

This is selected-set disagreement, never a release gate or a population metric.
Scores carry no labels. Changed human pins take effect on the next read.
"""
import hashlib
import json
import math
import os
from pathlib import Path
import re
import stat
from urllib.parse import urlencode

from api.services.review_queues import ReviewQueueRefused, _require, _unique


def load_scores(queues, root=None):
    root = Path(root or os.environ.get('KURA_REVIEW_SCORE_DIR', '~/.local/share/kura/review-scores')).expanduser()
    try:
        directory = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except FileNotFoundError:
        return {}
    except OSError as exc:
        raise ReviewQueueRefused('score_directory_unreadable') from exc
    try:
        names = sorted(os.listdir(directory))
        _require(len(names) <= 20, 'too_many_scores')
        by_queue = {q['queue_sha256']: q for q in queues}
        out = {}
        for name in names:
            _require(re.fullmatch(r'[a-f0-9]{64}\.json', name), 'score_filename_invalid')
            try:
                fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
                try:
                    info = os.fstat(fd)
                    _require(stat.S_ISREG(info.st_mode) and info.st_size <= 131072, 'score_file_invalid')
                    with os.fdopen(fd, 'rb', closefd=False) as handle:
                        raw = handle.read(131073)
                    _require(len(raw) <= 131072, 'score_file_invalid')
                finally:
                    os.close(fd)
            except OSError as exc:
                raise ReviewQueueRefused('score_file_unreadable') from exc
            digest = hashlib.sha256(raw).hexdigest()
            _require(name == digest + '.json', 'score_digest_mismatch')
            try:
                item = json.loads(raw, object_pairs_hook=_unique)
            except (ValueError, UnicodeError) as exc:
                raise ReviewQueueRefused('score_json_invalid') from exc
            _require(isinstance(item, dict) and set(item) == {
                'schema', 'queue_sha256', 'model_sha256', 'frame_sha256', 'threshold', 'scores'
            }, 'score_shape_invalid')
            _require(item['schema'] == 'frozen-review-scores/1', 'score_schema_invalid')
            ref = item['queue_sha256']
            _require(isinstance(ref, str) and ref in by_queue and ref not in out, 'score_queue_invalid')
            queue = by_queue[ref]
            _require(queue['model_sha256'] is not None and item['model_sha256'] == queue['model_sha256'] and item['frame_sha256'] == queue['frame_sha256'], 'score_binding_mismatch')
            def probability(value):
                return type(value) in (int, float) and 0 <= value <= 1 and math.isfinite(value)
            _require(probability(item['threshold']), 'score_threshold_invalid')
            scores = item['scores']
            _require(isinstance(scores, dict) and set(scores) == {str(i) for i in queue['post_ids']} and all(probability(v) for v in scores.values()), 'score_values_invalid')
            out[ref] = dict(item, score_sha256=digest)
        return out
    finally:
        os.close(directory)


def compare(queue, frozen, pins):
    counts = dict(tp=0, tn=0, fp=0, fn=0)
    disagreements = []
    for post_id, state in pins:
        if state not in ('present', 'absent'):
            continue
        predicted = frozen['scores'][str(post_id)] >= frozen['threshold']
        key = ('t' if predicted == (state == 'present') else 'f') + ('p' if predicted else 'n')
        counts[key] += 1
        if key in ('fp', 'fn'):
            disagreements.append(post_id)
    url = None
    if disagreements:
        url = '/?' + urlencode({'q': 'ids:' + ','.join(map(str, sorted(disagreements)))})
    return dict(state='compared', counts=counts, compared=sum(counts.values()),
                remaining=len(queue['post_ids'])-sum(counts.values()), disagreement_url=url,
                score_sha256=frozen['score_sha256'], model_sha256=frozen['model_sha256'],
                threshold=frozen['threshold'], population_quality_claim=False)
