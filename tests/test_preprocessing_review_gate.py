import pytest
from tagger.auto_review import decide

@pytest.mark.parametrize('metrics',[
 {'requires_preprocessing_review':True,'disposition':'deployable'},
 {'feature_contract':{'identity':{}},'disposition':'deployable'},
 {'requires_preprocessing_review':True,'disposition':'hold'},
])
def test_new_contract_is_held_for_owner_review(metrics):
    decision=decide(metrics,{}, {})
    assert not decision.ok and decision.bucket=='owner_decision'


def test_runner_preserves_contract_in_registration(monkeypatch):
    from tagger import vnext_runner as runner
    captured=[]
    monkeypatch.setattr(runner,'REVIEW_ONLY',True)
    monkeypatch.setattr(runner,'_register',lambda body:captured.append(body))
    def forbidden(*a,**kw):raise AssertionError('review-only must not upload')
    monkeypatch.setattr(runner,'_upload_artifact',forbidden)
    contract={'identity':{'schema':'kura-feature-contract/1'}}
    runner.submit_registration('test',{'classification':{'verdict':'deployable'},
        'gt':{'positive':20,'negative':50},'feature_contract':contract,
        'requires_preprocessing_review':True},'/local/artifact','a'*64,7840)
    metrics=captured[0]['metrics']
    assert metrics['feature_contract']==contract and metrics['requires_preprocessing_review']
    assert not decide(metrics,{},{}).ok
