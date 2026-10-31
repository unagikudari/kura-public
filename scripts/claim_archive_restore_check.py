#!/usr/bin/env python3
"""Verify claim archives in a private, resumable PostgreSQL; never restore into production."""
import argparse
import asyncio
import json
import os
from pathlib import Path
import re
import shutil
import shlex
import subprocess
import tempfile
import time
import asyncpg
from scripts.claim_storage_activate import verify_backup

BINARY=Path('/usr/lib/postgresql/18/bin')
COPY=re.compile(rb'^COPY ([\w".]+) \(([^)]+)\) FROM stdin;')


def reference_ids(archive,relation,max_ids=2000000):
    """Read compressed COPY records, not the restored heap twice on a slow HDD."""
    process=subprocess.Popen([str(BINARY/'pg_restore'),'--section=data','-f','-',str(archive)],stdout=subprocess.PIPE,stderr=subprocess.PIPE)
    posts=set();tags=set();rows=0;target=False;copying=False
    try:
        for line in process.stdout:
            if copying:
                if line.rstrip(b'\r\n')==b'\\.':copying=False;target=False;continue
                if target:
                    fields=line.split(b'\t',2)
                    posts.add(int(fields[0]));tags.add(int(fields[1]));rows+=1
                    if len(posts)>max_ids or len(tags)>max_ids:raise ValueError('Reference identity budget exceeded')
                    if rows%10000000==0:print(json.dumps({'stage':'read archive identities','rows':rows}),flush=True)
                continue
            match=COPY.match(line)
            if match:
                copying=True;name=match[1].replace(b'"',b'').decode('ascii');target=name==relation
                if target and [v.strip() for v in match[2].split(b',')][:2]!=[b'post_id',b'tag_id']:
                    raise ValueError('Unsupported claim COPY column order')
        if process.wait()!=0:raise ValueError('Archive identity stream failed')
        return posts,tags,rows
    finally:
        if process.poll() is None:process.kill();process.wait()
        process.stdout.close();process.stderr.close()


def save_state(root,state):
    temporary=root/'verification-state.new'
    with temporary.open('w') as out:
        os.fchmod(out.fileno(),0o600);json.dump(state,out);out.flush();os.fsync(out.fileno())
    os.replace(temporary,root/'verification-state.json')
    fd=os.open(root,os.O_RDONLY|os.O_DIRECTORY)
    try:os.fsync(fd)
    finally:os.close(fd)


async def fill_parents(socket,posts,tags):
    connection=await asyncpg.connect(user='claim_restore',database='postgres',host=str(socket),port=65443)
    try:
        await connection.execute('CREATE TABLE IF NOT EXISTS public.post(id int PRIMARY KEY);CREATE TABLE IF NOT EXISTS public.tag(id int PRIMARY KEY);CREATE TEMP TABLE restore_identity(id int)')
        for table,values in (('post',posts),('tag',tags)):
            await connection.execute('TRUNCATE restore_identity')
            await connection.copy_records_to_table('restore_identity',records=((v,) for v in sorted(values)))
            await connection.execute(f'INSERT INTO public.{table} SELECT id FROM restore_identity ORDER BY id ON CONFLICT DO NOTHING')
    finally:await connection.close()


def check(archive,work_root,*,resume=None,scratch_root=None):
    manifest=verify_backup(archive,expected='any');normalized=manifest.get('kind')=='normalized-claim-storage'
    relation='claim_storage.claim' if normalized else 'public.post_tag_actor_claim'
    reserve_gib=100 if normalized else 128
    if shutil.disk_usage(work_root).free<reserve_gib*1024**3:raise ValueError(f'Restore filesystem needs {reserve_gib}GiB free')
    started=time.monotonic();cache=None
    if resume is None:
        root=Path(tempfile.mkdtemp(prefix='restore-check-',dir=work_root))
        if scratch_root is not None:
            if shutil.disk_usage(scratch_root).free<100*1024**3:raise ValueError('Scratch requires 64GiB reserve plus 36GiB budget')
            cache=Path(tempfile.mkdtemp(prefix='restore-cache-',dir=scratch_root));(cache/'sort').mkdir()
        state={'format':1,'archive_sha256':manifest['sha256'],'data_complete':False,'cache_root':str(cache) if cache else None}
        save_state(root,state)
    else:
        root=resume.resolve()
        if root.is_symlink() or not root.name.startswith('restore-check-') or root.parent!=work_root.resolve() or root.stat().st_uid!=os.getuid():raise ValueError('Not an owned private restore job')
        state=json.loads((root/'verification-state.json').read_text())
        if state.get('archive_sha256')!=manifest['sha256']:raise ValueError('Resume archive differs')
        if state.get('cache_root'):
            cache=Path(state['cache_root'])
            if scratch_root is None or cache.parent!=scratch_root.resolve() or not cache.name.startswith('restore-cache-') or cache.stat().st_uid!=os.getuid():raise ValueError('Unexpected private scratch directory')
            (cache/'sort').mkdir(exist_ok=True)
    socket=root/'socket';socket.mkdir(exist_ok=True)
    env={k:v for k,v in os.environ.items() if not k.startswith('PG')}
    env.update(PGHOST=str(socket),PGPORT='65443',PGUSER='claim_restore',PGDATABASE='postgres')
    def call(name,*args):return subprocess.run([str(BINARY/name),*map(str,args)],env=env,check=True,capture_output=True)
    def sql(query):return call('psql','-X','-v','ON_ERROR_STOP=1','-Atc',query).stdout.decode().strip()
    options=f"-k {shlex.quote(str(socket))} -p 65443 -c listen_addresses='' -c shared_buffers=128MB -c maintenance_work_mem=512MB -c work_mem=128MB -c max_parallel_maintenance_workers=0 -c max_parallel_workers_per_gather=0 -c wal_level=minimal -c max_wal_senders=0 -c wal_compression=zstd -c max_wal_size=2GB -c autovacuum=off"
    running=False;success=False
    try:
        if not (root/'data/PG_VERSION').exists():
            args=['-D',root/'data','-A','trust','-U','claim_restore','--no-locale']
            if cache is not None:args+=['-X',cache/'wal']
            call('initdb',*args)
        # A resumed job must be stopped first; do not attach to an unknown live server.
        status=subprocess.run([str(BINARY/'pg_ctl'),'-D',str(root/'data'),'status'],capture_output=True)
        if status.returncode==0:raise ValueError('Stop the private restore server before resuming')
        call('pg_ctl','-D',root/'data','-l',root/'log','-o',options,'-w','start');running=True
        if sql('SELECT current_user')!='claim_restore' or Path(sql('SHOW data_directory')).resolve()!=(root/'data').resolve():raise ValueError('Unexpected verification database')
        if cache is not None:
            old=sql("SELECT pg_tablespace_location(oid) FROM pg_tablespace WHERE spcname='restore_sort'")
            if old and Path(old).resolve()!=(cache/'sort').resolve():sql('DROP TABLESPACE restore_sort');old=''
            if not old:
                location=str(cache/'sort').replace("'","''")
                sql(f"CREATE TABLESPACE restore_sort LOCATION '{location}'")
            sql("ALTER SYSTEM SET temp_tablespaces='restore_sort'");sql("ALTER SYSTEM SET temp_file_limit='32GB'");sql('SELECT pg_reload_conf()')
        if not state['data_complete']:
            print(json.dumps({'stage':'restore schema and data','job':str(root)}),flush=True)
            for section in ('pre-data','data'):call('pg_restore',*(['--clean','--if-exists'] if section=='pre-data' and resume is not None else []),'--no-owner','--no-privileges','--exit-on-error','--section='+section,'-d','postgres',archive)
            state['data_complete']=True;save_state(root,state)
        print(json.dumps({'stage':'collect identities from archive, not HDD heap'}),flush=True)
        posts,tags,expected_rows=reference_ids(archive,relation)
        asyncio.run(fill_parents(socket,posts,tags))
        print(json.dumps({'stage':'build indexes and validate foreign keys'}),flush=True)
        # Only post-data objects (constraints/indexes/triggers) are reset on
        # retry. No restored data table or row is removed.
        call('pg_restore','--clean','--if-exists','--no-owner','--no-privileges','--exit-on-error','--section=post-data','-d','postgres',archive)
        print(json.dumps({'stage':'reopen restored database'}),flush=True)
        call('pg_ctl','-D',root/'data','-m','fast','-w','stop');running=False
        call('pg_ctl','-D',root/'data','-l',root/'log','-o',options,'-w','start');running=True
        result=json.loads(sql(f"SELECT json_build_object('claims',(SELECT count(*) FROM {relation}),'referenced_posts',(SELECT count(*) FROM post),'referenced_tags',(SELECT count(*) FROM tag),'validated_foreign_keys',(SELECT count(*) FROM pg_constraint WHERE conrelid='{relation}'::regclass AND contype='f' AND convalidated),'relation_bytes',pg_total_relation_size('{relation}'))"))
        if result['claims']!=expected_rows or result['validated_foreign_keys']!=(3 if normalized else 2):raise ValueError('Restored rows or foreign keys differ')
        result.update(storage_kind='normalized' if normalized else 'legacy',reopened_after_clean_shutdown=True)
        if normalized:
            result['source_phase']=sql('SELECT phase FROM claim_storage.migration');result['actor_rows']=int(sql('SELECT count(*) FROM claim_storage.actor'))
            if result['source_phase']!='active':raise ValueError('Restored normalized claims are not authoritative')
            result['legacy_archive_present']=sql("SELECT to_regclass('claim_storage.legacy_claim_archive') IS NOT NULL")=='t'
            if result['legacy_archive_present']:raise ValueError('Normalized backup includes redundant archive')
        if verify_backup(archive,expected='any')['sha256']!=manifest['sha256']:raise ValueError('Archive changed during verification')
        result.update(format='kura-claim-archive-restore-v2',archive_sha256=manifest['sha256'],database_restore_verified=True,
            seconds=round(time.monotonic()-started,1),scope='Claim archive, indexes, FKs and clean reopen against reconstructed identity stubs; not full media/library restoration')
        success=True;return result
    finally:
        if running:call('pg_ctl','-D',root/'data','-m','fast','-w','stop')
        if success:
            shutil.rmtree(root)
            if cache is not None:shutil.rmtree(cache)
        else:print(json.dumps({'paused_private_restore':str(root),'data_complete':state['data_complete']}),flush=True)


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--archive',type=Path,required=True);p.add_argument('--work-root',type=Path,required=True);p.add_argument('--output',type=Path,required=True)
    p.add_argument('--resume',type=Path);p.add_argument('--scratch-root',type=Path)
    args=p.parse_args()
    if args.output.exists():p.error('Output exists')
    result=check(args.archive,args.work_root,resume=args.resume,scratch_root=args.scratch_root)
    with args.output.open('x') as out:
        os.fchmod(out.fileno(),0o600);json.dump(result,out,indent=2);out.write('\n');out.flush();os.fsync(out.fileno())
    print(json.dumps(result),flush=True)


if __name__=='__main__':main()
