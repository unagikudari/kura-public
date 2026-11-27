"""Node-local offline learning entry. Run under a bounded user service.

Only a staged job directory crosses into this process: no database, HTTP token,
or signing credentials. A job never publishes a model or edits human labels.
"""
from __future__ import annotations
import argparse
import fcntl
import json
import os
from pathlib import Path
import signal
import time

# Keep package imports behind the standard-library code verification below.
import hashlib


class JobRefused(ValueError):
    pass


def _verified(path, schema):
    doc=json.loads(path.read_text())
    body={k:v for k,v in doc.items() if k!='sha256'}
    digest=hashlib.sha256(json.dumps(body,sort_keys=True,separators=(',',':'),allow_nan=False).encode()).hexdigest()
    if doc.get('schema')!=schema or doc.get('sha256')!=digest:
        raise JobRefused('input_digest_invalid')
    return doc


def _bootstrap(root):
    code=root/'code'
    if Path(__file__).resolve()!=code/'tagger'/'offline_learning_job.py':
        raise JobRefused('worker_code_location_invalid')
    manifest=_verified(root/'code-manifest.json','tagger-job-code/1')
    job=_verified(root/'job.json','tagger-offline-job/1')
    if manifest['sha256']!=job['code_sha256']:
        raise JobRefused('code_manifest_binding_invalid')
    actual={}
    for path in code.rglob('*'):
        if path.is_symlink():raise JobRefused('code_symlink_invalid')
        if '__pycache__' in path.parts:
            continue  # Python may create these before module entry; never inputs.
        if path.is_file():actual[str(path.relative_to(code))]=hashlib.sha256(path.read_bytes()).hexdigest()
    if actual!=manifest['files']:
        raise JobRefused('code_files_invalid')
    from . import dedicated_protocol
    from .dedicated_cycle import run
    return dedicated_protocol,run


def clean_environment():
    allowed={'HOME','USER','LOGNAME','PATH','LANG','LC_ALL','TZ','XDG_CACHE_HOME',
             'HSA_OVERRIDE_GFX_VERSION','HIP_VISIBLE_DEVICES','ROCR_VISIBLE_DEVICES','CUDA_VISIBLE_DEVICES'}
    for key in list(os.environ):
        if key not in allowed:os.environ.pop(key,None)
    os.environ['HF_HUB_OFFLINE'] = '1'
    os.environ['HF_HUB_DISABLE_IMPLICIT_TOKEN'] = '1'


def validate(job):
    if job.get('schema') != 'tagger-offline-job/1':
        raise JobRefused('job schema invalid')
    for key in ('snapshot_sha256', 'dataset_sha256', 'code_sha256'):
        value=job.get(key)
        if not isinstance(value,str) or len(value)!=64 or any(c not in '0123456789abcdef' for c in value):
            raise JobRefused('job digest invalid')
    if job.get('device') not in ('cpu','cuda'):
        raise JobRefused('job device invalid')
    if type(job.get('threads')) is not int or not 1<=job['threads']<=8:
        raise JobRefused('job thread budget invalid')
    if type(job.get('runtime_s')) is not int or not 60<=job['runtime_s']<=21600:
        raise JobRefused('job runtime budget invalid')
    if type(job.get('min_free_ram_bytes')) is not int or job['min_free_ram_bytes']<5*1024**3:
        raise JobRefused('job memory admission invalid')
    minimum_gpu = job.get('min_free_gpu_bytes', 4 * 1024**3)
    if (type(minimum_gpu) is not int or not 1024**3 <= minimum_gpu <= 4 * 1024**3
            or ('min_free_gpu_bytes' in job and job['device'] != 'cuda')):
        raise JobRefused('job GPU admission invalid')
    fraction=job.get('gpu_fraction')
    if type(fraction) not in (int,float) or not 0<fraction<=.35:
        raise JobRefused('job GPU budget invalid')


def require_training_pillow():
    """Enforce the checked-in Pillow requirement in the actual training interpreter."""
    from packaging.requirements import Requirement
    from PIL import __version__

    reqs = Path(__file__).with_name("requirements.txt").read_text().splitlines()
    pillow = next(Requirement(line.split("#", 1)[0].strip())
                  for line in reqs if line.lower().startswith("pillow"))
    if not pillow.specifier.contains(__version__):
        raise JobRefused("training_pillow_below_required_version")


def _deadline(*_):
    raise TimeoutError('job deadline')


def execute(root):
    clean_environment()
    root=Path(root).resolve()
    with (root/'job.lock').open('a') as lock:
        try:
            fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:
            return {'outcome':'refused','state':'busy','reason':'job_already_running',
                    'next':'observe_existing_job','promotion_eligible':False}
        protocol,run = _bootstrap(root)
        return _execute_locked(root,protocol,run)


def _execute_locked(root,p,run):
    job=p.read_verified(root/'job.json','tagger-offline-job/1')
    validate(job)
    out=root/'job-result.json'
    if out.exists():
        result=p.read_verified(out,'tagger-offline-job-result/1')
        if result['job_sha256']!=job['sha256']:
            raise JobRefused('job result mismatch')
        return result
    start=time.monotonic()
    phase='preflight'
    try:
        snapshot=p.read_verified(root/'snapshot.json',p.SCHEMA)
        data=p.read_verified(root/'dataset'/'dataset.json','dedicated-dataset/1')
        if snapshot['sha256']!=job['snapshot_sha256'] or data['sha256']!=job['dataset_sha256']:
            raise JobRefused('job input mismatch')
        available=int(next(line.split()[1] for line in Path('/proc/meminfo').read_text().splitlines()
                           if line.startswith('MemAvailable:')))*1024
        if available<job['min_free_ram_bytes']:
            raise JobRefused('memory_unavailable')
        require_training_pillow()
        signal.signal(signal.SIGALRM,_deadline);signal.alarm(job['runtime_s'])
        os.environ.update(VNEXT_DEV=job['device'],VNEXT_EMB_BATCH='2',
                          OMP_NUM_THREADS=str(job['threads']),MKL_NUM_THREADS=str(job['threads']))
        import torch
        torch.set_num_threads(job['threads'])
        if torch.get_num_interop_threads()!=1:torch.set_num_interop_threads(1)
        if job['device']=='cuda':
            if not torch.cuda.is_available():raise JobRefused('gpu_unavailable')
            free,total=torch.cuda.mem_get_info()
            minimum_gpu = job.get('min_free_gpu_bytes', 4 * 1024**3)
            if 'min_free_gpu_bytes' in job:
                allocator_budget = int(total * job['gpu_fraction'])
                minimum_gpu = max(minimum_gpu, allocator_budget + 512 * 1024**2)
            if free < minimum_gpu:
                raise JobRefused('gpu_memory_unavailable')
            torch.cuda.set_per_process_memory_fraction(job['gpu_fraction'])
        phase='learning'
        baseline=root/'baseline.pt' if job.get('baseline_sha256') is not None else None
        result=run(root/'snapshot.json',root/'dataset',root/'result',baseline=baseline,
                   baseline_sha256=job.get('baseline_sha256'),baseline_threshold=job.get('baseline_threshold'),
                   device=job['device'])
        body={key:result[key] for key in ('outcome','state','reason','next','promotion_eligible')}
        body['cycle_sha256']=result['sha256']
    except Exception as exc:
        body={'outcome':'refused','state':'stopped','reason':str(exc) if isinstance(exc,JobRefused) else type(exc).__name__,
              'phase':phase,'next':'inspect_attempt_without_reset','promotion_eligible':False}
    finally:
        signal.alarm(0)
    return p.write_once(out,dict(body,schema='tagger-offline-job-result/1',
        job_sha256=job['sha256'],seconds=round(time.monotonic()-start,2)))


def main():
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--job-dir',required=True)
    try:
        result=execute(ap.parse_args().job_dir)
    except Exception as exc:
        result={'outcome':'refused','state':'preflight','reason':type(exc).__name__,
                'next':'inspect_attempt_without_reset','promotion_eligible':False}
    print(json.dumps(result))
    return 0 if result['outcome']=='succeeded' else 1


if __name__=='__main__':raise SystemExit(main())
