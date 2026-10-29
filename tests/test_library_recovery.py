"""Real complete-schema restoration checks table counts, constraints, blobs and file readback."""
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from collections import namedtuple
import psycopg2
import pytest
from test_collector_next import postgres  # noqa:F401
from test_pool_search_candidates import search_database  # noqa:F401
from scripts.collector_next_backup import pg_environment
from scripts import library_recovery_verify as verifier
from scripts.library_recovery_backup import WRITER


def test_complete_private_database_restoration_reopens_and_matches_source(search_database,monkeypatch):
    env=pg_environment(search_database)
    with psycopg2.connect(host=env['PGHOST'],port=env['PGPORT'],user=env['PGUSER'],password=env['PGPASSWORD'],dbname=env['PGDATABASE']) as c:
        with c.cursor() as q:
            q.execute('CREATE TABLE post(id int PRIMARY KEY,data bytea,thumbnail bytea)')
            q.execute('INSERT INTO post VALUES(1,%s,%s),(2,NULL,NULL)',(b'original-image-bytes',b'poster-bytes'))
            q.execute('CREATE ROLE recovery_owner NOLOGIN; CREATE ROLE recovery_reader NOLOGIN')
            q.execute('GRANT CREATE ON SCHEMA public TO recovery_owner; ALTER TABLE post OWNER TO recovery_owner; GRANT SELECT ON post TO recovery_reader')
            q.execute("CREATE FUNCTION recovery_identity() RETURNS text LANGUAGE sql SECURITY DEFINER AS 'SELECT current_user::text'")
            q.execute('ALTER FUNCTION recovery_identity() OWNER TO recovery_owner; REVOKE ALL ON FUNCTION recovery_identity() FROM PUBLIC; GRANT EXECUTE ON FUNCTION recovery_identity() TO recovery_reader')
            q.execute('GRANT recovery_owner TO recovery_reader WITH INHERIT FALSE, SET FALSE')
            q.execute("SELECT datlocprovider::text,datcollate,datctype,pg_encoding_to_char(encoding) FROM pg_database WHERE datname=current_database()")
            provider,collate,ctype,encoding=q.fetchone()
            q.execute("SELECT n.nspname,c.relname,k.conname,k.contype::text,k.convalidated FROM pg_constraint k JOIN pg_class c ON c.oid=k.conrelid JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname !~ '^pg_' AND n.nspname<>'information_schema' ORDER BY 1,2,3")
            constraints=[list(r) for r in q]
            q.execute("SELECT rolname FROM pg_roles WHERE rolname !~ '^pg_'");roles=[r[0] for r in q]
    c.close()
    usage=namedtuple('usage','total used free');monkeypatch.setattr(verifier.shutil,'disk_usage',lambda p:usage(1024**4,0,1024**4))
    with tempfile.TemporaryDirectory(prefix='library-recovery-test-') as temporary:
        root=Path(temporary)/'backup';root.mkdir();scratch=Path(temporary)/'scratch';scratch.mkdir()
        subprocess.run(['/usr/lib/postgresql/18/bin/pg_dump','-Fc','--no-owner','--no-privileges'],env=env,stdout=(root/'database.dump').open('wb'),check=True)
        with (root/'database.dump').open('rb') as f:digest=hashlib.file_digest(f,'sha256').hexdigest()
        media=root/'media/legacy/posts/example.webm';media.parent.mkdir(parents=True);media.write_bytes(b'filesystem-video-bytes')
        manifest={'format':'kura-library-recovery-v1','database_bytes':1024,'database_locale':dict(provider=provider,collate=collate,ctype=ctype,encoding=encoding),
                  'database_sha256':digest,'archive_bytes':(root/'database.dump').stat().st_size,'artifacts':[],
                  'media':[{'path':'legacy/posts/example.webm','bytes':media.stat().st_size,'sha256':hashlib.sha256(media.read_bytes()).hexdigest()}],
                  'extensions':['plpgsql'],'roles':roles,'tables':[{'schema':'public','table':'post','rows':2}],
                  'constraints':constraints,'samples':[{'id':1,'hashes':[hashlib.sha256(b'original-image-bytes').hexdigest(),hashlib.sha256(b'poster-bytes').hexdigest()]}],
                  'scope':'isolated fixture'}
        from scripts.library_recovery_security import capture
        def local_send(host,path,source):
            data=source.read();path.write_bytes(data);path.chmod(0o600)
            return {'path':path.name,'bytes':len(data),'sha256':hashlib.sha256(data).hexdigest()}
        with psycopg2.connect(host=env['PGHOST'],port=env['PGPORT'],user=env['PGUSER'],password=env['PGPASSWORD'],dbname=env['PGDATABASE']) as db:
            db.set_session(isolation_level='REPEATABLE READ',readonly=True)
            with db.cursor() as q:
                q.execute('SELECT pg_export_snapshot()');snapshot=q.fetchone()[0]
                capture(q,snapshot,env,None,root,local_send)
        db.close()
        (root/'manifest.json').write_text(json.dumps(manifest))
        original_apply=verifier.apply_security
        def check_security(root,query,call,security):
            result=original_apply(root,query,call,security)
            assert query('SET ROLE recovery_reader; SELECT recovery_identity(),(SELECT count(*) FROM post)')[0]==('recovery_owner',2)
            assert query("SELECT a.inherit_option,a.set_option FROM pg_auth_members a JOIN pg_roles r ON r.oid=a.roleid JOIN pg_roles m ON m.oid=a.member WHERE r.rolname='recovery_owner' AND m.rolname='recovery_reader'")[0]==(False,False)
            return result
        monkeypatch.setattr(verifier,'apply_security',check_security)
        result=verifier.verify(root,scratch)
        assert result['database_restore_verified'] and result['reopened_after_clean_shutdown']
        assert result['tables_verified']==1 and result['media_files_verified']==1 and result['blob_samples_verified']==1
        assert not (root/'restore').exists() and not list(scratch.iterdir())


def test_independent_writer_is_private_atomic_and_refuses_overwrite(tmp_path):
    output=tmp_path/'artifact';payload=b'backup data'*1000
    result=subprocess.run([sys.executable,'-c',WRITER,str(output)],input=payload,capture_output=True,check=True)
    assert json.loads(result.stdout)=={'bytes':len(payload),'sha256':hashlib.sha256(payload).hexdigest()}
    assert output.read_bytes()==payload and output.stat().st_mode&0o777==0o600
    refused=subprocess.run([sys.executable,'-c',WRITER,str(output)],input=b'changed',capture_output=True)
    assert refused.returncode!=0 and output.read_bytes()==payload


def test_corrupt_media_refuses_before_starting_a_database(tmp_path,monkeypatch):
    root=tmp_path/'backup';root.mkdir();(root/'media').mkdir();(root/'media/bad').write_bytes(b'wrong')
    (root/'manifest.json').write_text(json.dumps({'format':'kura-library-recovery-v1','database_bytes':1,'artifacts':[],
       'media':[{'path':'bad','bytes':5,'sha256':hashlib.sha256(b'right').hexdigest()}]}))
    (root/'permissions.dump').write_bytes(b'ACL fixture')
    security={'format':'kura-library-security-v1','archive':{'path':'permissions.dump','bytes':11,'sha256':hashlib.sha256(b'ACL fixture').hexdigest()}}
    payload=json.dumps(security).encode();(root/'permissions.manifest.json').write_bytes(payload)
    (root/'permissions.checksum.json').write_text(json.dumps({'bytes':len(payload),'sha256':hashlib.sha256(payload).hexdigest()}))
    usage=namedtuple('usage','total used free');monkeypatch.setattr(verifier.shutil,'disk_usage',lambda p:usage(1024**4,0,1024**4))
    with pytest.raises(ValueError,match='Media readback differs'):verifier.verify(root,tmp_path/'scratch')
    assert not (root/'restore/data').exists() and not (root/'restore-proof.json').exists()


def test_media_inventory_uses_the_runtime_location_and_missing_byte_fallback(search_database):
    from scripts.library_recovery_backup import filesystem_rows
    env=pg_environment(search_database)
    with psycopg2.connect(host=env['PGHOST'],port=env['PGPORT'],user=env['PGUSER'],password=env['PGPASSWORD'],dbname=env['PGDATABASE']) as c:
        with c.cursor() as q:
            q.execute('CREATE TABLE post(id int,status text,"mime-type" text,media_location text,data bytea,thumbnail bytea)')
            q.execute("INSERT INTO post VALUES(1,'ok','image/webp','pg','a','a'),(2,'ok','video/mp4','hdd','a','a'),(3,'ok','image/jpeg','pg',NULL,'a'),(4,'ok','image/webp','pg','a',NULL),(5,'lost','image/jpeg','hdd',NULL,NULL)")
            rows=filesystem_rows(q)
            assert {r[0] for r in rows}=={2,3,4}
            assert {r[0]:bool(r[2]) for r in rows}=={2:False,3:False,4:True}


def test_backup_sources_need_no_release_pins(tmp_path):
    import json
    from scripts.library_recovery_backup import source_list
    repo = tmp_path / 'kura'; repo.mkdir()
    assert source_list(repo) == [('core', repo, 'HEAD')]                    # a public tree, core alone
    (repo / '.kura-extensions.json').write_text(json.dumps({'schema': 1, 'extensions': [{'id': 'notes', 'commit': 'c1'}]}))
    assert source_list(repo)[1:] == [('notes', tmp_path / 'kura-notes', 'c1')]  # what compose installed
    (repo / 'release').mkdir()
    (repo / 'release/extension_sources.json').write_text(json.dumps({'extensions': {'notes': {'commit': 'p1'}}}))
    assert source_list(repo)[1:] == [('notes', tmp_path / 'kura-notes', 'p1')]  # the pins win


def test_backup_sources_use_the_checkout_compose_recorded(tmp_path):
    import json
    from scripts.library_recovery_backup import source_list
    repo = tmp_path / 'kura'; repo.mkdir()
    elsewhere = tmp_path / 'src' / 'notes-ext'
    (repo / '.kura-extensions.json').write_text(json.dumps({'schema': 1, 'extensions': [
        {'id': 'notes', 'commit': 'c1', 'source': str(elsewhere)}]}))
    assert source_list(repo)[1:] == [('notes', elsewhere, 'c1')]
