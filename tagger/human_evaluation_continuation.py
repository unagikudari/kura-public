"""Credential-free continuation of an exported frozen human-evaluation job.

Only local immutable files are written. A completed job is reusable; an orphaned
claim is never re-executed automatically. Neither export provenance nor a point
estimate is admission authority. Run once inside a bounded local service.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import re
import stat

from . import dedicated_protocol as p
from . import human_evaluation_action as action
from . import stratified_evaluation as evaluation

FILES = {'package.json', 'first-sample.json', 'first-scores.json',
         'extension-sample.json', 'extension-scores.json',
         'human_evaluation_action.py', 'stratified_evaluation.py'}
MAX_FILE = 8 * 1024**2
MAX_TOTAL = 64 * 1024**2
MAX_SCORES = 100_000
MAX_REQUESTED = 4096
HASH = re.compile(r'^[0-9a-f]{64}$')
POLICY = {'min_precision', 'min_recall', 'min_val_pos_support', 'min_pred_pos_support'}
EVALUATED = {
    'review_completed_quality_unresolved': ('explicit_uncertainty_remains',
        'retain_unknowns_and_review_evidence_limits_without_forcing_labels'),
    'measured_floor_not_met': ('supported_weighted_point_floor_not_met',
        'audit_labels_blind_to_scores_then_prepare_a_new_candidate'),
    'insufficient_observed_support': ('actual_support_below_policy',
        'plan_independent_evidence_without_relabeling_or_reusing_fit_data'),
    'point_floors_met_review_required': ('point_estimates_are_not_quality_approval',
        'review_sampling_sensitivity_provenance_and_fleet_evidence'),
}


class Refused(ValueError):
    pass


def need(value, reason):
    if not value:
        raise Refused(reason)


def sha(value):
    need(isinstance(value, str) and HASH.fullmatch(value), 'hash_invalid')
    return value


def pairs(items):
    result = {}
    for key, value in items:
        need(key not in result, 'duplicate_json_key')
        result[key] = value
    return result


class Reader:
    def __init__(self):
        self.total = 0

    def raw(self, path, expected=None):
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        try:
            info = os.fstat(fd)
            need(stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid()
                 and info.st_size <= MAX_FILE, 'input_file_refused')
            with os.fdopen(fd, 'rb', closefd=False) as stream:
                value = stream.read(MAX_FILE + 1)
        finally:
            os.close(fd)
        self.total += len(value)
        need(len(value) <= MAX_FILE and self.total <= MAX_TOTAL, 'input_budget_exceeded')
        if expected is not None:
            need(hashlib.sha256(value).hexdigest() == sha(expected), 'file_hash_mismatch')
        return value

    def doc(self, path, expected=None):
        raw = self.raw(path, expected)
        value = json.loads(raw, object_pairs_hook=pairs,
                           parse_constant=lambda _: (_ for _ in ()).throw(Refused('nonfinite_json')))
        need(isinstance(value, dict), 'object_required')
        return value, hashlib.sha256(raw).hexdigest()


def verified(value):
    need(value.get('sha256') == p.digest({k: v for k, v in value.items() if k != 'sha256'}),
         'body_hash_mismatch')
    return value


def identifiers(value):
    need(isinstance(value, dict), 'id_map_invalid')
    result = {}
    for key, item in value.items():
        need(isinstance(key, str) and re.fullmatch(r'[1-9][0-9]{0,18}', key)
             and int(key) <= 2**63 - 1, 'id_invalid')
        result[int(key)] = item
    return result


def _directory(path):
    path = Path(path)
    # The supplied parent must exist; persist each newly published directory
    # entry so a durable claim cannot disappear with an unflushed parent.
    path.mkdir(exist_ok=True, mode=0o700)
    st = path.lstat()
    need(stat.S_ISDIR(st.st_mode) and st.st_uid == os.getuid()
         and stat.S_IMODE(st.st_mode) == 0o700, 'output_directory_refused')
    fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
    return path


def prepare(*, bundle, manifest_sha256, observer_source_sha256, anchor,
            measurement_sha256, observation, observation_sha256, policy_sha256):
    for pin in (manifest_sha256, observer_source_sha256, measurement_sha256,
                observation_sha256, policy_sha256):
        sha(pin)
    reader = Reader()
    root = Path(bundle)
    need(root.is_dir() and not root.is_symlink(), 'bundle_directory_refused')
    manifest, _ = reader.doc(root / 'manifest.json', manifest_sha256)
    need(set(manifest) == {'files', 'model_sha256'} and set(manifest['files']) == FILES,
         'manifest_shape_invalid')
    model = sha(manifest['model_sha256'])
    raw = {name: reader.raw(root / name, sha(manifest['files'][name])) for name in sorted(FILES)}
    reader.raw(root / 'observe-union-human.py', observer_source_sha256)
    # Verify the installed pure evaluator, never execute uploaded Python files.
    evaluator_hashes = {
        Path(module.__file__).name: hashlib.sha256(Path(module.__file__).read_bytes()).hexdigest()
        for module in (action, evaluation)
    }
    for name, sha256 in evaluator_hashes.items():
        need(sha256 == manifest['files'][name], 'installed_evaluator_mismatch')
    docs = {name: verified(json.loads(value, object_pairs_hook=pairs))
            for name, value in raw.items() if name.endswith('.json')}
    original, anchor_file_sha = reader.doc(anchor)
    wire = original['measurement']['canonical_body']
    need(isinstance(wire, str) and len(wire.encode()) <= 262144
         and hashlib.sha256(wire.encode()).hexdigest() == sha(measurement_sha256),
         'anchor_hash_mismatch')
    a = json.loads(wire, object_pairs_hook=pairs)
    need(original['measurement_sha256'] == measurement_sha256
         and a['schema'] == 'original-measurement/1' and a['provenance'] == 'caller_assertion'
         and a['measurement_scope'] == 'internal_training' and a['quality_claim'] is False
         and a['artifact_sha256'] == model, 'anchor_binding_mismatch')
    threshold = a['threshold']
    need(type(threshold) in (int, float) and math.isfinite(threshold) and 0 < threshold < 1,
         'threshold_invalid')
    package = docs['package.json']
    need(package['model_sha256'] == model, 'package_model_mismatch')
    sha(package['source_checkpoint_sha256'])
    frames, requested = [], []
    for prefix in ('first', 'extension'):
        sample, scores = docs[prefix + '-sample.json'], docs[prefix + '-scores.json']
        sha(sample['frame_sha256']); sha(scores['frame_sha256'])
        sha(scores['model_checkpoint_sha256'] if prefix == 'first' else scores['model_sha256'])
        need(sample['model_sha256'] == model and sample['score_sha256'] == scores['sha256']
             and sample['frame_sha256'] == scores['frame_sha256']
             and sample['threshold'] == scores['threshold'] == threshold
             and sample['labels'] == scores['labels'] == {}
             and sample['publish'] is False and scores['publish'] is False,
             'frame_binding_mismatch')
        need((scores['model_checkpoint_sha256'] == package['source_checkpoint_sha256'])
             if prefix == 'first' else scores['model_sha256'] == model, 'score_model_mismatch')
        values = identifiers(scores['scores'])
        need(0 < len(values) <= MAX_SCORES, 'score_budget_exceeded')
        strata = sample['strata']
        need(set(strata) == {'predicted_present', 'predicted_absent'}, 'strata_invalid')
        selected = {k: v['sample_ids'] for k, v in strata.items()}
        need(all(isinstance(ids, list) for ids in selected.values()), 'sample_ids_invalid')
        ids = [i for group in selected.values() for i in group]
        need(all(type(i) is int and 0 < i <= 2**63 - 1 for i in ids)
             and sorted(ids) == sorted(sample['post_ids']), 'sample_membership_mismatch')
        requested.extend(ids)
        frames.append(dict(scores=values, sample=selected))
    need(0 < len(requested) <= MAX_REQUESTED and len(requested) == len(set(requested)),
         'request_budget_or_overlap')
    obs, observation_file_sha = reader.doc(observation)
    need(p.digest(obs) == sha(observation_sha256), 'observation_hash_mismatch')
    need(obs['manifest_sha256'] == manifest_sha256 and obs['model_sha256'] == model
         and obs['module_sha256'] == manifest['files']['stratified_evaluation.py']
         and obs['action_sha256'] == manifest['files']['human_evaluation_action.py']
         and obs['labels_inferred'] is False and obs['publish'] is False,
         'observation_binding_mismatch')
    policy = obs['policy']
    need(isinstance(policy, dict) and set(policy) == POLICY
         and p.digest(policy) == sha(policy_sha256), 'policy_binding_mismatch')
    for name, value in policy.items():
        need(type(value) in (int, float) and math.isfinite(value), 'policy_invalid')
        need(0 < value <= 1 if name in ('min_precision', 'min_recall')
             else 0 < value <= 2**63 - 1 and int(value) == value, 'policy_invalid')
    labels = identifiers(obs['labels'])
    need(set(labels) <= set(requested)
         and all(v in ('present', 'absent', 'uncertain', None) for v in labels.values()),
         'judgments_invalid')
    # Structural validation uses no human judgments; it is not the claimed job.
    evaluation.evaluate_frames(frames, {}, threshold=threshold,
        minimum_positive_support=int(policy['min_val_pos_support']),
        minimum_prediction_support=int(policy['min_pred_pos_support']))
    missing = sorted(i for i in requested if labels.get(i) is None)
    uncertain = sum(labels.get(i)=='uncertain' for i in requested)
    # Bind the existing evaluator's human-evaluation-input/2 identity without
    # running its human-quality calculation before the durable claim.
    normalized_policy = {k: (float(v) if k in ('min_precision', 'min_recall') else int(v))
                         for k, v in policy.items()}
    evaluation_input = dict(schema='human-evaluation-input/2', threshold=float(threshold),
        policy=normalized_policy, frames=sorted([dict(
            scores=sorted((i, float(v)) for i, v in frame['scores'].items()),
            sample={k: sorted(v) for k, v in sorted(frame['sample'].items())})
            for frame in frames], key=lambda frame: frame['scores'][0][0]),
        judgments=sorted((i, labels.get(i)) for frame in frames
                         for ids in frame['sample'].values() for i in ids))
    plan = dict(schema='human-evaluation-continuation-plan/2',
                evaluation_input_sha256=p.digest(evaluation_input),
                measurement_sha256=measurement_sha256, model_sha256=model, threshold=threshold,
                manifest_sha256=manifest_sha256, observer_source_sha256=observer_source_sha256,
                observation_sha256=observation_sha256, observation_file_sha256=observation_file_sha,
                anchor_file_sha256=anchor_file_sha, policy_sha256=policy_sha256,
                implementation=evaluator_hashes | {
                    path.name: hashlib.sha256(path.read_bytes()).hexdigest()
                    for path in (Path(__file__), Path(p.__file__))
                },
                requested=len(requested), unseen=len(missing), uncertain=uncertain,
                provenance='frozen_observer_export_unverified', admission='not_granted')
    return plan, frames, labels, policy, missing


def _receipt(path, schema, plan, claim=None):
    value, _ = Reader().doc(path)
    verified(value)
    need(value.get('schema') == schema and value.get('plan_sha256') == plan['sha256'],
         'receipt_binding_mismatch')
    if claim is not None:
        need(value.get('claim_sha256') == claim['sha256'], 'receipt_claim_mismatch')
    return value


def run(*, output, executor, **inputs):
    need(isinstance(executor, str) and re.fullmatch(r'[A-Za-z0-9_.:@/-]{1,100}', executor),
         'executor_invalid')
    plan, frames, labels, policy, missing = prepare(**inputs)
    # The observation owns one job even across consumer deployments. Source
    # changes and reformatted exports must collide with the immutable plan,
    # not silently create another execution directory.
    identity = {k: plan[k] for k in ('measurement_sha256', 'manifest_sha256',
                                   'observation_sha256', 'policy_sha256')}
    root = _directory(_directory(output) / p.digest(identity))
    lockfd = os.open(root / 'job.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    try:
        st = os.fstat(lockfd)
        need(stat.S_ISREG(st.st_mode) and st.st_uid == os.getuid() and st.st_nlink == 1,
             'lock_refused')
        try:
            fcntl.flock(lockfd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise Refused('job_already_running') from None
        for name in ('plan.json', 'waiting.json', 'claim.json', 'assessment.json', 'result.json'):
            path = root / name
            if path.exists() or path.is_symlink():
                st = path.lstat()
                need(stat.S_ISREG(st.st_mode) and st.st_uid == os.getuid()
                     and st.st_size <= MAX_FILE, 'output_file_refused')
        plan = p.write_once(root / 'plan.json', plan)
        if missing:
            return p.write_once(root / 'waiting.json', dict(
                schema='human-evaluation-continuation-result/2', plan_sha256=plan['sha256'],
                outcome='waiting', state='awaiting_human_judgments', reason='requested_judgments_missing',
                requested=plan['requested'], unseen=len(missing), uncertain=plan['uncertain'], executed=False,
                next='review_remaining_images_without_guessing_labels',
                admission='not_granted', publication_eligible=False, release_or_hold_changes=False))
        claimed, assessed = root / 'claim.json', root / 'assessment.json'
        if claimed.exists():
            claim = _receipt(claimed, 'human-evaluation-job-claim/1', plan)
            if not assessed.exists():
                raise Refused('job_interrupted_preserve_attempt')
        else:
            need(not assessed.exists() and not (root / 'result.json').exists(), 'orphan_result')
            claim = p.write_once(claimed, dict(schema='human-evaluation-job-claim/1',
                plan_sha256=plan['sha256'], executor=executor, pid=os.getpid(),
                claimed_at=datetime.now(timezone.utc).isoformat()))
            result = action.assess(frames, labels, threshold=plan['threshold'], policy=policy)
            p.write_once(assessed, dict(schema='human-evaluation-job-assessment/1',
                plan_sha256=plan['sha256'], claim_sha256=claim['sha256'], assessment=result,
                admission='not_granted', publication_eligible=False))
        report = _receipt(assessed, 'human-evaluation-job-assessment/1', plan, claim)
        result = report['assessment']
        need(result['schema'] == 'human-evaluation-next-action/2'
             and result['admission'] == 'not_granted' and result['publication_eligible'] is False
             and result['release_or_hold_changes'] is False
             and isinstance(result.get('state'), str) and result['state'] in EVALUATED
             and (result.get('reason'), result.get('next')) == EVALUATED[result['state']]
             and result['input_sha256'] == plan['evaluation_input_sha256']
             and result['threshold'] == plan['threshold']
             and result['measurement']['requested'] == plan['requested']
             and result['measurement']['missing_ids'] == sorted(i for i,v in labels.items() if v=='uncertain')
             and ((result['state']=='review_completed_quality_unresolved') == bool(plan['uncertain']))
             and result['responses']==dict(decisive=plan['requested']-plan['uncertain'],uncertain=plan['uncertain'],unseen=0), 'assessment_invalid')
        return p.write_once(root / 'result.json', dict(
            schema='human-evaluation-continuation-result/2', plan_sha256=plan['sha256'],
            claim_sha256=claim['sha256'], assessment_sha256=report['sha256'],
            outcome='succeeded', state=('review_completed_quality_unresolved' if plan['uncertain'] else 'evaluated_not_admitted'), reason=result['reason'],
            next=result['next'],
            evaluation_state=result['state'], requested=plan['requested'], unseen=0, uncertain=plan['uncertain'], executed=True,
            admission='not_granted', publication_eligible=False, release_or_hold_changes=False))
    finally:
        os.close(lockfd)


class ClosedParser(argparse.ArgumentParser):
    def error(self, message):
        raise Refused('arguments_invalid')


def main():
    ap = ClosedParser(description=__doc__)
    for name in ('bundle', 'manifest-sha256', 'observer-source-sha256', 'anchor',
                 'measurement-sha256', 'observation', 'observation-sha256',
                 'policy-sha256', 'output', 'executor'):
        ap.add_argument('--' + name, required=True)
    try:
        result = run(**vars(ap.parse_args()))
    except Exception as exc:
        # Only our closed refusal codes are public. Never echo paths or data.
        reason = str(exc) if isinstance(exc, Refused) else 'input_or_execution_refused'
        print(json.dumps(dict(outcome='refused', state='stopped', reason=reason, next='inspect_preserved_attempt_no_retry',
                              admission='not_granted', publication_eligible=False)))
        return 1
    print(json.dumps(result))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
