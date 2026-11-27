"""Execute the real evaluator locally; no DB, HTTP, GPU or production writes."""
from copy import deepcopy
import hashlib
import json
import multiprocessing as mp
from pathlib import Path
import subprocess
import sys
import time

import pytest

from tagger import dedicated_protocol as p
from tagger import human_evaluation_continuation as c


MODEL = 'a' * 64
POLICY = dict(min_precision=.8, min_recall=.7, min_val_pos_support=2,
              min_pred_pos_support=2)


def fixture(tmp_path, labels=None):
    bundle = tmp_path / 'bundle'; bundle.mkdir()
    for module in (c.action, c.evaluation):
        source = Path(module.__file__)
        (bundle / source.name).write_bytes(source.read_bytes())
    # This source is pinned but never executed: deliberately throws if imported.
    (bundle / 'observe-union-human.py').write_text('raise RuntimeError("must not execute observer")\n')
    package = p.write_once(bundle / 'package.json', dict(model_sha256=MODEL,
                                                       source_checkpoint_sha256='b' * 64))
    for prefix, start in (('first', 1), ('extension', 101)):
        scores = {str(i): .9 if i < start + 3 else .1 for i in range(start, start + 6)}
        values = dict(model_checkpoint_sha256='b' * 64) if prefix == 'first' else dict(model_sha256=MODEL)
        score = p.write_once(bundle / f'{prefix}-scores.json', dict(
            scores=scores, frame_sha256=('c' if prefix == 'first' else 'd') * 64,
            threshold=.5, labels={}, publish=False, **values))
        p.write_once(bundle / f'{prefix}-sample.json', dict(model_sha256=MODEL,
            score_sha256=score['sha256'], frame_sha256=score['frame_sha256'], threshold=.5,
            labels={}, publish=False, post_ids=list(range(start, start + 6)),
            strata=dict(predicted_present=dict(sample_ids=list(range(start, start + 3))),
                        predicted_absent=dict(sample_ids=list(range(start + 3, start + 6))))))
    manifest = dict(model_sha256=MODEL, files={name: hashlib.sha256((bundle / name).read_bytes()).hexdigest()
                                             for name in c.FILES})
    (bundle / 'manifest.json').write_text(json.dumps(manifest))
    anchor_body = dict(schema='original-measurement/1', provenance='caller_assertion',
        measurement_scope='internal_training', quality_claim=False, artifact_sha256=MODEL,
        threshold=.5, tag='fixture', tag_id=10)
    wire = json.dumps(anchor_body, sort_keys=True, separators=(',', ':'))
    ref = hashlib.sha256(wire.encode()).hexdigest()
    anchor = tmp_path / 'anchor.json'; anchor.write_text(json.dumps(dict(
        measurement_sha256=ref, measurement=dict(canonical_body=wire))))
    manifest_sha = hashlib.sha256((bundle / 'manifest.json').read_bytes()).hexdigest()
    if labels is None:
        labels = {str(i): 'present' if i in (1, 2, 3, 101, 102, 103) else 'absent'
                  for i in list(range(1, 7)) + list(range(101, 107))}
    obs = dict(manifest_sha256=manifest_sha, model_sha256=MODEL,
        module_sha256=manifest['files']['stratified_evaluation.py'],
        action_sha256=manifest['files']['human_evaluation_action.py'],
        labels=labels, policy=POLICY, labels_inferred=False, publish=False,
        assessment={'state': 'forged_pass_ignored'}, measurement={'judged': 999})
    observation = tmp_path / 'observation.json'; observation.write_text(json.dumps(obs))
    args = dict(bundle=bundle, manifest_sha256=manifest_sha,
        observer_source_sha256=hashlib.sha256((bundle / 'observe-union-human.py').read_bytes()).hexdigest(),
        anchor=anchor, measurement_sha256=ref, observation=observation,
        observation_sha256=p.digest(obs), policy_sha256=p.digest(POLICY),
        output=tmp_path / 'jobs', executor='test-owner')
    return args, obs


def update(args, obs):
    args['observation'].write_text(json.dumps(obs))
    args['observation_sha256'] = p.digest(obs)


def receipt(args):
    return next(args['output'].glob('*/assessment.json'))


def test_missing_waits_without_claim_then_complete_executes_once(tmp_path, monkeypatch):
    args, obs = fixture(tmp_path, {})
    waiting = c.run(**args)
    assert waiting['state'] == 'awaiting_human_judgments' and not waiting['executed']
    assert waiting['next'] == 'review_remaining_images_without_guessing_labels'
    assert {'outcome', 'state', 'reason', 'next'} <= set(waiting)
    assert not list(args['output'].glob('*/claim.json'))
    assert c.run(**args) == waiting
    obs['labels'] = {str(i): 'present' if i in (1, 2, 3, 101, 102, 103) else 'absent'
                     for i in list(range(1, 7)) + list(range(101, 107))}
    update(args, obs)
    calls = []; original = c.action.assess
    def assess(*a, **kw):
        calls.append(1); return original(*a, **kw)
    monkeypatch.setattr(c.action, 'assess', assess)
    result = c.run(**args)
    assert c.run(**(args | {'executor': 'second-owner'})) == result
    assert len(calls) == 1 and result['state'] == 'evaluated_not_admitted'
    assert result['evaluation_state'] == 'point_floors_met_review_required'
    assert result['next'] == 'review_sampling_sensitivity_provenance_and_fleet_evidence'
    assert {'outcome', 'state', 'reason', 'next'} <= set(result)
    assert result['admission'] == 'not_granted' and result['publication_eligible'] is False
    assert list(args['output'].glob('*/waiting.json'))
    report = p.read_verified(receipt(args))['assessment']
    assert report['measurement']['observed_counts'] == dict(tp=6, fp=0, tn=6, fn=0)
    assert report['state'] != obs['assessment']['state']


def test_actual_failure_overrides_forged_observer_pass(tmp_path):
    args, obs = fixture(tmp_path)
    for key in ('1', '2', '101', '102'): obs['labels'][key] = 'absent'
    update(args, obs)
    result = c.run(**args)
    assert result['evaluation_state'] == 'measured_floor_not_met'
    assert result['next'] == 'audit_labels_blind_to_scores_then_prepare_a_new_candidate'
    assert p.read_verified(receipt(args))['assessment']['point_floor_checks']['precision'] is False
    assert result['release_or_hold_changes'] is False


def test_unseen_is_waiting_never_negative(tmp_path):
    args, obs = fixture(tmp_path); obs['labels']['1'] = None; update(args, obs)
    r = c.run(**args)
    assert r['unseen'] == 1 and r['outcome'] == 'waiting'
    assert not list(args['output'].glob('*/assessment.json'))


@pytest.mark.parametrize('mutation', ['hash', 'model', 'policy', 'label', 'id', 'code', 'symlink'])
def test_invalid_inputs_refused_before_claim(tmp_path, mutation):
    args, obs = fixture(tmp_path)
    if mutation == 'hash': args['observation_sha256'] = '0' * 64
    elif mutation == 'model': obs['model_sha256'] = '0' * 64; update(args, obs)
    elif mutation == 'policy': obs['policy'] = dict(POLICY, min_precision=.1); update(args, obs)
    elif mutation == 'label': obs['labels']['1'] = 'yes'; update(args, obs)
    elif mutation == 'id': obs['labels']['999'] = 'present'; update(args, obs)
    elif mutation == 'code': (args['bundle'] / 'human_evaluation_action.py').write_text('pass')
    else:
        target = tmp_path / 'elsewhere'; args['observation'].rename(target); args['observation'].symlink_to(target)
    with pytest.raises((c.Refused, OSError)):
        c.run(**args)
    assert not args['output'].exists()


def test_same_semantic_export_reformat_cannot_execute_again(tmp_path, monkeypatch):
    args, obs = fixture(tmp_path); c.run(**args)
    args['observation'].write_text(json.dumps(obs, indent=2))
    monkeypatch.setattr(c.action, 'assess', lambda *a, **kw: pytest.fail('re-executed'))
    with pytest.raises(p.ProtocolError, match='immutable output'):
        c.run(**args)
    assert len(list(args['output'].glob('*/claim.json'))) == 1


def test_completed_assessment_can_finish_receipt_after_interruption(tmp_path, monkeypatch):
    args, _ = fixture(tmp_path); original = c.p.write_once
    def stop(path, body):
        if Path(path).name == 'result.json': raise RuntimeError('simulated interruption')
        return original(path, body)
    with monkeypatch.context() as m:
        m.setattr(c.p, 'write_once', stop)
        with pytest.raises(RuntimeError): c.run(**args)
    assert receipt(args).exists()
    monkeypatch.setattr(c.action, 'assess', lambda *a, **kw: pytest.fail('re-executed'))
    assert c.run(**args)['state'] == 'evaluated_not_admitted'


def _child(args, entered, release, count, result):
    original = c.action.assess
    def blocked(*a, **kw):
        with count.get_lock(): count.value += 1
        entered.set(); release.wait(10)
        return original(*a, **kw)
    c.action.assess = blocked
    try: result.put(c.run(**args)['state'])
    except c.Refused as exc: result.put(str(exc))


def test_real_process_concurrent_claim_and_completed_replay(tmp_path):
    args, _ = fixture(tmp_path); ctx = mp.get_context('fork')
    entered, release, count, results = ctx.Event(), ctx.Event(), ctx.Value('i', 0), ctx.Queue()
    first = ctx.Process(target=_child, args=(args, entered, release, count, results)); first.start()
    try:
        assert entered.wait(10)
        other = ctx.Process(target=_child, args=(args, entered, release, count, results)); other.start(); other.join(10)
        assert not other.is_alive() and results.get(timeout=2) == 'job_already_running'
        release.set(); first.join(10)
        assert results.get(timeout=2) == 'evaluated_not_admitted' and count.value == 1
        assert c.run(**args)['state'] == 'evaluated_not_admitted'
    finally:
        release.set(); first.join(2)
        if first.is_alive(): first.kill(); first.join()


def test_real_killed_process_does_not_auto_retry_claim(tmp_path):
    args, _ = fixture(tmp_path); ctx = mp.get_context('fork')
    entered, release, count, results = ctx.Event(), ctx.Event(), ctx.Value('i', 0), ctx.Queue()
    child = ctx.Process(target=_child, args=(args, entered, release, count, results)); child.start()
    try:
        assert entered.wait(10); child.kill(); child.join(10)
        with pytest.raises(c.Refused, match='job_interrupted_preserve_attempt'): c.run(**args)
        assert count.value == 1 and not list(args['output'].glob('*/assessment.json'))
    finally:
        if child.is_alive(): child.kill(); child.join()


def test_cli_has_no_db_worker_or_network_import_and_closed_output(tmp_path):
    args, _ = fixture(tmp_path)
    # Import guard is installed in a clean subprocess before the actual CLI.
    code = '''import sys,runpy
def audit(event,args):
 if event in ('socket.connect','socket.bind','subprocess.Popen','os.system'):
  raise RuntimeError('forbidden external effect')
sys.addaudithook(audit)
class Guard:
 def find_spec(self,name,path=None,target=None):
  if name.split('.')[0] in ('asyncpg','sqlalchemy','requests','httpx','torch','timm') or name=='api.config':
   raise RuntimeError('forbidden dependency')
sys.meta_path.insert(0,Guard())
runpy.run_module('tagger.human_evaluation_continuation',run_name='__main__')
'''
    cli = [sys.executable, '-c', code]
    for key, value in args.items(): cli += ['--' + key.replace('_', '-'), str(value)]
    done = subprocess.run(cli, text=True, capture_output=True, timeout=20)
    assert done.returncode == 0, done.stderr
    out = json.loads(done.stdout)
    assert out['state'] == 'evaluated_not_admitted' and 'labels' not in out
    assert str(tmp_path) not in done.stdout


def test_rehashed_untrusted_python_is_not_executed(tmp_path):
    args, obs = fixture(tmp_path)
    manifest_path = args['bundle'] / 'manifest.json'
    manifest = json.loads(manifest_path.read_text())
    source = args['bundle'] / 'human_evaluation_action.py'; source.write_text('raise SystemExit(99)')
    manifest['files'][source.name] = hashlib.sha256(source.read_bytes()).hexdigest()
    manifest_path.write_text(json.dumps(manifest))
    args['manifest_sha256'] = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
    obs['manifest_sha256'] = args['manifest_sha256']; obs['action_sha256'] = manifest['files'][source.name]
    update(args, obs)
    with pytest.raises(c.Refused, match='installed_evaluator_mismatch'): c.run(**args)
    assert not args['output'].exists()


def test_file_and_total_budgets_are_enforced(tmp_path, monkeypatch):
    monkeypatch.setattr(c, 'MAX_FILE', 512); monkeypatch.setattr(c, 'MAX_TOTAL', 700)
    one = tmp_path / 'one'; one.write_bytes(b'x' * 400)
    reader = c.Reader(); reader.raw(one)
    with pytest.raises(c.Refused, match='input_budget_exceeded'): reader.raw(one)
    one.write_bytes(b'x' * 513)
    with pytest.raises(c.Refused, match='input_file_refused'): c.Reader().raw(one)


def test_duplicate_json_key_is_rejected(tmp_path):
    args, _ = fixture(tmp_path)
    args['observation'].write_text('{"labels":{},"labels":{"1":"present"}}')
    with pytest.raises(c.Refused, match='duplicate_json_key'): c.run(**args)


@pytest.mark.parametrize('field,replacement', [('input_sha256', '0' * 64), ('next', 'publish_now'), ('reason', 'quality_approved')])
def test_resume_refuses_foreign_evaluation_input_or_next_even_if_rehashed(tmp_path, monkeypatch, field, replacement):
    args, _ = fixture(tmp_path); original = c.p.write_once
    def stop(path, body):
        if Path(path).name == 'result.json': raise RuntimeError('stop')
        return original(path, body)
    with monkeypatch.context() as m:
        m.setattr(c.p, 'write_once', stop)
        with pytest.raises(RuntimeError): c.run(**args)
    path = receipt(args); value = json.loads(path.read_text())
    value['assessment'][field] = replacement
    value['sha256'] = p.digest({k: v for k, v in value.items() if k != 'sha256'})
    path.write_text(json.dumps(value))
    with pytest.raises(c.Refused, match='assessment_invalid'): c.run(**args)


def test_output_fifo_is_refused_before_atomic_writer(tmp_path, monkeypatch):
    import os
    args, _ = fixture(tmp_path); c.run(**args)
    plan = next(args['output'].glob('*/plan.json')); plan.unlink(); os.mkfifo(plan)
    monkeypatch.setattr(c.p, 'write_once', lambda *a: pytest.fail('unsafe writer reached'))
    with pytest.raises(c.Refused, match='output_file_refused'): c.run(**args)


def test_valid_policy_floor_one_is_accepted(tmp_path):
    args, obs = fixture(tmp_path); obs['policy'] = dict(POLICY, min_precision=1)
    args['policy_sha256'] = p.digest(obs['policy']); update(args, obs)
    assert c.run(**args)['evaluation_state'] == 'point_floors_met_review_required'


def test_same_observation_does_not_reexecute_after_consumer_source_drift(tmp_path, monkeypatch):
    args, _ = fixture(tmp_path); c.run(**args)
    original_read = Path.read_bytes
    consumer = Path(c.__file__)
    def changed_source(path):
        value = original_read(path)
        return value + b'\n# newly deployed consumer\n' if path == consumer else value
    monkeypatch.setattr(Path, 'read_bytes', changed_source)
    # The changed implementation must collide with its immutable plan rather
    # than silently generating another execution directory for the same input.
    with pytest.raises(p.ProtocolError, match='immutable output'):
        c.run(**args)
    assert len(list(args['output'].glob('*/claim.json'))) == 1


@pytest.mark.parametrize('entry', ['package.json', 'first-sample.json', 'extension-scores.json'])
def test_null_manifest_file_pin_refused_before_output(tmp_path, entry):
    args, obs = fixture(tmp_path); path = args['bundle'] / 'manifest.json'
    manifest = json.loads(path.read_text()); manifest['files'][entry] = None
    path.write_text(json.dumps(manifest)); args['manifest_sha256'] = hashlib.sha256(path.read_bytes()).hexdigest()
    obs['manifest_sha256'] = args['manifest_sha256']; update(args, obs)
    with pytest.raises(c.Refused, match='hash_invalid'): c.run(**args)
    assert not args['output'].exists()


@pytest.mark.parametrize('key', ['manifest_sha256', 'observer_source_sha256'])
def test_null_required_raw_pin_cannot_disable_verification(tmp_path, key):
    args, obs = fixture(tmp_path); args[key] = None
    if key == 'manifest_sha256': obs['manifest_sha256'] = None; update(args, obs)
    with pytest.raises(c.Refused, match='hash_invalid'): c.run(**args)
    assert not args['output'].exists()


def test_orphan_claim_cannot_be_bypassed_by_source_drift(tmp_path, monkeypatch):
    args, _ = fixture(tmp_path)
    def stop(*a, **kw): raise RuntimeError('interrupted')
    with monkeypatch.context() as m:
        m.setattr(c.action, 'assess', stop)
        with pytest.raises(RuntimeError): c.run(**args)
    original_read = Path.read_bytes
    def changed(path):
        value = original_read(path)
        return value + b'\n# upgrade\n' if path == Path(c.__file__) else value
    monkeypatch.setattr(Path, 'read_bytes', changed)
    with pytest.raises(p.ProtocolError, match='immutable output'): c.run(**args)
    assert len(list(args['output'].glob('*/claim.json'))) == 1
    assert not list(args['output'].glob('*/assessment.json'))


def test_bad_cli_arguments_do_not_echo_values():
    value = 'private-argument-must-not-be-echoed'
    done = subprocess.run([sys.executable, '-m', 'tagger.human_evaluation_continuation',
                           '--unexpected', value], text=True, capture_output=True, timeout=10)
    assert done.returncode == 1 and json.loads(done.stdout)['reason'] == 'arguments_invalid'
    assert value not in done.stdout + done.stderr
    assert json.loads(done.stdout)['next'] == 'inspect_preserved_attempt_no_retry'
    assert {'outcome', 'state', 'reason', 'next'} <= set(json.loads(done.stdout))


@pytest.mark.parametrize('field', ['checkpoint', 'frame'])
def test_consistently_rehashed_null_internal_identity_is_refused(tmp_path, field):
    args, obs = fixture(tmp_path); bundle = args['bundle']
    docs = {n: json.loads((bundle / n).read_text()) for n in
            ('package.json', 'first-scores.json', 'first-sample.json')}
    if field == 'checkpoint':
        docs['package.json']['source_checkpoint_sha256'] = None
        docs['first-scores.json']['model_checkpoint_sha256'] = None
    else:
        docs['first-scores.json']['frame_sha256'] = None
        docs['first-sample.json']['frame_sha256'] = None
    for name in ('package.json', 'first-scores.json', 'first-sample.json'):
        value = docs[name]
        if name == 'first-sample.json': value['score_sha256'] = docs['first-scores.json']['sha256']
        value['sha256'] = p.digest({k: v for k, v in value.items() if k != 'sha256'})
        (bundle / name).write_text(json.dumps(value))
    manifest = json.loads((bundle / 'manifest.json').read_text())
    manifest['files'] = {n: hashlib.sha256((bundle / n).read_bytes()).hexdigest() for n in c.FILES}
    (bundle / 'manifest.json').write_text(json.dumps(manifest))
    args['manifest_sha256'] = hashlib.sha256((bundle / 'manifest.json').read_bytes()).hexdigest()
    obs['manifest_sha256'] = args['manifest_sha256']; update(args, obs)
    with pytest.raises(c.Refused, match='hash_invalid'): c.run(**args)
    assert not args['output'].exists()


def test_completed_insufficient_support_has_closed_next(tmp_path):
    args, obs = fixture(tmp_path); obs['policy'] = dict(POLICY, min_val_pos_support=20)
    args['policy_sha256'] = p.digest(obs['policy']); update(args, obs)
    result = c.run(**args)
    assert result['evaluation_state'] == 'insufficient_observed_support'
    assert result['next'] == 'plan_independent_evidence_without_relabeling_or_reusing_fit_data'
    assert not result['publication_eligible']


def test_all_responded_uncertain_finishes_review_not_quality(tmp_path):
    args, obs = fixture(tmp_path)
    obs['labels']['1']='uncertain';update(args,obs)
    result=c.run(**args)
    assert result['state']=='review_completed_quality_unresolved'
    assert result['unseen']==0 and result['uncertain']==1
    report=json.loads(receipt(args).read_text())['assessment']
    assert report['point_floor_checks'] is None
    assert report['measurement']['missing_ids']==[1]
    assert report['responses']=={'decisive':11,'uncertain':1,'unseen':0}
    assert result['publication_eligible'] is False
    assert c.run(**args)==result


def test_unseen_and_uncertain_wait_only_for_unseen(tmp_path):
    args,obs=fixture(tmp_path)
    obs['labels']['1']='uncertain';obs['labels'].pop('2');update(args,obs)
    result=c.run(**args)
    assert result['state']=='awaiting_human_judgments'
    assert result['unseen']==1 and result['uncertain']==1
    assert not list(args['output'].glob('*/claim.json'))


def test_multiframe_uncertainty_order_is_canonical_and_denominators_unchanged(tmp_path):
    args,obs=fixture(tmp_path)
    _,frames,labels,policy,_=c.prepare(**{k:v for k,v in args.items() if k not in ('output','executor')})
    baseline=c.action.assess(frames,labels,threshold=.5,policy=policy)['measurement']
    for i in [106,4,101,1]:obs['labels'][str(i)]='uncertain'
    update(args,obs);result=c.run(**args)
    report=json.loads(receipt(args).read_text())['assessment']
    assert report['measurement']['missing_ids']==[1,4,101,106]
    assert report['measurement']['requested']==12 and report['measurement']['weighted_estimate'] is None
    assert result['schema']=='human-evaluation-continuation-result/2'
    assert result['unseen']==0 and result['uncertain']==4
    assert report['responses']=={'decisive':8,'uncertain':4,'unseen':0}
    assert result['state']=='review_completed_quality_unresolved'
    for old,new in zip(baseline['frames'],report['measurement']['frames']):
        for stratum in old['strata']:
            for key in ('requested','frame_count','inclusion_probability','inverse_probability_weight'):
                assert old['strata'][stratum][key]==new['strata'][stratum][key]
