"""Recovery metadata summaries never expose archive/config contents or infer success."""
import importlib.util,json,subprocess
from pathlib import Path
spec=importlib.util.spec_from_file_location('backup_status',Path(__file__).parents[1]/'scripts/library-backup-status.py')
m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
def fixture(root):
 p=root/'20261022T0346';p.mkdir()
 manifest={'format':'kura-library-recovery-v1','started_at':'2026-10-22T03:00:00+00:00','archive_bytes':100,'database_sha256':'a'*64,'media':[{}], 'tables':[{}], 'constraints':[[]], 'secrets':'must never appear'}
 proof={'format':'kura-library-restore-proof-v1','database_restore_verified':True,'reopened_after_clean_shutdown':True,'database_sha256':'a'*64,'media_files_verified':1,'tables_verified':1,'constraints_verified':1}
 (p/'manifest.json').write_text(json.dumps(manifest));(p/'restore-proof.json').write_text(json.dumps(proof));return p,manifest,proof
def test_matching_restore_evidence_and_clean_reopen_are_required(tmp_path):
 p,manifest,proof=fixture(tmp_path)
 result=m.probe(tmp_path);assert result['state']=='verified';assert 'must never appear' not in json.dumps(result)
 for field,value in [('database_sha256','b'*64),('media_files_verified',0),('tables_verified',2),('constraints_verified',0),('reopened_after_clean_shutdown',False)]:
  invalid=proof|{field:value};(p/'restore-proof.json').write_text(json.dumps(invalid));assert m.probe(tmp_path)['state']=='unverified'
 (p/'restore-proof.json').unlink();assert m.probe(tmp_path)['state']=='unverified'
def test_latest_snapshot_cannot_borrow_an_older_restore_proof(tmp_path):
 p,manifest,proof=fixture(tmp_path);latest=tmp_path/'20261023T0346';latest.mkdir();(latest/'manifest.json').write_text(json.dumps(manifest))
 result=m.probe(tmp_path);assert result['state']=='unverified';assert result['backup']['snapshot']==latest.name
 assert result['backup']['verified_at'] is None
def test_remote_unavailable_retains_history_but_never_verified_status(tmp_path,monkeypatch):
 p,manifest,proof=fixture(tmp_path);previous=m.probe(tmp_path)['backup'];output=tmp_path/'status.json';m.write_report(output,{'backup':previous})
 def fail(*a,**k):raise subprocess.TimeoutExpired('ssh',25)
 monkeypatch.setattr(m.subprocess,'run',fail)
 result=m.collect('fixture',Path('/data/kura-recovery'),output)
 assert result['state']=='unavailable';assert result['backup']==previous;assert output.stat().st_mode&0o777==0o600
 assert not list(tmp_path.glob('.backup-status-*'))
def test_empty_backups_and_symlinked_evidence_are_not_success(tmp_path):
 assert m.probe(tmp_path)=={'state':'missing','backup':None}
 p,manifest,proof=fixture(tmp_path);(p/'restore-proof.json').unlink();(p/'restore-proof.json').symlink_to(p/'manifest.json')
 import pytest
 with pytest.raises(ValueError):m.probe(tmp_path)

def test_running_job_without_its_process_lock_is_failed_not_forever_running(tmp_path,monkeypatch):
 import fcntl
 p=tmp_path/'job.json';p.write_text(json.dumps({'format':'kura-library-backup-job-v1','state':'running','snapshot':'20261024T070000',
   'started_at':'2026-10-24T07:00:00+00:00','finished_at':None,'credentials':'PRIVATE'}))
 (tmp_path/'job.lock').touch()
 def unavailable(*a,**k):raise OSError('systemd unavailable')
 monkeypatch.setattr(m.subprocess,'run',unavailable)
 assert m.automation(tmp_path)['job']['state']=='failed'
 with (tmp_path/'job.lock').open('r') as lock:
  fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
  result=m.automation(tmp_path);assert result['job']['state']=='running'
  assert 'PRIVATE' not in json.dumps(result)
 (tmp_path/'job.lock').unlink()
 assert m.automation(tmp_path)['job']['state']=='failed'

def test_only_enabled_timer_with_a_next_run_is_advertised(tmp_path,monkeypatch):
 def run(args,**kwargs):
  if 'is-enabled' in args:return subprocess.CompletedProcess(args,0,'enabled\n','')
  return subprocess.CompletedProcess(args,0,json.dumps([{'unit':'kura-library-recovery.timer','next':1792860000000000}]),'')
 monkeypatch.setattr(m.subprocess,'run',run)
 result=m.automation(tmp_path)
 assert result['automation']['enabled'] and result['automation']['next_at']
