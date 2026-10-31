import io,json,os,signal,subprocess,time
from types import SimpleNamespace
import pytest
from scripts import kura_api_reload as m

def setup(monkeypatch):
    monkeypatch.setattr(m.subprocess,'check_output',lambda *a,**k:str(os.getpid()))
    monkeypatch.setattr(m.subprocess,'run',lambda *a,**k:SimpleNamespace(returncode=0))
    monkeypatch.setattr(m.urllib.request,'urlopen',lambda *a,**k:io.BytesIO(json.dumps({'commit':'abcdef123'}).encode()))
    killed=[];monkeypatch.setattr(m.os,'kill',lambda *a:killed.append(a));return killed

def test_complete_worker_set_must_change_before_reload_succeeds(monkeypatch):
    killed=setup(monkeypatch);samples=iter([{1,2},{2,3},{3,4}]);monkeypatch.setattr(m,'_serving_worker_pids',lambda *a:next(samples))
    monkeypatch.setattr(m.time,'sleep',lambda _:None);m._reload()
    assert killed==[(os.getpid(),signal.SIGHUP)]

def test_schema_change_refuses_before_signalling(monkeypatch):
    killed=setup(monkeypatch);monkeypatch.setattr(m,'_serving_worker_pids',lambda *a:{1,2})
    monkeypatch.setattr(m.subprocess,'run',lambda args,**k:SimpleNamespace(returncode=1 if args[0]=='git' else 0))
    with pytest.raises(RuntimeError,match='Schema'):m._reload()
    assert not killed

def test_unsupported_runtime_never_falls_back_to_stop_start(monkeypatch):
    killed=setup(monkeypatch);monkeypatch.setattr(m,'_serving_worker_pids',lambda *a:{1,2})
    def fail(*a,**k):raise subprocess.CalledProcessError(1,'capability check')
    monkeypatch.setattr(m.subprocess,'run',fail)
    with pytest.raises(subprocess.CalledProcessError):m._reload()
    assert not killed

def test_serving_age_uses_workers_instead_of_supervisor(monkeypatch):
    monkeypatch.setattr(m,'_serving_worker_pids',lambda *a:{os.getpid()})
    assert 0<m.serving_started_at()<=time.time()
