#!/usr/bin/env python3
"""Read recovery evidence without restoring, scanning media or exposing archives."""
import argparse,fcntl,json,os,re,shlex,subprocess,tempfile
from datetime import datetime,timezone
from pathlib import Path
FORMAT='kura-backup-status-v1'
def now():return datetime.now(timezone.utc).isoformat()
def read_json(path,limit=64*1024*1024):
    if path.is_symlink() or not path.is_file() or path.stat().st_size>limit:raise ValueError('invalid evidence file')
    return json.loads(path.read_text())
def probe(root,mount=None):
    if mount and (not mount.is_mount() or not root.resolve().is_relative_to(mount.resolve()) or mount.stat().st_dev==Path.home().stat().st_dev):
        raise ValueError('independent mount unavailable')
    candidates=sorted((p for p in root.iterdir() if p.is_dir() and not p.is_symlink() and re.fullmatch(r'\d{8}T\d{4,6}',p.name)),reverse=True)
    if not candidates:return {'state':'missing','backup':None}
    folder=candidates[0]
    manifest=read_json(folder/'manifest.json')
    if manifest.get('format')!='kura-library-recovery-v1':raise ValueError('unknown backup format')
    started=manifest['started_at'];datetime.fromisoformat(started)
    summary={'snapshot':folder.name,'created_at':started,'observed_at':now(),
             'archive_bytes':manifest['archive_bytes'],'media_files':len(manifest['media']),
             'verified_at':None,'tables_verified':0,'constraints_verified':0}
    verified=False
    if (folder/'restore-proof.json').exists():
        proof=read_json(folder/'restore-proof.json',65536)
        verified=(proof.get('format')=='kura-library-restore-proof-v1'
          and proof.get('database_restore_verified') is True and proof.get('reopened_after_clean_shutdown') is True
          and proof.get('database_sha256')==manifest.get('database_sha256')
          and isinstance(manifest.get('database_sha256'),str) and re.fullmatch('[0-9a-f]{64}',manifest['database_sha256']) is not None
          and proof.get('media_files_verified')==len(manifest['media'])
          and proof.get('tables_verified')==len(manifest['tables'])
          and proof.get('constraints_verified')==len(manifest['constraints']))
        if verified:
            summary.update(verified_at=datetime.fromtimestamp((folder/'restore-proof.json').stat().st_mtime,timezone.utc).isoformat(),
                           tables_verified=proof['tables_verified'],constraints_verified=proof['constraints_verified'])
    return {'state':'verified' if verified else 'unverified','backup':summary}
def write_report(path,report):
    path.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    fd,name=tempfile.mkstemp(prefix='.backup-status-',dir=path.parent)
    try:
        with os.fdopen(fd,'w') as out:os.fchmod(out.fileno(),0o600);json.dump(report,out);out.flush();os.fsync(out.fileno())
        os.replace(name,path)
    finally:
        if os.path.exists(name):os.unlink(name)
def automation(state_root):
    schedule={'enabled':False,'next_at':None}
    try:
        enabled=subprocess.run(['systemctl','--user','is-enabled','kura-library-recovery.timer'],capture_output=True,text=True,timeout=3)
        timers=subprocess.run(['systemctl','--user','list-timers','kura-library-recovery.timer','--all','--output=json','--no-pager'],capture_output=True,text=True,timeout=3,check=True)
        matching=[t for t in json.loads(timers.stdout) if t.get('unit')=='kura-library-recovery.timer']
        next_run=matching[0].get('next') if matching else None
        if enabled.returncode==0 and enabled.stdout.strip()=='enabled' and isinstance(next_run,int) and next_run>0:
            schedule={'enabled':True,'next_at':datetime.fromtimestamp(next_run/1000000,timezone.utc).isoformat()}
    except (OSError,ValueError,subprocess.SubprocessError):pass
    job=None
    try:
        value=read_json(state_root/'job.json',32768)
        if (value.get('format')=='kura-library-backup-job-v1' and value.get('state') in ('running','verified','failed')
            and re.fullmatch(r'\d{8}T\d{6}',value.get('snapshot',''))):
            datetime.fromisoformat(value['started_at'])
            if value.get('finished_at') is not None:datetime.fromisoformat(value['finished_at'])
            job={k:value.get(k) for k in ('state','snapshot','started_at','finished_at')}
            if job['state']=='running':
                # A crash/power loss must not leave a permanent "running" label.
                # The lock also covers manual invocations outside systemd.
                try:
                    with (state_root/'job.lock').open('r') as lock:
                        try:
                            fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
                            job['state']='failed'
                        except BlockingIOError:pass
                except OSError:job['state']='failed'
    except (OSError,ValueError,KeyError,TypeError):pass
    return {'automation':schedule,'job':job}


def collect(host,root,output):
    if not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9_.-]*',host):raise ValueError('invalid host')
    previous=None
    try:
        old=read_json(output,32768)
        previous=old.get('backup') if isinstance(old,dict) else None
    except (OSError,ValueError):pass
    report={'format':FORMAT,'checked_at':now(),'host':host,'destination':str(root),'creation_mode':'manual'}
    report.update(automation(output.parent))
    if report['automation']['enabled']:report['creation_mode']='weekly'
    try:
        command='python3 - --probe-root '+shlex.quote(str(root))+' --mount /data'
        result=subprocess.run(['ssh','-o','BatchMode=yes','-o','ConnectTimeout=5',host,command],
          input=Path(__file__).read_text(),text=True,capture_output=True,timeout=25,check=True)
        if len(result.stdout)>32768:raise ValueError('oversized status')
        status=json.loads(result.stdout)
        if not isinstance(status,dict) or status.get('state') not in ('verified','unverified','missing'):raise ValueError('invalid status')
        report.update(status)
    except (OSError,ValueError,subprocess.SubprocessError):
        report.update(state='unavailable',backup=previous)
    write_report(output,report)
    print(json.dumps({'state':report['state'],'checked_at':report['checked_at']}))
    return report
if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--probe-root',type=Path);p.add_argument('--mount',type=Path)
    p.add_argument('--host',default='trainer');p.add_argument('--remote-root',type=Path,default=Path('/data/kura-recovery'))
    p.add_argument('--output',type=Path,default=Path.home()/'.local/state/kura-library-backup/status.json');a=p.parse_args()
    if a.probe_root:print(json.dumps(probe(a.probe_root,a.mount)))
    else:collect(a.host,a.remote_root,a.output)
