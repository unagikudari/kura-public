"""Canonical preparation and credential-free node-local candidate measurement.

Called by learn.py under its existing lifecycle lease. This child returns a
registration payload; only the parent may submit it with the current fence.
"""
from __future__ import annotations
import argparse
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
import time
import shutil
import tempfile
from collections import Counter

from api.tagger_core.learning_artifacts import job_root

from . import dedicated_protocol as p
from . import learning_status
from .label_identity import label_identity

REPO=Path(__file__).resolve().parent.parent
SSH_OPTIONS=['-o','BatchMode=yes','-o','ConnectTimeout=10','-o','ForwardAgent=no',
             '-o','ClearAllForwardings=yes','-o','SendEnv=-*','-o','ControlPath=none']
RSYNC_SSH='ssh '+shlex.join(SSH_OPTIONS)


def command(args, *, timeout=60, cwd=None):
    return subprocess.check_output(args,timeout=timeout,cwd=cwd,text=True,
                                   stderr=subprocess.DEVNULL).strip()


def profile(path):
    doc=json.loads(Path(path).read_text())
    required = {'host','root','python','device','threads','memory_bytes','runtime_s',
                'min_free_ram_bytes','gpu_fraction'}
    if not required <= set(doc) <= required | {'min_free_gpu_bytes'}:
        raise p.ProtocolError('worker profile fields invalid')
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9.-]*',doc['host']):
        raise p.ProtocolError('worker host invalid')
    for key in ('root','python'):
        if not re.fullmatch(r'/[A-Za-z0-9_./-]+',doc[key]) or '..' in Path(doc[key]).parts:
            raise p.ProtocolError('worker path invalid')
    from .offline_learning_job import validate
    validate(dict(doc,schema='tagger-offline-job/1',snapshot_sha256='0'*64,
                  dataset_sha256='0'*64,code_sha256='0'*64))
    if type(doc['memory_bytes']) is not int or not 2*1024**3<=doc['memory_bytes']<=16*1024**3:
        raise p.ProtocolError('worker memory limit invalid')
    return doc


def ssh(worker, args, *, timeout=60):
    return command(['ssh',*SSH_OPTIONS,worker['host'],
                    shlex.join([str(v) for v in args])],timeout=timeout)


def _gt_and_baseline(tag):
    from . import vnext_arms as a
    from api.tagger_core.post_exclusion import training_label
    conn=a.connect_primary();conn.autocommit=False;conn.set_session(readonly=True)
    try:
        with conn.cursor() as cursor:
            cursor.execute("SET LOCAL statement_timeout='30s'")
        rows=a.q(conn,"SELECT post_id, verdict, derived_from FROM tagger_gt_label g WHERE tag=%s AND " + training_label("g"),(tag,))
        gt=dict(Counter(verdict for _, verdict, _ in rows))
        gt.update(Counter('human_'+verdict for _, verdict, source in rows
                          if source=='human_correction'))
        for key in ('positive','negative','human_positive','human_negative'):
            gt.setdefault(key,0)
        gt['label_sha256']=label_identity(rows)
        row=a.q(conn,'SELECT content_sha256,threshold FROM tagger_current_head_policy WHERE tag=%s',(tag,))
        if len(row)>1:raise p.ProtocolError('ambiguous current head')
        floor={k:float(v) for k,v in a.q(conn,'SELECT key,value FROM tagger_config')}
        return gt,({'sha256':row[0][0],'threshold':float(row[0][1])} if row else None),floor
    finally:conn.rollback();conn.close()


def _snapshot_has_unavailable_images(snapshot):
    from . import vnext_arms as a
    from api.tagger_core.post_exclusion import not_excluded

    ids = [row["id"] for row in snapshot["media"] if not row["exclusion"]]
    if not ids:
        return False
    conn = a.connect_primary()
    conn.autocommit = False
    conn.set_session(readonly=True)
    try:
        with conn.cursor() as cursor:
            cursor.execute("SET LOCAL statement_timeout='10s'")
        count = a.q(conn, "SELECT count(*) FROM post p WHERE p.id=ANY(%s) "
                          "AND p.status='ok' AND " + not_excluded("p.id"), (ids,))[0][0]
        return count != len(ids)
    finally:
        conn.rollback()
        conn.close()


def _hold(tag,event,gt,reason, *, support=None):
    learning_status.write(tag,event,'held',reason=reason)
    return {'outcome':'hold','registration':{'tag':tag,'source_host':'offline-learning',
        'artifact_ref':'(none: '+reason+')','artifact_sha256':'(none: '+reason+')',
        'lifecycle_event_id':event,'gt':gt,'metrics':{'disposition':'hold',
        'classification':{'verdict':'hold','why':reason},'reason':reason,
        **({'support': support} if support is not None else {})}}}


def _baseline(root, declaration):
    if declaration is None:return
    sha=declaration['sha256']
    if not isinstance(sha,str) or not re.fullmatch('[0-9a-f]{64}',sha):
        raise p.ProtocolError('current artifact digest invalid')
    path=root/'baseline.pt'
    if not path.exists():
        # Authentication remains on the canonical host. Only model bytes cross
        # to the trainer, never the header or source environment.
        import urllib.request
        from . import vnext_runner as r
        req=urllib.request.Request(r.KURA_API+'/tagger/artifact/'+sha,headers={'Authorization':r._auth()})
        with urllib.request.urlopen(req,context=r._ctx,timeout=300) as response:
            data=response.read(1024**3+1)
        if len(data)>1024**3 or hashlib.sha256(data).hexdigest()!=sha:
            raise p.ProtocolError('baseline download mismatch')
        p.write_bytes_once(path,data)
    if hashlib.sha256(path.read_bytes()).hexdigest()!=sha:
        raise p.ProtocolError('baseline changed')


def _code(root):
    target=root/'code'
    if (root/'code-manifest.json').exists():
        manifest=p.read_verified(root/'code-manifest.json','tagger-job-code/1')
        if any(hashlib.sha256((target/name).read_bytes()).hexdigest()!=sha
               for name,sha in manifest['files'].items()):
            raise p.ProtocolError('staged code changed')
        return manifest
    if not target.exists():
        archive=subprocess.check_output(['git','archive','HEAD'],cwd=REPO,timeout=120)
        staging=Path(tempfile.mkdtemp(prefix='.code-staging-',dir=root))
        # A partial extraction is preserved for diagnosis but never mistaken for
        # the committed code directory on the next pass.
        subprocess.run(['tar','-xf','-','-C',str(staging)],input=archive,check=True,timeout=120)
        staging.rename(target)
    names=command(['git','ls-tree','-r','--name-only','HEAD'],cwd=REPO).splitlines()
    manifest={name:hashlib.sha256((target/name).read_bytes()).hexdigest()
              for name in names if (target/name).is_file()}
    if 'tagger/offline_learning_job.py' not in manifest:
        raise p.ProtocolError('learning worker not committed')
    return p.write_once(root/'code-manifest.json',{'schema':'tagger-job-code/1','files':manifest})


def assigned_worker_matches(tag, event, worker):
    inputs = job_root(tag, event)/'inputs.json'
    if not inputs.exists():
        return True
    frozen = p.read_verified(inputs, 'tagger-dispatch-inputs/1')
    if (frozen['tag'], frozen['event']) != (tag, event):
        raise p.ProtocolError('dispatch input identity changed')
    return frozen['worker'] == worker


def dispatch(tag,event,config,timeout_s):
    root = job_root(tag, event)
    root.mkdir(mode=0o700,parents=True,exist_ok=True)
    with (root/'controller.lock').open('a') as lock:
        try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:raise p.ProtocolError('dispatch already running') from None
        return _dispatch_locked(tag,event,config,timeout_s)


def dedicated_runtime_s(base_seconds: int, image_count: int) -> int:
    """Budget extra time for validating and preparing large frozen datasets."""
    extra_seconds = max(0, image_count - 4000)
    return min(21600, base_seconds + extra_seconds)


def _dispatch_locked(tag,event,config,timeout_s):
    if type(event) is not int or event<1:raise p.ProtocolError('lifecycle event required')
    worker=profile(config)
    if worker['runtime_s']+900>=timeout_s:raise p.ProtocolError('worker can outlive lease budget')
    start=time.monotonic()
    learning_status.write(tag,event,"preparing",fresh_for_s=1200)
    root = job_root(tag, event)
    key = root.name
    root.mkdir(mode=0o700,parents=True,exist_ok=True)
    remote=Path(worker['root'])/key
    unit='kura-learning-'+key
    # Fail before bulk export if the selected node cannot even start its Python.
    ssh(worker,[worker['python'],'-c','import sys; assert sys.version_info >= (3,10)'])
    if (root/'inputs.json').exists():
        inputs=p.read_verified(root/'inputs.json','tagger-dispatch-inputs/1')
        if inputs['tag']!=tag or inputs['event']!=event or inputs['worker']!=worker:
            raise p.ProtocolError('dispatch input changed')
        gt,baseline,floor=inputs['gt'],inputs['baseline'],inputs['floor']
    else:
        gt,baseline,floor=_gt_and_baseline(tag)
        p.write_once(root/'inputs.json',{'schema':'tagger-dispatch-inputs/1','tag':tag,'event':event,
                                       'gt':gt,'baseline':baseline,'floor':floor,'worker':worker})
    snapshot=root/'snapshot.json'
    if not snapshot.exists():
        # Targets are selected from label counts before any predictions. Low
        # support remains a measured precondition, not an infrastructure retry.
        minimum={k:math.ceil(floor[k]) for k in
                 ('min_val_pos_support','min_held_out_samples','min_gt_positive','min_gt_negative')}
        if any(v<1 for v in minimum.values()):raise p.ProtocolError('split support policy invalid')
        if (gt['human_positive']<minimum['min_val_pos_support']+minimum['min_gt_positive']
                or gt['human_negative']<minimum['min_held_out_samples']+minimum['min_gt_negative']):
            return _hold(tag,event,gt,'insufficient_human_split_support', support={
                'positive': {'observed': gt['human_positive'], 'required': minimum['min_val_pos_support']+minimum['min_gt_positive']},
                'negative': {'observed': gt['human_negative'], 'required': minimum['min_held_out_samples']+minimum['min_gt_negative']},
            })
        sampling = {}
        if os.environ.get('KURA_LEARNING_PRODUCTION_SCORES') == '1':
            # This is set only before the immutable snapshot exists, so a
            # resumed job keeps its original population and seed.
            try:
                sample_size = int(os.environ.get('KURA_LEARNING_CATALOG_SAMPLE_SIZE', '4000'))
            except ValueError as exc:
                raise p.ProtocolError('catalog sample size must be an integer') from exc
            if not 1 <= sample_size <= 20000:
                raise p.ProtocolError('catalog sample size outside budget')
            sampling['catalog_sample_size'] = sample_size
        try:p.prepare(tag,snapshot,
                      positive=max(minimum['min_val_pos_support'],min(300,gt['human_positive']//5)),
                      negative=max(minimum['min_held_out_samples'],min(1000,gt['human_negative']//5)),
                      fit_positive_to_groups=True, **sampling)
        except p.ProtocolError:
            if not snapshot.exists():raise
    doc=p.read_verified(snapshot,p.SCHEMA)
    gt=dict(Counter(row['verdict'] for row in doc['gt']))
    gt.setdefault('positive',0);gt.setdefault('negative',0)
    gt['label_sha256']=label_identity(
        (row['id'],row['verdict'],row['derived_from']) for row in doc['gt'])
    if doc['status'] != 'prepared':
        review_groups = root / 'review-groups.json'
        if not review_groups.exists():
            groups = p.unjudged_catalog_groups(doc)
            p.write_once(review_groups, {
                'schema': 'tagger-review-groups/1', 'event': event, 'tag': tag,
                'snapshot_sha256': doc['sha256'], 'groups': [posts[:8] for posts in groups[:512]],
            })
        return _hold(tag,event,gt,'whole_group_split_unavailable')
    marker=root/'dispatch-intent.json'
    if not marker.exists() and _snapshot_has_unavailable_images(doc):
        return _hold(tag,event,gt,'frozen_images_no_longer_eligible')
    from .dedicated_dataset import export,FrozenImages
    dataset=root/'dataset'
    if not (dataset/'dataset.json').exists():
        # Do not delete another job's data to make room. The exporter has a
        # separate hard 16 GiB per-job cap; leave that much free as reserve too.
        if shutil.disk_usage(root).free<32*1024**3:
            raise p.ProtocolError('source dataset disk budget unavailable')
        if os.environ.get("KURA_LEARNING_PRODUCTION_SCORES") == "1":
            export(snapshot, dataset, include_production=True, resume=True)
        else:
            export(snapshot, dataset, resume=True)
    data=FrozenImages(dataset,doc)
    _baseline(root,baseline);code=_code(root)
    job_path = root / 'job.json'
    # Keep retries bound to the same job digest and remote receipts.
    runtime_s = (
        p.read_verified(job_path, 'tagger-offline-job/1')['runtime_s']
        if job_path.exists()
        else min(dedicated_runtime_s(worker['runtime_s'], len(data.hashes)), timeout_s - 900)
    )
    job=p.write_once(job_path,{'schema':'tagger-offline-job/1','tag':tag,'event':event,
        'snapshot_sha256':doc['sha256'],'dataset_sha256':data.doc['sha256'],'code_sha256':code['sha256'],
        'baseline_sha256':baseline['sha256'] if baseline else None,
        'baseline_threshold':baseline['threshold'] if baseline else None,
        'runtime_s': runtime_s,
        **{k:worker[k] for k in ('device','threads','min_free_ram_bytes',
                                  'gpu_fraction','min_free_gpu_bytes') if k in worker}})
    if time.monotonic()-start+runtime_s+120>=timeout_s:
        raise p.ProtocolError('preparation exhausted dispatch budget')
    if not marker.exists():
        ssh(worker,['mkdir','-p',str(remote)])
        stats=command(['env','LC_ALL=C','rsync','-e',RSYNC_SSH,'-a','--ignore-existing',
             '--dry-run','--stats','--exclude=dispatch-intent.json','--exclude=received',
             '--exclude=.code-staging-*','--exclude=catalog-extension',str(root)+'/',worker['host']+':'+str(remote)+'/'],timeout=120)
        match=re.search(r'^Total transferred file size: ([0-9,]+) bytes$',stats,re.M)
        if not match:raise p.ProtocolError('transfer size unavailable')
        required=int(match.group(1).replace(',',''))+16*1024**3
        ssh(worker,[worker['python'],'-c',
            'import shutil,sys; assert shutil.disk_usage(sys.argv[1]).free >= int(sys.argv[2])',
            worker['root'],str(required)])
        command(['rsync','-e',RSYNC_SSH,'-a','--ignore-existing','--chmod=D700,F600',
                 '--exclude=dispatch-intent.json','--exclude=received','--exclude=.code-staging-*',
                 '--exclude=catalog-extension',
                 str(root)+'/',worker['host']+':'+str(remote)+'/'],timeout=600)
        p.write_once(marker,{'schema':'tagger-dispatch-intent/1','job_sha256':job['sha256'],'unit':unit})
    elif p.read_verified(marker)['job_sha256']!=job['sha256']:
        raise p.ProtocolError('dispatch intent changed')

    def read_receipt():
        try: raw=ssh(worker,['cat',str(remote/'job-result.json')])
        except subprocess.CalledProcessError as exc:
            if exc.returncode==1:return None
            raise
        receipt=json.loads(raw)
        body={k:v for k,v in receipt.items() if k!='sha256'}
        if (receipt.get('schema')!='tagger-offline-job-result/1'
                or receipt.get('sha256')!=p.digest(body)
                or receipt.get('job_sha256')!=job['sha256']):
            raise p.ProtocolError('remote receipt corrupt')
        p.write_once(root/'received-job-result.json',body)
        if receipt['outcome']!='succeeded':raise p.ProtocolError('remote job refused')
        return receipt

    def launch():
        # Each attempt is recorded. A known absent unit may be launched again;
        # node-local flock + immutable cycle stage markers prevent retraining.
        attempts=list(root.glob('launch-attempt-*.json'))
        if len(attempts)>=3:raise p.ProtocolError('dispatch launch budget exhausted')
        p.write_once(root/f'launch-attempt-{len(attempts)+1}.json',
                     dict(schema='tagger-launch-attempt/1',job_sha256=job['sha256'],unit=unit))
        ssh(worker,['systemd-run','--user','--unit='+unit,
            '--property=WorkingDirectory='+str(remote/'code'),
            '--property=RuntimeMaxSec='+str(runtime_s),
            '--property=MemoryMax='+str(worker['memory_bytes']),
            '--property=CPUQuota='+str(worker['threads']*100)+'%',
            '--property=TimeoutStopSec=30','--property=NoNewPrivileges=yes',
            '--property=RestrictAddressFamilies=AF_UNIX AF_NETLINK',
            '--property=Nice=15','--property=UMask=0077',worker['python'],
            '-m','tagger.offline_learning_job','--job-dir',str(remote)])

    while time.monotonic()-start<timeout_s-60:
        try:
            receipt=read_receipt()
            if receipt is not None:break
            observed=ssh(worker,['systemctl','--user','show',unit,'-p','ActiveState','-p','LoadState'])
            state=dict(line.split('=',1) for line in observed.splitlines() if '=' in line)
            if state.get('ActiveState') in ('active','activating'):
                learning_status.write(tag,event,'measuring')
            if state.get('ActiveState') not in ('active','activating'):
                receipt=read_receipt()  # Publication can precede unit exit by milliseconds.
                if receipt is not None:break
                if state.get('LoadState')!='not-found':
                    raise p.ProtocolError('remote job stopped without result')
                if time.monotonic()-start+runtime_s+120>=timeout_s:
                    raise p.ProtocolError('dispatch budget exhausted')
                if _snapshot_has_unavailable_images(doc):
                    return _hold(tag,event,gt,'frozen_images_no_longer_eligible')
                launch()
        except subprocess.SubprocessError:
            pass  # A transport failure proves neither completion nor nonexecution.
        time.sleep(10)
    else:raise TimeoutError('remote job deadline')
    received=root/'received';received.mkdir(exist_ok=True,mode=0o700)
    for name in ('result.json','result.json.candidate.json','qualification.json','cycle-result.json'):
        command(['rsync','-e',RSYNC_SSH,'-a','--ignore-existing',worker['host']+':'+str(remote/'result'/name),str(received/name)])
    result=p.read_verified(received/'result.json','vnext-dedicated-measurement/1')
    quality=p.read_verified(received/'qualification.json','dedicated-serving-qualification/1')
    cycle=p.read_verified(received/'cycle-result.json','dedicated-cycle-result/1')
    if (receipt['cycle_sha256']!=cycle['sha256'] or cycle['qualification_sha256']!=quality['sha256']
            or quality['measurement_sha256']!=result['sha256'] or result['snapshot_sha256']!=doc['sha256']
            or result.get('tag')!=tag or quality.get('tag')!=tag
            or quality.get('snapshot_sha256')!=doc['sha256']
            or quality.get('bundle_sha256')!=result['bundle_sha256']
            or quality.get('threshold')!=result['threshold']):
        raise p.ProtocolError('remote result binding mismatch')
    production = {}
    expected = cycle.get("production_scores_sha256")
    if cycle["state"] == "awaiting_production_precision" and (
            expected is not None or getattr(data, "includes_production", False)):
        if expected is None:
            raise p.ProtocolError("production scores missing from cycle")
        name = "production-scores.json"
        command(["rsync", "-e", RSYNC_SSH, "-a", "--ignore-existing",
                 worker["host"] + ":" + str(remote / "result" / name), str(received / name)])
        scores = p.read_verified(received / name, "dedicated-production-scores/1")
        if (scores["sha256"] != expected or scores["tag"] != tag
                or scores.get("scope") != p.production_score_scope(doc)
                or scores["snapshot_sha256"] != doc["sha256"]
                or scores["measurement_sha256"] != result["sha256"]
                or scores["dataset_sha256"] != data.doc["sha256"]
                or scores["model_sha256"] != result["bundle_sha256"]
                or scores["threshold"] != result["threshold"]):
            raise p.ProtocolError("remote production scores binding mismatch")
        ids, exclusions = p.independent_production_frame(doc)
        values = dict(scores["scores"])
        if len(values) != len(scores["scores"]) or scores["exclusions"] != exclusions:
            raise p.ProtocolError("remote production score population mismatch")
        p.validate_scores(ids, values)
        from .canonical_evaluation import candidate_review_document, freeze_scored_frame

        from tagger.review_censor_variants import excluded_for_tag
        omitted = excluded_for_tag(tag, values)
        review_values = {pid: score for pid, score in values.items() if pid not in omitted}
        frame = freeze_scored_frame(
            scores["model_sha256"], review_values, threshold=scores["threshold"],
            per_stratum=min(250, doc["parameters"]["precision_n"]),
            seed=doc["seed"] + 9001,
        )
        p.write_once(received / "review-frame.json", frame)
        predicted_positive = len(frame["sample"]["predicted_present"])
        minimum_predicted_positive = math.ceil(floor["min_pred_pos_support"])
        if predicted_positive < minimum_predicted_positive:
            extension_root = root / "catalog-extension"
            if extension_root.is_dir():
                from .catalog_extension import stage_registration_review

                review = stage_registration_review(
                    extension_root, received, doc, result, scores, minimum_predicted_positive,
                )
                if review is not None:
                    production["production_review"] = review
            if "production_review" not in production:
                production["production_review_insufficient_support"] = {
                    "predicted_positive": predicted_positive,
                    "required_predicted_positive": minimum_predicted_positive,
                    "frame_sha256": p.digest(frame),
                    "production_scores_sha256": expected,
                    "scope": scores["scope"],
                }
        else:
            trial = None
            if frame["sample"]["predicted_present"] and frame["sample"]["predicted_absent"]:
                from .dedicated_qualification import stage_worker_trial_images, worker_trial_inputs

                trial = p.write_once(received / "worker-trial-inputs.json", {
                    "schema": "tagger-worker-trial-inputs/1",
                    "snapshot_sha256": doc["sha256"],
                    "artifact_sha256": result["bundle_sha256"], "threshold": result["threshold"],
                    "examples": worker_trial_inputs(doc, values, threshold=result["threshold"]),
                })
                stage_worker_trial_images(data, trial, received / "worker-trial-images")
            production["production_review"] = {
                "frame_sha256": p.digest(frame),
                "frame": frame,
                "measurement_sha256": result["sha256"],
                "production_scores_sha256": expected,
                "scope": scores["scope"],
                "worker_trial_inputs_sha256": trial["sha256"] if trial else None,
                **({"worker_trial_inputs": {k: v for k, v in trial.items() if k != "sha256"}} if trial else {}),
                "request": candidate_review_document(
                    tag=tag, frame=frame,
                    display_hashes={row["id"]: row["blob_sha256"] for row in doc["media"]},
                    snapshot_sha256=doc["sha256"], measurement_sha256=result["sha256"],
                ),
            }
        if "production_review" in production:
            model_file = "result.json.bundle.pt"
            command(["rsync", "-e", RSYNC_SSH, "-a", "--ignore-existing", "--max-size=512M",
                     worker["host"] + ":" + str(remote / "result" / model_file),
                     str(received / model_file)], timeout=600)
            if not (received / model_file).is_file():
                raise p.ProtocolError("candidate artifact unavailable within transfer budget")
    # No artifact PUT and no promotion: this receipt either explains a quality
    # rejection or awaits production-domain/fleet qualification. Retain model
    # bytes on the training node, with their hash, for that next stage.
    learning_status.write(tag,event,'measured')
    return {'outcome':cycle['state'],'registration':{'tag':tag,'source_host':worker['host'],
        'artifact_ref':worker['host']+':'+str(remote/'result'/'result.json.bundle.pt'),
        'artifact_sha256':result['bundle_sha256'],'gt':gt,'lifecycle_event_id':event,
        'metrics':{'feature_contract':result['feature_contract'],'requires_preprocessing_review':True,
                   'serving_qualification':quality,'snapshot_sha256':doc['sha256'],
                   'job_sha256':job['sha256'],'threshold':result['threshold'], **production}}}


def main():
    from api.extension_hooks import ExtensionIntegrityError

    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--tag',required=True);ap.add_argument('--event',type=int,required=True)
    ap.add_argument('--config',required=True);ap.add_argument('--timeout-s',type=int,required=True)
    a=ap.parse_args()
    try:
        result=dispatch(a.tag,a.event,a.config,a.timeout_s)
    except ExtensionIntegrityError as exc:
        reason = 'extension_integrity'
        print('[remote_learning] ' + json.dumps({'reason': reason, 'detail': str(exc)}),
              file=sys.stderr, flush=True)
        learning_status.write(a.tag, a.event, 'stopped', reason=reason)
        print(json.dumps({'outcome': 'failed', 'reason': reason, 'registration': None}))
        return 1
    except p.ProtocolError as exc:
        print('[remote_learning] ' + json.dumps({
            'tag': a.tag, 'event': a.event, 'reason': 'ProtocolError', 'detail': str(exc),
        }), file=sys.stderr, flush=True)
        learning_status.write(a.tag, a.event, 'stopped', reason='ProtocolError')
        print(json.dumps({'outcome': 'failed', 'reason': 'ProtocolError', 'registration': None}))
        return 1
    except Exception as exc:
        learning_status.write(a.tag,a.event,'stopped',reason=type(exc).__name__)
        print(json.dumps({'outcome':'failed','reason':type(exc).__name__,'registration':None}))
        return 1
    print(json.dumps(result))
    return 0


if __name__=='__main__':raise SystemExit(main())
