import asyncio
import json
import os
import signal
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock
import pytest
from tagger import learn_trial as trial


def args():
    return SimpleNamespace(tag='one', event=121, threads=2, timeout_seconds=1,
                           tagger_python=sys.executable)


@pytest.mark.parametrize('eligibility,tag,event,reviewable', [
    ([True, True, True], 'one', 121, True),
    ([True, True, False], 'one', 121, False),
    ([True, True, True], 'other', 121, False),
    ([True, True, True], 'one', 122, False)])
def test_exact_target_local_result_only(monkeypatch, tmp_path, eligibility, tag, event, reviewable):
    class DB:
        rollback = AsyncMock()
        async def __aenter__(self): return self
        async def __aexit__(self, *a): pass
    monkeypatch.setattr(trial.learn, 'async_session', DB)
    monkeypatch.setattr(trial, 'eligible', AsyncMock(side_effect=eligibility))
    acquire = AsyncMock(return_value=object()); release = AsyncMock()
    monkeypatch.setattr(trial.learn, 'acquire_lease', acquire)
    monkeypatch.setattr(trial.learn, 'release_lease', release)
    def forbidden(*a, **kw): raise AssertionError('must never register or run normal pass')
    monkeypatch.setattr(trial.learn, 'post_registration', forbidden)
    monkeypatch.setattr(trial.learn, 'run_pass', forbidden)
    monkeypatch.setattr(trial, 'run_child', lambda a,d: {'returncode':0,'timed_out':False,
        'output':{'registration':{'tag':tag,'lifecycle_event_id':event}}})
    assert asyncio.run(trial.run_trial(args(), tmp_path)) == (0 if reviewable else 1)
    report = json.loads((tmp_path/'result.json').read_text())
    assert report['reviewable'] is reviewable
    assert report['registered'] is False and report['promoted'] is False
    assert acquire.call_args.args[1] == 'lifecycle:one'
    release.assert_awaited_once()


def test_held_consumed_or_paused_stops_before_lease(monkeypatch, tmp_path):
    class DB:
        async def __aenter__(self): return self
        async def __aexit__(self, *a): pass
    acquire = AsyncMock()
    monkeypatch.setattr(trial.learn, 'async_session', DB)
    monkeypatch.setattr(trial, 'eligible', AsyncMock(return_value=False))
    monkeypatch.setattr(trial.learn, 'acquire_lease', acquire)
    with pytest.raises(RuntimeError): asyncio.run(trial.run_trial(args(), tmp_path))
    acquire.assert_not_awaited()
    assert not (tmp_path/'result.json').exists()


def test_timeout_stops_group_and_isolates_child(monkeypatch, tmp_path):
    monkeypatch.setattr(trial.subprocess, 'run', lambda *a,**kw: SimpleNamespace(returncode=0))
    real = subprocess.Popen; seen={}
    def launch(cmd, **kw):
        seen.update(cmd=cmd,env=kw['env'])
        p=real([sys.executable,'-c','import time;time.sleep(60)'], **kw)
        seen['pid']=p.pid
        return p
    monkeypatch.setattr(trial.subprocess,'Popen',launch)
    r=trial.run_child(args(),tmp_path)
    assert r['timed_out'] and r['returncode'] != 0
    assert seen['cmd'][-1]=='--review-only'
    assert seen['env']['VNEXT_STATE_DIR']==str(tmp_path/'state')
    assert seen['env']['KURA_ARTIFACT_DIR']==str(tmp_path/'artifacts')
    assert 'default_transaction_read_only=on' in seen['env']['PGOPTIONS']
    with pytest.raises(ProcessLookupError):os.killpg(seen['pid'],0)


@pytest.mark.parametrize('review_only', [True, False])
def test_runner_trial_never_uploads(monkeypatch, review_only):
    from tagger import vnext_runner as runner
    calls=[];payloads=[]
    monkeypatch.setattr(runner,'REVIEW_ONLY',review_only)
    monkeypatch.setattr(runner,'_upload_artifact',lambda *a:calls.append(a))
    monkeypatch.setattr(runner,'_register',lambda body:payloads.append(body))
    runner.submit_registration('one',{'classification':{'verdict':'deployable'},
        'gt':{'positive':20,'negative':50}},'/local/artifact','a'*64,121)
    assert len(calls)==(0 if review_only else 1)
    assert payloads[0]['tag']=='one' and payloads[0]['lifecycle_event_id']==121

@pytest.mark.parametrize('paused,candidate_ok,queue,expected', [
    (True,True,[('one',121)],False),
    (False,False,[('one',121)],False),
    (False,True,[('other',121)],False),
    (False,True,[('one',122)],False),
    (False,True,[('one',121)],True)])
def test_eligibility_checks_candidate_and_exact_event(paused,candidate_ok,queue,expected):
    class Result:
        def __init__(self,value):self.value=value
        def scalar_one_or_none(self):return self.value
        def scalar_one(self):return self.value
        def all(self):return self.value
    db=SimpleNamespace(execute=AsyncMock(side_effect=[Result(paused),Result(candidate_ok),
        Result([SimpleNamespace(tag=t,event_id=e) for t,e in queue])]))
    assert asyncio.run(trial.eligible(db,'one',121)) is expected


@pytest.mark.parametrize('failure', ['missing_dependency','timeout'])
def test_child_preflight_failure_never_starts_measurement(monkeypatch,tmp_path,failure):
    seen={}
    def preflight(cmd,**kw):
        seen.update(cmd=cmd,env=kw['env'])
        if failure=='timeout':raise subprocess.TimeoutExpired(cmd,60)
        return SimpleNamespace(returncode=1)
    def forbidden(*a,**kw):raise AssertionError('measurement must not start')
    monkeypatch.setattr(trial.subprocess,'run',preflight)
    monkeypatch.setattr(trial.subprocess,'Popen',forbidden)
    r=trial.run_child(args(),tmp_path)
    assert r['preflight_failed'] and r['returncode']==1 and r['output'] is None
    assert seen['cmd']==[sys.executable,'-m','tagger.learn_preflight','--tag','one','--event','121']
    assert 'default_transaction_read_only=on' in seen['env']['PGOPTIONS']
