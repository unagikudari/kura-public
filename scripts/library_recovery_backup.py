#!/usr/bin/env python3
"""Snapshot the library and required media to an independent host; launch private restoration."""
import argparse
from contextlib import nullcontext
from datetime import datetime,timezone
import hashlib
import io
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tarfile
import tempfile
import time
import uuid
import psycopg2
from psycopg2 import sql

# CLI is an operator tool. Load the running deployment's configuration before
# importing config; do not source shell code or print environment values.


def source_list(repo):
    """(name, checkout, revision) of core and the extensions it runs: the release pins when
    this tree has them, else what compose_extensions installed, else core alone. The
    checkout is the one compose recorded, else ../kura-<id>."""
    pins=repo/'release/extension_sources.json';state=repo/'.kura-extensions.json'
    installed={e['id']:e for e in json.loads(state.read_text())['extensions']} if state.exists() else {}
    if pins.exists():extensions={n:s['commit'] for n,s in json.loads(pins.read_text())['extensions'].items()}
    else:extensions={n:e['commit'] for n,e in installed.items()}
    checkout=lambda n:Path(installed[n]['source']) if installed.get(n,{}).get('source') else repo.parent/('kura-'+n)
    return [('core',repo,'HEAD')]+[(n,checkout(n),c) for n,c in extensions.items()]

def emit(stage,**values):print(json.dumps({'stage':stage,**values}),flush=True)


def remote(host,code,*args,**kwargs):
    command=shlex.join(['python3','-c',code,*map(str,args)])
    return subprocess.run(['ssh','-o','BatchMode=yes',host,command],check=True,capture_output=True,**kwargs)


WRITER="""import sys,os,hashlib,json
from pathlib import Path
p=Path(sys.argv[1]);part=p.with_name(p.name+'.part');digest=hashlib.sha256();size=0
with part.open('xb') as out:
 os.fchmod(out.fileno(),0o600)
 while b:=sys.stdin.buffer.read(4*1024**2):out.write(b);digest.update(b);size+=len(b)
 out.flush();os.fsync(out.fileno())
os.link(part,p);part.unlink()
fd=os.open(p.parent,os.O_RDONLY|os.O_DIRECTORY);os.fsync(fd);os.close(fd)
print(json.dumps({'bytes':size,'sha256':digest.hexdigest()}))
"""


def stream(host,path,source):
    command=shlex.join(['python3','-c',WRITER,str(path)])
    proc=subprocess.Popen(['ssh','-o','BatchMode=yes',host,command],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
    digest=hashlib.sha256();size=0;last=time.monotonic()
    try:
        while chunk:=source.read(4*1024**2):
            digest.update(chunk);size+=len(chunk);proc.stdin.write(chunk)
            if time.monotonic()-last>30:emit('artifact transfer',artifact=path.name,bytes=size);last=time.monotonic()
        proc.stdin.close();out=proc.stdout.read();error=proc.stderr.read()
        if proc.wait()!=0:raise ValueError('Independent artifact receiver failed')
        actual=json.loads(out)
        if actual!={'bytes':size,'sha256':digest.hexdigest()}:raise ValueError('Artifact transport digest differs')
        return {'path':path.name,**actual}
    finally:
        if proc.poll() is None:proc.terminate();proc.wait()


class DigestReader:
    def __init__(self,source):self.source=source;self.digest=hashlib.sha256()
    def read(self,size):
        value=self.source.read(size);self.digest.update(value);return value


def filesystem_rows(cursor):
    # Match get_post_content: display media is either still in the PG columns or a
    # rendition file (docs/notes/rendition-store-ideal.md); only posts with neither
    # need their filesystem fallback copied.
    cursor.execute("SELECT to_regclass('public.post_rendition') IS NOT NULL")
    if cursor.fetchone()[0]:
        view="(data IS NOT NULL OR EXISTS (SELECT 1 FROM post_rendition r WHERE r.post_id=post.id AND r.kind='view'))"
        thumb="(thumbnail IS NOT NULL OR EXISTS (SELECT 1 FROM post_rendition r WHERE r.post_id=post.id AND r.kind='thumb'))"
    else:
        view,thumb="data IS NOT NULL","thumbnail IS NOT NULL"
    cursor.execute(f"SELECT id,\"mime-type\",(media_location='pg' AND {view}),{thumb} FROM post WHERE status='ok' AND (media_location IS DISTINCT FROM 'pg' OR NOT {view} OR NOT {thumb})")
    return cursor.fetchall()


def backup(host,destination,*,scratch=None):
    from api.config import DATABASE_URL_SYNC,DATA_DIR
    from api.services import files
    from api.collector.storage import content_path
    from scripts.collector_next_backup import pg_environment
    from api.claim_storage.service import WRITER_LOCK_KEY
    repo=Path(__file__).resolve().parents[1]
    manifest={'format':'kura-library-recovery-v1','started_at':datetime.now(timezone.utc).isoformat(),
      'scope':'Current library DB, PG display media, rendition files, filesystem fallback content, experimental originals and deployment/source archives. External optional original archive and staging are not mirrored. DB login roles/ACL require deployment setup; no live services are started.',
      'artifacts':[],'media':[],'missing_regenerable_thumbnails':0}
    with tempfile.TemporaryDirectory(prefix='kura-library-metadata-') as tmp,psycopg2.connect(DATABASE_URL_SYNC) as db:
        tmp=Path(tmp);db.set_session(isolation_level='REPEATABLE READ',readonly=True)
        with db.cursor() as q:
            q.execute('SELECT pg_advisory_xact_lock_shared(%s)',(WRITER_LOCK_KEY,))
            q.execute('SELECT pg_export_snapshot()');snapshot=q.fetchone()[0]
            q.execute('SELECT pg_database_size(current_database())');manifest['database_bytes']=q.fetchone()[0]
            q.execute("SELECT datlocprovider::text,datcollate,datctype,pg_encoding_to_char(encoding) FROM pg_database WHERE datname=current_database()")
            provider,collate,ctype,encoding=q.fetchone();manifest['database_locale']=dict(provider=provider,collate=collate,ctype=ctype,encoding=encoding)
            q.execute("SELECT rolname FROM pg_roles WHERE rolname !~ '^pg_' ORDER BY rolname");manifest['roles']=[r[0] for r in q]
            q.execute('SELECT extname FROM pg_extension');manifest['extensions']=[r[0] for r in q]
            q.execute("SELECT n.nspname,c.relname FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE c.relkind IN ('r','p') AND n.nspname !~ '^pg_' AND n.nspname<>'information_schema' ORDER BY 1,2")
            tables=q.fetchall();manifest['tables']=[];emit('snapshot exact table counts',tables=len(tables))
            for schema,table in tables:
                q.execute(sql.SQL('SELECT count(*) FROM {}.{}').format(sql.Identifier(schema),sql.Identifier(table)))
                manifest['tables'].append({'schema':schema,'table':table,'rows':q.fetchone()[0]})
            q.execute("SELECT n.nspname,c.relname,k.conname,k.contype::text,k.convalidated FROM pg_constraint k JOIN pg_class c ON c.oid=k.conrelid JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname !~ '^pg_' AND n.nspname<>'information_schema' ORDER BY 1,2,3")
            manifest['constraints']=[list(r) for r in q]
            q.execute("SELECT id,encode(sha256(data),'hex'),encode(sha256(thumbnail),'hex') FROM post WHERE data IS NOT NULL AND octet_length(data)<=8388608 ORDER BY id DESC LIMIT 12")
            manifest['samples']=[{'id':r[0],'hashes':list(r[1:])} for r in q]
            pending=[]
            from api.services import originals
            # On a node without the archive disk, an archived original is a link to bytes on
            # another machine: it is recorded, not copied (the archive's independent copy is
            # made on the node that has the disk). On the archive node this is unchanged.
            # The reference that restores the link: post id, layout and address, one line each
            # in archived-originals.tsv next to the manifest (codex review of 1.269.0).
            manifest.setdefault('archived_originals_not_copied',0)
            archived_refs=tempfile.TemporaryFile(mode='w+b')
            for pid,mime,inline,thumb in filesystem_rows(q):
                if not inline:
                    path=files.content_path(pid,mime);loc=originals.classify(path)
                    if originals.remote() and loc is not None and loc.kind!='local':
                        manifest['archived_originals_not_copied']+=1
                        archived_refs.write(f"{pid}\t{loc.kind}\t{loc.sha or loc.legacy_name}\t{path.name}\n".encode())
                    else:pending.append((path,'legacy',pid,True))
                if not thumb:
                    p=files.thumbnail_path(pid)
                    if p.is_file():pending.append((p,'legacy',pid,False))
                    else:manifest['missing_regenerable_thumbnails']+=1
            custom=files.storage_directory('custom-thumbnail')
            if custom.is_dir():pending.extend((p,'legacy',None,False) for p in custom.rglob('*') if p.is_file())
            q.execute("SELECT to_regclass('collector_next.blob') IS NOT NULL")
            if q.fetchone()[0]:
                root=Path(os.environ.get('KURA_COLLECTOR_NEXT_ROOT',str(Path.home()/'.local/share/kura-collector-next')))
                q.execute("SELECT encode(sha256,'hex') FROM collector_next.blob")
                pending.extend((content_path(root,bytes.fromhex(r[0])),'collector',None,True) for r in q)
            else:root=None
            from api.services import renditions
            q.execute("SELECT to_regclass('public.rendition_object') IS NOT NULL")
            if q.fetchone()[0]:
                q.execute("SELECT sha256,mime FROM rendition_object")
                pending.extend((renditions.path_for(bytes(r[0]),r[1]),'rendition',None,True) for r in q)
            required_bytes=sum(p.stat().st_size for p,kind,pid,required in pending if required)
            setup="""import sys,os,shutil,json
from pathlib import Path
p=Path(sys.argv[1]);mount=Path('/data')
if not mount.is_mount() or not p.is_relative_to(mount) or mount.stat().st_dev==Path.home().stat().st_dev:raise ValueError('Independent HDD mount unavailable')
if shutil.disk_usage(mount).free<int(sys.argv[2]):raise ValueError('Recovery filesystem reserve reached')
p.parent.mkdir(mode=0o700,parents=True,exist_ok=True);p.mkdir(mode=0o700);(p/'media').mkdir(mode=0o700)
print('independent backup directory ready')
"""
            remote(host,setup,destination,manifest['database_bytes']*3+required_bytes*2+64*1024**3)
            from scripts.library_recovery_security import capture
            capture(q,snapshot,pg_environment(DATABASE_URL_SYNC),host,destination,stream)
            emit('copy required filesystem media',files=len(pending),bytes=required_bytes)
            tar_command=shlex.join(['tar','-x','--no-same-owner','--no-same-permissions','-f','-','-C',str(destination/'media')])
            receiver=subprocess.Popen(['ssh','-o','BatchMode=yes',host,tar_command],stdin=subprocess.PIPE,stdout=subprocess.DEVNULL,stderr=subprocess.PIPE)
            try:
                with tarfile.open(fileobj=receiver.stdin,mode='w|',bufsize=1024**2) as archive:
                    for i,(p,kind,pid,required) in enumerate(pending):
                        relative=Path(kind)/(p.relative_to(DATA_DIR) if kind=='legacy' else p.relative_to(renditions.ROOT) if kind=='rendition' else p.relative_to(root))
                        lock=files.content_lock(pid) if pid is not None else nullcontext()
                        with lock,p.open('rb') as inp:
                            before=os.fstat(inp.fileno());reader=DigestReader(inp)
                            info=tarfile.TarInfo(relative.as_posix());info.size=before.st_size;info.mode=0o600
                            archive.addfile(info,reader);after=os.fstat(inp.fileno())
                            if (before.st_ino,before.st_size,before.st_mtime_ns)!=(after.st_ino,after.st_size,after.st_mtime_ns):raise ValueError('Source media changed while backing up')
                            manifest['media'].append({'path':relative.as_posix(),'bytes':before.st_size,'sha256':reader.digest.hexdigest()})
                        if i%1000==0:emit('filesystem media transfer',files=i,total=len(pending))
                receiver.stdin.close()
                if receiver.wait()!=0:raise ValueError('Independent media receiver failed')
            finally:
                if receiver.poll() is None:receiver.terminate();receiver.wait()
            remote(host,"import subprocess,sys;subprocess.run(['sync','-f',sys.argv[1]],check=True)",destination)
            # Restore files/config only into an owned backup folder, never over a deployment.
            settings=tmp/'settings.tar'
            with tarfile.open(settings,'w',dereference=True) as archive:
                for p,name in [(repo/'api/env','api/env'),(Path.home()/'.config/kura','config/kura')]:
                    if p.exists():archive.add(p,arcname=name)
            with settings.open('rb') as inp:manifest['artifacts'].append(stream(host,destination/settings.name,inp))
            sources=source_list(repo)
            manifest['source_commits']={}
            for name,path,revision in sources:
                commit=subprocess.check_output(['git','-C',str(path),'rev-parse',revision],text=True).strip();manifest['source_commits'][name]=commit
                archive=tmp/(name+'.tar');subprocess.run(['git','-C',str(path),'archive','--format=tar','-o',str(archive),commit],check=True)
                with archive.open('rb') as inp:manifest['artifacts'].append(stream(host,destination/archive.name,inp))
            emit('snapshot database dump')
            with (tmp/'dump-errors.log').open('wb') as errors:
                proc=subprocess.Popen(['/usr/lib/postgresql/18/bin/pg_dump','-Fc','--compress=zstd:1','--no-owner','--no-privileges','--no-publications','--no-subscriptions','--lock-wait-timeout=2s','--snapshot='+snapshot],env=pg_environment(DATABASE_URL_SYNC),stdout=subprocess.PIPE,stderr=errors)
                try:artifact=stream(host,destination/'database.dump',proc.stdout)
                finally:
                    if proc.poll() is None and sys.exc_info()[0] is not None:proc.terminate()
                if proc.wait()!=0:raise ValueError('Snapshot pg_dump failed')
            manifest['database_sha256']=artifact['sha256'];manifest['archive_bytes']=artifact['bytes']
        payload=json.dumps(manifest,indent=2).encode();stream(host,destination/'manifest.json',io.BytesIO(payload))
        if manifest.get('archived_originals_not_copied'):
            archived_refs.seek(0);stream(host,destination/'archived-originals.tsv',archived_refs)
        verify=Path(__file__).with_name('library_recovery_verify.py')
        with verify.open('rb') as inp:stream(host,destination/'verify.py',inp)
    emit('independent library backup complete',archive_bytes=manifest['archive_bytes'],media_files=len(manifest['media']))
    # ssh の command は相手の home で走るので、相手側のパスは home からの相対で書く。
    command=shlex.join(['.local/share/kura-api/venv/bin/python','-u',str(destination/'verify.py'),'--backup',str(destination),'--scratch',str(scratch or Path('.cache/kura-probes'))])
    subprocess.run(['ssh','-o','BatchMode=yes',host,command],check=True)


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--host',default='trainer');parser.add_argument('--destination',type=Path,required=True);a=parser.parse_args()
    from dotenv import dotenv_values
    os.environ.update({k:v for k,v in dotenv_values(str(Path.home()/'kura/api/env'),interpolate=False).items() if v is not None})
    backup(a.host,a.destination)
