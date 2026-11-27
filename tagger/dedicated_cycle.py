"""One resumable, offline learning job: frozen inputs -> fit -> serving check.

Run inside a node-local bounded service. This worker has no DB/API credentials,
cannot publish, and never retries a partially written stage automatically.
Completed stages are reused only after their immutable input bindings match.
"""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
from pathlib import Path

from . import dedicated_protocol as p
from . import dedicated_qualification as q
from .dedicated_dataset import FrozenImages


def _sha(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def _measurement(path, snapshot):
    result = p.read_verified(path, 'vnext-dedicated-measurement/1')
    candidate = p.read_verified(str(path) + '.candidate.json', 'dedicated-candidate/1')
    if (result['snapshot_sha256'] != snapshot['sha256']
            or result['tag'] != snapshot['tag']
            or candidate['snapshot_sha256'] != snapshot['sha256']
            or candidate['tag'] != snapshot['tag']
            or result['candidate_sha256'] != candidate['sha256']
            or result['threshold'] != candidate['threshold']
            or result['bundle_sha256'] != candidate['bundle_sha256']
            or _sha(str(path) + '.bundle.pt') != result['bundle_sha256']):
        raise p.ProtocolError('cycle measurement binding mismatch')
    return result


def run(snapshot, media_dir, output, *, baseline=None, baseline_sha256=None,
        baseline_threshold=None, device='cpu'):
    import math
    if device not in ('cpu', 'cuda'):
        raise p.ProtocolError('cycle execution parameters invalid')
    if baseline is None and (baseline_sha256 is not None or baseline_threshold is not None):
        raise p.ProtocolError('cycle baseline binding incomplete')
    if baseline is not None and (baseline_threshold is None or not math.isfinite(baseline_threshold) or not 0 < baseline_threshold < 1):
        raise p.ProtocolError('cycle execution parameters invalid')
    doc = p.read_verified(snapshot, p.SCHEMA)
    dataset = FrozenImages(media_dir, doc)
    if baseline is not None and _sha(baseline) != baseline_sha256:
        raise p.ProtocolError('cycle baseline changed')
    root = Path(output)
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (root / 'cycle.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise p.ProtocolError('cycle already running') from None
        plan = p.write_once(root / 'cycle-plan.json', {
            'schema': 'dedicated-cycle-plan/1', 'snapshot_sha256': doc['sha256'],
            'dataset_sha256': dataset.doc['sha256'], 'tag': doc['tag'],
            'baseline_sha256': baseline_sha256, 'baseline_threshold': baseline_threshold,
            'device': device,
            'implementation': {name: _sha(Path(__file__).with_name(name)) for name in (
                'dedicated_cycle.py', 'dedicated_protocol.py', 'dedicated_dataset.py',
                'dedicated_qualification.py', 'feature_contract.py',
                'content_preprocessing.py', 'content_head_model.py', 'vnext_arms.py')},
        })
        result_path = root / 'result.json'
        if not result_path.exists():
            marker = root / 'measure-started.json'
            if marker.exists():
                raise p.ProtocolError('cycle measurement interrupted; preserve attempt')
            p.write_once(marker, {'schema': 'dedicated-stage-started/1',
                                 'plan_sha256': plan['sha256'], 'stage': 'measure'})
            p.measure(doc['tag'], snapshot, result_path, media_dir=media_dir)
        result = _measurement(result_path, doc)
        qualification_path = root / 'qualification.json'
        if not qualification_path.exists():
            marker = root / 'qualification-started.json'
            if marker.exists():
                raise p.ProtocolError('cycle qualification interrupted; preserve attempt')
            p.write_once(marker, {'schema': 'dedicated-stage-started/1',
                                 'plan_sha256': plan['sha256'], 'stage': 'qualification'})
            q.qualify(snapshot, result_path, media_dir, qualification_path,
                      baseline=baseline, baseline_sha256=baseline_sha256,
                      baseline_threshold=baseline_threshold, device=device)
        report = p.read_verified(qualification_path, 'dedicated-serving-qualification/1')
        if (report['snapshot_sha256'] != doc['sha256']
                or report['measurement_sha256'] != result['sha256']
                or report['bundle_sha256'] != result['bundle_sha256']
                or report['baseline_sha256'] != baseline_sha256
                or report['baseline_threshold'] != baseline_threshold
                or report['threshold'] != result['threshold']
                or report['device'] != device
                or report['promotion_eligible'] is not False):
            raise p.ProtocolError('cycle qualification binding mismatch')
        passed = report['parity']['passed'] and report['quality_floor']['passed']
        state = 'awaiting_production_precision' if passed else 'rejected'
        if report['state'] != state:
            raise p.ProtocolError('cycle qualification state inconsistent')
        production_binding = {}
        if passed and getattr(dataset, "includes_production", False):
            path = root / "production-scores.json"
            if not path.exists():
                q.score_production(snapshot, result_path, media_dir, path, device=device)
            scores = p.read_verified(path, "dedicated-production-scores/1")
            if (scores["snapshot_sha256"] != doc["sha256"]
                    or scores["dataset_sha256"] != dataset.doc["sha256"]
                    or scores["measurement_sha256"] != result["sha256"]
                    or scores["model_sha256"] != result["bundle_sha256"]
                    or scores["threshold"] != result["threshold"]):
                raise p.ProtocolError("production scores binding mismatch")
            production_binding["production_scores_sha256"] = scores["sha256"]
        return p.write_once(root / 'cycle-result.json', {
            'schema': 'dedicated-cycle-result/1', 'plan_sha256': plan['sha256'],
            'measurement_sha256': result['sha256'], 'qualification_sha256': report['sha256'],
            'outcome': 'succeeded', 'state': state,
            'reason': 'serving_quality_passed' if passed else 'serving_quality_failed',
            'next': 'sample_production_predictions' if passed else 'await_more_human_corrections',
            'promotion_eligible': False,
            **production_binding,
        })


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    for name in ('snapshot', 'media-dir', 'output'):
        ap.add_argument('--' + name, required=True)
    ap.add_argument('--baseline')
    ap.add_argument('--baseline-sha256')
    ap.add_argument('--baseline-threshold', type=float)
    ap.add_argument('--device', choices=('cpu', 'cuda'), default='cpu')
    try:
        result = run(**vars(ap.parse_args()))
    except Exception as exc:
        # Do not echo exception messages: this entry can be used by unattended
        # dispatchers. Detailed immutable stage artifacts stay node-local.
        print(json.dumps({'outcome': 'refused', 'state': 'stopped',
                          'reason': type(exc).__name__, 'next': 'inspect_stage_artifacts'}))
        return 1
    print(json.dumps(result))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
