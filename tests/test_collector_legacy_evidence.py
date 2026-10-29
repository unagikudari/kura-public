"""Source normalization must preserve ambiguity, negative decisions and every field."""
import json
import pytest
from api.collector.adapters.legacy_evidence import compact, restore, fingerprint
from scripts.collector_legacy_evidence import prepare
from test_collector_next import postgres  # noqa:F401


def snapshot():
    value = {'format': 'kura-legacy-read-audit-v2', 'ownership': 'shared-unassigned',
             'readiness': {'ready_for_import': False, 'provenance_converted': False},
             'dictionary': [{'id': 9, 'names': ['old-name', 'Ａlias']}],
             'provenance': {'format': 'kura-legacy-provenance-v1', 'missing_tables': [],
               'tables': {
                 'post_tag_pin': [{'post_id': 1, 'tag_id': 9, 'state': 'absent',
                    'pinned_by': 'owner', 'audit_id': 4, 'pinned_at': '2026-01-01T00:00:00+00:00'}],
                 'post_tag_audit': [{'id': 4, 'post_id': 1, 'actor_type': 'human',
                    'actor_id': 'owner', 'source': 'manual', 'host': None, 'release_id': None,
                    'removed_tags': ['historical-name'], 'added_tags': [], 'new_column': {'nested': 7}}],
                 'post_tag_actor_claim': [{'post_id': post, 'tag_id': 9, 'actor_id': actor,
                    'claimed_at': '2026-01-01T00:00:00+00:00'}
                    for post in range(1, 10) for actor in ('owner', 'model@unverified-version')],
               }}}
    value['snapshot_sha256'] = fingerprint(value)
    return value


def test_lossless_dictionary_keeps_source_kind_nulls_and_unknown_fields():
    source = snapshot()
    package = compact(source)
    assert restore(package) == source
    assert compact(source) == package
    assert package['summary']['source_rows'] == 20
    # Same text does not collapse pinned_by / actor_id / human audit context.
    assert package['summary']['attributions'] == 4
    assert {'actor_id': 'owner'} in package['attributions']
    assert {'pinned_by': 'owner'} in package['attributions']
    assert not package['readiness']['ready_for_import']
    assert package['summary']['normalized_evidence_json_bytes'] < package['summary']['source_evidence_json_bytes']
    assert source == snapshot()


def test_damage_and_lossy_shapes_are_rejected():
    source = snapshot()
    source['provenance']['tables']['post_tag_pin'][0]['state'] = 'present'
    with pytest.raises(ValueError, match='damaged'):
        compact(source)
    package = compact(snapshot())
    package['attributions'][0]['actor_id'] = 'fabricated'
    with pytest.raises(ValueError, match='damaged'):
        restore(package)
    package = compact(snapshot())
    package['tables']['post_tag_actor_claim']['rows'][0][-1] = 999
    package['package_sha256'] = fingerprint({k: v for k, v in package.items() if k != 'package_sha256'})
    with pytest.raises(ValueError, match='reference'):
        restore(package)
    source = snapshot()
    source['provenance']['tables']['post_tag_actor_claim'][0]['unexpected'] = None
    source['snapshot_sha256'] = fingerprint({k: v for k, v in source.items() if k != 'snapshot_sha256'})
    with pytest.raises(ValueError, match='Inconsistent'):
        compact(source)
    for value in ([], {}, {'format': 'old'}):
        with pytest.raises(ValueError):
            compact(value)


def test_private_atomic_package_refuses_overwrite_and_damaged_input(tmp_path):
    source, target = tmp_path/'source.json', tmp_path/'package.json'
    source.write_text(json.dumps(snapshot()))
    summary = prepare(source, target)
    assert summary['source_rows'] == 20
    assert target.stat().st_mode & 0o777 == 0o600
    assert restore(json.loads(target.read_text())) == snapshot()
    with pytest.raises(ValueError, match='exists'):
        prepare(source, target)
    source.write_text('{}')
    rejected = tmp_path/'rejected.json'
    with pytest.raises(ValueError):
        prepare(source, rejected)
    assert not rejected.exists()
    assert not list(tmp_path.glob('tmp*'))


def test_postgres_layout_preserves_claims_and_actor_scoped_queries(postgres):
    import asyncio
    from urllib.parse import urlsplit, parse_qs
    from scripts.collector_claim_dictionary_probe import measure
    params = parse_qs(urlsplit(postgres).query)
    result = asyncio.run(measure(params['host'][0], snapshot(),
        port=int(params['port'][0]), user='collector_test'))
    assert result['rows'] == 18 and result['source_claimants'] == 2
    assert result['roundtrip_differences'] == 0 and result['actor_scoped_result_match']
    for metrics in result['measurements'].values():
        assert metrics['total_relation_bytes'] > 0
        assert metrics['insert_wal_bytes'] > 0


def test_input_budget_is_checked_before_publication(tmp_path, monkeypatch):
    from scripts import collector_legacy_evidence as cli
    source, target = tmp_path/'large.json', tmp_path/'result.json'
    source.write_bytes(b' ' * 33)
    monkeypatch.setattr(cli, 'MAX_INPUT_BYTES', 32)
    with pytest.raises(ValueError, match='budget'):
        cli.prepare(source, target)
    assert not target.exists()
