"""Language labels must never change canonical tag identity."""
import pytest
from fastapi import HTTPException
from pydantic import ValidationError
from api.routers.tags import TagPatchRequest
from api.services.tag_localization import normalize_localizations, merge_localizations


def test_locale_normalization_and_explicit_clear():
    assert normalize_localizations({' JA ': ' 初音ミク ', 'zh-Hant': '初音未來'}) == {'ja': '初音ミク', 'zh-hant': '初音未來'}
    assert TagPatchRequest(version=1).localizedNames is None
    assert TagPatchRequest(version=1, localizedNames={}).localizedNames == {}


@pytest.mark.parametrize('value', [None, {'ja': ''}, {'ja': 'a\nb'}, {'JA': 'a', 'ja': 'b'}, {'../ja': 'x'}, {'__proto__': 'x'}, {'en': 'x' * 129}, {'ja': 123}])
def test_invalid_values_cannot_be_saved(value):
    with pytest.raises(ValidationError):
        TagPatchRequest(version=1, localizedNames=value)


def test_merge_preserves_nonconflicting_languages():
    assert merge_localizations({'ja': '名前'}, {'en': 'Name', 'ja': '名前'}) == {'ja': '名前', 'en': 'Name'}


def test_merge_conflict_is_actionable_and_does_not_mutate_input():
    source, target = {'ja': '甲'}, {'ja': '乙'}
    with pytest.raises(HTTPException) as error:
        merge_localizations(source, target)
    assert error.value.status_code == 409
    assert error.value.detail['locales'] == ['ja']
    assert error.value.detail['fix']
    assert source == {'ja': '甲'} and target == {'ja': '乙'}


def test_machine_projection_has_no_translation_field():
    from api.schemas.common import MicroTag
    assert 'localizedNames' not in MicroTag(names=['canonical'], category='general').model_dump()
    assert MicroTag(names=['canonical'], category='general', localizedNames={'ja': '名称'}).model_dump()['localizedNames'] == {'ja': '名称'}
