"""Synthetic projection controls: these are not proof of human quality or live jobs."""
import json
import os
from pathlib import Path
import pytest
from api.services import measurement_continuation as c


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.write_text(json.dumps(value)); path.chmod(0o600)
    return c.hashlib.sha256(path.read_bytes()).hexdigest()


def receipt(path, body):
    value = dict(body, sha256=c.digest(body)); write(path, value); return value


@pytest.fixture
def fixture(tmp_path):
    packet = tmp_path/'packet'; observer = tmp_path/'observer'
    packet.mkdir(mode=0o700); observer.mkdir(mode=0o700)
    original = dict(schema='original-measurement/1', artifact_sha256='a'*64, threshold=.5)
    wire = json.dumps(original); ref = c.hashlib.sha256(wire.encode()).hexdigest()
    anchor_hash = write(packet/'anchor-export.json', dict(measurement_sha256=ref, measurement=dict(canonical_body=wire)))
    module_files = {}
    for name in c.MODULES:
        path = packet/'source'/'tagger'/name
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        path.write_text('# inert synthetic source\n'); path.chmod(0o600)
        module_files[name] = c.hashlib.sha256(path.read_bytes()).hexdigest()
    observer_source = observer/'observe-union-human.py'; observer_source.write_text('# never imported\n'); observer_source.chmod(0o600)
    manifest_files = dict(module_files)
    for name, ids in [('first-sample.json', list(range(1,172))), ('extension-sample.json', list(range(172,342)))]:
        sample = receipt(observer/name, dict(model_sha256='a'*64, threshold=.5, labels={}, publish=False, frame_sha256='e'*64, post_ids=ids))
        manifest_files[name] = c.hashlib.sha256((observer/name).read_bytes()).hexdigest()
    manifest_hash = write(observer/'manifest.json', dict(model_sha256='a'*64, files=manifest_files))
    policy = dict(min_precision=.8)
    contract = dict(measurement_sha256=ref, model_sha256='a'*64, manifest_sha256=manifest_hash,
                    observer_source_sha256=c.hashlib.sha256(observer_source.read_bytes()).hexdigest(),
                    anchor_file_sha256=anchor_hash, policy_sha256=c.digest(policy),
                    source_commit='b'*40, source_files=module_files)
    write(packet/'source-contract.json', contract)
    observation = dict(model_sha256='a'*64, manifest_sha256=manifest_hash, labels={'1':'uncertain'},
                       module_sha256=module_files['stratified_evaluation.py'], action_sha256=module_files['human_evaluation_action.py'],
                       policy=policy, publish=False, labels_inferred=False)
    obs_sha = c.digest(observation)
    obs_hash = write(observer/'observations'/(obs_sha+'.json'), observation)
    write(observer/'latest.json', dict(observation_sha256=obs_sha, observed_at='2026-11-17T00:00:00Z'))
    identity = {k:contract[k] for k in ('measurement_sha256','manifest_sha256','policy_sha256')}
    identity['observation_sha256'] = obs_sha
    job = packet/'jobs-v1'/c.digest(identity)
    plan = receipt(job/'plan.json', dict(schema='human-evaluation-continuation-plan/1',
        **{k:contract[k] for k in c.BINDINGS}, threshold=.5, evaluation_input_sha256='c'*64,
        observation_sha256=obs_sha, observation_file_sha256=obs_hash,
        implementation={k:v for k,v in module_files.items() if k!='__init__.py'},
        requested=341, missing=341, provenance='frozen_observer_export_unverified', admission='not_granted'))
    waiting = receipt(job/'waiting.json', dict(schema='human-evaluation-continuation-result/1',
        plan_sha256=plan['sha256'], outcome='waiting', state='awaiting_human_judgments',
        reason='requested_judgments_missing', requested=341, missing=341, executed=False,
        next='review_remaining_images_without_guessing_labels', admission='not_granted',
        publication_eligible=False, release_or_hold_changes=False))
    for root in (packet, observer):
        for path in root.rglob('*'):
            if path.is_dir():path.chmod(0o700)
    return dict(packet=packet,observer=observer,ref=ref,original=original,job=job,plan=plan,waiting=waiting)


def project(f):
    return c.projection(f['ref'], f['original'], packet_path=str(f['packet']), observer_path=str(f['observer']))


def test_waiting_is_previous_record_not_heartbeat(fixture):
    f=fixture; before=(f['job']/'waiting.json').stat().st_mtime_ns
    a=project(f); b=project(f)
    assert a==b and a['state']=='available' and a['missing']==341
    assert a['heartbeat_available'] is False and a['admission']=='not_granted'
    assert (f['job']/'waiting.json').stat().st_mtime_ns==before
    assert a['observer_observed_at']=='2026-11-17T00:00:00Z'


def test_v2_waiting_preserves_uncertainty(fixture):
    f=fixture; namespace(f,2); plan={k:v for k,v in f['plan'].items() if k not in ('sha256','missing')}
    plan.update(schema='human-evaluation-continuation-plan/2',unseen=340,uncertain=1)
    plan=receipt(f['job']/'plan.json',plan)
    waiting={k:v for k,v in f['waiting'].items() if k not in ('sha256','missing')}
    waiting.update(schema='human-evaluation-continuation-result/2',plan_sha256=plan['sha256'],unseen=340,uncertain=1)
    receipt(f['job']/'waiting.json',waiting)
    value=project(f)
    assert value['state']=='available' and value['unseen']==340 and value['uncertain']==1
    assert 'missing' not in value


@pytest.mark.parametrize('mutation', ['source','anchor','model','threshold','hash','boolean_count','unknown_field','nan','duplicate','symlink','oversize'])
def test_fail_closed(fixture,mutation):
    f=fixture; path=f['job']/'plan.json'
    if mutation=='source':
        (f['packet']/'source/tagger/human_evaluation_continuation.py').write_text('raise RuntimeError("must never import")')
    elif mutation=='anchor':
        (f['packet']/'anchor-export.json').write_text('{}')
    elif mutation in ('model','threshold','boolean_count','unknown_field'):
        body={k:v for k,v in f['plan'].items() if k!='sha256'}
        body[dict(model='model_sha256',threshold='threshold',boolean_count='requested',unknown_field='extra')[mutation]] = dict(model='d'*64,threshold=.4,boolean_count=True,unknown_field=1)[mutation]
        receipt(path,body)
    elif mutation=='hash': path.write_text('{}')
    elif mutation=='nan': path.write_text('{"a":NaN}')
    elif mutation=='duplicate': path.write_text('{"a":1,"a":2}')
    elif mutation=='symlink': path.unlink();path.symlink_to(f['packet']/'anchor-export.json')
    elif mutation=='oversize': path.write_bytes(b' '*(c.MAX_FILE+1))
    assert project(f)['state'] in ('invalid','unavailable')


def test_missing_current_job_not_empty_quality(fixture):
    f=fixture
    (f['job']/'waiting.json').unlink();(f['job']/'plan.json').unlink();f['job'].rmdir()
    assert project(f)['reason']=='current_observation_has_no_job'


def test_claim_without_result_unknown_not_running(fixture):
    f=fixture;(f['job']/'waiting.json').unlink()
    write(f['job']/'claim.json', {})
    result=project(f)
    assert result['state']=='unavailable' and result['heartbeat_available'] is False


def test_not_configured_is_not_missing(monkeypatch):
    monkeypatch.delenv('KURA_MEASUREMENT_CONTINUATION_DIR',raising=False)
    monkeypatch.delenv('KURA_MEASUREMENT_OBSERVER_DIR',raising=False)
    assert c.projection('a'*64,{})['reason']=='not_configured'


def test_pointer_changes_during_read_refuses(fixture,monkeypatch):
    original=c.doc; count=0
    def changed(root,name):
        nonlocal count
        result=original(root,name)
        if name=='latest.json':
            count+=1
            if count==2:return result[0],'f'*64,result[2]
        return result
    monkeypatch.setattr(c,'doc',changed)
    assert project(fixture)['state']=='invalid'


def namespace(f,version):
    parent=f['packet']/f'jobs-v{version}'
    if f['job'].parent!=parent:
        old=f['job'].parent;old.rename(parent);f['job']=parent/f['job'].name


def settle(f,version,uncertain=0):
    namespace(f,version)
    latest=json.loads((f['observer']/'latest.json').read_text())
    obs=json.loads((f['observer']/'observations'/(latest['observation_sha256']+'.json')).read_text())
    obs['labels']={str(i):'uncertain' if i<=uncertain else 'present' for i in range(1,342)}
    obs_sha=c.digest(obs); obs_raw=write(f['observer']/'observations'/(obs_sha+'.json'),obs)
    latest['observation_sha256']=obs_sha;write(f['observer']/'latest.json',latest)
    plan={k:v for k,v in f['plan'].items() if k not in ('sha256','missing')}
    plan.update(schema=f'human-evaluation-continuation-plan/{version}',observation_sha256=obs_sha,observation_file_sha256=obs_raw)
    counts=dict(missing=0) if version==1 else dict(unseen=0,uncertain=uncertain)
    plan.update(counts)
    new=f['job'].parent/c.digest({k:plan[k] for k in ('measurement_sha256','manifest_sha256','policy_sha256','observation_sha256')})
    f['job'].rename(new);f['job']=new
    plan=receipt(new/'plan.json',plan);(new/'waiting.json').unlink()
    claim=receipt(new/'claim.json',dict(schema='human-evaluation-job-claim/1',plan_sha256=plan['sha256'],executor='synthetic-test',pid=123,claimed_at='2026-11-17T00:00:00Z'))
    state='review_completed_quality_unresolved' if uncertain else 'measured_floor_not_met'
    reason,next_step=c.EVALUATED[state]
    action=dict(schema=f'human-evaluation-next-action/{version}',state=state,reason=reason,next=next_step,
                input_sha256=plan['evaluation_input_sha256'],threshold=.5,measurement=dict(requested=341,missing_ids=list(range(1,uncertain+1))),
                admission='not_granted',publication_eligible=False,release_or_hold_changes=False)
    if version==2:action['responses']=dict(decisive=341-uncertain,uncertain=uncertain,unseen=0)
    assessment=receipt(new/'assessment.json',dict(schema='human-evaluation-job-assessment/1',plan_sha256=plan['sha256'],claim_sha256=claim['sha256'],assessment=action,admission='not_granted',publication_eligible=False))
    result=receipt(new/'result.json',dict(schema=f'human-evaluation-continuation-result/{version}',plan_sha256=plan['sha256'],claim_sha256=claim['sha256'],assessment_sha256=assessment['sha256'],outcome='succeeded',state=state if uncertain else 'evaluated_not_admitted',reason=reason,next=next_step,evaluation_state=state,requested=341,**counts,executed=True,admission='not_granted',publication_eligible=False,release_or_hold_changes=False))
    return result


@pytest.mark.parametrize(('version','uncertain'),[(1,0),(2,0),(2,1),(2,341)])
def test_complete_chain_remains_not_admitted(fixture,version,uncertain):
    settle(fixture,version,uncertain)
    value=project(fixture)
    assert value['state']=='available'
    assert value['record_state']==('review_completed_quality_unresolved' if uncertain else 'evaluated_not_admitted')
    assert value['admission']=='not_granted' and value['publication_eligible'] is False
    if uncertain:assert value['uncertain']==uncertain and value['unseen']==0


@pytest.mark.parametrize('field',['claim_sha256','assessment_sha256','plan_sha256','state','publication_eligible'])
def test_completed_substitution_refused(fixture,field):
    result=settle(fixture,2,1)
    body={k:v for k,v in result.items() if k!='sha256'}
    body[field]=True if field=='publication_eligible' else 'f'*64
    receipt(fixture['job']/'result.json',body)
    assert project(fixture)['state']=='invalid'


def test_future_schema_unavailable(fixture):
    body={k:v for k,v in fixture['plan'].items() if k!='sha256'}
    body['schema']='human-evaluation-continuation-plan/3'
    receipt(fixture['job']/'plan.json',body)
    assert project(fixture)['reason']=='schema_unsupported'


def test_http_projection_no_store_and_no_path_authority(fixture,monkeypatch):
    import asyncio
    from fastapi import FastAPI, HTTPException
    from httpx import ASGITransport, AsyncClient
    from api.routers import tagger_measurements as routes
    app=FastAPI();app.include_router(routes.router)
    async def database():yield object()
    async def allowed():return object()
    app.dependency_overrides[routes.get_db]=database
    app.dependency_overrides[routes.get_current_user]=allowed
    async def anchor(db,ref):
        assert ref==fixture['ref'];return dict(body=fixture['original'])
    monkeypatch.setattr(routes.service,'anchor',anchor)
    monkeypatch.setenv('KURA_MEASUREMENT_CONTINUATION_DIR',str(fixture['packet']))
    monkeypatch.setenv('KURA_MEASUREMENT_OBSERVER_DIR',str(fixture['observer']))
    async def go():
        async with AsyncClient(transport=ASGITransport(app=app),base_url='http://fixture') as client:
            url='/api/tagger/measurements/'+fixture['ref']+'/continuation'
            ok=await client.get(url);assert ok.status_code==200 and ok.json()['state']=='available'
            for suffix in ('?path=/etc/passwd','?root=secret','?a=1&a=2'):
                bad=await client.get(url+suffix);assert bad.status_code==422
                assert bad.headers['cache-control']=='private, no-store' and 'secret' not in bad.text
            bad=await client.get('/api/tagger/measurements/not-a-hash/continuation')
            assert bad.status_code==422 and bad.headers['cache-control']=='private, no-store'
            monkeypatch.setenv('KURA_DB_ROLE','replica')
            bad=await client.get(url);assert bad.status_code==503 and bad.headers['cache-control']=='private, no-store'
            monkeypatch.setenv('KURA_DB_ROLE','canonical')
            async def denied():raise HTTPException(401,'not authenticated')
            app.dependency_overrides[routes.get_current_user]=denied
            bad=await client.get(url);assert bad.status_code==401 and bad.headers['cache-control']=='private, no-store'
    asyncio.run(go())


def bind_review(f, **changes):
    contract=json.loads((f['packet']/'source-contract.json').read_text())
    q=dict(schema='human-review-set/2',purpose='candidate_evaluation',model_sha256=contract['model_sha256'],post_ids=list(range(1,342)),
           source_provenance=dict(kind='frozen_candidate_sample',artifact_sha256='2'*64,supersedes_queue_sha256='3'*64))
    queue_sha=write(f['observer']/'candidate-queue.json',q)
    binding=dict(schema='explicit-candidate-continuation-binding/1',queue_sha256=queue_sha,
                 original_manifest_sha256='2'*64,manifest_sha256=contract['manifest_sha256'],
                 observer_source_sha256=contract['observer_source_sha256'],model_sha256=contract['model_sha256'],
                 supersedes_queue_sha256='3'*64,response_source='authenticated_account_explicit_events_not_independence_proof',
                 admission='not_granted')
    latest=json.loads((f['observer']/'latest.json').read_text())
    obs=json.loads((f['observer']/'observations'/(latest['observation_sha256']+'.json')).read_text())
    obs.update(schema='explicit-candidate-observation/1',queue_sha256=queue_sha,
               response_provenance=binding['response_source'],event_refs={'1':dict(event_id=1,actor_id=1,received_at='2026-11-17T00:00:00Z',source_context_sha256='e'*64)})
    obs_sha=c.digest(obs);obs_raw=write(f['observer']/'observations'/(obs_sha+'.json'),obs)
    latest['observation_sha256']=obs_sha;write(f['observer']/'latest.json',latest)
    plan={k:v for k,v in json.loads((f['job']/'plan.json').read_text()).items() if k!='sha256'}
    plan.update(observation_sha256=obs_sha,observation_file_sha256=obs_raw)
    new=f['job'].parent/c.digest({k:plan[k] for k in ('measurement_sha256','manifest_sha256','policy_sha256','observation_sha256')})
    f['job'].rename(new);f['job']=new;plan=receipt(new/'plan.json',plan)
    waiting={k:v for k,v in f['waiting'].items() if k!='sha256'};waiting['plan_sha256']=plan['sha256'];receipt(new/'waiting.json',waiting)
    binding.update(changes)
    contract['review_binding_sha256']=write(f['packet']/'review-binding.json',binding)
    write(f['packet']/'source-contract.json',contract)
    write(f['packet']/'pins.json',contract)
    return binding


def test_explicit_binding_preserves_distinct_sample_manifest(fixture):
    binding=bind_review(fixture)
    result=project(fixture)
    assert result['state']=='available' and result['review_binding']==binding
    assert binding['original_manifest_sha256']!=result['manifest_sha256']


@pytest.mark.parametrize('change',[dict(model_sha256='f'*64),dict(manifest_sha256='f'*64),dict(observer_source_sha256='f'*64),dict(extra=1),dict(admission='granted'),dict(queue_sha256='3'*64)])
def test_changed_review_binding_refused(fixture,change):
    bind_review(fixture,**change)
    assert project(fixture)['state']=='invalid'


def test_unpinned_binding_not_inferred(fixture):
    binding=bind_review(fixture)
    path=fixture['packet']/'pins.json';pins=json.loads(path.read_text());pins['review_binding_sha256']='f'*64;write(path,pins)
    assert project(fixture)['state']=='invalid'


def test_absent_binding_never_guesses_queue(fixture):
    assert project(fixture)['review_binding'] is None


def test_ambiguous_job_namespace_refused(fixture):
    (fixture['packet']/'jobs-v2').mkdir(mode=0o700)
    assert project(fixture)['state']=='invalid'


def test_job_namespace_must_match_declared_schema(fixture):
    namespace(fixture,2)
    assert project(fixture)['state']=='invalid'


def rewire_observation(f, change):
    latest=json.loads((f['observer']/'latest.json').read_text())
    obs=json.loads((f['observer']/'observations'/(latest['observation_sha256']+'.json')).read_text())
    change(obs)
    sha=c.digest(obs);raw=write(f['observer']/'observations'/(sha+'.json'),obs)
    latest['observation_sha256']=sha;write(f['observer']/'latest.json',latest)
    body={k:v for k,v in json.loads((f['job']/'plan.json').read_text()).items() if k!='sha256'}
    body.update(observation_sha256=sha,observation_file_sha256=raw)
    new=f['job'].parent/c.digest({k:body[k] for k in ('measurement_sha256','manifest_sha256','policy_sha256','observation_sha256')})
    f['job'].rename(new);f['job']=new;plan=receipt(new/'plan.json',body)
    waiting={k:v for k,v in json.loads((new/'waiting.json').read_text()).items() if k!='sha256'}
    waiting['plan_sha256']=plan['sha256'];receipt(new/'waiting.json',waiting)


@pytest.mark.parametrize('change',[
    lambda o:o.update(queue_sha256='f'*64),
    lambda o:o.update(response_provenance='old_pins'),
    lambda o:o.update(event_refs={}),
    lambda o:o['event_refs']['1'].update(actor_id=True),
    lambda o:o['event_refs']['1'].update(event_id=0),
    lambda o:o['event_refs']['1'].update(received_at='2026-11-17T00:00:00'),
    lambda o:o['event_refs']['1'].update(source_context_sha256='not-a-sha'),
])
def test_explicit_event_bindings_refuse_even_rehashed_chain(fixture,change):
    bind_review(fixture);rewire_observation(fixture,change)
    assert project(fixture)['state']=='invalid'
