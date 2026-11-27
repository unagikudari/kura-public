"""Fail closed when character metadata and loaded release do not agree."""
import asyncio
import copy
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
import pytest
from tagger import character_policy as policy, cl2_chars


def meta():
    return dict(id=14,character_component=dict(release_digest='a'*64,
        description=cl2_chars.release_description(threshold=.45,evidence='fixture qualification, not production')))


def test_legacy_and_cl_metadata_have_distinct_bindings():
    assert policy.decode({'id':14}) == dict(release_id=14, threshold=None, release_digest=None)
    assert policy.decode(meta())==dict(release_id=14,release_digest='a'*64,threshold=.45)


@pytest.mark.parametrize('mutate', [
    lambda m:m.update(id=True),
    lambda m:m['character_component'].update(release_digest='bad'),
    lambda m:m['character_component']['description']['components'].pop(),
    lambda m:m['character_component']['description']['components'][0].update(ordinal=False),
    lambda m:m['character_component']['description']['components'][1].update(content_sha256='b'*64),
    lambda m:m['character_component']['description']['components'][0]['params'].update(threshold=.35),
    lambda m:m['character_component']['description']['threshold_row'].update(category=True),
    lambda m:m['character_component']['description']['threshold_row'].update(evidence=''),
])
def test_malformed_policy_never_becomes_legacy(mutate):
    m=meta();mutate(m)
    with pytest.raises(policy.RequiredCharacterComponentError):policy.decode(m)


def test_release_race_or_cached_fallback_refuses():
    binding=policy.decode(meta())
    policy.require_release(binding,14)
    for other in (13,None,True,'14'):
        with pytest.raises(policy.RequiredCharacterComponentError):policy.require_release(binding,other)


def test_network_failure_does_not_select_legacy(monkeypatch):
    import requests
    monkeypatch.setattr(requests,'get',Mock(side_effect=RuntimeError('sentinel private text')))
    with pytest.raises(policy.RequiredCharacterComponentError,match='^character_release_unavailable$'):
        policy.fetch('https://invalid.example',{})


def test_server_reads_component_and_threshold_rows_without_serving_model_bytes():
    from api.routers.tagger import _release_character_component
    expected=meta()['character_component'];desc=expected['description']
    rows=[dict(release_digest='a'*64,**c) for c in desc['components']]
    result1=SimpleNamespace(mappings=lambda:SimpleNamespace(all=lambda:rows))
    result2=SimpleNamespace(mappings=lambda:SimpleNamespace(one_or_none=lambda:desc['threshold_row']))
    db=SimpleNamespace(execute=AsyncMock(return_value=result1))
    assert asyncio.run(_release_character_component(db,14))==expected
    assert db.execute.call_args_list[0].args[1]=={'id':14}
    assert 'm.release_id=:id' in str(db.execute.call_args_list[0].args[0])


def test_server_uses_only_release_manifest_not_global_threshold_or_checkout(monkeypatch):
    from api.routers.tagger import _release_character_component
    desc=meta()['character_component']['description']
    rows=[dict(release_digest='a'*64,**c) for c in desc['components']]
    monkeypatch.setattr(cl2_chars, 'release_description', Mock(side_effect=AssertionError('API must not decode')))
    db=SimpleNamespace(execute=AsyncMock(return_value=SimpleNamespace(mappings=lambda:SimpleNamespace(all=lambda:rows))))
    result=asyncio.run(_release_character_component(db,14))
    assert result['description']==desc
    assert db.execute.await_count==1
    assert 'tagger_producer_threshold' not in str(db.execute.call_args.args[0])


def test_corrupt_character_manifest_does_not_break_metadata_endpoint():
    from api.routers.tagger import _release_character_component
    rows=[dict(release_digest=d,**meta()['character_component']['description']['components'][0]) for d in ['a'*64,'b'*64]]
    db=SimpleNamespace(execute=AsyncMock(return_value=SimpleNamespace(mappings=lambda:SimpleNamespace(all=lambda:rows))))
    result=asyncio.run(_release_character_component(db,14))
    assert result=={'error':'character_manifest_ambiguous'}
    with pytest.raises(policy.RequiredCharacterComponentError):policy.decode(dict(id=14,character_component=result))


def test_legacy_to_cl_transition_is_also_bound():
    binding=policy.decode({'id':14})
    policy.require_release(binding,14)
    with pytest.raises(policy.CharacterReleaseChanged):policy.require_release(binding,15)


def test_deriver_contract_is_independent_of_source_file_digest(monkeypatch):
    old=meta()
    monkeypatch.setattr(cl2_chars,'digest',lambda p:'f'*64)
    assert policy.decode(old)['threshold']==.45


def test_empty_legacy_manifest_does_not_fetch_cl_threshold():
    from api.routers.tagger import _release_character_component
    db=SimpleNamespace(execute=AsyncMock(return_value=SimpleNamespace(mappings=lambda:SimpleNamespace(all=lambda:[]))))
    assert asyncio.run(_release_character_component(db,14)) is None
    assert db.execute.await_count==1
