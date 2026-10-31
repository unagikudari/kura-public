"""Scheduled orchestration publishes only verified snapshots and keeps history."""
import json
from pathlib import Path
import subprocess
import sys

import pytest
from scripts import library_recovery_schedule as schedule


def artifacts(path,proof=True):
    path.mkdir()
    (path/'manifest.json').write_text(json.dumps({'database_sha256':'a'*64,'tables':[{}],'constraints':[{}],'media':[{}]}))
    if proof:
        (path/'restore-proof.json').write_text(json.dumps({'format':'kura-library-restore-proof-v1','database_restore_verified':True,
            'reopened_after_clean_shutdown':True,'database_sha256':'a'*64,'tables_verified':1,'constraints_verified':1,'media_files_verified':1}))


def publisher(remote):
    def publish(source,target):
        subprocess.run([sys.executable,'-c',schedule.PUBLISH,str(remote/source.name),str(remote/target.name)],check=True,capture_output=True)
    return publish


def test_success_is_published_after_verification_and_lock_blocks_overlap(tmp_path):
    remote=tmp_path/'remote';remote.mkdir();state=tmp_path/'state'
    seen=[]
    def backup(host,destination):
        receipt=json.loads((state/'job.json').read_text());assert receipt['state']=='running'
        assert not (remote/receipt['snapshot']).exists()
        with pytest.raises(BlockingIOError):
            schedule.run(host,Path('/data/kura-recovery'),state,backup=lambda *_:None,publish=lambda *_:None)
        artifacts(remote/destination.name)
        seen.append(destination)
    result=schedule.run('fixture',Path('/data/kura-recovery'),state,backup=backup,publish=publisher(remote))
    assert result['state']=='verified'
    assert (remote/result['snapshot']/'restore-proof.json').is_file()
    assert not (remote/seen[0].name).exists()
    assert (state/'job.json').stat().st_mode&0o777==0o600


def test_failure_preserves_previous_backup_and_redacts_exception(tmp_path,capsys):
    remote=tmp_path/'remote';remote.mkdir();artifacts(remote/'20261022T034600');state=tmp_path/'state'
    def failed(*_):raise RuntimeError('password=PRIVATE')
    with pytest.raises(RuntimeError,match='previous published snapshots retained'):
        schedule.run('fixture',Path('/data/kura-recovery'),state,backup=failed,publish=publisher(remote))
    receipt=(state/'job.json').read_text()
    assert json.loads(receipt)['state']=='failed'
    assert 'PRIVATE' not in receipt+capsys.readouterr().out
    assert (remote/'20261022T034600/restore-proof.json').is_file()


def test_missing_restore_proof_cannot_publish(tmp_path):
    remote=tmp_path/'remote';remote.mkdir();state=tmp_path/'state'
    def incomplete(host,destination):artifacts(remote/destination.name,proof=False)
    with pytest.raises(RuntimeError):
        schedule.run('fixture',Path('/data/kura-recovery'),state,backup=incomplete,publish=publisher(remote))
    assert json.loads((state/'job.json').read_text())['state']=='failed'
    assert all(p.name.startswith('.incomplete-') for p in remote.iterdir())
