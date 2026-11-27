"""Pure assertion contract and optional-worker-free API import controls."""
from copy import deepcopy
import hashlib
import json

import pytest
from api.tagger_core import measurement_evidence as m
from api.tagger_core import qualification_evidence as q

A, B, C, D = ('a'*64, 'b'*64, 'c'*64, 'd'*64)


def measurement():
    prep = {'schema': 'kura-content-preprocessing/1', 'color': 'RGB',
            'size': [448, 448], 'normalization': {'mean': [.5]*3, 'std': [.5]*3}}
    return {'artifact_sha256': A, 'threshold': .5,
            'source': {key: B for key in m.SOURCE_FIELDS},
            'feature_contract': {'schema': 'kura-feature-contract/1', 'backbone_sha256': C,
                'preprocessing': prep, 'preprocessing_sha256': q.digest(prep),
                'classifier_reset': True, 'libraries': {'torch': '2.9.1+cpu', 'torchvision': '0.24.1+cpu',
                    'timm': '1.0.27', 'Pillow': '12.3.0'}, 'implementation_sha256': D},
            'internal_validation': {'tp': 160, 'fp': 2, 'tn': 798, 'fn': 40,
                                    'precision': 160/162, 'recall': .8, 'auc': .99509375}}


def runtime(parity=None):
    return dict(artifact_sha256=A, threshold=.5, observed_at='2026-11-17T04:00:00+09:00',
        source_sha256=B, inventory_sha256=C, inputs_sha256=D, host='fixture', profile='full CPU observation',
        device='cpu', provider='CPUExecutionProvider', model_inventory=[A, B], memory_limit_bytes=6*1024**3,
        peak_bytes=5*1024**3, threads=2, deadline_s=600, elapsed_s=480, completed=16, total=16,
        oom_events=0, parity_passed=parity)


def anchor(value=None):
    return m.original_measurement(3, 'fixture', value or measurement())


def test_original_internal_measurement_has_no_registration_or_growth_baseline():
    value = measurement(); before = deepcopy(value); packet = anchor(value)
    assert packet['body']['measurement_scope'] == 'internal_training'
    assert packet['body']['provenance'] == 'caller_assertion'
    assert packet['body']['quality_claim'] is False
    assert not {'id', 'registration_id', 'lifecycle_event_id', 'gt', 'observed_at'} & packet['body'].keys()
    assert packet['measurement_sha256'] == hashlib.sha256(packet['canonical_body'].encode()).hexdigest()
    assert m.verify_anchor(packet['measurement_sha256'], json.loads(packet['canonical_body'])) == packet
    assert anchor(dict(reversed(list(value.items())))) == packet
    assert value == before
    value['internal_validation']['tp'] = 1
    assert packet['body']['internal_validation']['tp'] == 160


@pytest.mark.parametrize('key', ['gt', 'lifecycle_event_id', 'registration_id', 'publication_eligible', 'human_review_event_id'])
def test_caller_cannot_attach_operational_authority_to_an_original_measurement(key):
    value = measurement(); value[key] = 1
    with pytest.raises(q.QualificationEvidenceRefused, match='report_shape_invalid'):
        anchor(value)


@pytest.mark.parametrize('parity', [None, False, True])
def test_runtime_uses_existing_report_contract_and_immutable_anchor(parity):
    a = anchor(); value = runtime(parity)
    packet = m.assemble(a['measurement_sha256'], a['body'], a['measurement_sha256'], 'runtime', value)
    body = json.loads(packet['canonical_body'])
    assert body['schema'] == 'measurement-qualification-evidence/1'
    assert body['measurement_sha256'] == a['measurement_sha256']
    assert body['report']['parity_passed'] is parity and body['provenance'] == 'caller_assertion'
    assert 'registration_id' not in body
    assert packet['receipt_sha256'] == hashlib.sha256(packet['canonical_body'].encode()).hexdigest()
    assert m.assemble(a['measurement_sha256'], a['body'], a['measurement_sha256'], 'runtime', body['report']) == packet
    value['parity_passed'] = 'mutated'
    assert packet['body']['report']['parity_passed'] is parity


def test_same_artifact_different_original_measurement_cannot_receive_wrong_evidence():
    first = anchor(); changed = measurement(); changed['source']['training_result_sha256'] = C
    second = anchor(changed)
    assert first['measurement_sha256'] != second['measurement_sha256']
    with pytest.raises(q.QualificationEvidenceRefused, match='measurement_anchor_mismatch'):
        m.assemble(second['measurement_sha256'], second['body'], first['measurement_sha256'], 'runtime', runtime())
    with pytest.raises(q.QualificationEvidenceRefused, match='measurement_anchor_mismatch'):
        m.assemble(first['measurement_sha256'], second['body'], first['measurement_sha256'], 'runtime', runtime())


@pytest.mark.parametrize('field,value', [('artifact_sha256', B), ('threshold', .6), ('parity_passed', 0)])
def test_report_substitution_is_refused_by_shared_validator(field, value):
    a = anchor(); report = runtime(); report[field] = value
    with pytest.raises(q.QualificationEvidenceRefused):
        m.assemble(a['measurement_sha256'], a['body'], a['measurement_sha256'], 'runtime', report)


def test_internal_metrics_and_preprocessing_must_be_consistent_but_are_not_quality():
    value = measurement(); value['internal_validation']['precision'] = 1.
    with pytest.raises(q.QualificationEvidenceRefused, match='measurement_metric_mismatch'):
        anchor(value)
    value = measurement(); value['feature_contract']['preprocessing_sha256'] = A
    with pytest.raises(q.QualificationEvidenceRefused, match='measurement_preprocessing_mismatch'):
        anchor(value)
    value = measurement(); value['internal_validation']['tp'] = True
    with pytest.raises(q.QualificationEvidenceRefused):
        anchor(value)


def test_human_sample_remains_assertion_not_an_authenticated_human_event():
    a = anchor()
    report = {key: runtime()[key] for key in ('artifact_sha256', 'threshold', 'observed_at')}
    report.update(frame_sha256=B, sample_sha256=C, policy_sha256=D,
                  judgments=[dict(post_id=1, state='uncertain', reviewer='claimed reviewer',
                      reviewed_at='2026-11-17T04:00:00+09:00', source_ref=A)])
    packet = m.assemble(a['measurement_sha256'], a['body'], a['measurement_sha256'], 'human_sample', report)
    assert packet['body']['provenance'] == 'caller_assertion'
    assert packet['body']['report']['judgments'][0]['state'] == 'uncertain'
    report['provenance'] = 'canonical_human_review_event'
    with pytest.raises(q.QualificationEvidenceRefused, match='report_shape_invalid'):
        m.assemble(a['measurement_sha256'], a['body'], a['measurement_sha256'], 'human_sample', report)


def test_forged_anchor_provenance_and_quality_cannot_survive_hash_verification():
    a = anchor()
    for change in ({'provenance': 'authenticated_human_event'}, {'quality_claim': True},
                   {'measurement_scope': 'independent_quality'}):
        forged = a['body'] | change
        with pytest.raises(q.QualificationEvidenceRefused, match='measurement_anchor_mismatch'):
            m.verify_anchor(q.digest(forged), forged)


def test_registration_context_and_schema_are_still_separate():
    reg = dict(id=12, tag='fixture', tag_id=3, lifecycle_event_id=7, artifact_sha256=A,
               metrics={'threshold': .5}, gt={'positive': 20, 'negative': 30})
    context = q.context(reg); before = q.assemble(context, q.digest(context), 'runtime', runtime())
    a = anchor(); new = m.assemble(a['measurement_sha256'], a['body'], a['measurement_sha256'], 'runtime', runtime())
    assert q.CONTEXT_FIELDS == {'id', 'tag', 'tag_id', 'lifecycle_event_id', 'artifact_sha256', 'metrics', 'gt'}
    assert q.assemble(context, q.digest(context), 'runtime', runtime()) == before
    assert before['body']['schema'] == 'qualification-evidence/1'
    assert before['receipt_sha256'] != new['receipt_sha256']
    assert q.admission_status() == dict(admission='not_granted', publication_eligible=False, release_or_hold_changes=False)


def test_new_api_modules_import_without_optional_worker_package():
    import subprocess, sys
    from pathlib import Path
    code = '''
import importlib.abc, sys
class NoWorker(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == 'tagger' or fullname.startswith('tagger.'):
            raise ModuleNotFoundError('optional worker package deliberately absent')
sys.meta_path.insert(0, NoWorker())
from api.tagger_core import measurement_evidence
from api.services import measurement_evidence
from api.routers import tagger_measurements
print('api_without_worker_ok')
'''
    result = subprocess.run([sys.executable, '-c', code], cwd=Path(__file__).resolve().parents[1],
                            text=True, capture_output=True, timeout=20)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == 'api_without_worker_ok'


def test_declared_preprocessing_metadata_does_not_claim_executable_validation():
    value = measurement()
    value['feature_contract']['preprocessing']['resize'] = 'future_worker_mode'
    value['feature_contract']['preprocessing_sha256'] = q.digest(value['feature_contract']['preprocessing'])
    # Integrity/schema assertion only. This would need separate worker-side
    # qualification and cannot acquire approval through this intake.
    stored = anchor(value)
    assert stored['body']['feature_contract']['preprocessing']['resize'] == 'future_worker_mode'
    assert stored['body']['quality_claim'] is False
