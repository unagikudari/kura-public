"""Routing, rollback and immutable assets use synthetic state; never live Serve."""
import copy,importlib.util,json,os,sys
from pathlib import Path
import pytest
spec=importlib.util.spec_from_file_location('web_release',Path(__file__).parents[1]/'scripts/web-release.py')
m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
ORIGIN='https://example.invalid:8443'
def config():return {'Web':{'example.invalid:8443':{'Handlers':{'/':{'Proxy':'http://127.0.0.1:3000'},'/api':{'Proxy':'http://127.0.0.1:3000/api'},'/data/':{'Proxy':'http://127.0.0.1:3000/data/'},'/dashboards':{'Proxy':'http://127.0.0.1:8000/dashboards'}}},'other.invalid:123':{'Handlers':{'/':{'Proxy':'http://127.0.0.1:1234'}}}}}
def test_switch_and_failed_probe_rollback_only_owned_routes(monkeypatch,tmp_path):
 original=config();current=copy.deepcopy(original);calls=[]
 monkeypatch.setattr(m,'config',lambda:current)
 def set_route(origin,path,target):
  calls.append(path);current['Web']['example.invalid:8443']['Handlers'][path]={'Proxy':target}
 monkeypatch.setattr(m,'set_route',set_route)
 def fail(origin):raise ValueError('candidate refused')
 with pytest.raises(ValueError):m.switch(ORIGIN,3002,fail,tmp_path)
 assert current==original;assert set(calls)=={'/','/api','/data/'}
 m.switch(ORIGIN,3002,lambda origin:None,tmp_path)
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
 monkeypatch.setattr(m,'validate_node',lambda node:'v24.22.1')
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

@pytest.mark.parametrize('version',['v22.23.3','v24.14.1','v24.22.0','v26.12.0','v25.99.0','v24.22.1-rc.1','invalid'])
def test_insecure_or_unknown_runtime_is_rejected(tmp_path,monkeypatch,version):
 node=tmp_path/'node';node.touch()
 monkeypatch.setattr(m.subprocess,'check_output',lambda *a,**k:version+'\n')
 with pytest.raises(ValueError):m.validate_node(node)

@pytest.mark.parametrize('version',['v22.23.4','v24.22.1','v24.23.0','v26.12.1'])
def test_fixed_runtime_is_accepted(tmp_path,monkeypatch,version):
 node=tmp_path/'node';node.touch()
 monkeypatch.setattr(m.subprocess,'check_output',lambda *a,**k:version+'\n')
 assert m.validate_node(node)==version

def test_runtime_rejection_precedes_release_or_activation_mutations(tmp_path,monkeypatch):
 def reject(node):raise ValueError('old runtime')
 monkeypatch.setattr(m,'validate_node',reject)
 root=tmp_path/'not-created'
 with pytest.raises(ValueError,match='old runtime'):m.prepare(tmp_path,root,Path('/old/node'))
 with pytest.raises(ValueError,match='old runtime'):m.activate(tmp_path,root,tmp_path,{'node':'/old/node'},ORIGIN)
 assert not root.exists()
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
def edge(tmp_path,port=3003):
 d=tmp_path/'edge';d.mkdir();(d/'upstream.conf').write_text(f'upstream kura_web {{ server 127.0.0.1:{port}; }}\n');return d
def test_an_origin_off_serve_switches_the_nginx_edge_and_rolls_back(tmp_path,monkeypatch):
 d=edge(tmp_path);calls=[]
 monkeypatch.setattr(m,'config',lambda:{'Web':{}})
 def fail(origin):raise ValueError('candidate refused')
 monkeypatch.setattr(m,'settled',lambda probe,origin:probe(origin))
 # What the origin actually serves follows upstream.conf only while a reload takes effect.
 live={'port':3003};versions={3002:'new',3003:'old'};took=[True]
 def command(*a):
  calls.append(a[-2:])
  if a[-1]=='reload' and took[0]:live['port']=m.edge_port(d)
  return ''
 monkeypatch.setattr(m,'command',command)
 monkeypatch.setattr(m,'response',lambda url:(200,json.dumps({'version':versions[live['port'] if url.startswith(ORIGIN) else int(url.split(':')[2].split('/')[0])]}).encode()))
 with pytest.raises(ValueError):m.switch(ORIGIN,3002,fail,d)
 assert m.edge_port(d)==3003 and live['port']==3003 and calls.count(('-s','reload'))==2
 assert m.switch(ORIGIN,3002,lambda origin:None,d)=={'/':'http://127.0.0.1:3003'}
 assert m.edge_port(d)==3002 and m.current_port(ORIGIN,d)==3002
 m.restore(ORIGIN,{'/':'http://127.0.0.1:3003'},d);assert m.edge_port(d)==3003
 # A rollback reload nginx did not take leaves the new release live: say so, and never
 # report the config file's port as the live one (the next release would stop that slot).
 took[0]=False;live['port']=3002
 with pytest.raises(RuntimeError):m.switch(ORIGIN,3002,fail,d)
 with pytest.raises(ValueError):m.current_port(ORIGIN,d)
def test_the_ingress_must_be_exactly_one_of_serve_and_edge(tmp_path,monkeypatch):
 d=edge(tmp_path)
 monkeypatch.setattr(m,'config',config)
 with pytest.raises(ValueError):m.ingress(ORIGIN,d)
 assert m.ingress(ORIGIN,tmp_path/'none')=='serve'
 monkeypatch.setattr(m,'config',lambda:{'Web':{}})
 with pytest.raises(ValueError):m.ingress(ORIGIN,tmp_path/'none')
 (d/'upstream.conf').write_text('upstream kura_web { server 127.0.0.1:8001; }\n')
 with pytest.raises(ValueError):m.edge_port(d)
def test_settling_waits_out_the_reload_then_gives_up(monkeypatch):
 seen=[]
 def probe(origin):
  seen.append(1)
  if len(seen)<3:raise ValueError('Public version differs')
 monkeypatch.setattr(m.time,'sleep',lambda s:None)
 m.settled(probe,ORIGIN);assert len(seen)==3
 with pytest.raises(ValueError):m.settled(lambda o:(_ for _ in ()).throw(ValueError('x')),ORIGIN,timeout=0)


def cli_fixture(tmp_path,monkeypatch):
    src=tmp_path/'source';(src/'web').mkdir(parents=True)
    root=tmp_path/'release-state';calls=[]
    monkeypatch.setattr(sys,'argv',['web-release','--source',str(src),'--root',str(root),'--origin',ORIGIN,'--node','/fixture/node'])
    monkeypatch.setattr(m,'validate_node',lambda node:'v24.22.1')
    monkeypatch.setattr(m,'built_origin',lambda source:ORIGIN)
    def prepare(*args):calls.append('prepare');return root/'candidate',{'fixture':True}
    monkeypatch.setattr(m,'prepare',prepare)
    monkeypatch.setattr(m,'activate',lambda *args:calls.append('activate'))
    monkeypatch.setattr(m,'command',lambda *args:pytest.fail('preflight invoked an external command'))
    return src,root,calls

@pytest.mark.parametrize('kind',['missing','directory','symlink','dangling_symlink','fifo','wrong_owner','public_mode','executable','empty','oversized'])
def test_probe_env_refusal_precedes_cli_release_mutations(tmp_path,monkeypatch,kind):
    src,root,calls=cli_fixture(tmp_path,monkeypatch);env=src/'web/.env.server'
    if kind=='directory':env.mkdir()
    elif kind in ('symlink','dangling_symlink'):
        target=tmp_path/'private-env'
        if kind=='symlink':target.write_bytes(b'fixture');target.chmod(0o600)
        env.symlink_to(target)
    elif kind=='fifo':os.mkfifo(env,0o600)
    elif kind!='missing':
        env.write_bytes(b'' if kind=='empty' else b'x'*(65537 if kind=='oversized' else 7));env.chmod(0o600)
        if kind=='public_mode':env.chmod(0o644)
        if kind=='executable':env.chmod(0o700)
        if kind=='wrong_owner':monkeypatch.setattr(m.os,'getuid',lambda:env.stat().st_uid+1)
    with pytest.raises(ValueError,match='Private probe environment'):m.main()
    assert calls==[] and not root.exists()

@pytest.mark.parametrize('size',[1,65536])
def test_valid_probe_env_enters_normal_cli_release_flow_without_reading_values(tmp_path,monkeypatch,size):
    src,root,calls=cli_fixture(tmp_path,monkeypatch);env=src/'web/.env.server'
    env.write_bytes(b'x'*size);env.chmod(0o600)
    # These are deliberately not valid credentials: preflight is metadata-only.
    def no_read(*args,**kwargs):pytest.fail('preflight read environment contents')
    monkeypatch.setattr(Path,'read_text',no_read);monkeypatch.setattr(Path,'read_bytes',no_read)
    m.main()
    assert calls==['prepare','activate'] and (root/'deploy.lock').is_file()
