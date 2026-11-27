from types import SimpleNamespace
from unittest.mock import Mock
import pytest
from tagger import nodec_autotag_worker as worker, execution_receipt as receipt, character_policy
from tests.test_execution_receipt import body


def test_wd14_claim_and_completion_share_snapshot_without_actor_change(monkeypatch):
    b = body()
    facts = {k:v for k,v in b.items() if k not in ('schema','host','profile','release_id','component_source','qualification_ref')}
    monkeypatch.setattr(worker, 'AUTOTAG_HOST', 'nodeb')
    monkeypatch.setattr(worker, 'PIXAI_CHARS', {'source':b['component_source'], 'execution':facts})
    monkeypatch.setattr(worker, '_CL_EXECUTION_RECEIPT', None)
    monkeypatch.setattr(worker, '_CL_WORKER_ID', None)
    monkeypatch.setattr(worker, '_provider_label', lambda:'ROCMExecutionProvider')
    monkeypatch.setattr(worker, 'ensemble_source_id', lambda *a:'unchanged-actor')
    post = Mock(return_value=SimpleNamespace(status_code=200,raise_for_status=lambda:None,json=lambda:{'claimed':[]}))
    monkeypatch.setattr(worker.requests,'post',post)
    state=SimpleNamespace(release_id=14,model_sha256='fixture')
    worker.claim({},1,600,state)
    claim=post.call_args.kwargs
    packet=claim['json']['packet']
    assert packet == receipt.make(b)
    assert claim['params']['provider']=='ROCMExecutionProvider'
    heads = [{"tag": "fixture", "artifact_sha256": "a" * 64,
              "threshold": .5, "apply_mode": "replace"}]
    worker.complete({}, 123, "ok", tags=[], release_id=14, executed_content_heads=heads)
    complete=post.call_args.kwargs['json']
    assert complete['worker_id']==claim['params']['worker_id']
    assert complete['execution_receipt_sha256']==packet['receipt_sha256']
    assert complete['source']=='unchanged-actor'
    assert complete['executed_content_heads'] == heads
    facts['ort_version']='1.23.0'
    post.reset_mock()
    with pytest.raises(character_policy.CharacterReleaseChanged):
        worker.claim({},1,600,state)
    post.assert_not_called()


def test_full_worker_sends_matching_receipt_and_keeps_source(monkeypatch):
    from tagger import loop
    b=body();b['profile']='full'
    packet=receipt.make(b)
    monkeypatch.setattr(loop,'EXECUTION_RECEIPT',packet)
    monkeypatch.setattr(loop,'TAGGER_SOURCE','unchanged-full-actor')
    monkeypatch.setattr(loop,'TAGGER_RELEASE_ID',14)
    request=Mock(return_value={'claimed':[]})
    monkeypatch.setattr(loop,'_req',request)
    loop.claim('auto_tag',1)
    assert request.call_args.args[2]=={'packet':packet}
    loop.complete('auto_tag',123,'done',tags=[])
    payload=request.call_args.args[2]
    assert payload['execution_receipt_sha256']==packet['receipt_sha256']
    assert payload['worker_id']==loop.WORKER_ID
    assert payload['source']=='unchanged-full-actor'


def setup_receipt_worker(monkeypatch):
    b=body()
    facts={k:v for k,v in b.items() if k not in ('schema','host','profile','release_id','component_source','qualification_ref')}
    monkeypatch.setattr(worker,'AUTOTAG_HOST','nodeb')
    monkeypatch.setattr(worker,'PIXAI_CHARS',{'source':b['component_source'],'execution':facts})
    monkeypatch.setattr(worker,'_CL_EXECUTION_RECEIPT',None)
    monkeypatch.setattr(worker,'_CL_WORKER_ID',None)
    return b


def test_restart_reuses_worker_heartbeat_key(monkeypatch):
    setup_receipt_worker(monkeypatch)
    worker._execution_receipt(14)
    first=worker._CL_WORKER_ID
    monkeypatch.setattr(worker,'_CL_EXECUTION_RECEIPT',None)
    worker._execution_receipt(14)
    assert first==worker._CL_WORKER_ID=='nodeb-autotag'


def test_failed_completion_uses_bound_snapshot_without_release_argument(monkeypatch):
    setup_receipt_worker(monkeypatch)
    packet=worker._execution_receipt(14)
    monkeypatch.setattr(worker,'ensemble_source_id',lambda release,heads: 'actor-'+str(release))
    post=Mock(return_value=SimpleNamespace(status_code=200,raise_for_status=lambda:None,json=lambda:{}))
    monkeypatch.setattr(worker.requests,'post',post)
    worker.complete({},123,'failed',error='fixture failure')
    sent=post.call_args.kwargs['json']
    assert sent['release_id']==14 and sent['source']=='actor-14'
    assert sent['execution_receipt_sha256']==packet['receipt_sha256']


def test_full_worker_detects_head_policy_change_without_new_release(monkeypatch):
    from tagger import loop

    initial = {"tag": "fixture", "content_sha256": "a" * 64, "threshold": .5,
               "apply_mode": "replace", "gate_tags": []}
    fetch = Mock(return_value=[initial])
    monkeypatch.setattr(worker, "load_head_policies", fetch)
    loaded = {"__report_policies__": [initial]}
    options = {"api_url": "https://fixture.invalid", "headers": {}}
    assert loop._head_configuration_matches(loaded, **options)
    fetch.assert_called_once_with(**options, require_complete=True)
    fetch.return_value = [initial | {"threshold": .6}]
    assert not loop._head_configuration_matches(loaded, **options)
