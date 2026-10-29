import asyncio,hashlib,os,threading,time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from api.services.policy_artifacts import PolicyArtifacts,policy_artifact_availability
from api.tagger_core.artifact_store import ArtifactStore

def fixture(tmp_path):
 s=ArtifactStore(tmp_path);data=b'correct model';sha=hashlib.sha256(data).hexdigest();s.put_bytes(data,sha);return s,sha

def test_unchanged_polls_do_not_read_bytes_again_but_expiry_does(tmp_path,monkeypatch):
 s,sha=fixture(tmp_path);calls=[];real=s.have;clock=[0]
 monkeypatch.setattr(s,'have',lambda h:(calls.append(h),real(h))[1]);index=PolicyArtifacts(clock=lambda:clock[0])
 assert index.check(s,[sha,sha])=={sha:True};assert index.check(s,[sha])=={sha:True};assert len(calls)==1
 clock[0]=601;assert index.check(s,[sha])=={sha:True};assert len(calls)==2

def test_same_size_edit_with_restored_mtime_and_replacement_invalidate(tmp_path):
 s,sha=fixture(tmp_path);index=PolicyArtifacts();p=s.path_for(sha);assert index.check(s,[sha])[sha]
 original=p.stat();p.write_bytes(b'broken  model');os.utime(p,ns=(original.st_atime_ns,original.st_mtime_ns))
 assert not index.check(s,[sha])[sha]
 replacement=tmp_path/'replacement';replacement.write_bytes(b'correct model');replacement.replace(p)
 assert index.check(s,[sha])[sha]
 p.unlink();assert not index.check(s,[sha])[sha]

def test_concurrent_scans_are_coalesced_and_capacity_bounded(tmp_path,monkeypatch):
 s,sha=fixture(tmp_path);calls=[];real=s.have
 def slow(h):calls.append(h);time.sleep(.05);return real(h)
 monkeypatch.setattr(s,'have',slow);index=PolicyArtifacts(capacity=1)
 with ThreadPoolExecutor(4) as pool:assert all(pool.map(lambda _:index.check(s,[sha])[sha],range(4)))
 assert len(calls)==1
 other=hashlib.sha256(b'other').hexdigest();s.put_bytes(b'other',other);index.check(s,[other]);assert len(index.entries)==1

def test_hash_work_does_not_block_other_async_requests(tmp_path,monkeypatch):
 s,sha=fixture(tmp_path);entered=threading.Event();finish=threading.Event();real=s.have
 def slow(h):entered.set();assert finish.wait(2);return real(h)
 monkeypatch.setattr(s,'have',slow)
 async def exercise():
  task=asyncio.create_task(policy_artifact_availability(s,[sha]))
  while not entered.is_set():await asyncio.sleep(.001)
  # This code can only run while the expensive verifier is outside this loop.
  assert not task.done();finish.set();assert (await task)[sha]
 asyncio.run(exercise())


def test_model_download_streams_and_retains_exact_bytes_and_ranges(tmp_path,monkeypatch):
 from fastapi import FastAPI
 from fastapi.testclient import TestClient
 from api.routers import tagger
 from api.tagger_core import artifact_store
 s,sha=fixture(tmp_path);monkeypatch.setattr(artifact_store,'default_store',lambda:s)
 real_read=Path.read_bytes
 def no_eager_read(path,*a,**k):
  if path==s.path_for(sha):raise AssertionError('model must not be buffered on the event loop')
  return real_read(path,*a,**k)
 monkeypatch.setattr(Path,'read_bytes',no_eager_read)
 app=FastAPI();app.include_router(tagger.router)
 with TestClient(app) as client:
  result=client.get(f'/api/tagger/artifact/{sha}');assert result.status_code==200;assert result.content==b'correct model'
  assert result.headers['content-disposition'].endswith(f'{sha}.safetensors"')
  result=client.get(f'/api/tagger/artifact/{sha}',headers={'range':'bytes=0-3'})
  assert result.status_code==206 and result.content==b'corr'
