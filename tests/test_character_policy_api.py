"""The worker must distinguish an absent policy from an unavailable character artifact."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from api.errors import KuraError
from api.routers.tagger import list_head_policies
from api.tagger_core import artifact_store


@pytest.mark.parametrize('category,available', [('character', True), ('character', False), ('general', False)])
def test_character_policy_category_and_required_artifact(monkeypatch, category, available, tmp_path):
    row = dict(tag='alice', category=category, patch_version=1, content_sha256='a' * 64,
        threshold=.8, apply_mode='replace', gate_tags=[], operating_point='test', kind='content_head_bundle')
    result = SimpleNamespace(mappings=lambda: SimpleNamespace(all=lambda: [row]))
    db = SimpleNamespace(execute=AsyncMock(return_value=result), rollback=AsyncMock())
    if available:(tmp_path/('a'*64)).write_bytes(b'fixture')
    monkeypatch.setattr(artifact_store, 'default_store', lambda: SimpleNamespace(have=lambda sha: available,path_for=lambda sha:tmp_path/sha))
    if category == 'character' and not available:
        with pytest.raises(KuraError) as exc:
            asyncio.run(list_head_policies(db))
        assert exc.value.status == 503
        assert exc.value.error == "character_head_unavailable"
        assert exc.value.context == {"tag": "alice", "content_sha256": "a" * 64}
        assert 'Restore' in exc.value.fix
    else:
        policies = asyncio.run(list_head_policies(db))
        assert [policy.category for policy in policies] == ([category] if available else [])

    db.rollback.assert_awaited_once()


def _endpoint(monkeypatch, rows, tmp_path, capsys=None):
    result = SimpleNamespace(mappings=lambda: SimpleNamespace(all=lambda: rows))
    db = SimpleNamespace(execute=AsyncMock(return_value=result), rollback=AsyncMock())
    for r in rows:
        (tmp_path / r['content_sha256']).write_bytes(b'fixture')
    monkeypatch.setattr(artifact_store, 'default_store',
                        lambda: SimpleNamespace(have=lambda sha: True, path_for=lambda sha: tmp_path / sha))
    return asyncio.run(list_head_policies(db))


def _row(tag, version, sha, kind, category='general'):
    return dict(tag=tag, category=category, patch_version=version, content_sha256=sha * 64, threshold=.5,
                apply_mode='replace', gate_tags=[], operating_point='feat1024@max_f1', kind=kind)


def test_only_head_bundles_are_served(monkeypatch, tmp_path, capsys):
    """A version in force whose bytes are not a head (anilingus v3, a bare probe) is not handed to workers.

    The view (0186) has already reduced each tag to its version in force; the rows here are what it returns.
    """
    rows = [_row('anilingus', 3, 'a', 'probe_safetensors'),
            _row('dark_labia_majora', 8, 'c', 'content_head_bundle'),
            _row('unclassified', 1, 'd', None)]
    policies = _endpoint(monkeypatch, rows, tmp_path)
    assert [(p.tag, p.patch_version) for p in policies] == [('dark_labia_majora', 8)]
    log = capsys.readouterr().out
    assert 'anilingus v3=probe_safetensors' in log
    assert 'unclassified v1=NULL' in log


def test_a_character_policy_that_is_not_a_bundle_stops_tagging(monkeypatch, tmp_path):
    with pytest.raises(KuraError) as exc:
        _endpoint(monkeypatch, [_row('alice', 1, 'e', 'probe_safetensors', category='character')], tmp_path)
    assert exc.value.status == 503 and exc.value.error == 'character_head_unavailable'
    assert exc.value.context['kind'] == 'probe_safetensors'
