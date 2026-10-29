import pytest
from tagger.api_target import api_origin, api_base


def test_missing_target_is_optional_only_during_import():
    assert api_origin(environ={}, required=False) == ''
    assert api_base(environ={}, required=False) == ''
    with pytest.raises(ValueError, match='set KURA_URL'):
        api_origin(environ={})


@pytest.mark.parametrize('setting', ['https://archive.example.invalid', 'https://archive.example.invalid/api/',
                                   'https://archive.example.invalid/prefix/api'])
def test_origin_and_api_base_are_normalized_once(setting):
    origin = setting.rstrip('/').removesuffix('/api')
    assert api_origin(environ={'KURA_API': setting}) == origin
    assert api_base(environ={'KURA_API': setting}) == origin + '/api'


def test_explicit_target_and_canonical_setting_take_precedence():
    env = {'KURA_URL': 'https://canonical.example.invalid', 'KURA_API': 'http://replica.example.invalid/api'}
    assert api_origin(environ=env) == env['KURA_URL']
    assert api_origin('https://chosen.example.invalid/api',environ=env) == 'https://chosen.example.invalid'


@pytest.mark.parametrize('target', ['file:///tmp/api','https://name:secret@example.invalid',
                                   'https://example.invalid/?token=secret','https://example.invalid/#secret',
                                   '/api','https://example.invalid:bad','http://[invalid','https://example.invalid/\napi'])
def test_invalid_or_secret_bearing_target_is_rejected_without_echo(target):
    with pytest.raises(ValueError) as error:
        api_origin(environ={'KURA_URL':target})
    assert target not in str(error.value)
