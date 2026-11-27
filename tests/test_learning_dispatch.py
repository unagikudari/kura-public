import json
from types import SimpleNamespace
import pytest
from tagger import dedicated_protocol as p
from tagger.auto_review import decide
from tagger import offline_learning_job as job
from tagger import remote_learning as remote
from api.tagger_core.learning_evidence import serving_summary

FLOOR=dict(min_precision=.85,min_recall=.7,min_val_pos_support=20,min_pred_pos_support=20)


def evidence(tmp_path, *, fp=201):
    tp,tn,fn=215,799,85
    report=p.write_once(tmp_path/'q.json',dict(schema='dedicated-serving-qualification/1',
        tag='boat',bundle_sha256='a'*64,snapshot_sha256='b'*64,threshold=.25,parity={'passed':True},
        heldout=dict(tp=tp,fp=fp,tn=tn,fn=fn,precision=tp/(tp+fp),recall=tp/(tp+fn))))
    return dict(serving_qualification=report,snapshot_sha256='b'*64,threshold=.25,
                feature_contract={'schema':'new'},requires_preprocessing_review=True)


def test_bad_real_candidate_is_terminal_rejection_not_owner_approval(tmp_path):
    m=evidence(tmp_path)
    d=decide(m,{},FLOOR,artifact_sha256='a'*64,tag='boat')
    assert not d.ok and d.bucket=='rejected' and d.reason=='serving_quality_failed:precision'
    rendered=serving_summary(m['serving_qualification'])
    assert '51.7%' in rendered and '1300件' in rendered and '誤付与 201' in rendered
    assert '未配備候補' in rendered and '本番全体の精度ではありません' in rendered


def test_passed_balanced_holdout_does_not_authorize_deployment(tmp_path):
    d=decide(evidence(tmp_path,fp=10),{},FLOOR,artifact_sha256='a'*64,tag='boat')
    assert not d.ok and d.bucket=='owner_decision'
    assert 'production_precision' in d.reason


@pytest.mark.parametrize('change',['artifact','threshold','digest','counts'])
def test_qualification_cannot_cross_bindings(tmp_path,change):
    m=evidence(tmp_path); artifact='a'*64
    if change=='artifact':artifact='c'*64
    elif change=='threshold':m['threshold']=.9
    elif change=='digest':m['serving_qualification']['sha256']='0'*64
    else:
        q=m['serving_qualification'];q['heldout']['fp']=-1
        q['sha256']=p.digest({k:v for k,v in q.items() if k!='sha256'})
    d=decide(m,{},FLOOR,artifact_sha256=artifact,tag="boat")
    assert not d.ok and d.reason=='serving_evidence_invalid'


def test_remote_selection_preserves_parent_lease_protocol(monkeypatch):
    from tagger import learn
    seen=[]
    def run(cmd,**kwargs):
        seen.append((cmd,kwargs));return SimpleNamespace(stdout='{"outcome":"hold","registration":{}}',returncode=0)
    monkeypatch.setattr(learn.subprocess,'run',run)
    args=SimpleNamespace(tagger_python='/venv/python',remote_config='/profile.json')
    learn.run_child('example',12,7200,args)
    assert seen[0][0]==['/venv/python','-m','tagger.remote_learning','--tag','example',
        '--event','12','--config','/profile.json','--timeout-s','7200']
    assert seen[0][1]['timeout']==7200


def test_worker_scrubs_credentials(monkeypatch):
    for k in ('KURA_API_KEY','PGPASSWORD','DATABASE_URL','HF_TOKEN','HUGGING_FACE_HUB_TOKEN'):
        monkeypatch.setenv(k,'PRIVATE_SENTINEL')
    job.clean_environment()
    import os
    assert all('PRIVATE_SENTINEL'!=v for v in os.environ.values())
    assert os.environ['HF_HUB_OFFLINE']=='1'


def test_duplicate_worker_does_not_poison_result(tmp_path):
    import fcntl
    with (tmp_path/'job.lock').open('w') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        assert job.execute(tmp_path)['state']=='busy'
    assert not (tmp_path/'job-result.json').exists()


@pytest.mark.parametrize('host',['-oProxyCommand=bad','host;bad','a b'])
def test_profile_rejects_shell_and_option_hosts(tmp_path,host):
    profile=dict(host=host,root='/tmp/jobs',python='/venv/python',device='cpu',threads=4,
                 memory_bytes=12*1024**3,runtime_s=7200,min_free_ram_bytes=5*1024**3,gpu_fraction=.35)
    path=tmp_path/'profile.json';path.write_text(json.dumps(profile))
    with pytest.raises(p.ProtocolError):remote.profile(path)


@pytest.mark.parametrize(('device', 'minimum', 'accepted'), [
    ('cuda', 2 * 1024**3, True),
    ('cpu', 2 * 1024**3, False),
    ('cuda', 1024**3 - 1, False),
    ('cuda', 4 * 1024**3 + 1, False),
    ('cuda', True, False),
])
def test_worker_profile_gpu_memory_override(tmp_path, device, minimum, accepted):
    profile = dict(host='p52', root='/private/jobs', python='/venv/python',
                   device=device, threads=4, memory_bytes=6 * 1024**3,
                   runtime_s=7200, min_free_ram_bytes=5 * 1024**3,
                   gpu_fraction=.35, min_free_gpu_bytes=minimum)
    path = tmp_path / 'profile.json'
    path.write_text(json.dumps(profile))
    if accepted:
        assert remote.profile(path) == profile
    else:
        with pytest.raises(job.JobRefused, match='GPU admission'):
            remote.profile(path)


def test_dashboard_bad_measurement_not_silently_zero():
    assert '読めません' in serving_summary({'schema':'dedicated-serving-qualification/1','heldout':{}})


@pytest.mark.parametrize('mode',['lost_ack','never_started','finish_race','bad_receipt'])
def test_dispatch_recovery(tmp_path,monkeypatch,mode):
    import hashlib
    from pathlib import Path
    from tagger import dedicated_dataset
    worker=dict(host='nodeb',root='/private/jobs',python='/venv/python',device='cpu',threads=4,
        memory_bytes=12*1024**3,runtime_s=3600,min_free_ram_bytes=5*1024**3,gpu_fraction=.35)
    monkeypatch.setenv('XDG_STATE_HOME',str(tmp_path))
    monkeypatch.setattr(remote,'profile',lambda path:worker)
    monkeypatch.setattr(remote,'_gt_and_baseline',lambda tag:({'positive':100,'negative':100,
        'human_positive':100,'human_negative':100},None,dict(FLOOR,min_held_out_samples=20,min_gt_positive=10,min_gt_negative=10)))
    def prepare(tag,path,**kwargs):
        p.write_once(path,dict(schema=p.SCHEMA,tag=tag,status='prepared',gt=[{'id':1,'verdict':'positive','derived_from':'human_correction'}]))
    monkeypatch.setattr(p,'prepare',prepare)
    monkeypatch.setattr(remote, '_snapshot_has_unavailable_images', lambda doc: False)
    monkeypatch.setattr(dedicated_dataset, 'export', lambda *a, **kw: None)
    monkeypatch.setattr(dedicated_dataset,'FrozenImages',lambda *a:SimpleNamespace(doc={'sha256':'d'*64},hashes={}))
    monkeypatch.setattr(remote,'_code',lambda root:{'sha256':'c'*64})
    monkeypatch.setattr(remote.shutil,'disk_usage',lambda path:SimpleNamespace(free=100*1024**3))
    units=[];outputs={};missed_final=False
    monkeypatch.setattr(remote.time,'sleep',lambda n:None)
    def ssh(worker,args,**kwargs):
        nonlocal missed_final
        if args[0]=='systemd-run':
            units.append(args)
            raise remote.subprocess.CalledProcessError(255,['ssh'])
        if args[0]=='systemctl':
            if mode=='finish_race' and units:return 'LoadState=loaded\nActiveState=inactive'
            return 'LoadState=not-found\nActiveState=inactive'
        if args[0]=='cat':
            if not units or (mode=='never_started' and len(units)<2):
                raise remote.subprocess.CalledProcessError(1,['cat'])
            if mode=='finish_race' and not missed_final:
                missed_final=True
                raise remote.subprocess.CalledProcessError(1,['cat'])
            root=next((tmp_path/'kura-tagger'/'learning-jobs').iterdir())
            j=p.read_verified(root/'job.json');s=p.read_verified(root/'snapshot.json')
            result=p.write_once(tmp_path/'result.json',dict(schema='vnext-dedicated-measurement/1',
                tag='example',snapshot_sha256=s['sha256'],bundle_sha256='a'*64,threshold=.25,feature_contract={}))
            q=p.write_once(tmp_path/'qualification.json',dict(schema='dedicated-serving-qualification/1',measurement_sha256=result['sha256'],tag='example',snapshot_sha256=s['sha256'],bundle_sha256='a'*64,threshold=.25))
            c=p.write_once(tmp_path/'cycle-result.json',dict(schema='dedicated-cycle-result/1',qualification_sha256=q['sha256'],state='rejected'))
            outputs.update({'result.json':result,'qualification.json':q,'cycle-result.json':c,'result.json.candidate.json':{}})
            receipt=p.write_once(tmp_path/'receipt.json',dict(schema='tagger-offline-job-result/1',
                job_sha256=j['sha256'],cycle_sha256=c['sha256'],outcome='succeeded'))
            if mode=='bad_receipt':receipt['sha256']='0'*64
            return json.dumps(receipt)
        return ''
    monkeypatch.setattr(remote,'ssh',ssh)
    def command(args,**kwargs):
        if '--dry-run' in args:return 'Total transferred file size: 100 bytes'
        if args[0]=='rsync' and args[-2].startswith('nodeb:'):
            path=Path(args[-1]);path.write_text(json.dumps(outputs[path.name]))
        return ''
    monkeypatch.setattr(remote,'command',command)
    if mode=='bad_receipt':
        with pytest.raises(p.ProtocolError,match='remote receipt corrupt'):
            remote.dispatch('example',17,'profile',7200)
        assert not list(tmp_path.rglob('received-job-result.json'))
        return
    first=remote.dispatch('example',17,'profile',7200)
    second=remote.dispatch('example',17,'profile',7200)
    assert first==second and first['outcome']=='rejected'
    assert len(units)==(2 if mode=="never_started" else 1)
    assert first['registration']['artifact_sha256']=='a'*64
    assert '--property=RestrictAddressFamilies=AF_UNIX AF_NETLINK' in units[0]



def test_progress_is_not_a_permanent_running_claim(tmp_path,monkeypatch):
    from tagger import learning_status as status
    monkeypatch.setenv('XDG_STATE_HOME',str(tmp_path))
    status.write('example',1,'measuring')
    doc=json.loads(status.path().read_text())
    assert status.read(now=doc['observed_at']+10)['label']=='学習・評価を実行中'
    assert '稼働未確認' in status.read(now=doc['observed_at']+91)['label']
    status.write('example',1,'registered')
    assert status.read(now=doc['observed_at']+99999)['label']=='評価履歴を登録済み'
    assert status.path().stat().st_mode & 0o777 == 0o600


def test_dashboard_parity_failure_and_corruption_visible(tmp_path):
    m=evidence(tmp_path)
    q=m['serving_qualification'];q['parity']['passed']=False
    q['sha256']=p.digest({k:v for k,v in q.items() if k!='sha256'})
    assert '再現性検査に失敗' in serving_summary(q)
    q['heldout']['fp']=0
    assert '読めません' in serving_summary(q)


def test_review_cannot_relabel_measurement(tmp_path):
    d=decide(evidence(tmp_path),{},FLOOR,artifact_sha256='a'*64,tag='another')
    assert d.reason=='serving_evidence_invalid'


def test_support_hold_preserves_policy_evidence_without_creating_candidate(monkeypatch):
 monkeypatch.setattr(remote.learning_status,'write',lambda *a,**kw:None)
 support={'positive':{'observed':159,'required':40},'negative':{'observed':0,'required':60}}
 result=remote._hold('example',42,{'positive':159,'negative':0},'insufficient_human_split_support',support=support)
 assert result['outcome']=='hold'
 assert result['registration']['metrics']['support']==support
 assert result['registration']['artifact_ref'].startswith('(none:')


def test_review_bundle_upload_verifies_local_bytes_and_remote_kind(tmp_path, monkeypatch):
    import hashlib
    import io
    import json
    import pytest
    from tagger import learn

    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    data = b"fixture model bytes"
    sha = hashlib.sha256(data).hexdigest()
    key = hashlib.sha256(b"fixture@17").hexdigest()[:24]
    path = tmp_path / "kura-tagger" / "learning-jobs" / key / "received" / "result.json.bundle.pt"
    path.parent.mkdir(parents=True)
    path.write_bytes(data)
    payload = {"tag": "fixture", "lifecycle_event_id": 17, "artifact_sha256": sha}
    uploaded = []
    kind = "content_head_bundle"

    def upload(request, timeout):
        assert request.method == "PUT"
        assert request.full_url.endswith("/tagger/artifact/" + sha)
        uploaded.append(request.data.read())
        return io.BytesIO(json.dumps({"content_sha256": sha, "byte_size": len(data),
                                      "kind": kind}).encode())

    monkeypatch.setattr(learn.urllib.request, "urlopen", upload)
    learn._upload_review_candidate(payload, "https://fixture.invalid", "Token fixture")
    assert uploaded == [data]
    kind = "unknown"
    with pytest.raises(ValueError, match="upload receipt mismatch"):
        learn._upload_review_candidate(payload, "https://fixture.invalid", "Token fixture")
    path.write_bytes(b"wrong bytes")
    with pytest.raises(ValueError, match="artifact digest mismatch"):
        learn._upload_review_candidate(payload, "https://fixture.invalid", "Token fixture")
    assert len(uploaded) == 2



def test_offline_training_checks_installed_pillow_against_requirements(monkeypatch):
    import PIL
    from tagger import offline_learning_job

    monkeypatch.setattr(PIL, "__version__", "12.3.0")
    with pytest.raises(offline_learning_job.JobRefused, match="training_pillow_below_required_version"):
        offline_learning_job.require_training_pillow()
    monkeypatch.setattr(PIL, "__version__", "13.0.0")
    offline_learning_job.require_training_pillow()


def test_queued_measurement_stays_with_its_assigned_worker(tmp_path, monkeypatch):
    monkeypatch.setenv('XDG_STATE_HOME', str(tmp_path))
    nodeb = {'host': 'nodeb'}
    p52 = {'host': 'p52'}
    root = remote.job_root('example', 17)
    assert remote.assigned_worker_matches('example', 17, p52)
    root.mkdir(parents=True)
    p.write_once(root / 'inputs.json', {
        'schema': 'tagger-dispatch-inputs/1', 'tag': 'example',
        'event': 17, 'worker': nodeb,
    })
    assert remote.assigned_worker_matches('example', 17, nodeb)
    assert not remote.assigned_worker_matches('example', 17, p52)


def test_qualification_tick_screens_without_legacy_promotion(tmp_path, monkeypatch):
    import asyncio

    from tagger import auto_review, learn

    monkeypatch.setattr(learn, "STATE", tmp_path)
    calls = []

    async def evaluate():
        calls.append("evaluate")
        return False

    async def screen(*, review_only):
        assert review_only
        assert await learn.qualification_pass() == 0
        calls.append("screen")
        return {"promoted": []}

    async def qualify():
        calls.append("qualify")
        return False

    monkeypatch.setattr(learn, "refresh_evaluations", evaluate)
    monkeypatch.setattr(auto_review, "run", screen)
    monkeypatch.setattr(learn, "run_candidate_qualification", qualify)
    assert asyncio.run(learn.qualification_pass()) == 0
    assert calls == ["evaluate", "screen", "qualify"]


def test_dispatch_reports_composition_failure_on_stderr(monkeypatch, capsys):
    import sys

    from api.extension_hooks import ExtensionIntegrityError

    def missing_overlay(*args):
        raise ExtensionIntegrityError("invalid installed extension file: web/tests/example.mjs")

    monkeypatch.setattr(remote, "dispatch", missing_overlay)
    monkeypatch.setattr(remote.learning_status, "write", lambda *args, **kwargs: None)
    monkeypatch.setattr(sys, "argv", [
        "remote_learning", "--tag", "example", "--event", "17",
        "--config", "/profile.json", "--timeout-s", "7200",
    ])
    assert remote.main() == 1
    output = capsys.readouterr()
    assert json.loads(output.out) == {
        "outcome": "failed", "reason": "extension_integrity", "registration": None,
    }
    assert "web/tests/example.mjs" in output.err


def test_qualification_tick_verifies_before_generating_publication(monkeypatch, tmp_path):
    import asyncio

    from tagger import candidate_publication as publisher
    from tagger import learn

    plan = tmp_path / "publication.json"
    calls = []

    async def attest(sessions, settings, *, trial_declaration):
        calls.append(("attest", settings["verifier_user_id"], trial_declaration))
        return []

    async def generate(sessions, settings, *, trial_declaration):
        calls.append(("generate",))
        return {"schema": "tagger-publication-plan/1", "candidates": []}

    async def run(sessions, value):
        calls.append(("run", value["schema"]))
        return []

    monkeypatch.delenv("KURA_TAGGER_WORKER_TRIAL_PLAN", raising=False)
    monkeypatch.setenv("KURA_TAGGER_PUBLICATION_PLAN", str(plan))
    monkeypatch.setattr(publisher, "attest_qualified_candidates", attest)
    monkeypatch.setattr(publisher, "generate_publication_plan", generate)
    monkeypatch.setattr(publisher, "run_publication_plan", run)
    settings = {"schema": "tagger-automatic-publication/1", "trusted_reviewer_ids": [7],
                "policies": {}}
    plan.write_text(json.dumps(settings))
    assert asyncio.run(learn.run_candidate_qualification()) is False
    assert calls == [("generate",), ("run", "tagger-publication-plan/1")]
    calls.clear()
    plan.write_text(json.dumps(settings | {"verifier_user_id": 7}))
    assert asyncio.run(learn.run_candidate_qualification()) is False
    assert calls == [("attest", 7, {"trials": []}), ("generate",),
                     ("run", "tagger-publication-plan/1")]


def test_trial_cap_prefers_operator_tags_and_reports_the_rest():
    from tagger.worker_trial_dispatch import select_within_trial_cap

    found = [{"id": i, "tag": f"t{i}"} for i in range(1, 8)]
    chosen, deferred = select_within_trial_cap(found, explicit={"t6": {}}, worker_count=4)
    assert [row["tag"] for row in chosen] == ["t6", "t1", "t2", "t3"]
    assert deferred == ["t4", "t5", "t7"]
    assert len(chosen) * 4 <= 16
    chosen, deferred = select_within_trial_cap(found, explicit={}, worker_count=20)
    assert [row["tag"] for row in chosen] == ["t1"] and len(deferred) == 6


def _git_repo(path):
    import subprocess
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    (path / "a.txt").write_text("one\n")
    subprocess.run(["git", "-C", str(path), "add", "a.txt"], check=True)
    subprocess.run(["git", "-C", str(path), "-c", "user.name=t", "-c", "user.email=t@e",
                    "commit", "-qm", "one"], check=True)
    return subprocess.check_output(["git", "-C", str(path), "rev-parse", "HEAD"], text=True).strip()


def test_trial_source_is_the_deployed_tree_and_pinned_once(tmp_path):
    import hashlib
    import subprocess
    from tagger import worker_trial_dispatch as trials

    repo = tmp_path / "repo"
    commit = _git_repo(repo)
    tree = subprocess.check_output(["git", "-C", str(repo), "ls-tree", "-r", "HEAD"])
    assert trials.deployed_source(repo, mainline="HEAD") == (commit, hashlib.sha256(tree).hexdigest())
    # A commit the mainline does not contain is never pinned.
    subprocess.run(["git", "-C", str(repo), "checkout", "-q", "-b", "side"], check=True)
    (repo / "a.txt").write_text("two\n")
    subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@e",
                    "commit", "-qam", "two"], check=True)
    with pytest.raises(ValueError, match="not on the published mainline"):
        trials.deployed_source(repo, mainline=commit)
    calls = []

    def source():
        calls.append(1)
        return ("a" * 40, "b" * 64) if len(calls) == 1 else ("c" * 40, "d" * 64)

    first = trials.pinned_trial_source(tmp_path / "trial", source=source)
    again = trials.pinned_trial_source(tmp_path / "trial", source=source)
    assert (first["commit"], again["commit"], len(calls)) == ("a" * 40, "a" * 40, 1)


def test_auto_trial_checkout_creates_a_clean_worktree_of_the_pin(tmp_path, monkeypatch):
    import subprocess
    from tagger import worker_trial_dispatch as trials

    service = tmp_path / "service"
    _git_repo(service)
    commit, digest = trials.deployed_source(service, mainline="HEAD")
    trial_dir = tmp_path / "received"
    trials.pinned_trial_source(trial_dir, source=lambda: (commit, digest))
    monkeypatch.setattr(trials, "_worker_command", lambda worker, args, timeout=60: subprocess.run(
        args, check=True, capture_output=True, text=True, timeout=timeout).stdout)
    node = {"home": str(tmp_path / "home")}
    checkout = trials._auto_trial_checkout({"host": "x"}, node, service, trial_dir,
                                           {"source_sha256": digest})
    assert checkout == tmp_path / "home/.local/state/kura-tagger/trial-checkouts" / commit
    assert trials.deployed_source(checkout, mainline="HEAD") == (commit, digest)
    # Idempotent on the next tick; a modified or half-removed checkout is rebuilt.
    assert trials._auto_trial_checkout({"host": "x"}, node, service, trial_dir,
                                       {"source_sha256": digest}) == checkout
    (checkout / "a.txt").write_text("tampered\n")
    trials._auto_trial_checkout({"host": "x"}, node, service, trial_dir, {"source_sha256": digest})
    assert (checkout / "a.txt").read_text() == "one\n"
    import shutil
    shutil.rmtree(checkout)
    trials._auto_trial_checkout({"host": "x"}, node, service, trial_dir, {"source_sha256": digest})
    assert trials.deployed_source(checkout, mainline="HEAD") == (commit, digest)
    # Unused pinned checkouts age out; the current one is kept.
    stale = checkout.parent / ("f" * 40)
    stale.mkdir()
    subprocess.run(["touch", "-d", "30 days ago", str(stale)], check=True)
    trials._auto_trial_checkout({"host": "x"}, node, service, trial_dir, {"source_sha256": digest})
    assert not stale.exists() and checkout.exists()
    with pytest.raises(ValueError, match="needs trial settings"):
        trials._auto_trial_checkout({"host": "x"}, node, service, tmp_path / "unpinned",
                                    {"source_sha256": digest})
    with pytest.raises(ValueError, match="source pin mismatch"):
        trials._auto_trial_checkout({"host": "x"}, node, service, trial_dir,
                                    {"source_sha256": "0" * 64})


def test_lifecycle_health_names_why_a_worker_or_candidate_is_waiting():
    from api.services.lifecycle_health import waiting_on, worker_reason

    base = {"confirmed": False, "reported_at": "t", "fresh": True, "expected_provider": None,
            "provider": "CPUExecutionProvider", "release_id": 15, "active_release_id": 15}
    assert worker_reason(base | {"confirmed": True}) is None
    assert worker_reason(base | {"fresh": False}) == "heartbeat_missing"
    assert worker_reason(base | {"reported_at": None}) == "heartbeat_missing"
    assert worker_reason(base | {"expected_provider": "CUDAExecutionProvider"}) == "provider_mismatch"
    assert worker_reason(base | {"release_id": 14}) == "release_mismatch"
    assert worker_reason(base) == "not_executed_since_publication"
    ok = {"scope": "catalog_sample_outside_frozen_gt_groups",
          "evaluation": "point_floors_met_review_required",
          "workers_passed": 4, "workers_expected": 4}
    assert waiting_on(ok) == "verification_and_publication"
    assert waiting_on(ok | {"workers_passed": 3}) == "worker_trials"
    assert waiting_on(ok | {"evaluation": "awaiting_human_judgments"}) == "human_review_answers"
    assert waiting_on(ok | {"evaluation": None}) == "no_evaluation_recorded"
    assert waiting_on(ok | {"evaluation": "unreadable"}) == "evidence_unreadable"
    assert waiting_on(ok | {"evaluation": "something_new"}) == "evaluation_unresolved"
    assert waiting_on(ok | {"scope": None}) == "legacy_without_production_review"
    assert waiting_on(ok | {"scope": "catalog_extension_outside_frozen_gt_groups"}) == (
        "publication_scope_ineligible")


def test_dedicated_runtime_scales_with_frozen_image_count():
    from tagger.remote_learning import dedicated_runtime_s

    assert dedicated_runtime_s(7200, 4000) == 7200
    assert dedicated_runtime_s(7200, 12499) == 15699
    assert dedicated_runtime_s(7200, 26100) == 21600
