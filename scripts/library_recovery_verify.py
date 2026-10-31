#!/usr/bin/env python3
"""Restore a library backup in an owned Unix-socket-only PostgreSQL, never a live DB."""
import argparse
import hashlib
import json
import os
import re
from pathlib import Path
import shutil
import shlex
import subprocess
import tempfile
import time
import psycopg2
from psycopg2 import sql

BINARY=Path('/usr/lib/postgresql/18/bin')


def emit(stage,**values):print(json.dumps({'stage':stage,**values}),flush=True)


def prepare_roles(query,security):
    flags={'super':'SUPERUSER','inherit':'INHERIT','createrole':'CREATEROLE','createdb':'CREATEDB','replication':'REPLICATION','bypassrls':'BYPASSRLS'}
    for role in security['roles']:
        if role['name']=='library_restore':raise ValueError('Source role conflicts with private bootstrap')
        if role['name']!=security['bootstrap_role']:
            query(sql.SQL('CREATE ROLE {} NOLOGIN').format(sql.Identifier(role['name'])))
        options=[]
        for key,keyword in flags.items():
            if not isinstance(role[key],bool):raise ValueError('Invalid role capability')
            options.append(keyword if role[key] else 'NO'+keyword)
        query(sql.SQL('ALTER ROLE {} WITH {} NOLOGIN').format(sql.Identifier(role['name']),sql.SQL(' ').join(map(sql.SQL,options))))
    # Replace fresh-cluster builtin memberships with the captured grantors/options.
    current=query("SELECT r.rolname,m.rolname,g.rolname FROM pg_auth_members a JOIN pg_roles r ON r.oid=a.roleid JOIN pg_roles m ON m.oid=a.member JOIN pg_roles g ON g.oid=a.grantor")
    for role,member,grantor in current:
        query(sql.SQL('REVOKE {} FROM {} GRANTED BY {}').format(sql.Identifier(role),sql.Identifier(member),sql.Identifier(grantor)))
    for role,member,grantor,admin,inherit,can_set in security['memberships']:
        query(sql.SQL('GRANT {} TO {} WITH ADMIN {}, INHERIT {}, SET {} GRANTED BY {}').format(
            sql.Identifier(role),sql.Identifier(member),sql.SQL(str(admin).upper()),sql.SQL(str(inherit).upper()),sql.SQL(str(can_set).upper()),sql.Identifier(grantor)))


def apply_security(root,query,call,security):
    allowed={'TABLE','VIEW','MATERIALIZED VIEW','SEQUENCE','FOREIGN TABLE','SCHEMA','FUNCTION','PROCEDURE','AGGREGATE','DOMAIN','TYPE'}
    database=query('SELECT current_database()')[0][0]
    query(sql.SQL('ALTER DATABASE {} OWNER TO {}').format(sql.Identifier(database),sql.Identifier(security['database_owner'])))
    for item in security['owners']:
        if item['kind'] not in allowed:raise ValueError('Unknown ownership kind')
        identity=sql.Identifier(item['schema'],item['name']) if item['schema'] else sql.Identifier(item['name'])
        if 'arguments' in item:identity=sql.SQL('{}({})').format(identity,sql.SQL(item['arguments']))
        query(sql.SQL('ALTER {} {} OWNER TO {}').format(sql.SQL(item['kind']),identity,sql.Identifier(item['owner'])))
    listing=call('pg_restore','-l',root/'permissions.dump').stdout.decode()
    selected=[]
    for line in listing.splitlines():
        fields=line.split()
        if len(fields)>4 and (fields[3]=='ACL' or fields[3:5]==['DEFAULT','ACL']):selected.append(line)
    path=root/'restore/permissions.list';path.write_text('\n'.join(selected)+'\n');path.chmod(0o600)
    call('pg_restore','--no-tablespaces','--exit-on-error','--use-list',path,'-d','postgres',root/'permissions.dump')
    if security['schema_revision'] is not None and query('SELECT version_num FROM kura_alembic_version')[0][0]!=security['schema_revision']:
        raise ValueError('Ownership snapshot belongs to another schema revision')
    return len(security['owners'])


def content_address(item):
    """The SHA256 a backed-up file must hold when its name is its content address: the
    manifest's address_sha256, else the rendition (rendition/xx/<sha>.<ext>) or collector blob
    (collector/originals/xx/<sha>) name, so backups made before #154 are checked too."""
    if item.get('address_sha256'):return item['address_sha256']
    parts=Path(item['path']).parts
    stem=parts[-1].split('.',1)[0] if parts else ''
    if len(parts)>=3 and parts[0]=='rendition' and re.fullmatch('[0-9a-f]{64}',stem):return stem
    if len(parts)>=4 and parts[:2]==('collector','originals') and re.fullmatch('[0-9a-f]{64}',parts[-1]):return parts[-1]
    return None


def verify(root,scratch):
    root=root.resolve();manifest=json.loads((root/'manifest.json').read_text())
    security_bytes=(root/'permissions.manifest.json').read_bytes()
    security_checksum=json.loads((root/'permissions.checksum.json').read_text())
    if len(security_bytes)!=security_checksum['bytes'] or hashlib.sha256(security_bytes).hexdigest()!=security_checksum['sha256']:
        raise ValueError('Ownership metadata readback differs')
    security=json.loads(security_bytes)
    if security['format']!='kura-library-security-v1':raise ValueError('Ownership/ACL snapshot required')
    if manifest['format']!='kura-library-recovery-v1' or root.stat().st_uid!=os.getuid():raise ValueError('Not an owned library backup')
    if (root/'restore-proof.json').exists():raise ValueError('Restore proof already exists')
    if shutil.disk_usage(root).free<manifest['database_bytes']*2+64*1024**3:raise ValueError('Restore filesystem reserve reached')
    scratch.mkdir(mode=0o700,parents=True,exist_ok=True)
    if shutil.disk_usage(scratch).free<100*1024**3:raise ValueError('Scratch requires 100GiB free')
    work=root/'restore';work.mkdir(mode=0o700)
    cache=Path(tempfile.mkdtemp(prefix='library-restore-',dir=scratch));(cache/'sort').mkdir()
    sock=work/'socket';sock.mkdir();env={k:v for k,v in os.environ.items() if not k.startswith('PG')}
    env.update(PGHOST=str(sock),PGPORT='65444',PGUSER='library_restore',PGDATABASE='postgres')
    def call(name,*args):return subprocess.run([str(BINARY/name),*map(str,args)],env=env,capture_output=True,check=True)
    def connection():return psycopg2.connect(host=str(sock),port=65444,user='library_restore',dbname='postgres')
    def query(statement,params=None):
        c=connection();c.autocommit=True
        try:
            with c.cursor() as q:q.execute(statement,params);return q.fetchall() if q.description else []
        finally:c.close()
    running=False;success=False;started=time.monotonic()
    options=f"-k {shlex.quote(str(sock))} -p 65444 -c listen_addresses='' -c shared_buffers=256MB -c maintenance_work_mem=512MB -c work_mem=64MB -c max_parallel_maintenance_workers=0 -c max_parallel_workers_per_gather=0 -c wal_level=minimal -c max_wal_senders=0 -c wal_compression=zstd -c max_wal_size=2GB -c autovacuum=off"
    try:
        emit('verify artifact readback')
        for item in manifest['artifacts']+[security['archive']]:
            p=root/item['path']
            if not p.resolve().is_relative_to(root):raise ValueError('Artifact path escapes backup')
            with p.open('rb') as f:digest=hashlib.file_digest(f,'sha256').hexdigest()
            if digest!=item['sha256'] or p.stat().st_size!=item['bytes']:raise ValueError('Artifact readback differs')
        for i,item in enumerate(manifest['media']):
            p=root/'media'/item['path']
            if not p.resolve().is_relative_to((root/'media').resolve()):raise ValueError('Media path escapes backup')
            with p.open('rb') as f:digest=hashlib.file_digest(f,'sha256').hexdigest()
            if digest!=item['sha256'] or p.stat().st_size!=item['bytes']:raise ValueError('Media readback differs')
            # and a content-addressed file holds the bytes its address (the restored DB's key) names (#154)
            if content_address(item) not in (None,digest):raise ValueError(f"Media {item['path']} does not hold its content address")
            if i%5000==0:emit('media readback',files=i,total=len(manifest['media']))
        locale=manifest['database_locale']
        if locale['provider']!='c':raise ValueError('Unsupported database locale provider')
        call('initdb','-D',work/'data','-X',cache/'wal','-A','trust','-U',security['bootstrap_role'],
             '--encoding='+locale['encoding'],'--lc-collate='+locale['collate'],'--lc-ctype='+locale['ctype'])
        call('pg_ctl','-D',work/'data','-l',work/'postgres.log','-o',options,'-w','start');running=True
        bootstrap=psycopg2.connect(host=str(sock),port=65444,user=security['bootstrap_role'],dbname='postgres')
        bootstrap.autocommit=True
        try:
            with bootstrap.cursor() as q:
                q.execute('SHOW data_directory')
                if Path(q.fetchone()[0]).resolve()!=(work/'data').resolve():raise ValueError('Unexpected bootstrap database')
                q.execute('CREATE ROLE library_restore LOGIN SUPERUSER')
        finally:bootstrap.close()
        if query('SELECT current_user')[0][0]!='library_restore' or Path(query('SHOW data_directory')[0][0]).resolve()!=(work/'data').resolve():raise ValueError('Unexpected private database')
        available={r[0] for r in query('SELECT name FROM pg_available_extensions')}
        if not set(manifest['extensions'])<=available:raise ValueError('Private host lacks required extensions')
        prepare_roles(query,security)
        location=str(cache/'sort').replace("'","''")
        query(f"CREATE TABLESPACE restore_sort LOCATION '{location}'")
        query("ALTER SYSTEM SET temp_tablespaces='restore_sort'")
        query("ALTER SYSTEM SET temp_file_limit='32GB'")
        query('SELECT pg_reload_conf()')
        emit('restore complete database')
        digest=hashlib.sha256();n=0;last=time.monotonic()
        with (work/'restore-errors.log').open('wb') as errors:
            proc=subprocess.Popen([str(BINARY/'pg_restore'),'--no-owner','--no-privileges','--no-tablespaces','--exit-on-error','-d','postgres'],stdin=subprocess.PIPE,stdout=subprocess.DEVNULL,stderr=errors,env=env)
            try:
                with (root/'database.dump').open('rb') as inp:
                    while chunk:=inp.read(4*1024**2):
                        if shutil.disk_usage(cache).free<64*1024**3:raise ValueError('Private restore scratch reserve reached')
                        digest.update(chunk);n+=len(chunk);proc.stdin.write(chunk)
                        if time.monotonic()-last>30:emit('database restore stream',archive_bytes=n);last=time.monotonic()
                proc.stdin.close()
                if proc.wait()!=0:raise ValueError('Private pg_restore failed; private error log retained')
            finally:
                if proc.poll() is None:proc.terminate();proc.wait()
        if digest.hexdigest()!=manifest['database_sha256'] or n!=manifest['archive_bytes']:raise ValueError('Database archive readback differs')
        emit('restore ownership and ACLs')
        ownership_count=apply_security(root,query,call,security)
        emit('reopen restored database')
        call('pg_ctl','-D',work/'data','-m','fast','-w','stop');running=False
        call('pg_ctl','-D',work/'data','-l',work/'postgres.log','-o',options,'-w','start');running=True
        for item in manifest['tables']:
            rows=query(sql.SQL('SELECT count(*) FROM {}.{}').format(sql.Identifier(item['schema']),sql.Identifier(item['table'])))[0][0]
            if rows!=item['rows']:raise ValueError('Restored table count differs')
        constraints=query("SELECT n.nspname,c.relname,k.conname,k.contype::text,k.convalidated FROM pg_constraint k JOIN pg_class c ON c.oid=k.conrelid JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname !~ '^pg_' AND n.nspname<>'information_schema' ORDER BY 1,2,3")
        if [list(r) for r in constraints]!=manifest['constraints']:raise ValueError('Restored constraints differ')
        for item in manifest['samples']:
            r=query('SELECT encode(sha256(data),\'hex\'),encode(sha256(thumbnail),\'hex\') FROM post WHERE id=%s',(item['id'],))[0]
            if list(r)!=item['hashes']:raise ValueError('Restored image or thumbnail differs')
        result={'format':'kura-library-restore-proof-v1','database_restore_verified':True,'reopened_after_clean_shutdown':True,
                'database_sha256':digest.hexdigest(),'tables_verified':len(manifest['tables']),'constraints_verified':len(constraints),
                'ownership_objects_restored':ownership_count,'role_memberships_restored':len(security['memberships']),'blob_samples_verified':len(manifest['samples']),'media_files_verified':len(manifest['media']),
                'seconds':round(time.monotonic()-started,1),'scope':manifest['scope']}
        with (root/'restore-proof.json').open('x') as out:os.fchmod(out.fileno(),0o600);json.dump(result,out,indent=2);out.flush();os.fsync(out.fileno())
        success=True;emit('library recovery verification complete',**result);return result
    finally:
        if running:call('pg_ctl','-D',work/'data','-m','fast','-w','stop')
        if success:shutil.rmtree(work);shutil.rmtree(cache)
        else:emit('private verification preserved',path=str(work))


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--backup',type=Path,required=True);p.add_argument('--scratch',type=Path,required=True);a=p.parse_args()
    verify(a.backup,a.scratch)
