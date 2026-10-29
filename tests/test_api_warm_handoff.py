"""Actual Uvicorn: keep serving during a slow startup and a failed replacement."""
import json,os,signal,socket,subprocess,sys,time,urllib.request
from pathlib import Path
import pytest

def test_warm_handoff_keeps_requests_alive_and_refuses_failed_replacement(tmp_path):
    runtime=os.environ.get('KURA_TEST_UVICORN_PYTHON',sys.executable)
    check="import inspect;from uvicorn.supervisors.multiprocess import Multiprocess;assert 'wait_until_ready' in inspect.getsource(Multiprocess.restart_all)"
    if subprocess.run([runtime,'-c',check],capture_output=True).returncode:pytest.skip('runtime has no startup-ready handoff')
    marker=tmp_path/'fail-startup'
    (tmp_path/'handoff_app.py').write_text('''import asyncio,json,os
from pathlib import Path
async def app(scope,receive,send):
 if scope['type']=='lifespan':
  while True:
   event=await receive()
   if event['type']=='lifespan.startup':
    if Path(os.environ['FAIL_FILE']).exists():
     await send({'type':'lifespan.startup.failed','message':'synthetic startup failure'});return
    await asyncio.sleep(.5);await send({'type':'lifespan.startup.complete'})
   else:await send({'type':'lifespan.shutdown.complete'});return
 else:
  await send({'type':'http.response.start','status':200,'headers':[(b'content-type',b'application/json')]})
  await send({'type':'http.response.body','body':json.dumps({'pid':os.getpid()}).encode()})
''')
    with socket.socket() as s:s.bind(('127.0.0.1',0));port=s.getsockname()[1]
    with (tmp_path/'uvicorn.log').open('w') as log:
        process=subprocess.Popen([runtime,'-m','uvicorn','handoff_app:app','--app-dir',str(tmp_path),'--host','127.0.0.1','--port',str(port),'--workers','2','--timeout-worker-healthcheck','3'],env={**os.environ,'FAIL_FILE':str(marker)},stdout=log,stderr=log,start_new_session=True)
        def request():
            with urllib.request.urlopen(f'http://127.0.0.1:{port}/',timeout=2) as response:
                assert response.status==200;return json.load(response)['pid']
        try:
            deadline=time.monotonic()+15;old=set()
            while time.monotonic()<deadline and len(old)<2:
                try:old.add(request())
                except OSError:time.sleep(.1)
            assert len(old)==2
            os.kill(process.pid,signal.SIGHUP);new=set();samples=0;deadline=time.monotonic()+15
            while time.monotonic()<deadline and len(new)<2:
                pid=request();samples+=1
                if pid not in old:new.add(pid)
                time.sleep(.03)
            assert len(new)==2 and samples>5
            marker.touch();os.kill(process.pid,signal.SIGHUP)
            seen=set();deadline=time.monotonic()+5
            while time.monotonic()<deadline:seen.add(request());time.sleep(.03)
            assert seen==new
        finally:
            os.killpg(process.pid,signal.SIGTERM)
            try:process.wait(timeout=10)
            except subprocess.TimeoutExpired:os.killpg(process.pid,signal.SIGKILL);process.wait()
