"""Read-only release binding for the optional CL character component.

No model bytes pass through the API. Absent policy means legacy release, but
malformed/unavailable policy must never silently become legacy fallback.
"""
import json
import re
from . import cl2_chars
from .pixai_chars import RequiredCharacterComponentError


def decode(meta):
    require = cl2_chars.require
    require(isinstance(meta, dict) and type(meta.get('id')) is int and meta['id'] > 0,
            'character_release_metadata_invalid')
    policy = meta.get('character_component')
    if policy is None:
        return dict(release_id=meta['id'], threshold=None, release_digest=None)
    require(isinstance(policy, dict) and set(policy) == {'release_digest','description'},
            'character_policy_shape_invalid')
    require(isinstance(policy['release_digest'],str)
            and re.fullmatch('[0-9a-f]{64}',policy['release_digest']), 'character_manifest_digest_invalid')
    description = policy['description']
    require(isinstance(description,dict), 'character_description_invalid')
    row = description.get('threshold_row')
    require(isinstance(row,dict), 'character_threshold_row_invalid')
    expected = cl2_chars.release_description(threshold=row.get('threshold'), evidence=row.get('evidence'))
    try:
        # JSON comparison distinguishes bool/int and rejects NaN or non-JSON data.
        equal = json.dumps(description,sort_keys=True,allow_nan=False) == json.dumps(expected,sort_keys=True,allow_nan=False)
    except (ValueError,TypeError):
        equal = False
    require(equal, 'character_release_component_mismatch')
    return dict(release_id=meta['id'],release_digest=policy['release_digest'],threshold=row['threshold'])


def fetch(kura_url, headers):
    import requests
    try:
        response = requests.get(kura_url.rstrip('/')+'/api/tagger/release/active',headers=headers,timeout=10)
        response.raise_for_status()
        return decode(response.json())
    except RequiredCharacterComponentError:
        raise
    except Exception as exc:
        raise RequiredCharacterComponentError('character_release_unavailable') from exc


class CharacterReleaseChanged(RequiredCharacterComponentError):
    """Exit the worker so its supervisor restarts with fresh release policy."""


def require_release(policy, release_id):
    if (not isinstance(policy, dict) or type(release_id) is not int
            or release_id != policy['release_id']):
        raise CharacterReleaseChanged('character_loaded_release_mismatch')
