"""Bounded local receipt projection. Never imports a job, executes it or grants quality.

Two operator-configured directories are required. Receipt paths are fixed below
those directories; all JSON paths are data, never filesystem authority. Times
are receipt filesystem metadata and SHA-matched observer declarations, not a
heartbeat. An absent current-observation job is not replaced by older progress.
"""
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
import re
import stat

MAX_FILE = 4 * 1024 * 1024
MODULES = {'__init__.py', 'human_evaluation_continuation.py', 'dedicated_protocol.py',
           'human_evaluation_action.py', 'stratified_evaluation.py'}
BINDINGS = {'measurement_sha256', 'model_sha256', 'manifest_sha256',
            'observer_source_sha256', 'anchor_file_sha256', 'policy_sha256'}
PLAN = BINDINGS | {'schema', 'threshold', 'evaluation_input_sha256',
                  'observation_sha256', 'observation_file_sha256', 'implementation',
                  'requested', 'provenance', 'admission', 'sha256'}


class Invalid(ValueError):
    pass


def need(value, reason='record_invalid'):
    if not value:
        raise Invalid(reason)


def sha(value):
    need(isinstance(value, str) and re.fullmatch('[a-f0-9]{64}', value))
    return value


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'),
                                     allow_nan=False).encode()).hexdigest()


def pairs(items):
    result = {}
    for key, value in items:
        need(key not in result, 'duplicate_key')
        result[key] = value
    return result


def integer(value, upper=10000):
    need(type(value) is int and 0 <= value <= upper)
    return value


def timestamp(value):
    need(isinstance(value, str) and len(value) <= 64)
    need(datetime.fromisoformat(value).utcoffset() is not None)
    return value


@contextmanager
def directory(path, parent=None):
    # Reject symlinks in every configured absolute path component too.
    if parent is None:
        need(isinstance(path, str) and path.startswith('/') and '..' not in path.split('/'), 'directory_invalid')
        fd = os.open('/', os.O_RDONLY | os.O_DIRECTORY)
        try:
            for part in filter(None, path.split('/')):
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                os.close(fd)
                fd = child
        except BaseException:
            os.close(fd)
            raise
    else:
        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
    try:
        info = os.fstat(fd)
        need(info.st_uid == os.getuid() and not info.st_mode & 0o022, 'directory_unsafe')
        yield fd
    finally:
        os.close(fd)


def raw(root, name, limit=MAX_FILE):
    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=root)
    try:
        before = os.fstat(fd)
        need(stat.S_ISREG(before.st_mode) and before.st_uid == os.getuid()
             and not before.st_mode & 0o022 and before.st_nlink == 1
             and 0 <= before.st_size <= limit, 'file_unsafe')
        with os.fdopen(fd, 'rb', closefd=False) as stream:
            value = stream.read(limit + 1)
        after = os.fstat(fd)
        need(len(value) <= limit and (before.st_size, before.st_mtime_ns, before.st_ctime_ns)
             == (after.st_size, after.st_mtime_ns, after.st_ctime_ns), 'file_changed')
        return value, after
    finally:
        os.close(fd)


def doc(root, name):
    value, info = raw(root, name)
    parsed = json.loads(value, object_pairs_hook=pairs,
                        parse_constant=lambda _: (_ for _ in ()).throw(Invalid('nonfinite')))
    need(isinstance(parsed, dict))
    return parsed, hashlib.sha256(value).hexdigest(), info


def verified(value):
    need(value.get('sha256') == digest({k: v for k, v in value.items() if k != 'sha256'}), 'receipt_hash_mismatch')
    return value


def exists(root, name):
    try:
        os.stat(name, dir_fd=root, follow_symlinks=False)
        return True
    except FileNotFoundError:
        return False


EVALUATED = {
    'measured_floor_not_met': ('supported_weighted_point_floor_not_met', 'audit_labels_blind_to_scores_then_prepare_a_new_candidate'),
    'insufficient_observed_support': ('actual_support_below_policy', 'plan_independent_evidence_without_relabeling_or_reusing_fit_data'),
    'point_floors_met_review_required': ('point_estimates_are_not_quality_approval', 'review_sampling_sensitivity_provenance_and_fleet_evidence'),
    'review_completed_quality_unresolved': ('explicit_uncertainty_remains', 'retain_unknowns_and_review_evidence_limits_without_forcing_labels'),
}


def completed(job, plan, version, counts, labels):
    need(counts['missing' if version == 1 else 'unseen'] == 0)
    claim, _, _ = doc(job, 'claim.json'); verified(claim)
    need(set(claim) == {'schema', 'sha256', 'plan_sha256', 'executor', 'pid', 'claimed_at'}
         and claim['schema'] == 'human-evaluation-job-claim/1' and claim['plan_sha256'] == plan['sha256'])
    need(isinstance(claim['executor'], str) and re.fullmatch(r'[A-Za-z0-9_.:@/-]{1,100}', claim['executor']))
    need(integer(claim['pid'], 2**31-1) > 0); timestamp(claim['claimed_at'])
    assessment, _, _ = doc(job, 'assessment.json'); verified(assessment)
    need(set(assessment) == {'schema', 'sha256', 'plan_sha256', 'claim_sha256', 'assessment', 'admission', 'publication_eligible'}
         and assessment['schema'] == 'human-evaluation-job-assessment/1'
         and assessment['plan_sha256'] == plan['sha256'] and assessment['claim_sha256'] == claim['sha256']
         and assessment['admission'] == 'not_granted' and assessment['publication_eligible'] is False)
    result = assessment['assessment']; need(isinstance(result, dict))
    state = result['state']; need(isinstance(state, str) and state in EVALUATED)
    need(result['schema'] == f'human-evaluation-next-action/{version}'
         and (result['reason'], result['next']) == EVALUATED[state]
         and result['input_sha256'] == plan['evaluation_input_sha256']
         and type(result['threshold']) in (int, float) and result['threshold'] == plan['threshold']
         and type(result['measurement']['requested']) is int and result['measurement']['requested'] == plan['requested']
         and result['admission'] == 'not_granted' and result['publication_eligible'] is False
         and result['release_or_hold_changes'] is False)
    uncertain = sorted(int(k) for k,v in labels.items() if v == 'uncertain') if version == 2 else []
    need(isinstance(result['measurement']['missing_ids'], list)
         and all(type(i) is int for i in result['measurement']['missing_ids'])
         and result['measurement']['missing_ids'] == uncertain)
    need((state == 'review_completed_quality_unresolved') == bool(uncertain))
    if version == 2:
        expected_responses = dict(decisive=plan['requested']-counts['uncertain'], uncertain=counts['uncertain'], unseen=0)
        need(result['responses'] == expected_responses and all(type(v) is int for v in result['responses'].values()))
    receipt, _, info = doc(job, 'result.json'); verified(receipt)
    record_state = 'review_completed_quality_unresolved' if uncertain else 'evaluated_not_admitted'
    expected = dict(schema=f'human-evaluation-continuation-result/{version}', plan_sha256=plan['sha256'],
        claim_sha256=claim['sha256'], assessment_sha256=assessment['sha256'], outcome='succeeded',
        state=record_state, reason=result['reason'], next=result['next'], evaluation_state=state,
        requested=plan['requested'], **counts, executed=True, admission='not_granted',
        publication_eligible=False, release_or_hold_changes=False)
    need(receipt == dict(expected, sha256=digest(expected)), 'receipt_binding_mismatch')
    return dict(state='available', reason='immutable_completion_record', record_state=record_state,
        receipt_sha256=receipt['sha256'], first_recorded_at=datetime.fromtimestamp(info.st_mtime, timezone.utc).isoformat(),
        requested=plan['requested'], **counts)


def _read(packet, observer, ref, original):
    contract, _, _ = doc(packet, 'source-contract.json')
    need(set(contract) in (BINDINGS | {'source_commit', 'source_files'},
                           BINDINGS | {'source_commit', 'source_files', 'review_binding_sha256'}))
    for key in BINDINGS:
        sha(contract[key])
    if contract['measurement_sha256'] != ref:
        return dict(state='missing', reason='no_matching_measurement')
    need(contract['model_sha256'] == original['artifact_sha256'], 'model_mismatch')
    need(isinstance(contract['source_commit'], str) and re.fullmatch('[a-f0-9]{40}', contract['source_commit']))
    files = contract['source_files']
    need(isinstance(files, dict) and set(files) == MODULES)
    with directory('source', packet) as source, directory('tagger', source) as modules:
        for name, expected in files.items():
            need(hashlib.sha256(raw(modules, name)[0]).hexdigest() == sha(expected), 'source_hash_mismatch')
    anchor, anchor_hash, _ = doc(packet, 'anchor-export.json')
    need(anchor_hash == contract['anchor_file_sha256'] and anchor['measurement_sha256'] == ref, 'anchor_mismatch')
    wire = anchor['measurement']['canonical_body']
    need(isinstance(wire, str) and hashlib.sha256(wire.encode()).hexdigest() == ref
         and json.loads(wire, object_pairs_hook=pairs) == original, 'anchor_mismatch')
    manifest, manifest_hash, _ = doc(observer, 'manifest.json')
    need(manifest_hash == contract['manifest_sha256'] and manifest['model_sha256'] == original['artifact_sha256'], 'manifest_mismatch')
    need(hashlib.sha256(raw(observer, 'observe-union-human.py')[0]).hexdigest()
         == contract['observer_source_sha256'], 'observer_source_mismatch')
    manifest_files = manifest['files']
    need(isinstance(manifest_files, dict))
    requested_ids = []
    for name in ('first-sample.json', 'extension-sample.json'):
        sample, sample_hash, _ = doc(observer, name); verified(sample)
        need(sample_hash == sha(manifest_files[name]) and sample['model_sha256'] == contract['model_sha256']
             and type(sample['threshold']) in (int, float) and sample['threshold'] == original['threshold']
             and sample['labels'] == {} and sample['publish'] is False, 'sample_binding_mismatch')
        sha(sample['frame_sha256'])
        ids = sample['post_ids']
        need(isinstance(ids, list) and 0 < len(ids) <= 4096)
        need(all(type(i) is int and 0 < i <= 2**63-1 for i in ids))
        requested_ids.extend(ids)
    need(0 < len(requested_ids) <= 4096 and len(set(requested_ids)) == len(requested_ids))
    review_binding = None
    if 'review_binding_sha256' in contract:
        binding, binding_hash, _ = doc(packet, 'review-binding.json')
        pins, _, _ = doc(packet, 'pins.json')
        need(binding_hash == sha(contract['review_binding_sha256']) == pins['review_binding_sha256']
             and all(pins[k] == contract[k] for k in BINDINGS | {'source_commit', 'source_files'}), 'review_binding_pin_mismatch')
        need(set(binding) == {'schema', 'queue_sha256', 'original_manifest_sha256', 'manifest_sha256',
             'observer_source_sha256', 'model_sha256', 'supersedes_queue_sha256', 'response_source', 'admission'})
        for key in ('queue_sha256', 'original_manifest_sha256', 'manifest_sha256',
                    'observer_source_sha256', 'model_sha256', 'supersedes_queue_sha256'):
            sha(binding[key])
        need(binding['schema'] == 'explicit-candidate-continuation-binding/1'
             and all(binding[k] == contract[k] for k in ('manifest_sha256', 'model_sha256', 'observer_source_sha256'))
             and binding['response_source'] == 'authenticated_account_explicit_events_not_independence_proof'
             and binding['admission'] == 'not_granted'
             and binding['queue_sha256'] != binding['supersedes_queue_sha256'], 'review_binding_mismatch')
        queue, queue_hash, _ = doc(observer, 'candidate-queue.json')
        need(queue_hash == binding['queue_sha256'] and queue['schema'] == 'human-review-set/2'
             and queue['purpose'] == 'candidate_evaluation' and queue['model_sha256'] == binding['model_sha256']
             and queue['post_ids'] == requested_ids and all(type(i) is int for i in queue['post_ids'])
             and queue['source_provenance'] == dict(kind='frozen_candidate_sample',
                 artifact_sha256=binding['original_manifest_sha256'], supersedes_queue_sha256=binding['supersedes_queue_sha256']),
             'review_queue_population_mismatch')
        review_binding = binding
    latest, latest_hash, _ = doc(observer, 'latest.json')
    observation_sha = sha(latest['observation_sha256'])
    observed_at = timestamp(latest['observed_at'])
    with directory('observations', observer) as snapshots:
        observation, observation_hash, _ = doc(snapshots, observation_sha + '.json')
    need(digest(observation) == observation_sha, 'observation_hash_mismatch')
    need(observation['model_sha256'] == contract['model_sha256']
         and observation['manifest_sha256'] == contract['manifest_sha256']
         and observation['publish'] is False and observation['labels_inferred'] is False
         and digest(observation['policy']) == contract['policy_sha256'], 'observation_binding_mismatch')
    need(observation['module_sha256'] == sha(manifest_files['stratified_evaluation.py'])
         and observation['action_sha256'] == sha(manifest_files['human_evaluation_action.py']), 'observation_source_mismatch')
    labels = observation['labels']
    need(isinstance(labels, dict) and all(isinstance(k, str) and re.fullmatch('[1-9][0-9]*', k)
         and int(k) in requested_ids and v in ('present', 'absent', 'uncertain', None) for k, v in labels.items()))
    if review_binding is not None:
        need(observation.get('schema') == 'explicit-candidate-observation/1'
             and observation.get('queue_sha256') == review_binding['queue_sha256']
             and observation.get('response_provenance') == review_binding['response_source'], 'event_observation_binding_mismatch')
        refs = observation.get('event_refs')
        need(isinstance(refs, dict) and set(refs) == set(labels))
        event_ids = []
        for key, event in refs.items():
            need(labels[key] in ('present', 'absent', 'uncertain') and isinstance(event, dict)
                 and set(event) == {'event_id', 'actor_id', 'received_at', 'source_context_sha256'})
            need(integer(event['event_id'], 2**63-1) > 0 and integer(event['actor_id'], 2**63-1) > 0)
            timestamp(event['received_at']); sha(event['source_context_sha256']); event_ids.append(event['event_id'])
        need(len(set(event_ids)) == len(event_ids), 'event_id_reused')
    identity = {key: contract[key] for key in ('measurement_sha256', 'manifest_sha256', 'policy_sha256')}
    identity['observation_sha256'] = observation_sha
    job_sha = digest(identity)
    namespaces = [name for name in ('jobs-v1', 'jobs-v2') if exists(packet, name)]
    need(len(namespaces) == 1, 'job_namespace_missing_or_ambiguous')
    with directory(namespaces[0], packet) as jobs:
        if not exists(jobs, job_sha):
            need(doc(observer, 'latest.json')[1] == latest_hash, 'observation_changed_during_read')
            return dict(state='missing', reason='current_observation_has_no_job', observation_sha256=observation_sha,
                        manifest_sha256=contract['manifest_sha256'], observer_observed_at=observed_at, review_binding=review_binding)
        with directory(job_sha, jobs) as job:
            plan, _, _ = doc(job, 'plan.json'); verified(plan)
            version = {'human-evaluation-continuation-plan/1': 1, 'human-evaluation-continuation-plan/2': 2}.get(plan.get('schema'))
            if version is None:
                return dict(state='unavailable', reason='schema_unsupported')
            need(namespaces[0] == f'jobs-v{version}', 'job_namespace_schema_mismatch')
            count_keys = {'missing'} if version == 1 else {'unseen', 'uncertain'}
            need(set(plan) == PLAN | count_keys)
            need(all(plan[key] == contract[key] for key in BINDINGS)
                 and plan['threshold'] == original['threshold'] and type(plan['threshold']) in (int, float)
                 and plan['observation_sha256'] == observation_sha and plan['observation_file_sha256'] == observation_hash
                 and plan['implementation'] == {k: v for k, v in files.items() if k != '__init__.py'}
                 and plan['provenance'] == 'frozen_observer_export_unverified'
                 and plan['admission'] == 'not_granted', 'plan_binding_mismatch')
            sha(plan['evaluation_input_sha256'])
            requested = integer(plan['requested']); need(requested == len(requested_ids))
            counts = {k: integer(plan[k], requested) for k in count_keys}
            if version == 2:
                need(counts == dict(unseen=sum(labels.get(str(i)) not in ('present', 'absent', 'uncertain') for i in requested_ids),
                                   uncertain=sum(labels.get(str(i)) == 'uncertain' for i in requested_ids)), 'counts_mismatch')
            else:
                need(counts['missing'] == sum(labels.get(str(i)) not in ('present', 'absent') for i in requested_ids), 'counts_mismatch')
            names = {name for name in ('waiting.json', 'claim.json', 'assessment.json', 'result.json') if exists(job, name)}
            # A claim is not proof of liveness, completion, or a successful job.
            if names == {'waiting.json'}:
                receipt, _, info = doc(job, 'waiting.json'); verified(receipt)
                expected = dict(schema=f'human-evaluation-continuation-result/{version}',
                    plan_sha256=plan['sha256'], outcome='waiting', state='awaiting_human_judgments',
                    reason='requested_judgments_missing', requested=requested, **counts, executed=False,
                    next='review_remaining_images_without_guessing_labels', admission='not_granted',
                    publication_eligible=False, release_or_hold_changes=False)
                need(receipt == dict(expected, sha256=digest(expected)), 'receipt_binding_mismatch')
                need(counts['missing' if version == 1 else 'unseen'] > 0)
                record = dict(state='available', reason='immutable_waiting_record', receipt_sha256=receipt['sha256'],
                    first_recorded_at=datetime.fromtimestamp(info.st_mtime, timezone.utc).isoformat(),
                    requested=requested, **counts, record_state=receipt['state'])
            elif names == {'claim.json', 'assessment.json', 'result.json'}:
                record = completed(job, plan, version, counts, labels)
            elif names:
                record = dict(state='unavailable', reason='completion_chain_incomplete')
            else:
                record = dict(state='missing', reason='plan_without_receipt')
    # Do not mix a replaced latest pointer's timestamp with the selected record.
    need(doc(observer, 'latest.json')[1] == latest_hash, 'observation_changed_during_read')
    return dict(record, schema_version=version, observation_sha256=observation_sha,
                observer_observed_at=observed_at, manifest_sha256=contract['manifest_sha256'],
                source_commit=contract['source_commit'], job_sha256=job_sha, review_binding=review_binding)


def projection(ref, original, *, packet_path=None, observer_path=None):
    packet_path = packet_path if packet_path is not None else os.environ.get('KURA_MEASUREMENT_CONTINUATION_DIR')
    observer_path = observer_path if observer_path is not None else os.environ.get('KURA_MEASUREMENT_OBSERVER_DIR')
    envelope = dict(measurement_sha256=ref, admission='not_granted', publication_eligible=False,
                    release_or_hold_changes=False, heartbeat_available=False,
                    provenance='local_receipt_unverified')
    if not packet_path or not observer_path:
        return dict(envelope, state='unavailable', reason='not_configured')
    try:
        sha(ref)
        with directory(packet_path) as packet, directory(observer_path) as observer:
            value = _read(packet, observer, ref, original)
        return dict(envelope, **value)
    except FileNotFoundError:
        return dict(envelope, state='unavailable', reason='required_file_missing')
    except OSError:
        return dict(envelope, state='unavailable', reason='local_store_unreadable')
    except (Invalid, ValueError, TypeError, KeyError, OverflowError, RecursionError):
        return dict(envelope, state='invalid', reason='local_record_unverifiable')
