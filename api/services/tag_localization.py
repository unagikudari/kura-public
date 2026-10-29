"""Localized labels belong to tag identity; aliases remain canonical lookup keys."""
import re
from fastapi import HTTPException


def normalize_localizations(values: dict[str, str]) -> dict[str, str]:
    if len(values) > 32:
        raise ValueError('Use at most 32 languages per tag.')
    result = {}
    for language, value in values.items():
        locale = language.strip().lower()
        if not re.fullmatch(r'[a-z]{2,8}(?:-[a-z0-9]{1,8})*', locale) or len(locale) > 35:
            raise ValueError('Use a language code such as ja, en, or zh-hant.')
        name = value.strip()
        if not name or len(name) > 128 or any(ord(c) < 32 or ord(c) == 127 for c in name):
            raise ValueError('Display names must contain 1–128 characters without control characters.')
        if locale in result:
            raise ValueError('Each language may have only one display name.')
        result[locale] = name
    return result


def merge_localizations(source: dict[str, str], target: dict[str, str], *, source_id: int | None = None, target_id: int | None = None) -> dict[str, str]:
    conflicts = sorted(key for key in source.keys() & target.keys() if source[key] != target[key])
    if conflicts:
        raise HTTPException(status_code=409, detail={
            'code': 'tag_localization_conflict',
            'source_id': source_id, 'target_id': target_id,
            'locales': conflicts,
            'fix': '統合前に、衝突する言語の表示名を一致させるか、片方の表示名を削除してください。',
        })
    merged = {**source, **target}
    if len(merged) > 32:
        raise HTTPException(status_code=409, detail={
            'code': 'tag_localization_limit',
            'fix': '統合後の言語別表示名が32言語以内になるよう、不要な表示名を削除してください。',
        })
    return merged


def localized_names(tag) -> dict[str, str]:
    return {row.locale: row.name for row in tag.localizations}


async def transfer_localizations(db, source, target):
    """Transfer labels after locking both parent tags; caller owns the transaction."""
    from sqlalchemy import delete
    from api.db.models import TagLocalization
    target_names = localized_names(target)
    merged = merge_localizations(localized_names(source), target_names,
                                 source_id=source.tag_id, target_id=target.tag_id)
    for locale, name in merged.items():
        if locale not in target_names:
            target.localizations.append(TagLocalization(locale=locale, name=name))
    await db.execute(delete(TagLocalization).where(TagLocalization.tag_id == source.tag_id))
