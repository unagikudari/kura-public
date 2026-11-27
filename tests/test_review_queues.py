import asyncio
import hashlib
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock
from urllib.parse import parse_qs, urlsplit

import pytest
from api.services.review_queues import load_queues, ReviewQueueRefused
from api.routers import pools, posts
from api.errors import KuraError



@pytest.fixture(autouse=True)
def file_import_adapter(monkeypatch):
    # These unit tests exercise the legacy file validator and endpoint rendering;
    # real DB registry/endpoint integration is covered by test_review_set_registry.
    from api.services import review_reselection, review_set_registry
    async def legacy(db):
        return load_queues()
    monkeypatch.setattr(review_set_registry,"load_configured",legacy)
    monkeypatch.setattr(review_set_registry,"legacy_lora_refs",AsyncMock(return_value=None))
    monkeypatch.setattr(review_set_registry,"candidates_for_release_review",AsyncMock(return_value=[]))
    monkeypatch.setattr(review_reselection,"retired_refs",AsyncMock(return_value=set()))

def item():
    return dict(schema='human-review-set/1', tag='some_tag', purpose='fixed_evaluation',
                frame_sha256='a'*64, model_sha256='b'*64, post_ids=[3, 8, 12])


def write(root, value):
    raw = json.dumps(value, sort_keys=True).encode()
    path = root / (hashlib.sha256(raw).hexdigest() + '.json')
    path.write_bytes(raw)
    return path


def test_immutable_set_links_to_existing_human_flow_without_pool(tmp_path):
    path = write(tmp_path, item())
    before = path.read_bytes()
    queue, = load_queues(tmp_path)
    assert path.read_bytes() == before
    query = parse_qs(urlsplit(queue['browse_url']).query)
    assert query == {'q': ['ids:3,8,12'], 'bulk': ['1'], 'bulkTag': ['some_tag']}
    assert ' -locked:some_tag -removed:some_tag' in parse_qs(urlsplit(queue['unjudged_url']).query)['q'][0]
    assert not ({'scores', 'labels', 'quality_pass'} & queue.keys())


@pytest.mark.parametrize('change', [
    {'post_ids': []}, {'post_ids': [True]}, {'post_ids': [1,1]}, {'post_ids': [0]},
    {'post_ids': [2**31]}, {'post_ids': list(range(1,502))}, {'post_ids': '1,2'},
    {'tag': 'tag other'}, {'tag': ''}, {'purpose': 'publish'}, {'labels': [True]},
    {'tag': 'a\x7f'}, {'tag': 'a\x80'},
    {'model_sha256': 'not-a-hash'}, {'schema': 'new'},
    {'model_sha256': None},
])
def test_invalid_set_refuses_not_empty_success(tmp_path, change):
    write(tmp_path, dict(item(), **change))
    with pytest.raises(ReviewQueueRefused): load_queues(tmp_path)


def test_training_support_does_not_invent_a_model(tmp_path):
    write(tmp_path,dict(item(),purpose='training_support',model_sha256=None))
    queue, = load_queues(tmp_path)
    assert queue['model_sha256'] is None


def test_symlink_and_changed_bytes_refuse(tmp_path):
    path = write(tmp_path, item()); raw = path.read_bytes()
    path.write_bytes(raw + b' ')
    with pytest.raises(ReviewQueueRefused, match='digest_mismatch'): load_queues(tmp_path)
    path.unlink(); target = tmp_path.parent / 'target-set'; target.write_bytes(raw)
    path.symlink_to(target)
    with pytest.raises(ReviewQueueRefused, match='file_unreadable'): load_queues(tmp_path)


def test_missing_is_distinct_from_empty_and_symlink_root(tmp_path):
    assert load_queues(tmp_path/'missing') is None
    assert load_queues(tmp_path) == []
    link = tmp_path.parent/'queue-link'; link.symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(ReviewQueueRefused, match='directory_unreadable'): load_queues(link)


def test_duplicate_json_key_refuses(tmp_path):
    raw = b'{"schema": "human-review-set/1", "schema": "human-review-set/1"}'
    (tmp_path/(hashlib.sha256(raw).hexdigest()+'.json')).write_bytes(raw)
    with pytest.raises(ReviewQueueRefused, match='duplicate_key'): load_queues(tmp_path)


def test_fifo_is_refused_without_waiting_for_writer(tmp_path):
    import os
    os.mkfifo(tmp_path / ('a'*64+'.json'))
    with pytest.raises(ReviewQueueRefused, match='file_invalid'): load_queues(tmp_path)


def test_endpoint_counts_only_explicit_pins_and_does_not_write(tmp_path, monkeypatch):
    write(tmp_path,item()); monkeypatch.setenv('KURA_REVIEW_QUEUE_DIR',str(tmp_path))
    db = SimpleNamespace(execute=AsyncMock(side_effect=[SimpleNamespace(all=lambda:[('some_tag',41,'some_tag')]), SimpleNamespace(all=lambda:[(3,'present'),(8,'absent')])]))
    response = asyncio.run(pools.review_queues(db, SimpleNamespace(name='reader')))
    queue, = response['queues']
    assert (queue['total'],queue['remaining'],queue['judged']) == (3,1,{'present':1,'absent':1})
    assert 'post_ids' not in queue
    assert str(db.execute.call_args.args[0]).startswith('SELECT ')


def test_queue_metadata_requires_authenticated_reader():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    app = FastAPI(); app.include_router(pools.dashboard_router)
    from fastapi.responses import JSONResponse
    @app.exception_handler(KuraError)
    async def refusal(request, exc):
        return JSONResponse(status_code=exc.status, content={'detail':exc.body()})
    db = SimpleNamespace(execute=AsyncMock())
    app.dependency_overrides[pools.get_db] = lambda: db
    with TestClient(app) as client:
        response = client.get('/api/tagger-dashboard/review-queues')
    assert response.status_code == 401
    db.execute.assert_not_called()


@pytest.mark.parametrize('query', ['ids:', 'ids:1,,2', 'ids:0', 'ids:-1', 'ids:1,x',
                                  'ids:2147483648', 'ids:１', 'locked:x ids:bad',
                                  'ids:'+','.join(map(str,range(1,502)))])
def test_invalid_id_sets_refuse_before_any_database_read(query):
    db = SimpleNamespace(execute=AsyncMock())
    with pytest.raises(KuraError) as exc:
        asyncio.run(posts._build_where_clauses(db, query))
    assert exc.value.status == 422
    db.execute.assert_not_called()


@pytest.mark.parametrize('query,operator', [('ids:3,8,3', 'IN'), ('-ids:3,8','NOT IN')])
def test_id_set_uses_bound_membership_not_or_across_other_filters(query,operator):
    clauses, pool = asyncio.run(posts._build_where_clauses(None, query+' safety:safe'))
    assert pool is None and len(clauses)==2
    compiled=clauses[0].compile()
    assert operator in str(compiled)
    assert [3,8] in compiled.params.values()


@pytest.mark.parametrize('resolved', [[], [(41,'alias_target')], [(41,'Some_Tag')], [(41,'some_tag'), (42,'some_tag')]])
def test_endpoint_refuses_unknown_noncanonical_or_ambiguous_tag(tmp_path, monkeypatch, resolved):
    write(tmp_path,item()); monkeypatch.setenv('KURA_REVIEW_QUEUE_DIR',str(tmp_path))
    db = SimpleNamespace(execute=AsyncMock(return_value=SimpleNamespace(all=lambda:[('some_tag',tag_id,canonical) for tag_id,canonical in resolved])))
    with pytest.raises(KuraError) as exc:
        asyncio.run(pools.review_queues(db, SimpleNamespace(name='reader')))
    assert exc.value.status == 503
    assert 'tag_unresolved' in str(exc.value.body())
    assert db.execute.call_count == 1


def test_directory_entry_does_not_leak_descriptor(tmp_path):
    import os
    (tmp_path/('a'*64+'.json')).mkdir()
    before=len(os.listdir('/proc/self/fd'))
    for _ in range(5):
        with pytest.raises(ReviewQueueRefused,match='file_invalid'):load_queues(tmp_path)
    assert len(os.listdir('/proc/self/fd'))==before


def test_oversize_encoded_link_is_refused(tmp_path):
    write(tmp_path,dict(item(),tag='字'*200,post_ids=list(range(2147483000,2147483500))))
    with pytest.raises(ReviewQueueRefused,match='link_too_long'):load_queues(tmp_path)


def test_boundary_request_keeps_model_and_never_infers_labels(tmp_path):
    write(tmp_path,dict(item(),purpose='boundary_recheck'))
    q,=load_queues(tmp_path)
    assert q['purpose']=='boundary_recheck' and q['model_sha256']=='b'*64
    assert 'labels' not in q


def test_candidate_evaluation_request_requires_exact_model_and_no_labels(tmp_path):
    write(tmp_path,dict(item(),purpose='candidate_evaluation'))
    q,=load_queues(tmp_path)
    assert q['purpose']=='candidate_evaluation' and q['model_sha256']=='b'*64
    assert 'labels' not in q


def test_candidate_evaluation_cannot_omit_model(tmp_path):
    write(tmp_path,dict(item(),purpose='candidate_evaluation',model_sha256=None))
    with pytest.raises(ReviewQueueRefused,match='reference_invalid'):
        load_queues(tmp_path)


@pytest.mark.parametrize('field',['labels','predicted_labels'])
def test_candidate_request_cannot_smuggle_labels(tmp_path,field):
    write(tmp_path,{**item(),'purpose':'candidate_evaluation',field:{'1':True}})
    with pytest.raises(ReviewQueueRefused,match='shape_invalid'):
        load_queues(tmp_path)


def explicit_item():
    return dict(item(), schema='human-review-set/2', purpose='historical_label_recheck',
                model_sha256=None, source_provenance={'kind':'historical_human_training',
                'artifact_sha256':'c'*64}, media_sha256={str(i):'d'*64 for i in (3,8,12)})

def test_explicit_recheck_preserves_frozen_provenance_and_uses_new_response_route(tmp_path):
    write(tmp_path, explicit_item())
    q, = load_queues(tmp_path)
    assert q['browse_url'] == q['unjudged_url'] == '/tagger-review/' + q['queue_sha256']
    assert q['source_provenance'] == explicit_item()['source_provenance']
    assert q['model_sha256'] is None

@pytest.mark.parametrize('change', [
    {'media_sha256':{'3':'d'*64}},
    {'media_sha256':{'3':'d'*64,'8':'d'*64,'12':'not-hash'}},
    {'source_provenance':{'kind':'frozen_evaluation','artifact_sha256':'c'*64}},
    {'source_provenance':{'kind':'historical_human_training','artifact_sha256':'bad'}},
    {'purpose':'explicit_evaluation'},
    {'labels':[True,False,True]},
])
def test_explicit_recheck_refuses_incomplete_media_or_relabelled_provenance(tmp_path, change):
    write(tmp_path,dict(explicit_item(),**change))
    with pytest.raises(ReviewQueueRefused): load_queues(tmp_path)


def test_candidate_explicit_preserves_original_and_only_supersedes_same_population(tmp_path):
    old = item() | {'purpose': 'candidate_evaluation'}
    original = write(tmp_path, old)
    explicit = old | dict(schema='human-review-set/2', media_sha256={str(i):'c'*64 for i in old['post_ids']},
        source_provenance={'kind':'frozen_candidate_sample','artifact_sha256':'d'*64,'supersedes_queue_sha256':original.stem})
    new = write(tmp_path, explicit)
    rows = load_queues(tmp_path)
    assert next(q for q in rows if q['queue_sha256']==original.stem)['superseded_by']==new.stem
    assert original.read_bytes() and len([q for q in rows if not q.get('superseded_by')])==1
    assert next(q for q in rows if q['queue_sha256']==new.stem)['browse_url']=='/tagger-review/'+new.stem
    new.unlink()
    write(tmp_path, explicit | {'post_ids':[3,8], 'media_sha256':{'3':'c'*64,'8':'c'*64}})
    with pytest.raises(ReviewQueueRefused, match='supersession_population_changed'):load_queues(tmp_path)


@pytest.mark.parametrize('field,value',[('frame_sha256','e'*64),('model_sha256','f'*64),('tag','other'),('post_ids',[12,8,3])])
def test_candidate_supersession_cannot_change_bound_population(tmp_path,field,value):
    old=item()|{'purpose':'candidate_evaluation'};original=write(tmp_path,old)
    new=old|dict(schema='human-review-set/2',media_sha256={str(i):'c'*64 for i in old['post_ids']},
      source_provenance={'kind':'frozen_candidate_sample','artifact_sha256':'d'*64,'supersedes_queue_sha256':original.stem})
    write(tmp_path,new|{field:value})
    with pytest.raises(ReviewQueueRefused,match='supersession_population_changed'):load_queues(tmp_path)


def test_explicit_training_support_preserves_model_free_population_and_request(tmp_path):
    from api.services.review_requests import load_requests
    qr=tmp_path/'queues';qr.mkdir();rr=tmp_path/'requests';rr.mkdir(mode=0o700)
    old=dict(item(),purpose='training_support',model_sha256=None)
    original=write(qr,old)
    new=dict(old,schema='human-review-set/2',media_sha256={str(i):'c'*64 for i in old['post_ids']},
             source_provenance={'kind':'frozen_training_support','artifact_sha256':'d'*64,
                                'supersedes_queue_sha256':original.stem})
    replacement=write(qr,new)
    request=dict(schema='review-request/1',queue_sha256=replacement.stem,frame_sha256=old['frame_sha256'],
                 reason='class_group_support',evidence_sha256='e'*64,requested_at='2026-11-17T00:00:00Z')
    request_file=write(rr,request);request_file.chmod(0o600)
    loaded=load_queues(qr);active=[q for q in loaded if not q.get('superseded_by')]
    assert len(active)==1 and active[0]['model_sha256'] is None
    assert active[0]['post_ids']==old['post_ids']
    assert active[0]['browse_url']=='/tagger-review/'+replacement.stem
    assert load_requests(loaded,rr)=={replacement.stem:request}
    assert json.loads(original.read_bytes())==old


@pytest.mark.parametrize('change',[{'post_ids':[3,8]}, {'frame_sha256':'f'*64}, {'tag':'other'}])
def test_training_support_supersession_rejects_population_drift(tmp_path,change):
    old=dict(item(),purpose='training_support',model_sha256=None);source=write(tmp_path,old)
    new=dict(old,**change);new.update(schema='human-review-set/2',
        media_sha256={str(i):'c'*64 for i in new['post_ids']},
        source_provenance={'kind':'frozen_training_support','artifact_sha256':'d'*64,'supersedes_queue_sha256':source.stem})
    write(tmp_path,new)
    with pytest.raises(ReviewQueueRefused,match='supersession_population_changed'):load_queues(tmp_path)


def test_explicit_training_support_refuses_invented_model(tmp_path):
    old=dict(item(),purpose='training_support',model_sha256=None);source=write(tmp_path,old)
    write(tmp_path,dict(old,schema='human-review-set/2',model_sha256='f'*64,
        media_sha256={str(i):'c'*64 for i in old['post_ids']},
        source_provenance={'kind':'frozen_training_support','artifact_sha256':'d'*64,'supersedes_queue_sha256':source.stem}))
    with pytest.raises(ReviewQueueRefused,match='training_support_model_not_applicable'):load_queues(tmp_path)


@pytest.mark.parametrize('purpose', ['fixed_evaluation', 'boundary_recheck', 'production_precision'])
def test_any_frozen_request_opens_in_the_review_view_through_a_bound_successor(tmp_path, purpose):
    old = item() | {'purpose': purpose}
    original = write(tmp_path, old)
    new = write(tmp_path, old | dict(schema='human-review-set/2', media_sha256={str(i): 'c'*64 for i in old['post_ids']},
        source_provenance={'kind': 'frozen_review_set', 'artifact_sha256': 'd'*64, 'supersedes_queue_sha256': original.stem}))
    rows = load_queues(tmp_path)
    assert next(q for q in rows if q['queue_sha256'] == original.stem)['superseded_by'] == new.stem
    assert next(q for q in rows if q['queue_sha256'] == new.stem)['browse_url'] == '/tagger-review/' + new.stem


def test_frozen_successor_kind_follows_the_purpose(tmp_path):
    old = item() | {'purpose': 'fixed_evaluation'}
    original = write(tmp_path, old)
    write(tmp_path, old | dict(schema='human-review-set/2', media_sha256={str(i): 'c'*64 for i in old['post_ids']},
        source_provenance={'kind': 'frozen_candidate_sample', 'artifact_sha256': 'd'*64, 'supersedes_queue_sha256': original.stem}))
    with pytest.raises(ReviewQueueRefused): load_queues(tmp_path)


def test_lora_origin_keeps_existing_review_identity(tmp_path):
    from api.services.review_queues import read_legacy_lora_refs, review_lanes

    findings = dict(item(), schema='human-review-set/2', purpose='historical_label_recheck',
                    model_sha256=None, media_sha256={str(i): 'c'*64 for i in (3, 8, 12)})
    provenance = {'kind': 'historical_human_training', 'artifact_sha256': 'd'*64}
    legacy = write(tmp_path, dict(findings, source_provenance=provenance))
    explicit = write(tmp_path, dict(findings, source_provenance=dict(provenance, origin='lora_findings')))
    routine = write(tmp_path, item())
    queues = load_queues(tmp_path)
    manifest = tmp_path / 'origins.json'
    manifest.write_text(json.dumps([legacy.stem, routine.stem]))
    review_lanes(queues, read_legacy_lora_refs(manifest))
    assert {q['queue_sha256']: q['review_lane'] for q in queues} == {
        legacy.stem: 'lora_findings', explicit.stem: 'lora_findings', routine.stem: 'lifecycle',
    }
    assert hashlib.sha256(legacy.read_bytes()).hexdigest() == legacy.stem
