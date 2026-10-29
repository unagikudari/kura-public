#!/usr/bin/env python3
"""Preserve application ownership, role capabilities/membership and official ACL archive."""
import argparse
import io
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
from datetime import datetime,timezone


def capture(cursor,snapshot,environment,host,destination,send):
    cursor.execute('SELECT rolname FROM pg_roles WHERE oid=10');bootstrap_role=cursor.fetchone()[0]
    cursor.execute('SELECT pg_get_userbyid(datdba) FROM pg_database WHERE datname=current_database()');database_owner=cursor.fetchone()[0]
    cursor.execute("SELECT rolname,rolsuper,rolinherit,rolcreaterole,rolcreatedb,rolreplication,rolbypassrls,rolcanlogin FROM pg_roles WHERE rolname !~ '^pg_' ORDER BY rolname")
    roles=[dict(zip(('name','super','inherit','createrole','createdb','replication','bypassrls','login'),r)) for r in cursor]
    if any(r['name']=='library_restore' for r in roles):raise ValueError('Reserved private restore role already exists in source')
    cursor.execute("SELECT r.rolname,m.rolname,g.rolname,a.admin_option,a.inherit_option,a.set_option FROM pg_auth_members a JOIN pg_roles r ON r.oid=a.roleid JOIN pg_roles m ON m.oid=a.member JOIN pg_roles g ON g.oid=a.grantor ORDER BY a.oid")
    memberships=[list(r) for r in cursor]
    owners=[]
    cursor.execute("SELECT nspname,pg_get_userbyid(nspowner) FROM pg_namespace WHERE nspname !~ '^pg_' AND nspname<>'information_schema'")
    owners.extend({'kind':'SCHEMA','schema':None,'name':r[0],'owner':r[1]} for r in cursor)
    cursor.execute("""SELECT n.nspname,c.relname,c.relkind::text,pg_get_userbyid(c.relowner) FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
      WHERE c.relkind IN ('r','p','v','m','S','f') AND n.nspname !~ '^pg_' AND n.nspname<>'information_schema'
      AND NOT EXISTS(SELECT 1 FROM pg_depend d WHERE d.classid='pg_class'::regclass AND d.objid=c.oid AND d.deptype='e')
      ORDER BY CASE WHEN c.relkind='S' THEN 1 ELSE 0 END,c.oid""")
    kinds={'r':'TABLE','p':'TABLE','v':'VIEW','m':'MATERIALIZED VIEW','S':'SEQUENCE','f':'FOREIGN TABLE'}
    owners.extend({'kind':kinds[r[2]],'schema':r[0],'name':r[1],'owner':r[3]} for r in cursor)
    cursor.execute("""SELECT n.nspname,p.proname,pg_get_function_identity_arguments(p.oid),p.prokind::text,pg_get_userbyid(p.proowner)
      FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace WHERE n.nspname !~ '^pg_' AND n.nspname<>'information_schema'
      AND NOT EXISTS(SELECT 1 FROM pg_depend d WHERE d.classid='pg_proc'::regclass AND d.objid=p.oid AND d.deptype='e') ORDER BY p.oid""")
    owners.extend({'kind':{'p':'PROCEDURE','a':'AGGREGATE'}.get(r[3],'FUNCTION'),'schema':r[0],'name':r[1],'arguments':r[2],'owner':r[4]} for r in cursor)
    cursor.execute("""SELECT n.nspname,t.typname,t.typtype::text,pg_get_userbyid(t.typowner) FROM pg_type t JOIN pg_namespace n ON n.oid=t.typnamespace
      WHERE n.nspname !~ '^pg_' AND n.nspname<>'information_schema' AND t.typelem=0 AND (t.typtype IN ('d','e','r') OR (t.typtype='c' AND EXISTS(SELECT 1 FROM pg_class c WHERE c.oid=t.typrelid AND c.relkind='c')))
      AND NOT EXISTS(SELECT 1 FROM pg_depend d WHERE d.classid='pg_type'::regclass AND d.objid=t.oid AND d.deptype='e') ORDER BY t.oid""")
    owners.extend({'kind':'DOMAIN' if r[2]=='d' else 'TYPE','schema':r[0],'name':r[1],'owner':r[3]} for r in cursor)
    # Refuse unsupported application-owned SQL object kinds rather than silently
    # losing their ownership. Extension-owned operators/classes are installed by PG.
    for catalog,namespace in [('pg_collation','collnamespace'),('pg_conversion','connamespace'),('pg_ts_config','cfgnamespace'),('pg_ts_dict','dictnamespace'),('pg_opclass','opcnamespace'),('pg_opfamily','opfnamespace'),('pg_operator','oprnamespace')]:
        cursor.execute(f"SELECT count(*) FROM {catalog} o JOIN pg_namespace n ON n.oid=o.{namespace} WHERE n.nspname !~ '^pg_' AND n.nspname<>'information_schema' AND NOT EXISTS(SELECT 1 FROM pg_depend d WHERE d.classid='{catalog}'::regclass AND d.objid=o.oid AND d.deptype='e')")
        if cursor.fetchone()[0]:raise ValueError('Application SQL ownership requires an additional object-kind adapter')
    cursor.execute("SELECT to_regclass('kura_alembic_version') IS NOT NULL")
    has_ledger=cursor.fetchone()[0]
    revision=None
    if has_ledger:cursor.execute('SELECT version_num FROM kura_alembic_version');revision=cursor.fetchone()[0]
    with tempfile.TemporaryDirectory(prefix='kura-permissions-') as tmp:
        with (Path(tmp)/'errors').open('wb') as error:
            proc=subprocess.Popen(['/usr/lib/postgresql/18/bin/pg_dump','-Fc','--schema-only','--no-publications','--no-subscriptions','--lock-wait-timeout=2s','--snapshot='+snapshot],env=environment,stdout=subprocess.PIPE,stderr=error)
            try:artifact=send(host,destination/'permissions.dump',proc.stdout)
            finally:
                if proc.poll() is None:proc.wait()
            if proc.returncode:raise ValueError('Ownership/ACL snapshot export failed')
    result={'format':'kura-library-security-v1','captured_at':datetime.now(timezone.utc).isoformat(),'database_owner':database_owner,'bootstrap_role':bootstrap_role,
            'roles':roles,'memberships':memberships,'owners':owners,'schema_revision':revision,'archive':artifact}
    payload=json.dumps(result,indent=2).encode()
    send(host,destination/'permissions.manifest.json',io.BytesIO(payload))
    checksum={'bytes':len(payload),'sha256':hashlib.sha256(payload).hexdigest()}
    send(host,destination/'permissions.checksum.json',io.BytesIO(json.dumps(checksum).encode()))
    return result


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--host',default='trainer');p.add_argument('--destination',type=Path,required=True);a=p.parse_args()
    from dotenv import dotenv_values
    os.environ.update({k:v for k,v in dotenv_values(str(Path.home()/'kura/api/env'),interpolate=False).items() if v is not None})
    import psycopg2
    from api.config import DATABASE_URL_SYNC
    from scripts.collector_next_backup import pg_environment
    from scripts.library_recovery_backup import stream
    with psycopg2.connect(DATABASE_URL_SYNC) as db:
        db.set_session(isolation_level='REPEATABLE READ',readonly=True)
        with db.cursor() as q:
            q.execute('SELECT pg_export_snapshot()');snapshot=q.fetchone()[0]
            result=capture(q,snapshot,pg_environment(DATABASE_URL_SYNC),a.host,a.destination,stream)
    print(json.dumps({'security_objects':len(result['owners']),'roles':len(result['roles']),'memberships':len(result['memberships'])}))
