"""An unavailable character component is not a successful negative prediction."""
from types import SimpleNamespace
from unittest.mock import Mock

import numpy as np
import pytest
from tagger import nodec_autotag_worker as w, pixai_chars


def setup_post(monkeypatch, hits=()):
    monkeypatch.setattr(w, 'PIXAI_CHARS', {'chars': {0: 'alice'}})
    monkeypatch.setattr(w, 'fetch_image', lambda *a: object())
    monkeypatch.setattr(w, 'infer', lambda *a, **kw: (['1girl', *kw.get('characters', [])], None, {}))
    monkeypatch.setattr(w, 'apply_content_heads', lambda tags, *a: tags)
    monkeypatch.setattr(w, 'apply_skin_model', lambda tags, *a: tags)
    monkeypatch.setattr(pixai_chars, 'char_tags', lambda *a: list(hits))
    completion = Mock(return_value={})
    monkeypatch.setattr(w, 'complete', completion)
    return completion


def test_genuine_zero_characters_can_complete(monkeypatch):
    completion = setup_post(monkeypatch)
    assert w.process_item({}, None, [], [], 448, {'id': 12})[0]
    assert completion.call_args.args[2] == 'ok'
    assert completion.call_args.kwargs['tags'] == [{'name': '1girl', 'category': 'general'}]


def test_character_runtime_failure_neither_reaps_nor_spends_image_retry(monkeypatch):
    completion = setup_post(monkeypatch)
    def fail(*args):
        raise RuntimeError('backend unavailable')
    monkeypatch.setattr(pixai_chars, 'char_tags', fail)
    with pytest.raises(pixai_chars.RequiredCharacterComponentError, match='post 12'):
        w.process_item({}, None, [], [], 448, {'id': 12})
    completion.assert_not_called()  # neither ok/reap nor failed/attempt increment
    assert w.PIXAI_CHARS is None
    # The next refreshed component resumes normal processing.
    monkeypatch.setattr(pixai_chars, 'load', lambda: {'chars': {0: 'alice'}, 'sha12': 'test'})
    monkeypatch.setattr(pixai_chars, 'char_tags', lambda *a: [('alice', .9)])
    w.refresh_pixai_chars()
    assert w.process_item({}, None, [], [], 448, {'id': 12})[0]
    assert {'name': 'alice', 'category': 'character'} in completion.call_args.kwargs['tags']


def test_invalid_image_still_uses_normal_failure_path(monkeypatch):
    completion = setup_post(monkeypatch)
    monkeypatch.setattr(w, 'fetch_image', Mock(side_effect=ValueError('invalid image')))
    assert not w.process_item({}, None, [], [], 448, {'id': 12})[0]
    assert completion.call_args.args[2] == 'failed'


def test_batch_aborts_after_component_failure(monkeypatch):
    setup_post(monkeypatch)
    monkeypatch.setattr(w, 'claim', lambda *a: [{'id': 1}, {'id': 2}])
    fn = Mock(side_effect=pixai_chars.RequiredCharacterComponentError('broken'))
    monkeypatch.setattr(w, 'process_item', fn)
    state = SimpleNamespace(model=None, names=[], cats=[], img_size=448, release_id=14,
                            veto_heads={}, reap_gates=[], release_reap=set())
    with pytest.raises(pixai_chars.RequiredCharacterComponentError):
        w.process_batch({}, state, 2, 600)
    assert fn.call_count == 1


def setup_main(monkeypatch, argv):
    monkeypatch.setattr(w, "API", "https://worker.example.invalid")
    monkeypatch.setattr(w, 'PIXAI_CHARS', None)
    monkeypatch.setattr(w.sys, 'argv', argv)
    monkeypatch.setattr(w, 'auth_header', lambda: ({}, None))
    for name in ['_resolve_device', 'refresh_content_heads', 'refresh_pixai_chars']:
        monkeypatch.setattr(w, name, lambda: None)
    monkeypatch.setattr(w, 'refresh_model', lambda state: None)
    monkeypatch.setattr(w, 'load_skin_model', lambda: (None, None))
    http = Mock(side_effect=AssertionError('must not claim posts'))
    monkeypatch.setattr(w.requests, 'post', http)
    return http


def test_trial_missing_component_does_not_claim(monkeypatch):
    http = setup_main(monkeypatch, ['worker', '--trial', '2'])
    assert w.main() == 1
    http.assert_not_called()


def test_resident_missing_component_backs_off_without_claim(monkeypatch):
    http = setup_main(monkeypatch, ['worker'])
    waits = []
    monkeypatch.setattr(w, 'YIELD_FLAG', 'test-yield')
    monkeypatch.setattr(w.os.path, 'exists', lambda path: bool(waits))
    monkeypatch.setattr(w.time, 'sleep', waits.append)
    assert w.main() == 0
    assert waits == [30]
    http.assert_not_called()


@pytest.mark.parametrize('scores', [np.array([np.nan]), np.array([np.inf]), np.array([-0.1]), np.array([1.1]), np.array([])])
def test_invalid_predictions_are_component_errors(monkeypatch, scores):
    monkeypatch.setattr(pixai_chars, 'preprocess', lambda *a: None)
    session = SimpleNamespace(get_inputs=lambda: [SimpleNamespace(name='input')],
                              get_outputs=lambda: [SimpleNamespace(name='prediction')],
                              run=lambda *a: [np.array([scores])])
    with pytest.raises(pixai_chars.RequiredCharacterComponentError):
        pixai_chars.char_tags({'sess': session, 'chars': {0: 'alice'}}, object())


def test_full_worker_aborts_batch_without_completion(monkeypatch):
    loop = pytest.importorskip("tagger.loop")  # needs the optional inference runtime
    monkeypatch.setattr(loop, 'KURA_URL', 'https://worker.example.invalid')
    monkeypatch.setattr(loop, 'KURA_USER', 'test')
    monkeypatch.setattr(loop, 'KURA_TOKEN', 'test')
    monkeypatch.setattr(loop, '_on_ac_power', lambda: True)
    monkeypatch.setattr(loop, 'load_all_models', lambda **kw: {'wd14_sess': SimpleNamespace(get_providers=lambda: ['CPUExecutionProvider'])})
    monkeypatch.setattr(loop, 'model_source_id', lambda *a: 'test')
    monkeypatch.setattr(loop, 'claim', lambda *a: [{'id': 1}, {'id': 2}])
    process = Mock(side_effect=pixai_chars.RequiredCharacterComponentError('fallback failed'))
    monkeypatch.setattr(loop, 'process_one', process)
    completion = Mock()
    monkeypatch.setattr(loop, 'complete', completion)
    assert loop.main() == 1
    assert process.call_count == 1
    completion.assert_not_called()


def test_resident_policy_outage_backs_off_without_claim(monkeypatch):
    http = setup_main(monkeypatch, ['worker'])
    refresh = Mock(side_effect=[None, pixai_chars.RequiredCharacterComponentError('policy offline')])
    monkeypatch.setattr(w, 'refresh_content_heads', refresh)
    waits = []
    monkeypatch.setattr(w, 'YIELD_FLAG', 'test-yield')
    monkeypatch.setattr(w.os.path, 'exists', lambda path: bool(waits))
    monkeypatch.setattr(w.time, 'sleep', waits.append)
    assert w.main() == 0
    assert waits == [30]
    http.assert_not_called()


def test_missing_target_refuses_before_claim_or_model_loading(monkeypatch):
    http = setup_main(monkeypatch, ['worker'])
    monkeypatch.setattr(w, 'API', '')
    assert w.main() == 2
    http.assert_not_called()


def test_full_worker_missing_target_refuses_before_model_loading(monkeypatch):
    loop = pytest.importorskip("tagger.loop")  # needs the optional inference runtime
    monkeypatch.setattr(loop, 'KURA_URL', '')
    models = Mock()
    monkeypatch.setattr(loop, 'load_all_models', models)
    assert loop.main() == 2
    models.assert_not_called()
