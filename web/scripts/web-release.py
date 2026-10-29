#!/usr/bin/env python3
"""Prepare an immutable web release, warm it, then switch only Kura Serve routes."""
import argparse,fcntl,hashlib,json,os,re,shutil,subprocess,tempfile,time,urllib.error,urllib.request
from pathlib import Path
from urllib.parse import urlsplit
PORTS=(3002,3003)
PATHS=('/', '/api', '/data/')
class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self,*a,**k):return None
HTTP=urllib.request.build_opener(NoRedirect)
def command(*args):return subprocess.check_output(args,text=True,stderr=subprocess.PIPE)
def read(path):return json.loads(path.read_text())
def atomic(path,value):
    path.parent.mkdir(mode=0o700,parents=True,exist_ok=True)
    fd,name=tempfile.mkstemp(prefix='.pending-',dir=path.parent)
    try:
        with os.fdopen(fd,'w') as out:os.fchmod(out.fileno(),0o600);json.dump(value,out);out.flush();os.fsync(out.fileno())
        os.replace(name,path)
    finally:
        if os.path.exists(name):os.unlink(name)
def response(url):
    try:r=HTTP.open(url,timeout=5)
    except urllib.error.HTTPError as e:r=e
    with r:return r.status,r.read(1024*1024)
def routes(config,origin):
    host=urlsplit(origin).netloc
    handlers=config.get('Web',{}).get(host,{}).get('Handlers',{})
    root=handlers.get('/',{}).get('Proxy','').rstrip('/')
    if root not in [f'http://127.0.0.1:{p}' for p in (3000,*PORTS)]:raise ValueError('Unknown web ingress')
    selected={}
    for path,handler in handlers.items():
        if path=='/' or any(path==p or path.startswith(p+'/') for p in ('/api','/data','/m')):
            expected=root+('' if path=='/' else path)
            if handler.get('Proxy','').rstrip('/')!=expected.rstrip('/'):raise ValueError('Ingress bypasses private web boundary')
            selected[path]=handler['Proxy']
    if any(path not in selected for path in PATHS):raise ValueError('Expected Kura routes missing')
    return selected
def set_route(origin,path,target):
    command('tailscale','serve','--bg','--yes',f'--https={urlsplit(origin).port or 443}',f'--set-path={path}',target)
def config():return json.loads(command('tailscale','serve','status','--json'))
def switch(origin,port,probe):
    before=routes(config(),origin)
    try:
        for path in before:set_route(origin,path,f'http://127.0.0.1:{port}'+('' if path=='/' else path))
        after=routes(config(),origin)
        if any(urlsplit(v).port!=port for v in after.values()):raise ValueError('Ingress switch did not settle')
        probe(origin)
    except BaseException:
        # Keep both processes alive even if a route or rollback fails. Never
        # strand Serve pointing at a stopped candidate, or reset other services.
        for path,target in before.items():set_route(origin,path,target)
        if routes(config(),origin)!=before:raise RuntimeError('Ingress rollback requires inspection')
        raise
    return before

def prepare(source,root,node):
    build=source/'web/build';version=read(build/'client/_app/version.json')['version']
    if not re.fullmatch('[A-Za-z0-9_.-]{1,80}',version):raise ValueError('Invalid build version')
    commit=command('git','-C',str(source),'rev-parse','--short=12','HEAD').strip()
    metadata=(source/'web/src/lib/build_info.ts').read_text()
    info=json.loads(metadata.split('export const BUILD_INFO = ',1)[1].split(' as const;',1)[0])
    if not commit.startswith(info['commit']) or info.get('dirty') or command('git','-C',str(source),'status','--porcelain','--untracked-files=no').strip():
        raise ValueError('Build must match a clean committed checkout')
    name=f'{commit}-{version}'
    release=root/'releases'/name
    if release.exists():raise ValueError('Release already exists; never overwrite live artifacts')
    release.parent.mkdir(mode=0o700,parents=True,exist_ok=True)
    stage=Path(tempfile.mkdtemp(prefix='.preparing-',dir=release.parent))
    try:
        shutil.copytree(build,stage/'build')
        (stage/'package.json').write_text('{"type":"module"}\n')
        immutable=Path('build/client/_app/immutable')
        assets=[str(p.relative_to(stage)) for p in (stage/immutable).rglob('*') if p.is_file()]
        # Only each release's own inventory, not its inherited files, is copied.
        # This bounds retained chunks to the three preceding release generations.
        active=read(root/'active.json').get('release') if (root/'active.json').exists() else None
        old=[]
        for previous in release.parent.iterdir():
            if previous.is_dir() and (previous/'release.json').is_file():
                record=read(previous/'release.json')
                if record.get('activated') or str(previous)==active:old.append(previous)
        old=sorted(old,key=lambda p:read(p/'release.json')['created_at'],reverse=True)[:3]
        for previous in old:
            for item in read(previous/'release.json')['assets']:
                rel=Path(item)
                if not rel.is_relative_to(immutable) or '..' in rel.parts:raise ValueError('Invalid retained asset path')
                src=previous/rel;dst=stage/rel
                if dst.exists():
                    if hashlib.sha256(src.read_bytes()).digest()!=hashlib.sha256(dst.read_bytes()).digest():raise ValueError('Immutable asset collision')
                else:dst.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(src,dst)
        manifest={'name':name,'commit':commit,'build_version':version,'node':str(node),'assets':assets,'created_at':time.time()}
        atomic(stage/'release.json',manifest);stage.rename(release)
        return release,manifest
    except BaseException:shutil.rmtree(stage,ignore_errors=True);raise

def warm(port,version,timeout=30):
    deadline=time.monotonic()+timeout
    while time.monotonic()<deadline:
        try:
            if response(f'http://127.0.0.1:{port}/login')[0]==200 and json.loads(response(f'http://127.0.0.1:{port}/_app/version.json')[1])['version']==version:
                if response(f'http://127.0.0.1:{port}/api/posts?limit=1')[0]!=401:raise ValueError('Candidate leaked anonymous library access')
                return
        except (OSError,ValueError):pass
        time.sleep(.2)
    raise RuntimeError('Candidate did not become ready; ingress unchanged')
def authenticated_probe(port,source,node):
    # Native Node reads the same private env file as systemd. Only the path is
    # in argv; the key and response body are never emitted or persisted.
    code="const r=await fetch('http://127.0.0.1:"+str(port)+"/api/posts?limit=1',{headers:{'x-api-key':process.env.KURA_API_KEY},signal:AbortSignal.timeout(5000)});await r.arrayBuffer();if(r.status!==200)process.exit(1);"
    env={k:v for k,v in os.environ.items() if k!='KURA_API_KEY'}
    subprocess.run([str(node),'--env-file='+str(source/'web/.env.server'),'--input-type=module','-e',code],
                   env=env,capture_output=True,timeout=10,check=True)
def live_probe(origin,version):
    if response(origin+'/')[0]!=303 or response(origin+'/api/posts?limit=1')[0]!=401 or response(origin+'/data/nonexistent.jpg')[0]!=401:raise ValueError('Private ingress check failed')
    if json.loads(response(origin+'/_app/version.json')[1])['version']!=version:raise ValueError('Public version differs')
def activate(source,root,release,manifest,origin):
    state=root/'active.json';old=read(state) if state.exists() else None
    current=routes(config(),origin)['/']
    active_port=urlsplit(current).port
    if old and old['port']!=active_port:raise ValueError('Receipt and ingress differ')
    port=PORTS[1] if active_port==PORTS[0] else PORTS[0]
    # Do not terminate uploads/streams on a previous generation just to reuse a
    # slot. The operator can deploy after it drains, retaining the active release.
    if command('ss','-Htn','state','established',f'( sport = :{port} )').strip():raise RuntimeError('Inactive slot still has live connections')
    for record in (root/'releases').glob('*/release.json'):
        value=read(record)
        if value.get('port')==port:command('systemctl','--user','stop',f"kura-web-release@{value['name']}.service")
    if command('ss','-Hltn',f'( sport = :{port} )').strip():raise RuntimeError('Inactive slot listener is not available')
    manifest={**manifest,'port':port,'origin':origin}
    atomic(release/'release.json',manifest)
    envdir=Path.home()/'.config/kura-web-releases';envdir.mkdir(mode=0o700,parents=True,exist_ok=True)
    envfile=envdir/(manifest['name']+'.env')
    values={'NODE_BINARY':manifest['node'],'PORT':str(port),'HOST':'127.0.0.1','NODE_ENV':'production','ORIGIN':origin,
            'INTERNAL_API_URL':'http://127.0.0.1:8001','BODY_SIZE_LIMIT':'2149580800'}
    with envfile.open('x') as out:os.fchmod(out.fileno(),0o600);out.write(''.join(k+'='+json.dumps(v)+'\n' for k,v in values.items()))
    unit=f"kura-web-release@{manifest['name']}.service"
    command('systemctl','--user','start',unit)
    warm(port,manifest['build_version'])
    authenticated_probe(port,source,manifest['node'])
    command('systemctl','--user','enable',unit)
    before=switch(origin,port,lambda base:live_probe(base,manifest['build_version']))
    receipt={'format':'kura-web-release-v1','port':port,'release':str(release),'unit':unit,'commit':manifest['commit'],
             'build_version':manifest['build_version'],'switched_at':time.time(),'previous_routes':before,'previous':old}
    # Do not recursively accumulate historical receipts in the active pointer.
    if receipt['previous']:receipt['previous']={k:v for k,v in receipt['previous'].items() if k not in ('previous','previous_routes')}
    try:atomic(state,receipt)
    except BaseException:
        for path,target in before.items():set_route(origin,path,target)
        raise
    atomic(release/'release.json',{**manifest,'activated':True})
    if old:command('systemctl','--user','disable',old['unit']) # keep in-flight requests alive
    print(json.dumps({k:receipt[k] for k in ('port','unit','commit','build_version')}))
def built_origin(source):
    """SvelteKit 3 (adapter-node 6) fixes the origin at build time; ORIGIN at start is ignored."""
    for line in (source/'web/build/adapter-node.js').read_text().splitlines():
        if line.startswith('export const origin = '):
            value=line.split('=',1)[1].strip().rstrip(';')
            return None if value=='undefined' else json.loads(value)
    raise ValueError('build/adapter-node.js declares no origin')
def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--source',type=Path,required=True);p.add_argument('--origin',required=True)
    p.add_argument('--root',type=Path,default=Path.home()/'.local/share/kura-web');p.add_argument('--node',type=Path,default=Path(shutil.which('node') or ''))
    a=p.parse_args()
    if not a.node.is_file() or not a.node.is_absolute():raise ValueError('Absolute Node runtime required')
    origin=urlsplit(a.origin)
    if origin.scheme!='https' or not origin.hostname or origin.path or origin.username:raise ValueError('HTTPS origin required')
    if built_origin(a.source)!=a.origin.rstrip('/'):
        raise ValueError(f'the build is for origin {built_origin(a.source)!r}, not {a.origin!r}: run ORIGIN={a.origin.rstrip("/")} npm run build')
    a.root.mkdir(mode=0o700,parents=True,exist_ok=True)
    with (a.root/'deploy.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        release,manifest=prepare(a.source,a.root,a.node)
        activate(a.source,a.root,release,manifest,a.origin)
if __name__=='__main__':main()
