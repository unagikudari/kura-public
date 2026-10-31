#!/usr/bin/env python3
"""Publish a scheduled recovery snapshot only after independent restoration."""
import argparse
from datetime import datetime,timezone
import fcntl
import json
import os
from pathlib import Path
import subprocess
import sys


def stamp():
    return datetime.now(timezone.utc).isoformat()


def write_receipt(path,value):
    from importlib.util import module_from_spec,spec_from_file_location
    spec=spec_from_file_location('backup_status',Path(__file__).parents[1]/'web/scripts/library-backup-status.py')
    module=module_from_spec(spec);spec.loader.exec_module(module)
    module.write_report(path,value)


PUBLISH='''import json,sys
from pathlib import Path
source,target=map(Path,sys.argv[1:])
if source.parent!=target.parent or not source.name.startswith('.incomplete-') or target.exists():raise ValueError('Invalid publication target')
manifest=json.loads((source/'manifest.json').read_text());proof=json.loads((source/'restore-proof.json').read_text())
if (proof.get('format')!='kura-library-restore-proof-v1' or proof.get('database_restore_verified') is not True
 or proof.get('reopened_after_clean_shutdown') is not True or proof.get('database_sha256')!=manifest.get('database_sha256')
 or proof.get('tables_verified')!=len(manifest['tables']) or proof.get('constraints_verified')!=len(manifest['constraints'])
 or proof.get('media_files_verified')!=len(manifest['media'])):raise ValueError('Matching restore proof required')
source.rename(target)
import os
fd=os.open(target.parent,os.O_RDONLY|os.O_DIRECTORY);os.fsync(fd);os.close(fd)
print('verified snapshot published')
'''


def run(host,root,state,*,backup=None,publish=None):
    if not root.is_absolute() or root==Path('/data') or not root.is_relative_to('/data') or '..' in root.parts:
        raise ValueError('Recovery root must be beneath /data')
    state.mkdir(parents=True,exist_ok=True,mode=0o700)
    with (state/'job.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        snapshot=datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')
        pending=root/('.incomplete-'+snapshot)
        value={'format':'kura-library-backup-job-v1','state':'running','snapshot':snapshot,
               'started_at':stamp(),'finished_at':None}
        receipt=state/'job.json';write_receipt(receipt,value)
        try:
            if backup is None:
                from scripts.library_recovery_backup import backup as perform_backup
                backup=lambda h,d:perform_backup(h,d,scratch=root.parent/'.kura-recovery-work')
            if publish is None:
                from scripts.library_recovery_backup import remote
                publish=lambda source,target:remote(host,PUBLISH,source,target)
            backup(host,pending)
            publish(pending,root/snapshot)
        except BaseException as error:
            write_receipt(receipt,value|{'state':'failed','finished_at':stamp(),'error_type':type(error).__name__})
            # Exceptions from DB/SSH can embed credentials or archive contents.
            print(json.dumps({'stage':'scheduled recovery failed','error_type':type(error).__name__}),flush=True)
            raise RuntimeError('Scheduled recovery failed; previous published snapshots retained') from None
        write_receipt(receipt,value|{'state':'verified','finished_at':stamp()})
        return value|{'state':'verified','finished_at':stamp()}


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--host',default='trainer');parser.add_argument('--root',type=Path,default=Path('/data/kura-recovery'))
    parser.add_argument('--state',type=Path,default=Path.home()/'.local/state/kura-library-backup')
    args=parser.parse_args()
    from dotenv import dotenv_values
    os.environ.update({k:v for k,v in dotenv_values(str(Path.home()/'kura/api/env'),interpolate=False).items() if v is not None})
    try:
        run(args.host,args.root,args.state)
    finally:
        # Refresh the small UI receipt after either outcome. A failed collector
        # does not erase the job receipt; its regular timer will retry.
        subprocess.run([sys.executable,str(Path(__file__).parents[1]/'web/scripts/library-backup-status.py')],
                       stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=40,check=False)
