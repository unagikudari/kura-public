#!/usr/bin/env python3
"""Warm-worker API reload. Refuse unsupported runtimes; never fall back to stop/start."""
import argparse,fcntl,json,os,re,signal,subprocess,sys,time,urllib.request
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from scripts.media_replication_gate import _serving_worker_pids

def serving_started_at(unit='kura-api.service',port=8001):
    pids=_serving_worker_pids(unit,port)
    if not pids:return 0
    boot=int(next(l.split()[1] for l in Path('/proc/stat').read_text().splitlines() if l.startswith('btime ')))
    ticks=os.sysconf('SC_CLK_TCK')
    return int(min(boot+int(Path(f'/proc/{pid}/stat').read_text().rsplit(') ',1)[1].split()[19])/ticks for pid in pids))
def _reload(unit='kura-api.service',port=8001,timeout=180):
    parent=int(subprocess.check_output(['systemctl','--user','show',unit,'-p','MainPID','--value'],text=True).strip())
    old=_serving_worker_pids(unit,port)
    if len(old)<2:raise RuntimeError('At least two serving workers are required; no restart fallback')
    runtime=Path('/proc',str(parent),'cmdline').read_bytes().split(b'\0')[0].decode()
    # Check the exact runtime supervising this service, not the caller's venv.
    code="import inspect;from uvicorn.supervisors.multiprocess import Multiprocess; s=inspect.getsource(Multiprocess.restart_all);assert 'wait_until_ready' in s and s.index('wait_until_ready')<s.index('old_process.terminate')"
    subprocess.run([runtime,'-c',code],check=True,capture_output=True,timeout=10)
    with urllib.request.urlopen(f'http://127.0.0.1:{port}/api/build',timeout=15) as response:previous=json.load(response)['commit']
    if not re.fullmatch('[0-9a-f]{7,40}',previous):raise RuntimeError('Unknown running API revision')
    repo=Path(__file__).resolve().parents[1]
    if subprocess.run(['git','-C',str(repo),'diff','--quiet',previous,'HEAD','--','api/alembic/versions']).returncode:
        raise RuntimeError('Schema revisions changed: coordinated migration required, no rolling reload fallback')
    os.kill(parent,signal.SIGHUP)
    deadline=time.monotonic()+timeout
    while time.monotonic()<deadline:
        if int(subprocess.check_output(['systemctl','--user','show',unit,'-p','MainPID','--value'],text=True).strip())!=parent:
            raise RuntimeError('Supervisor changed during reload')
        current=_serving_worker_pids(unit,port)
        if len(current)==len(old) and not current.intersection(old):
            with urllib.request.urlopen(f'http://127.0.0.1:{port}/api/build',timeout=5) as response:build=json.load(response)
            print(json.dumps({'rolling_reload':True,'workers':len(current),'commit':build['commit']}));return
        time.sleep(.25)
    raise RuntimeError('Reload did not finish; warm handoff keeps existing workers on startup failure')
def reload(unit='kura-api.service',port=8001,timeout=180):
    root=Path.home()/'.local/state/kura-api-reload';root.mkdir(mode=0o700,parents=True,exist_ok=True)
    with (root/'reload.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        return _reload(unit,port,timeout)
if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--started-at',action='store_true');a=p.parse_args()
    if a.started_at:print(serving_started_at())
    else:reload()
