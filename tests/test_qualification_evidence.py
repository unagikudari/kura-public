"""Qualification receipts validate assertions; they never manufacture approval."""
import asyncio
from copy import deepcopy
import hashlib
import json

import pytest
from api.tagger_core import qualification_evidence as q

A, B, C, D = ('a'*64, 'b'*64, 'c'*64, 'd'*64)
AT = '2026-11-17T04:00:00+09:00'


def test_base_comparison_is_rejected_before_publication_reads_the_database():
    from api.tagger_core.canonical_evaluation import require_publication_review

    row = {"metrics": {"production_review": {
        "scope": "catalog_sample_outside_frozen_gt_groups",
        "request": {"purpose": "base_comparison"},
    }}}
    with pytest.raises(ValueError, match="publication_review_purpose_invalid"):
        asyncio.run(require_publication_review(None, row, {}, answers_sha256=A))


def registration():
    return dict(id=12, tag='fixture', tag_id=3, lifecycle_event_id=7,
                artifact_sha256=A, metrics={'threshold': .25, 'feature_contract': {'schema': 'fixture/1'}},
                gt={'positive': 20, 'negative': 30}, status='rejected')


def report(kind='human_sample'):
    common = dict(artifact_sha256=A, threshold=.25, observed_at=AT)
    if kind == 'human_sample':
        return common | dict(frame_sha256=B, sample_sha256=C, policy_sha256=D,
            judgments=[dict(post_id=10, state='uncertain', reviewer='human assertion', reviewed_at=AT, source_ref=B)])
    if kind == 'runtime':
        return common | dict(source_sha256=B, inventory_sha256=C, inputs_sha256=D,
            host='nodeb', profile='full', device='cpu', provider='CPUExecutionProvider',
            model_inventory=[A, B], memory_limit_bytes=6*1024**3, peak_bytes=6*1024**3,
            threads=2, deadline_s=600, elapsed_s=450.5, completed=16, total=16,
            oom_events=0, parity_passed=True)
    return common | dict(evidence_sha256s=[B], reviewer='reviewer assertion', reviewed_at=AT,
                         verdict='approve', findings=[])


def assemble(kind='human_sample', value=None, reg=None):
    ctx=q.context(reg or registration())
    return q.assemble(ctx, q.digest(ctx), kind, report(kind) if value is None else value)


def test_context_selects_immutable_fields_and_detaches_nested_values():
    reg=registration(); ctx=q.context(reg)
    assert set(ctx)==q.CONTEXT_FIELDS and 'status' not in ctx
    reg['metrics']['feature_contract']['schema']='changed'
    assert ctx['metrics']['feature_contract']['schema']=='fixture/1'
    changed=registration(); changed.update(status='validated', rejected_reason='mutable')
    assert q.context(changed)==ctx
    changed['lifecycle_event_id']=None; changed['gt']=None
    assert q.context(changed)['lifecycle_event_id'] is None


@pytest.mark.parametrize('kind', ['human_sample','runtime','independent_review'])
def test_closed_report_roundtrip_and_replay(kind):
    value=report(kind); before=deepcopy(value); packet=assemble(kind, value)
    wire=packet['canonical_body']
    assert json.loads(wire)==packet['body']
    assert packet['receipt_sha256']==hashlib.sha256(wire.encode('ascii')).hexdigest()
    assert packet['context_sha256']==q.digest(packet['context'])
    assert packet['body']['provenance']=='caller_assertion'
    assert assemble(kind, dict(reversed(list(value.items()))))==packet
    assert value==before
    value['observed_at']='2026-11-17T04:01:00+09:00'
    assert packet['body']['report']['observed_at']==AT
    assert assemble(kind,value)['receipt_sha256']!=packet['receipt_sha256']
    assert q.admission_status()==dict(admission='not_granted',publication_eligible=False,release_or_hold_changes=False)


def test_uncertain_is_retained_and_empty_labels_are_only_observations():
    packet=assemble(); assert packet['body']['report']['judgments'][0]['state']=='uncertain'
    value=report(); value['judgments']=[]
    assert assemble(value=value)['body']['report']['judgments']==[]
    assert q.admission_status()['admission']=='not_granted'


def test_cpu_observation_and_failed_partial_are_not_fleet_qualification():
    value=report('runtime'); value.update(completed=4,oom_events=1,parity_passed=False,peak_bytes=9*1024**3,elapsed_s=900)
    stored=assemble('runtime',value)['body']['report']
    assert stored['device']=='cpu' and stored['completed']==4 and stored['parity_passed'] is False
    assert 'fleet' not in stored and not q.admission_status()['publication_eligible']


@pytest.mark.parametrize('state', [None, False, True])
def test_runtime_parity_preserves_three_states_through_replay_and_roundtrip(state):
    value=report('runtime'); value['parity_passed']=state
    packet=assemble('runtime',value)
    restored=json.loads(packet['canonical_body'])['report']
    assert restored['parity_passed'] is state
    assert assemble('runtime',restored)==packet
    assert packet['receipt_sha256']==hashlib.sha256(packet['canonical_body'].encode('ascii')).hexdigest()
    value['parity_passed']='changed'
    assert packet['body']['report']['parity_passed'] is state
    # Frozen legacy digests from the original boolean-only contract.
    legacy={False:'d17b53c12f75bec0f5d68954fd64bfe8819ab412586240de9e6bebda9b2abe18',
            True:'beef1bbaf6cf054dfd9f266a62f2c889194cea4ee89f88bd8258b1365f71cd6c'}
    if state is not None:
        assert packet['receipt_sha256']==legacy[state]
    else:
        assert packet['receipt_sha256'] not in legacy.values()
    assert q.admission_status()['admission']=='not_granted'


@pytest.mark.parametrize('field,value', [('id',True),('tag_id',0),('lifecycle_event_id',-1),
    ('artifact_sha256','A'*64),('artifact_sha256','x'),('tag',''),('gt',[]),('metrics',None)])
def test_context_rejects_malformed_fields(field,value):
    reg=registration();reg[field]=value
    with pytest.raises(q.QualificationEvidenceRefused):q.context(reg)


@pytest.mark.parametrize('value',[True,False,0,1,-.1,float('nan'),float('inf'),'0.25',10**400])
def test_invalid_threshold(value):
    reg=registration();reg['metrics']['threshold']=value
    with pytest.raises(q.QualificationEvidenceRefused):q.context(reg)


def test_missing_context_and_missing_threshold():
    reg=registration();del reg['tag_id']
    with pytest.raises(q.QualificationEvidenceRefused):q.context(reg)
    reg=registration();del reg['metrics']['threshold']
    with pytest.raises(q.QualificationEvidenceRefused):q.context(reg)


@pytest.mark.parametrize('field,value', [('id',13),('artifact_sha256',B),('gt',{'positive':21}),
    ('metrics',{'threshold':.26}),('tag','another'),('lifecycle_event_id',8)])
def test_expected_context_refuses_substitution(field,value):
    ctx=q.context(registration()); changed=ctx | {field:value}
    with pytest.raises(q.QualificationEvidenceRefused,match='registration_context_mismatch'):
        q.assemble(changed,q.digest(ctx),'human_sample',report())


@pytest.mark.parametrize('field,value', [('artifact_sha256',B),('threshold',.26)])
@pytest.mark.parametrize('kind',['human_sample','runtime','independent_review'])
def test_report_refuses_cross_candidate_and_operating_point(kind,field,value):
    r=report(kind);r[field]=value
    with pytest.raises(q.QualificationEvidenceRefused,match='binding_mismatch'):assemble(kind,r)


@pytest.mark.parametrize('kind',['human_sample','runtime','independent_review'])
def test_unknown_fields_and_missing_fields_refused(kind):
    r=report(kind);r['publication_eligible']=True
    with pytest.raises(q.QualificationEvidenceRefused,match='shape'):assemble(kind,r)
    del r['publication_eligible'];del r['observed_at']
    with pytest.raises(q.QualificationEvidenceRefused,match='shape'):assemble(kind,r)
    r=report(kind);r['provenance']='canonical_human_snapshot'
    with pytest.raises(q.QualificationEvidenceRefused,match='shape'):assemble(kind,r)


@pytest.mark.parametrize('at',['2026-11-17T04:00:00','2026-11-17','garbage','2026-13-17T04:00:00Z'])
def test_attribution_requires_timezone_timestamp(at):
    r=report();r['judgments'][0]['reviewed_at']=at
    with pytest.raises(q.QualificationEvidenceRefused):assemble(value=r)


@pytest.mark.parametrize('field,value',[('state',None),('state','negative'),('reviewer',''),
    ('post_id',True),('source_ref','unverified://arbitrary')])
def test_judgment_rejects_implicit_or_malformed_attribution(field,value):
    r=report();r['judgments'][0][field]=value
    with pytest.raises(q.QualificationEvidenceRefused):assemble(value=r)


def test_duplicate_judgments_refused_even_if_conflicting():
    r=report();r['judgments'].append(r['judgments'][0] | {'state':'absent'})
    with pytest.raises(q.QualificationEvidenceRefused,match='duplicate_judgment'):assemble(value=r)


@pytest.mark.parametrize('field,value',[('threads',True),('threads',0),('memory_limit_bytes',0),
    ('peak_bytes',-1),('deadline_s',0),('elapsed_s',float('nan')),('completed',17),('total',0),
    ('oom_events',False),('parity_passed',1),('parity_passed',0),('parity_passed','not_measured'),
    ('model_inventory',[B]),('model_inventory',[A,A])])
def test_runtime_refuses_invalid_scopes_and_numbers(field,value):
    r=report('runtime');r[field]=value
    with pytest.raises(q.QualificationEvidenceRefused):assemble('runtime',r)


def test_independent_review_is_assertion_not_approval():
    r=report('independent_review');packet=assemble('independent_review',r)
    assert packet['body']['report']['verdict']=='approve'
    assert packet['body']['provenance']=='caller_assertion'
    r['evidence_sha256s']=[]
    with pytest.raises(q.QualificationEvidenceRefused):assemble('independent_review',r)
    r=report('independent_review');r['findings']=['x'*2001]
    with pytest.raises(q.QualificationEvidenceRefused):assemble('independent_review',r)


@pytest.mark.parametrize('value',[{'x':float('nan')},{1:'nonstring key'},{'x':set()},('tuple',)])
def test_canonical_refuses_non_json(value):
    with pytest.raises(q.QualificationEvidenceRefused):q.canonical(value)


def test_canonical_ascii_size_and_depth_bounds():
    assert q.canonical({'日本':'語'})=='{"\\u65e5\\u672c":"\\u8a9e"}'
    with pytest.raises(q.QualificationEvidenceRefused,match='document_too_large'):
        q.canonical({'x':'a'*10},max_bytes=10)
    reg=registration();reg['metrics']['extra']='x'*q.CONTEXT_MAX_BYTES
    with pytest.raises(q.QualificationEvidenceRefused,match='document_too_large'):q.context(reg)
    value={}
    for _ in range(34):value={'x':value}
    with pytest.raises(q.QualificationEvidenceRefused,match='json_too_deep'):q.canonical(value)


def test_assemble_unknown_kind_and_extra_context_fields():
    with pytest.raises(q.QualificationEvidenceRefused):assemble('other',{})
    ctx=q.context(registration())
    with pytest.raises(q.QualificationEvidenceRefused,match='context_shape_invalid'):
        q.assemble(ctx | {'status':'validated'},q.digest(ctx),'human_sample',report())


@pytest.mark.parametrize('value', ['\x00', '\ud800', {'\udfff': 'value'}, {'safe': '\x00'}])
def test_canonical_refuses_text_postgresql_cannot_store(value):
    with pytest.raises(q.QualificationEvidenceRefused, match='json_text_invalid'):
        q.canonical(value)



def test_runtime_parity_diagnostics_record_score_failure_with_no_crossings():
    r = report("runtime")
    r["parity_passed"] = False
    r["parity"] = {
        "passed": False, "max_absolute_error": .0063, "decision_differences": 0,
        "tolerance": .0001, "n": r["total"],
    }
    assert assemble("runtime", r)["body"]["report"]["parity"] == r["parity"]
    with pytest.raises(q.QualificationEvidenceRefused, match="parity_diagnostics_mismatch"):
        assemble("runtime", r | {"parity_passed": True})
