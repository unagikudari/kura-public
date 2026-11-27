import json
import os
import pytest
from tagger import experiment_status as status
from api.tagger_core.dashboard_experiment import render


def sample(**updates):
    return dict(schema=status.SCHEMA, tag='sample', host='nodeb', experiment='comparison',
                phase='extracting', done=4, total=10, observed_at=1000, published=False, **updates)


def test_progress_is_not_quality_or_publication():
    html = render(status.validate(sample(), now=1010))
    assert '<progress value="4" max="10"' in html
    assert 'data-tagger-category=overview' in html
    assert '本番未反映' in html and '精度や全工程の完了率ではありません' in html
    assert '判別不能とは区別' in html


def test_stale_or_future_does_not_claim_running():
    for now in (999, 1121):
        html = render(status.validate(sample(), now=now))
        assert '稼働状況は未確認' in html and '<progress' not in html


@pytest.mark.parametrize('key,value', [('published', True), ('done', True), ('done', 11),
                                      ('total', -1), ('phase', 'published'),
                                      ('observed_at', float('nan')), ('observed_at', True)])
def test_invalid_hints_are_refused(key, value):
    value_dict = sample(); value_dict[key] = value
    with pytest.raises(ValueError): status.validate(value_dict)


def test_strings_are_escaped_and_unknown_fields_dropped():
    value = sample(); value.update(tag='<script>alert(1)</script>', secret='do not render')
    validated = status.validate(value, now=1010)
    assert 'secret' not in validated
    html = render(validated)
    assert '<script>' not in html and '&lt;script&gt;' in html


def test_read_file_boundaries(tmp_path, monkeypatch):
    target = tmp_path / 'progress.json'; monkeypatch.setattr(status, 'path', lambda: target)
    assert status.read() is None
    target.write_text(json.dumps(sample())); target.chmod(0o600)
    assert status.read(now=1010)['fresh']
    target.chmod(0o666); assert status.read() is None
    target.unlink(); original = tmp_path / 'other'; original.write_text(json.dumps(sample()))
    target.symlink_to(original); assert status.read() is None
    target.unlink(); target.write_text(' ' * (status.LIMIT + 1)); assert status.read() is None
    target.unlink(); os.mkfifo(target); assert status.read() is None


def test_observer_does_not_echo_journal_or_claim_dead_unit_is_training():
    from tagger.observe_experiment import project
    lines = ['arbitrary secret', json.dumps(dict(state='training', arm='trainable_last_stage', epoch=2, secret='hidden'))]
    args = dict(tag='test', host='nodeb', experiment='comparison')
    value = project(lines, True, False, False, False, **args)
    assert (value['phase'], value['done'], value['total']) == ('training', 7, 10)
    assert 'secret' not in json.dumps(value)
    assert project(lines, False, False, False, False, **args)['phase'] == 'stopped'
    assert project(lines, True, True, False, False, **args)['phase'] == 'stopped'
    assert project(lines, True, True, True, True, **args)['phase'] == 'stopped'
    assert project(lines, True, False, True, False, **args)['phase'] == 'evaluating'
    assert project(lines, False, False, True, True, **args)['phase'] == 'completed'


def test_atomic_writer_keeps_source_time_and_filters_extra_keys(tmp_path, monkeypatch):
    target = tmp_path / 'progress.json'; monkeypatch.setattr(status, 'path', lambda: target)
    value = sample(); value['secret'] = 'must not persist'
    status.write(value)
    assert target.stat().st_mode & 0o777 == 0o600
    assert 'secret' not in target.read_text()
    assert status.read(now=1121)['fresh'] is False


def test_packaging_failure_is_observed_even_while_finish_unit_is_active(tmp_path, monkeypatch, capsys):
    from tagger import observe_experiment as observer
    (tmp_path / 'packaged').mkdir()
    (tmp_path / 'result.json').write_text('{"outcome":"succeeded"}')
    (tmp_path / 'packaged/failed.json').write_text('{"outcome":"refused"}')
    monkeypatch.setattr('sys.argv', ['observer', '--training-unit', 'train.service', '--finish-unit', 'finish.service',
                                   '--result-dir', str(tmp_path), '--tag', 'test', '--host', 'nodeb', '--experiment', 'test'])
    monkeypatch.setattr(observer, 'command', lambda argv: 'active\n' if argv[0] == 'systemctl' else '')
    observer.main()
    assert json.loads(capsys.readouterr().out)['phase'] == 'stopped'


def test_observation_names_the_experiment_time_and_count_unit():
    html = render(status.validate(sample(), now=1010))
    assert '実験ID:' in html and 'comparison' in html
    assert '4 / 10 枚' in html
    assert '1970-01-01T00:16:40+00:00' in html
    assert '候補一覧の評価値とは別' in html
    value = sample(); value['phase'] = 'training'
    assert '4 / 10 epoch' in render(status.validate(value, now=1010))


def test_empty_observation_keeps_a_refresh_target():
    assert 'id=experiment-observation' in render(None)


def test_out_of_range_timestamp_cannot_crash_dashboard():
    value = sample(); value['observed_at'] = 1e300
    assert '観測時刻が不正' in render(status.validate(value, now=1010))


def test_zero_unknown_work_is_not_drawn_as_zero_progress():
 value=sample();value.update(done=0,total=0)
 html=render(status.validate(value,now=1010))
 assert '0 / 0' not in html and '工程数は未記録' in html and '<progress' not in html
 assert '最後に増えた時刻: 未記録' in html


@pytest.mark.parametrize('phase', ['completed','stopped'])
def test_terminal_report_does_not_turn_into_unknown_when_collector_stops(phase):
 value=sample();value.update(phase=phase,done=10,total=10)
 html=render(status.validate(value,now=10000))
 assert '過去の報告・現在の稼働を示しません' in html and '稼働状況は未確認' not in html
 assert ('実験完了の記録・品質未承認' if phase=='completed' else '停止の記録・原因確認が必要') in html
 assert '本番未反映' in html and '<progress' not in html


def test_resource_qualification_is_not_candidate_quality_scoring():
 value=sample();value['phase']='qualifying'
 html=render(status.validate(value,now=1010))
 assert 'メモリ・速度を確認中（品質評価とは別）' in html
 assert '<progress' in html and '本番未反映' in html
 assert '<progress' not in render(status.validate(value,now=1121))
