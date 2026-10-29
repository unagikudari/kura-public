#!/usr/bin/env python3
"""Back up authoritative normalized claims to a mounted external filesystem."""
import argparse
from datetime import datetime,timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import psycopg2
from api.config import DATABASE_URL
from api.claim_storage.service import WRITER_LOCK_KEY
from scripts.collector_next_backup import pg_environment


def _external_destination(target,mount):
    if not mount.is_mount():raise ValueError('External backup mount is unavailable')
    target.parent.mkdir(parents=True,exist_ok=True)
    if target.parent.stat().st_dev!=mount.stat().st_dev or mount.stat().st_dev==Path.home().stat().st_dev:
        raise ValueError('Backup must remain on the declared external filesystem')
    if shutil.disk_usage(mount).free<64*1024**3:raise ValueError('Backup filesystem reserve reached')

def backup(target,mount):
    if target.suffix!='.dump' or target.exists() or target.with_suffix('.manifest.json').exists():
        raise ValueError('Choose a new .dump and manifest path')
    _external_destination(target,mount)
    env=pg_environment(DATABASE_URL)
    with psycopg2.connect(host=env['PGHOST'],port=env['PGPORT'],user=env['PGUSER'],
                          password=env['PGPASSWORD'],dbname=env['PGDATABASE']) as db:
        with db.cursor() as cursor:
            cursor.execute('SET LOCAL statement_timeout=10000')
            cursor.execute('SELECT pg_advisory_xact_lock_shared(%s)',(WRITER_LOCK_KEY,))
            cursor.execute('SELECT phase FROM claim_storage.migration')
            if cursor.fetchone()[0]!='active':raise ValueError('Normalized claims are not authoritative')
            # Normal writes remain concurrent. Ownership transitions wait until
            # this snapshot artifact is fully published or the backup fails.
            return _write_backup(target,env)


def _write_backup(target,env):
    started=datetime.now(timezone.utc).isoformat();temporary=None
    binary=Path('/usr/lib/postgresql/18/bin')
    try:
        with tempfile.NamedTemporaryFile(dir=target.parent,delete=False) as out:
            temporary=Path(out.name);os.fchmod(out.fileno(),0o600)
            subprocess.run([str(binary/'pg_dump'),'-Fc','--compress=zstd:3','--schema=claim_storage',
                '--exclude-table=claim_storage.legacy_claim_archive','--no-publications','--no-subscriptions','--lock-wait-timeout=2s'],
                env=env,stdout=out,stderr=subprocess.PIPE,check=True)
            out.flush();os.fsync(out.fileno())
        subprocess.run([str(binary/'pg_restore'),'-f','/dev/null',str(temporary)],capture_output=True,check=True)
        with temporary.open('rb') as stream:digest=hashlib.file_digest(stream,'sha256').hexdigest()
        manifest={'format':'kura-normalized-claims-backup-v1','kind':'normalized-claim-storage',
            'schema':'claim_storage','excluded_table':'claim_storage.legacy_claim_archive',
            'file':target.name,'bytes':temporary.stat().st_size,'sha256':digest,'started_at':started,
            'decompression_verified':True,'database_restore_verified':False}
        os.link(temporary,target)
        with target.with_suffix('.manifest.json').open('x') as out:
            os.fchmod(out.fileno(),0o600);json.dump(manifest,out,indent=2);out.write('\n');out.flush();os.fsync(out.fileno())
        fd=os.open(target.parent,os.O_RDONLY|os.O_DIRECTORY)
        try:os.fsync(fd)
        finally:os.close(fd)
        return {'output':str(target),'bytes':manifest['bytes'],'sha256':digest,'database_restore_verified':False}
    finally:
        if temporary is not None:temporary.unlink(missing_ok=True)


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--output',type=Path,required=True);p.add_argument('--mount',type=Path,required=True)
    args=p.parse_args();print(json.dumps(backup(args.output,args.mount)))


if __name__=='__main__':main()
