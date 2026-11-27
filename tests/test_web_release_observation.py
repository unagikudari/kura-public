import json
import subprocess

import pytest

from api.services import web_release_observation as monitor


@pytest.fixture
def inputs(tmp_path, monkeypatch):
    active = 'a' * 12
    release = tmp_path / 'releases' / 'live'
    release.mkdir(parents=True)
    fields = {'commit': active, 'build_version': '1234'}
    (release / 'release.json').write_text(json.dumps(dict(fields, origin='https://edge.example')))
    (tmp_path / 'active.json').write_text(json.dumps(dict(
        fields, format='kura-web-release-v1', release=str(release))))
    current = {'served': '1234', 'changes': '', 'route': 303, 'git_ok': True}
    def read(url):
        if url.endswith('/_app/version.json'):
            return 200, json.dumps({'version': current['served']})
        if url.endswith('/api/info'):
            return 200, '{"extensions":{"lora":{}}}'
        return current['route'], ''
    def git(repo, command, *args):
        assert command in ('diff', 'ls-remote')
        if not current['git_ok']:
            raise subprocess.TimeoutExpired('git', 8)
        if command == 'ls-remote':
            return 'b' * 40 + '\trefs/heads/main'
        assert 'release/extension_sources.json' in args
        assert ':(exclude)web/tests/**' in args
        return current['changes']
    monkeypatch.setattr(monitor, 'read_http', read)
    monkeypatch.setattr(monitor, 'git', git)
    return tmp_path, current


@pytest.mark.parametrize(('changes', 'state'), [
    ('', 'current'),
    ('web/src/routes/+page.svelte', 'pending'),
    ('release/extension_sources.json', 'pending'),
])
def test_observation_compares_deploy_inputs(inputs, changes, state):
    root, current = inputs
    current['changes'] = changes
    assert monitor.observe(root=root)['state'] == state


def test_edge_version_or_missing_extension_overrides_freshness(inputs):
    root, current = inputs
    current['served'] = 'old'
    assert monitor.observe(root=root)['reason'] == 'edge_version'
    assert monitor.observe(root=root)['state'] == 'mismatch'
    current.update(served='1234', route=404)
    assert monitor.observe(root=root)['reason'] == 'extension_route'
    assert monitor.observe(root=root)['state'] == 'mismatch'


def test_unreachable_git_and_bad_receipt_are_not_current(inputs):
    root, current = inputs
    current['git_ok'] = False
    assert monitor.observe(root=root)['state'] == 'unobserved'
    (root / 'active.json').write_text('{}')
    assert monitor.observe(root=root)['reason'] == 'receipt'


def test_dashboard_exposes_only_fresh_journal_state(monkeypatch):
    monkeypatch.setattr(monitor, 'latest_observation',
                        lambda: {'state': 'pending', 'observed_at': 1000, 'active_commit': 'fixture'})
    assert monitor.dashboard_status(1001) == {'state': 'pending', 'observed_at': 1000}
    assert monitor.dashboard_status(1800) == {'state': 'unobserved'}


def test_journal_only_marks_state_changes(inputs, monkeypatch, capsys):
    root, current = inputs
    sample = monitor.observe(root=root)
    monkeypatch.setattr(monitor, 'observe', lambda: sample.copy())
    monkeypatch.setattr(monitor, 'latest_observation', lambda: sample.copy())
    monitor.main()
    assert not json.loads(capsys.readouterr().out.removeprefix(monitor.PREFIX))['changed']
    monkeypatch.setattr(monitor, 'latest_observation', lambda: dict(sample, state='pending'))
    monitor.main()
    assert json.loads(capsys.readouterr().out.removeprefix(monitor.PREFIX))['changed']
