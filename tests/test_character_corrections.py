"""Character corrections own the final output in both worker profiles."""
import io
import sys
from types import SimpleNamespace
from unittest.mock import Mock

import numpy as np
import pytest
from PIL import Image

pytest.importorskip("torch")  # the tagger's inference runtime is an optional install
from tagger import nodec_autotag_worker as worker, pixai_chars


@pytest.fixture
def full():
    # Load inference dependencies only for selected tests, not during collection
    # of unrelated marker-only suites using a different runtime.
    # the tagger's inference runtime (torch, av) is an optional install
    return pytest.importorskip("tagger.infer")


def image_bytes():
    out = io.BytesIO()
    Image.new('RGB', (50, 50)).save(out, format='JPEG')
    return out.getvalue()


def models(monkeypatch, full, chars=(('alice', .95),), patches=None, heads=None):
    monkeypatch.setattr(full, '_run_pixai', lambda *a: {'general': [('1girl', .9)], 'character': list(chars)})
    monkeypatch.setattr(full, '_run_mldanbooru', lambda *a: [])
    monkeypatch.setattr(full, '_run_wd14_fixed', lambda *a, **kw: (patches or {}, None))
    return dict(pixai_sess=None, pixai_tags=[{'name': t, 'category': 4} for t in ('alice', 'bob')],
                wd14_sess=None, wd14_tags=[], ml_sess=None, ml_classes=[],
                wd14_fixed={'tags': []} if patches is not None else None, content_heads=heads)


def test_full_negative_character_patch_is_not_resurrected(monkeypatch, full):
    bundle = models(monkeypatch, full, patches={'alice': .1, 'bob': .9})
    tags = full.tag_image(image_bytes(), 'image/jpeg', bundle)
    assert tags == [{'name': 'bob', 'category': 'character'}, {'name': '1girl', 'category': 'general'}]
    assert full.tag_image(image_bytes(), 'image/jpeg', bundle, return_scores=True) == {'1girl': .9, 'bob': .9}


def heads(**probabilities):
    result = {}
    for tag, prob in probabilities.items():
        result[tag] = SimpleNamespace(prob=prob)
        result[f'__policy__{tag}'] = dict(category='character', threshold=.6, apply_mode='replace', gate_tags=[])
        result[f'__sha__{tag}'] = 'a' * 12
    return result


def test_full_character_heads_remove_and_add_unknown_vocab(monkeypatch, full):
    monkeypatch.setattr(worker, '_content_head_prob', lambda img, model: model.prob)
    bundle = models(monkeypatch, full, heads=heads(alice=.1, new_character=.9))
    assert full.tag_image(image_bytes(), 'image/jpeg', bundle) == [
        {'name': 'new_character', 'category': 'character'}, {'name': '1girl', 'category': 'general'}]


def test_worker_corrected_character_rows_share_full_threshold(monkeypatch):
    import torch
    # WD14 nodes need not have full-pipeline dependencies such as PyAV.
    monkeypatch.setitem(sys.modules, "tagger.infer", None)
    monkeypatch.setattr(worker, 'DEVICE', 'cpu')
    model = lambda x: torch.logit(torch.tensor([[.9, .1, .7, .99, .8]]))
    tags, rating, scores = worker.infer(model, ['1girl', 'alice', 'bob', 'stock_only', 'safe'],
        [0, 4, 4, 4, 9], 8, Image.new('RGB', (8, 8)), characters=['alice'], patched_characters=['alice', 'bob'])
    assert tags == ['1girl', 'bob']
    assert rating == 'safe'


@pytest.mark.parametrize('prob', [float('nan'), float('inf'), -.1, 1.1])
def test_invalid_character_head_cannot_retract_a_tag(monkeypatch, prob):
    monkeypatch.setattr(worker, '_content_head_prob', lambda img, model: model.prob)
    with pytest.raises(pixai_chars.RequiredCharacterComponentError):
        worker.apply_content_heads(['alice'], None, heads(alice=prob))


@pytest.mark.parametrize('threshold', [None, float('nan'), float('inf'), True, -.1, 1.1])
def test_invalid_character_policy_refuses_claim(threshold):
    bundle = heads(alice=.1)
    bundle['__policy__alice']['threshold'] = threshold
    with pytest.raises(pixai_chars.RequiredCharacterComponentError):
        pixai_chars.require_character_heads(bundle)


def test_source_changes_with_character_operating_point():
    bundle = heads(alice=.1)
    old = pixai_chars.character_head_source(bundle)
    bundle['__policy__alice']['threshold'] = .7
    assert old != pixai_chars.character_head_source(bundle)
    assert 'characters=corrected-v1' in worker.ensemble_source_id(14, bundle)


def test_full_loader_uses_caller_endpoint_and_artifact_fetch(monkeypatch, full):
    policy = dict(tag='alice', category='character')
    policies = Mock(return_value=[policy, dict(tag='general', category='general')])
    artifact = Mock(return_value='artifact-file')
    monkeypatch.setattr(worker, 'load_head_policies', policies)
    monkeypatch.setattr(worker, '_fetch_artifact', artifact)
    def load(selected, *, artifact_fetcher):
        assert selected == [policy, dict(tag='general', category='general')]
        assert artifact_fetcher('digest') == 'artifact-file'
        h = heads(alice=.9, general=.1)
        h['__policy__general']['category'] = 'general'
        return h
    monkeypatch.setattr(worker, 'load_content_heads', load)
    full.load_policy_heads(api_url='https://configured.example', headers={'Authorization': 'test'})
    policies.assert_called_once_with(api_url='https://configured.example', headers={'Authorization': 'test'}, require_complete=True)
    artifact.assert_called_once_with('digest', api_url='https://configured.example', headers={'Authorization': 'test'})


def test_truncated_image_is_failed_image_not_model_outage(monkeypatch):
    raw = image_bytes()[:-20]
    Image.open(io.BytesIO(raw))  # Header is valid, pixels are not.
    response = SimpleNamespace(content=raw, raise_for_status=lambda: None)
    monkeypatch.setattr(worker.requests, 'get', lambda *a, **kw: response)
    complete = Mock(return_value={})
    monkeypatch.setattr(worker, 'complete', complete)
    assert not worker.process_item({}, None, [], [], 448, {'id': 1})[0]
    assert complete.call_args.args[2] == 'failed'


def test_fallback_invalid_output_is_required_failure(monkeypatch, full):
    monkeypatch.setattr(full, '_preprocess_wd14', lambda *a: None)
    session = SimpleNamespace(get_inputs=lambda: [SimpleNamespace(name='input')],
        get_outputs=lambda: [SimpleNamespace(name='output')], run=lambda *a: [np.array([[np.nan]])])
    with pytest.raises(pixai_chars.RequiredCharacterComponentError):
        full._run_wd14_chars(b'', session, [{'name': 'alice', 'category': 4}])


@pytest.mark.parametrize('prob', [float('nan'), float('inf')])
def test_invalid_patch_probability_in_either_profile(monkeypatch, prob, full):
    import torch
    bundle = models(monkeypatch, full, patches={'alice': prob})
    with pytest.raises(pixai_chars.RequiredCharacterComponentError):
        full.tag_image(image_bytes(), 'image/jpeg', bundle)
    monkeypatch.setattr(worker, 'DEVICE', 'cpu')
    model = lambda x: torch.tensor([[float('nan')]])
    with pytest.raises(pixai_chars.RequiredCharacterComponentError):
        worker.infer(model, ['alice'], [4], 8, Image.new('RGB', (8, 8)),
                     characters=['alice'], patched_characters=['alice'])


def test_required_correction_backbone_error_is_not_an_image_failure(monkeypatch, full):
    def broken(*a, **kw):
        raise RuntimeError('backend unavailable')
    bundle = models(monkeypatch, full, patches={})
    bundle['wd14_fixed']['patched'] = {'alice': 0}
    monkeypatch.setattr(full, '_run_wd14_fixed', broken)
    with pytest.raises(pixai_chars.RequiredCharacterComponentError):
        full.tag_image(image_bytes(), 'image/jpeg', bundle)
    monkeypatch.setattr(worker, 'DEVICE', 'cpu')
    with pytest.raises(pixai_chars.RequiredCharacterComponentError):
        worker.infer(broken, ['alice'], [4], 8, Image.new('RGB', (8, 8)),
                     characters=['alice'], patched_characters=['alice'])


def test_policy_transport_error_never_becomes_empty_ensemble(monkeypatch):
    monkeypatch.setattr(worker.requests, 'get', Mock(side_effect=RuntimeError('offline')))
    with pytest.raises(pixai_chars.RequiredCharacterComponentError):
        worker.load_head_policies(api_url='https://test.example', headers={})


@pytest.mark.parametrize('output', [[], [.9], [.9, .9, .9], [[.9, .9]]])
def test_malformed_correction_vector_defers_without_charging_image(monkeypatch, output):
    import torch
    monkeypatch.setattr(worker, 'DEVICE', 'cpu')
    monkeypatch.setattr(worker, 'fetch_image', lambda *a: Image.new('RGB', (8, 8)))
    monkeypatch.setattr(worker, 'PIXAI_CHARS', {'chars': {0: 'alice'}})
    monkeypatch.setattr(worker, 'apply_pixai_chars', lambda *a: ['alice'])
    completion = Mock()
    monkeypatch.setattr(worker, 'complete', completion)
    model = lambda x: torch.tensor([output])
    with pytest.raises(pixai_chars.RequiredCharacterComponentError, match='shape'):
        worker.process_item({}, model, ['1girl', 'alice'], [0, 4], 8, {'id': 1}, patched_tags=['alice'])
    completion.assert_not_called()


def test_declared_character_head_runs_after_veto(monkeypatch, full):
    from tagger import veto_apply
    bundle = models(monkeypatch, full, patches={}, heads=heads(alice=.9))
    bundle['veto_heads'] = {'alice': {'threshold': .5}}
    monkeypatch.setattr(full, '_run_wd14_fixed', lambda *a, **kw: ({}, object()))
    veto = Mock(return_value=.1)
    monkeypatch.setattr(veto_apply, 'run_veto', veto)
    monkeypatch.setattr(worker, '_content_head_prob', lambda img, model: model.prob)
    assert {'name': 'alice', 'category': 'character'} in full.tag_image(image_bytes(), 'image/jpeg', bundle)
    veto.assert_called_once()


def test_bfloat16_correction_output_can_complete(monkeypatch):
    import torch
    monkeypatch.setattr(worker, 'DEVICE', 'cpu')
    model = lambda x: torch.tensor([[2., -2., 2.]], dtype=torch.bfloat16)
    tags, _, _ = worker.infer(model, ['1girl', 'alice', 'bob'], [0, 4, 4], 8,
        Image.new('RGB', (8, 8)), characters=['alice'], patched_characters=['alice', 'bob'])
    assert tags == ['1girl', 'bob']


@pytest.mark.parametrize("boundary", ["input_device", "output_cpu"])
def test_device_transfer_failure_aborts_batch_without_completing_images(monkeypatch, boundary):
    monkeypatch.setattr(worker, "_CHARACTER_RELEASE_POLICY", dict(release_id=14, threshold=None))
    import torch
    monkeypatch.setattr(worker, 'DEVICE', 'cpu')
    monkeypatch.setattr(worker, 'fetch_image', lambda *a: Image.new('RGB', (8, 8)))
    monkeypatch.setattr(worker, 'PIXAI_CHARS', {'chars': {0: 'alice'}})
    monkeypatch.setattr(worker, 'apply_pixai_chars', lambda *a: ['alice'])
    monkeypatch.setattr(worker, 'CONTENT_HEADS', {})
    completion = Mock()
    monkeypatch.setattr(worker, 'complete', completion)
    monkeypatch.setattr(worker, 'claim', lambda *a: [{'id': 1}, {'id': 2}])
    # Forward succeeds; only the output device-transfer boundary fails.
    model = Mock(return_value=torch.tensor([[2., -2.]]))
    if boundary == 'output_cpu':
        monkeypatch.setattr(torch.Tensor, 'cpu', Mock(side_effect=RuntimeError('device transfer failed')))
    else:
        original_to = torch.Tensor.to
        def transfer(tensor, *args, **kwargs):
            if args and args[0] == 'cpu':
                raise RuntimeError('input device transfer failed')
            return original_to(tensor, *args, **kwargs)
        monkeypatch.setattr(torch.Tensor, 'to', transfer)
    state = SimpleNamespace(model=model, names=['1girl', 'alice'], cats=[0, 4], img_size=8,
        release_id=14, veto_heads={}, reap_gates=[], release_reap={'alice'})
    with pytest.raises(pixai_chars.RequiredCharacterComponentError):
        worker.process_batch({}, state, 2, 600)
    assert model.call_count == (1 if boundary == "output_cpu" else 0)
    completion.assert_not_called()


def test_full_cl_empty_does_not_fallback_and_general_survives(monkeypatch, full):
    from tagger import cl2_chars
    bundle = models(monkeypatch, full)
    bundle['cl2_characters'] = {'source': 'cl_tagger_v2=test'}
    monkeypatch.setattr(cl2_chars, 'char_tags', lambda *a: [])
    fallback = Mock(side_effect=AssertionError('legacy fallback forbidden'))
    monkeypatch.setattr(full, '_run_wd14_chars', fallback)
    assert full.tag_image(image_bytes(), 'image/jpeg', bundle) == [{'name':'1girl','category':'general'}]
    fallback.assert_not_called()


def test_full_cl_result_is_subject_to_existing_negative_patch(monkeypatch, full):
    from tagger import cl2_chars
    bundle = models(monkeypatch, full, patches={'alice': .1, 'bob': .9})
    bundle['cl2_characters'] = {'source':'cl_tagger_v2=test'}
    monkeypatch.setattr(cl2_chars, 'char_tags', lambda *a: [('alice', .99)])
    assert full.tag_image(image_bytes(), 'image/jpeg', bundle) == [
        {'name':'bob','category':'character'}, {'name':'1girl','category':'general'}]


def test_full_cl_failure_does_not_use_legacy_result(monkeypatch, full):
    from tagger import cl2_chars
    bundle = models(monkeypatch, full)
    bundle['cl2_characters'] = {'source':'cl_tagger_v2=test'}
    monkeypatch.setattr(cl2_chars, 'char_tags', Mock(side_effect=pixai_chars.RequiredCharacterComponentError('failed')))
    with pytest.raises(pixai_chars.RequiredCharacterComponentError):
        full.tag_image(image_bytes(), 'image/jpeg', bundle)


def test_wd14_reload_keeps_cl_choice_and_truthful_source(monkeypatch):
    from tagger import cl2_chars
    monkeypatch.setattr(worker, 'PIXAI_CHARS', None)
    monkeypatch.setattr(worker, '_CHARACTER_THRESHOLD', None)
    loader = Mock(return_value={'chars':{0:'alice'}, 'source':'cl_tagger_v2=abc'})
    monkeypatch.setattr(cl2_chars, 'load', loader)
    monkeypatch.setattr(pixai_chars, 'load', Mock(side_effect=AssertionError('legacy reload forbidden')))
    worker.refresh_pixai_chars(character_threshold=.55)
    assert 'cl_tagger_v2=abc' in worker.ensemble_source_id(14, {})
    monkeypatch.setattr(cl2_chars, 'char_tags', Mock(side_effect=RuntimeError('offline')))
    with pytest.raises(pixai_chars.RequiredCharacterComponentError): worker.apply_pixai_chars(None)
    worker.refresh_pixai_chars()
    assert loader.call_count == 2
    assert loader.call_args.kwargs == {'threshold': .55}


def test_full_general_policy_corrects_base_without_becoming_character(monkeypatch, full):
    monkeypatch.setattr(worker, '_content_head_prob', lambda img, model: model.prob)
    h = heads(incorrect_general=.1, new_general=.9, new_character=.9)
    for name in ('incorrect_general', 'new_general'):
        h[f'__policy__{name}']['category'] = 'general'
    bundle = models(monkeypatch, full, chars=(), heads=h)
    monkeypatch.setattr(full, '_run_pixai', lambda *a: {'general': [('incorrect_general', .99)], 'character': [('alice', .9)]})
    result = {row['name']: row['category'] for row in full.tag_image(image_bytes(), 'image/jpeg', bundle)}
    assert 'incorrect_general' not in result
    assert result['new_general'] == 'general'
    assert result['new_character'] == 'character'


def test_full_policy_loader_includes_general_heads(monkeypatch, full):
    policies = [dict(tag='alice', category='character'), dict(tag='general', category='general')]
    monkeypatch.setattr(worker, 'load_head_policies', lambda **kw: policies)
    selected = []
    def load(rows, **kwargs):
        selected.extend(rows)
        loaded = heads(alice=.9, general=.1)
        loaded['__policy__general']['category'] = 'general'
        return loaded
    monkeypatch.setattr(worker, 'load_content_heads', load)
    full.load_policy_heads(api_url='https://configured.example', headers={})
    assert selected == policies


def test_general_policy_is_in_source_identity():
    from tagger.content_head_policy import source
    h = heads(general=.1)
    h['__policy__general']['category'] = 'general'
    before = source(h)
    h['__policy__general']['threshold'] = .8
    assert source(h) != before
    before = source(h)
    h['__sha__general'] = 'b' * 12
    assert source(h) != before


@pytest.mark.parametrize('failure', ['missing', 'nan', 'exception'])
def test_full_failed_general_head_never_completes_as_negative(monkeypatch, full, failure):
    from tagger.content_head_policy import RequiredContentHeadError
    h = heads(general=.1)
    h['__policy__general']['category'] = 'general'
    if failure == 'missing':
        h['general'] = None
    def predict(*args):
        if failure == 'exception':
            raise RuntimeError('model unavailable')
        return float('nan')
    monkeypatch.setattr(worker, '_content_head_prob', predict)
    bundle = models(monkeypatch, full, heads=h)
    with pytest.raises(RequiredContentHeadError):
        full.tag_image(image_bytes(), 'image/jpeg', bundle)


def test_only_evaluated_replace_heads_gain_reap_authority(monkeypatch, full):
    h = heads(evaluated=.1, gated=.1, additive=.1)
    for tag in ('evaluated', 'gated', 'additive'):
        h[f'__policy__{tag}']['category'] = 'general'
    h['__policy__gated']['gate_tags'] = ['not_in_image']
    h['__policy__additive']['apply_mode'] = 'add_only'
    monkeypatch.setattr(worker, '_content_head_prob', lambda img, model: model.prob)
    notes = {}
    full.tag_image(image_bytes(), 'image/jpeg', models(monkeypatch, full, heads=h), notes=notes)
    assert notes['content_head_reap'] == ['evaluated']


def test_full_completion_uses_this_images_evaluated_heads(monkeypatch):
    from tagger import loop
    monkeypatch.setattr(loop, '_fetch_bytes', lambda url: b'image')
    def tag(data, mime, models, *, notes):
        notes['content_head_reap'] = ['evaluated']
        return [{'name': 'base', 'category': 'general'}]
    monkeypatch.setattr(loop, 'tag_image', tag)
    monkeypatch.setattr(loop, 'VETO_GATES', ['release_patch'])
    completed = Mock()
    monkeypatch.setattr(loop, 'complete', completed)
    assert loop.process_one({'id': 1, 'content_url': 'https://example.invalid'}, {})[0]
    assert completed.call_args.kwargs['veto_gates'] == ['evaluated', 'release_patch']


def test_failed_full_head_never_sends_successful_completion(monkeypatch):
    from tagger import loop
    from tagger.content_head_policy import RequiredContentHeadError
    monkeypatch.setattr(loop, '_fetch_bytes', lambda url: b'image')
    def fail(*args, **kwargs):
        raise RequiredContentHeadError('content_head_inference_failed:general')
    monkeypatch.setattr(loop, 'tag_image', fail)
    completed = Mock()
    monkeypatch.setattr(loop, 'complete', completed)
    with pytest.raises(RequiredContentHeadError):
        loop.process_one({'id': 1, 'content_url': 'https://example.invalid'}, {})
    completed.assert_not_called()


def test_full_head_report_uses_explicit_loaded_set_and_endpoint(monkeypatch):
    h = heads(general=.9)
    posted = Mock(return_value=Mock())
    monkeypatch.setattr(worker.requests, 'post', posted)
    monkeypatch.setattr(worker, 'CONTENT_HEADS', {})
    monkeypatch.setattr(worker, 'auth_header', lambda: pytest.fail('must use caller auth'))
    worker.report_node_heads([dict(tag='general', content_sha256='a'*64)], heads=h,
        api_url='https://configured.example', headers={'Authorization':'sentinel'}, host='qualified-node')
    assert posted.call_args.args[0] == 'https://configured.example/api/tagger/node-heads'
    assert posted.call_args.kwargs['json']['host'] == 'qualified-node'
    assert posted.call_args.kwargs['json']['heads'][0]['loaded'] is True


def test_full_refuses_an_old_api_that_ignores_strict_inventory(monkeypatch):
    response = Mock(headers={}, json=lambda: [])
    monkeypatch.setattr(worker.requests, 'get', Mock(return_value=response))
    with pytest.raises(pixai_chars.RequiredCharacterComponentError, match='inventory'):
        worker.load_head_policies(api_url='https://test.example', headers={}, require_complete=True)


@pytest.mark.parametrize('cl_active', [False, True])
def test_full_main_defers_the_batch_without_spending_image_attempts(monkeypatch, cl_active):
    from tagger import loop, character_policy
    from tagger.content_head_policy import RequiredContentHeadError
    monkeypatch.setattr(loop, 'KURA_URL', 'https://example.invalid')
    monkeypatch.setattr(loop, 'KURA_USER', 'test')
    monkeypatch.setattr(loop, 'KURA_TOKEN', 'test')
    monkeypatch.setattr(loop, '_on_ac_power', lambda: True)
    monkeypatch.setattr(character_policy, 'fetch', lambda *a: {'threshold': .45 if cl_active else None})
    monkeypatch.setattr(character_policy, 'require_release', lambda *a: None)
    monkeypatch.setattr(loop, 'load_all_models', lambda **kw: {
        'wd14_sess': None if cl_active else SimpleNamespace(get_providers=lambda: ['CPUExecutionProvider']),
        'pixai_sess': SimpleNamespace(get_providers=lambda: ['CPUExecutionProvider']), 'content_heads': {}})
    monkeypatch.setattr(loop, 'model_source_id', lambda m: 'test')
    monkeypatch.setattr(worker, 'report_node_heads', lambda *a, **kw: None)
    monkeypatch.setattr(loop, 'claim', Mock(return_value=[{'id': 1}, {'id': 2}]))
    process = Mock(side_effect=RequiredContentHeadError('content_head_inference_failed:general'))
    monkeypatch.setattr(loop, 'process_one', process)
    completion = Mock()
    monkeypatch.setattr(loop, 'complete', completion)
    assert loop.main() == 1
    assert process.call_count == 1
    completion.assert_not_called()


@pytest.mark.parametrize('loaded', [{}, {'alice': None}])
def test_full_loader_rejects_incomplete_inventory(monkeypatch, full, loaded):
    from tagger.content_head_policy import RequiredContentHeadError
    policies = [dict(tag='general', category='general', threshold=.5, apply_mode='replace')]
    monkeypatch.setattr(worker, 'load_head_policies', lambda **kw: policies)
    monkeypatch.setattr(worker, 'load_content_heads', lambda *a, **kw: loaded)
    with pytest.raises(RequiredContentHeadError):
        full.load_policy_heads(api_url='https://configured.example', headers={})


@pytest.mark.parametrize('category', [None, '', 4])
def test_required_head_rejects_invalid_category(category):
    from tagger.content_head_policy import RequiredContentHeadError, require_loaded
    h = heads(general=.9)
    h['__policy__general']['category'] = category
    with pytest.raises(RequiredContentHeadError, match='category_invalid'):
        require_loaded(h)


@pytest.mark.parametrize('failure', ['missing', 'threshold', 'mode'])
def test_direct_required_inference_rejects_invalid_head(failure):
    from tagger.content_head_policy import RequiredContentHeadError
    h = heads(general=.9)
    h['__policy__general']['category'] = 'general'
    if failure == 'missing': h['general'] = None
    elif failure == 'threshold': h['__policy__general']['threshold'] = None
    else: h['__policy__general']['apply_mode'] = 'invalid'
    with pytest.raises(RequiredContentHeadError):
        worker.apply_content_heads([], None, h, required=True)


@pytest.mark.parametrize('category', ['male_genitals', 'female_genitals', 'anus', 'general', 'character', 'custom_category'])
def test_head_category_is_vocab_taxonomy_not_two_model_output_lanes(category):
    from tagger.content_head_policy import require_loaded
    h = heads(existing_tag=.1)
    h['__policy__existing_tag']['category'] = category
    require_loaded(h, [dict(tag='existing_tag', category=category)])
