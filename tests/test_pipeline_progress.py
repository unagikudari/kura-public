"""Unattended failures and historical observations must never look successful."""
import asyncio
from datetime import datetime, timedelta, timezone
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from api.services import tagger_progress as progress
from api.tagger_core import gt_sync


from tagger import vnext_runner
@pytest.mark.parametrize('summary,code', [
    ({'_global': {'negatives_synced': 12}}, 0),
    ({'_global': {'error': 'database unavailable'}}, 1),
    ({'_global': {}, 'x:pool1': {'human_positive': 5}, 'y:pool2': {'error': ''}}, 1),
])
def test_sync_failure_stops_following_cycle_stages(monkeypatch, summary, code):
    monkeypatch.setattr(gt_sync, 'run_sync', AsyncMock(return_value=summary))
    assert gt_sync.main() == code


@pytest.mark.parametrize('mode,code', [('missing', 1), ('exception', 1), ('bad_exit', 1), ('hold', 75)])
def test_gate_fault_is_not_idle_success_and_never_claims(monkeypatch, tmp_path, mode, code):
    gate = tmp_path / 'gate.py'
    if mode != 'missing':
        gate.touch()
    monkeypatch.setattr(vnext_runner, 'GPU_GATE', gate)
    monkeypatch.setattr(vnext_runner, 'heartbeat', lambda: None)
    def run(*args, **kwargs):
        if mode == 'exception':
            raise TimeoutError()
        return SimpleNamespace(returncode=75 if mode == 'hold' else 2)
    monkeypatch.setattr(vnext_runner.subprocess, 'run', run)
    def forbidden(*args, **kwargs):
        pytest.fail('failed admission must not access inbox or claim work')
    monkeypatch.setattr(vnext_runner, 'broker', forbidden)
    assert vnext_runner.tick() == code


NOW = datetime(2026, 9, 14, tzinfo=timezone.utc)


def snapshot(tmp_path, age=0):
    path = tmp_path / 'snapshot.json'
    path.write_text(json.dumps({'generated_at': (NOW - timedelta(seconds=age)).isoformat(),
                               'tags': {'smegma': {'disposition': 'hold', 'reason': 'downstream_saturated',
                                                   'backlog': 10, 'backlog_max': 10}}}))
    return path


@pytest.mark.parametrize('age,state', [(0, 'current'), (1920, 'current'), (1921, 'stale'), (-61, 'invalid')])
def test_snapshot_freshness(tmp_path, age, state):
    assert progress.read_snapshot(snapshot(tmp_path, age), now=NOW)['observation_state'] == state


@pytest.mark.parametrize('body', ['{', 'null', '{}', '{"generated_at":"2026-09-14", "tags":{}}',
                                 '{"generated_at":"2026-09-14T00:00:00Z", "tags":{"x":null}}'])
def test_bad_snapshot_cannot_report_current(tmp_path, body):
    path = tmp_path / 'snapshot.json'; path.write_text(body)
    got = progress.read_snapshot(path, now=NOW)
    assert got['observation_state'] == 'invalid' and got['tags'] == {}


def test_missing_snapshot(tmp_path):
    assert progress.read_snapshot(tmp_path / 'missing')['observation_state'] == 'missing'


def test_invalid_count_cannot_reach_html_metadata(tmp_path):
    path = snapshot(tmp_path)
    doc = json.loads(path.read_text())
    doc['min_gt_negative'] = '<script>bad()</script>'
    path.write_text(json.dumps(doc))
    got = progress.read_snapshot(path, now=NOW)
    assert got['observation_state'] == 'invalid' and got['min_gt_negative'] is None


@pytest.mark.parametrize('control,current', [(None, False), ({'paused': True, 'halt_reason': 'operator'}, False),
                                           ({'paused': False, 'halt_reason': None}, True)])
def test_live_pause_overrides_fresh_snapshot(tmp_path, control, current):
    result = SimpleNamespace(mappings=lambda: SimpleNamespace(one_or_none=lambda: control))
    db = SimpleNamespace(execute=AsyncMock(return_value=result))
    got = asyncio.run(progress.progress(db, path=snapshot(tmp_path), now=NOW))
    assert got['current'] is current
    assert got['execution_state'] == 'unobserved'
    assert got['scope'] == 'automatic_reconciler' and not got['includes_dedicated_jobs']
    assert 'smegma' in got['tags']  # historical evidence is retained, not relabelled


def test_all_dispositions_have_explanations():
    assert '10/10' in progress.describe_decision({'disposition': 'hold', 'reason': 'downstream_saturated', 'backlog': 10, 'backlog_max': 10})
    for disposition in ['decision_defer', 'dispatch_failed', 'new_kind']:
        assert 'new_reason' in progress.describe_decision({'disposition': disposition, 'reason': 'new_reason'})
    assert '実行未確認' in progress.describe_decision({'disposition': 'in_flight'})


@pytest.mark.parametrize('paused,age,notice', [
    (True, 0, '自動処理停止: operator'),
    (False, 1921, '自動学習の状態: 更新が古い'),
])
def test_dashboard_keeps_snapshot_only_tags_when_not_current(monkeypatch, tmp_path, paused, age, notice):
    from collections import defaultdict
    from api.routers.pools import tagger_dashboard
    from api.services import tagger_situation

    # No tag exists in any database result. Only the historical snapshot knows it.
    class EmptyResult:
        def mappings(self): return self
        def all(self): return []
        def first(self): return None
        def scalar(self): return 0
        def scalar_one(self): return 0
        def one(self): return defaultdict(int)

    db = SimpleNamespace(execute=AsyncMock(return_value=EmptyResult()))
    control_db = SimpleNamespace(execute=AsyncMock(return_value=SimpleNamespace(
        mappings=lambda: SimpleNamespace(one_or_none=lambda: {'paused': paused, 'halt_reason': 'operator'}))))
    observed = asyncio.run(progress.progress(control_db, path=snapshot(tmp_path, age), now=NOW))
    monkeypatch.setattr(progress, 'progress', AsyncMock(return_value=observed))
    monkeypatch.setattr(tagger_situation, 'sweep_coverage', AsyncMock(return_value={
        'processed': 0, 'total_posts': 0, 'pct': 0, 'remaining': 0}))
    monkeypatch.setattr(tagger_situation, 'queue_state', AsyncMock(return_value={'queued': 0}))
    monkeypatch.setattr(tagger_situation, 'character_coverage', AsyncMock(return_value={
        'covered': 0, 'total_posts': 0, 'applied_24h': {}, 'pixai_hosts': []}))
    response = asyncio.run(tagger_dashboard(raw=True, db=db))
    html = response.body.decode()
    import re
    row = next(r for r in re.findall(r'<tr\b[^>]*>.*?</tr>', html, re.S) if "data-tag='smegma'" in r)
    assert notice in row
    assert '10/10' not in row  # old decision is not advertised as current
    assert '＋正例 あと' not in row
