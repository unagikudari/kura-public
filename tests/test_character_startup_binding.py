"""Startup wiring with fake network/model loaders, without claiming live work."""
from types import SimpleNamespace
from unittest.mock import Mock
import pytest
from tagger import character_policy, cl2_chars, nodec_autotag_worker as worker


def test_wd14_startup_selects_published_character_before_claim(monkeypatch):
    policy=dict(release_id=14,release_digest='a'*64,threshold=.45)
    monkeypatch.setattr(worker,'API','https://invalid.example')
    monkeypatch.setattr(worker.sys,'argv',['worker','--trial','1'])
    monkeypatch.setattr(worker,'PIXAI_CHARS',None)
    monkeypatch.setattr(worker,'_CHARACTER_THRESHOLD',None)
    monkeypatch.setattr(worker,'_CHARACTER_RELEASE_POLICY',None)
    monkeypatch.setattr(worker,'auth_header',lambda:({},None))
    monkeypatch.setattr(worker,'_resolve_device',lambda:None)
    monkeypatch.setattr(worker,'refresh_content_heads',lambda:None)
    monkeypatch.setattr(worker,'load_skin_model',lambda:(None,None))
    monkeypatch.setattr(worker,'refresh_model',lambda state:setattr(state,'release_id',14))
    monkeypatch.setattr(worker,'recompute_reap_gates',lambda state:None)
    monkeypatch.setattr(character_policy,'fetch',lambda *args:policy)
    loader=Mock(return_value={'source':'cl_tagger_v2=fixture','chars':{0:'alice'}})
    monkeypatch.setattr(cl2_chars,'load',loader)
    def claim(*args):
        assert worker.PIXAI_CHARS['source']=='cl_tagger_v2=fixture'
        assert worker._CHARACTER_RELEASE_POLICY==policy
        return []
    monkeypatch.setattr(worker,'claim',claim)
    assert worker.main()==0
    loader.assert_called_once_with(threshold=.45)


def test_wd14_cached_model_mismatch_cannot_claim(monkeypatch):
    monkeypatch.setattr(worker,'_CHARACTER_RELEASE_POLICY',dict(release_id=14))
    claim=Mock(side_effect=AssertionError('must not claim'))
    monkeypatch.setattr(worker,'claim',claim)
    with pytest.raises(character_policy.RequiredCharacterComponentError):
        worker.process_batch({},SimpleNamespace(release_id=13),1,600)
    claim.assert_not_called()


@pytest.mark.parametrize('loaded_id',[14,13])
def test_full_startup_binds_the_actual_loaded_release(monkeypatch,loaded_id):
    loop=pytest.importorskip('tagger.loop')
    monkeypatch.setattr(loop,'KURA_URL','https://invalid.example')
    monkeypatch.setattr(loop,'KURA_USER','fixture')
    monkeypatch.setattr(loop,'KURA_TOKEN','fixture')
    # Initial AC check passes; exit at first loop hook without a claim.
    monkeypatch.setattr(loop,'_on_ac_power',Mock(side_effect=[True,False]))
    monkeypatch.setattr(character_policy,'fetch',lambda *a:dict(release_id=14,threshold=.45))
    loader=Mock(return_value={'wd14_fixed':{'release_id':loaded_id},
        'wd14_sess':SimpleNamespace(get_providers=lambda:['CPUExecutionProvider'])})
    monkeypatch.setattr(loop,'load_all_models',loader)
    monkeypatch.setattr(loop,'model_source_id',lambda *a:'fixture')
    monkeypatch.setattr(loop,'reap_domain',lambda *a:[])
    claim=Mock(side_effect=AssertionError('must not claim'))
    monkeypatch.setattr(loop,'claim',claim)
    if loaded_id==14:assert loop.main()==0
    else:
        with pytest.raises(character_policy.RequiredCharacterComponentError):loop.main()
    assert loader.call_args.kwargs['character_threshold']==.45
    claim.assert_not_called()


@pytest.mark.parametrize('threshold', [None,.45])
def test_resident_release_change_exits_instead_of_reloading_forever(monkeypatch,threshold):
    from tests.test_character_component_completion import setup_main
    setup_main(monkeypatch,['worker'])
    monkeypatch.setattr(character_policy,'fetch',lambda *a:dict(release_id=14,threshold=threshold))
    monkeypatch.setattr(worker,'refresh_pixai_chars',lambda **kw:None)
    ids=iter([14,15])
    monkeypatch.setattr(worker,'refresh_model',lambda state:setattr(state,'release_id',next(ids)))
    monkeypatch.setattr(worker,'recompute_reap_gates',lambda *a:None)
    monkeypatch.setattr(worker.os.path,'exists',lambda p:False)
    monkeypatch.setattr(worker.time,'sleep',lambda *a:pytest.fail('must exit, not sleep/reload'))
    assert worker.main()==75


def test_both_worker_profiles_advertise_cl_generation():
    from tagger import loop
    assert worker.WORKER_GENERATION==loop.WORKER_GENERATION==6
