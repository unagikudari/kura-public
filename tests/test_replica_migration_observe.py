import json
import sys

import pytest

from scripts import replica_migration_observe as monitor


@pytest.mark.parametrize(('canonical', 'replica', 'committed', 'direction', 'tracked'), [
    ('0157', '0156', {'0156'}, 'canonical_ahead', False),
    ('0157', '0156', {'0156', '0157'}, 'canonical_ahead', True),
    ('0157', '0157', {'0156'}, 'aligned', False),
    ('0156', '0157', {'0156', '0157'}, 'replica_ahead', True),
    ('0157', '0157', {'0156', '0157'}, 'aligned', True),
])
def test_ledger_direction_and_commit_status_are_independent(canonical, replica, committed, direction, tracked):
    result = monitor.observe(canonical, replica, committed)
    assert result['replica_vs_canonical'] == direction
    assert result['canonical_in_repository'] is tracked


def test_comparison_rejects_empty_or_bad_evidence():
    with pytest.raises(ValueError):
        monitor.observe('0212', 'unknown', {'0212'})
    with pytest.raises(ValueError):
        monitor.observe('0212', '0212', set())


def test_main_never_reports_alignment_on_failed_canonical_read(monkeypatch, capsys):
    monkeypatch.setattr(sys, 'argv', ['observe', '--verified-main-ref', 'a' * 40])
    monkeypatch.setattr(monitor, 'committed_revisions', lambda *args: {'0212'})
    monkeypatch.setattr(monitor, 'settings', lambda *args: {
        'host': 'localhost', 'port': 5432, 'user': 'kura',
        'name': 'fixture', 'password': 'fixture',
    })
    monkeypatch.setattr(monitor, 'canonical_dsn', lambda: 'fixture')
    def read(**connection):
        if 'dsn' in connection:
            raise RuntimeError('fixture secret')
        return '0212'
    monkeypatch.setattr(monitor, 'read_head', read)
    assert monitor.main() == 2
    assert json.loads(capsys.readouterr().out) == {
        'status': 'unobserved', 'source': 'canonical', 'error_type': 'RuntimeError',
    }
