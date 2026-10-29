"""Routing, rollback and immutable assets use synthetic state; never live Serve."""
import copy,importlib.util,json
from pathlib import Path
import pytest
spec=importlib.util.spec_from_file_location('web_release',Path(__file__).parents[1]/'scripts/web-release.py')
m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
ORIGIN='https://example.invalid:8443'
def config():return {'Web':{'example.invalid:8443':{'Handlers':{'/':{'Proxy':'http://127.0.0.1:3000'},'/api':{'Proxy':'http://127.0.0.1:3000/api'},'/data/':{'Proxy':'http://127.0.0.1:3000/data/'},'/dashboards':{'Proxy':'http://127.0.0.1:8000/dashboards'}}},'other.invalid:123':{'Handlers':{'/':{'Proxy':'http://127.0.0.1:1234'}}}}}
def test_switch_and_failed_probe_rollback_only_owned_routes(monkeypatch):
 original=config();current=copy.deepcopy(original);calls=[]
 monkeypatch.setattr(m,'config',lambda:current)
 def set_route(origin,path,target):
  calls.append(path);current['Web']['example.invalid:8443']['Handlers'][path]={'Proxy':target}
 monkeypatch.setattr(m,'set_route',set_route)
 def fail(origin):raise ValueError('candidate refused')
 with pytest.raises(ValueError):m.switch(ORIGIN,3002,fail)
 assert current==original;assert set(calls)=={'/','/api','/data/'}
 m.switch(ORIGIN,3002,lambda origin:None)
 assert all(':3002' in v for v in m.routes(current,ORIGIN).values())
 assert current['Web']['other.invalid:123']==original['Web']['other.invalid:123']
 assert current['Web']['example.invalid:8443']['Handlers']['/dashboards']==original['Web']['example.invalid:8443']['Handlers']['/dashboards']
def test_direct_backend_bypass_is_rejected_before_any_switch():
 c=config();c['Web']['example.invalid:8443']['Handlers']['/api']['Proxy']='http://127.0.0.1:8001/api'
 with pytest.raises(ValueError):m.routes(c,ORIGIN)
def source(tmp_path,version,asset,content):
 root=tmp_path/'source';build=root/'web/build';(build/'client/_app/immutable').mkdir(parents=True,exist_ok=True)
 for p in (build/'client/_app/immutable').iterdir():p.unlink()
 (build/'client/_app/version.json').write_text(json.dumps({'version':version}))
 (build/'client/_app/immutable'/asset).write_text(content)
 (root/'web/src/lib').mkdir(parents=True,exist_ok=True)
 (root/'web/src/lib/build_info.ts').write_text('export const BUILD_INFO = '+json.dumps({'commit':'abcdef12','dirty':False})+' as const;')
 return root
def test_release_copy_is_immutable_and_keeps_previous_hashed_assets(tmp_path,monkeypatch):
 monkeypatch.setattr(m,'command',lambda *a: '' if 'status' in a else 'abcdef123456')
 root=tmp_path/'releases';src=source(tmp_path,'1','old.js','old');first,first_manifest=m.prepare(src,root,Path('/fixture/node'));m.atomic(first/'release.json',{**first_manifest,'activated':True})
 src=source(tmp_path,'2','new.js','new');second,manifest=m.prepare(src,root,Path('/fixture/node'))
 assert (first/'build/client/_app/immutable/old.js').read_text()=='old'
 assert (second/'build/client/_app/immutable/old.js').read_text()=='old'
 assert manifest['assets']==['build/client/_app/immutable/new.js']
 with pytest.raises(ValueError):m.prepare(src,root,Path('/fixture/node'))
 src=source(tmp_path,'3','old.js','collision')
 with pytest.raises(ValueError):m.prepare(src,root,Path('/fixture/node'))
 assert not list((root/'releases').glob('.preparing-*'))
def test_failed_warmup_never_changes_ingress(monkeypatch):
 monkeypatch.setattr(m,'response',lambda url:(503,b'{}'))
 with pytest.raises(RuntimeError):m.warm(3002,'fixture',timeout=.01)
def test_the_build_must_carry_the_origin_it_is_released_at(tmp_path):
 # SvelteKit 3 / adapter-node 6 fix the origin at build time; ORIGIN at start is ignored
 b=tmp_path/'web/build';b.mkdir(parents=True)
 (b/'adapter-node.js').write_text('export const x = 1;\nexport const origin = "https://example.invalid:8443";\n')
 assert m.built_origin(tmp_path)==ORIGIN
 (b/'adapter-node.js').write_text('export const origin = undefined;\n')
 assert m.built_origin(tmp_path) is None
