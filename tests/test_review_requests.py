"""Requests never rewrite a queue or label; all I/O uses private temporary files."""
import asyncio
import hashlib
import json
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock
import pytest
from api.services.review_requests import load_requests, ReviewRequestRefused
from api.services.review_queues import load_queues
from api.routers import pools


def queue():
    return dict(schema='human-review-set/1',purpose='training_support',tag='fixture',
                frame_sha256='a'*64,model_sha256=None,post_ids=[3,8])


def write(root,item):
    raw=json.dumps(item,sort_keys=True).encode();path=root/(hashlib.sha256(raw).hexdigest()+'.json');path.write_bytes(raw);path.chmod(0o600);return path


def fixture(tmp_path):
    qr=tmp_path/'queues';rr=tmp_path/'requests';qr.mkdir();rr.mkdir(mode=0o700);path=write(qr,queue());q,=load_queues(qr)
    item=dict(schema='review-request/1',queue_sha256=q['queue_sha256'],frame_sha256=q['frame_sha256'],
              reason='class_group_support',evidence_sha256='b'*64,requested_at='2026-11-17T00:00:00Z')
    return qr,rr,q,item,path


def test_request_binds_existing_queue_without_mutation(tmp_path):
    qr,rr,q,item,path=fixture(tmp_path);before=path.read_bytes();write(rr,item)
    assert load_requests([q],rr)=={q['queue_sha256']:item}
    assert path.read_bytes()==before and load_queues(qr)==[q]


@pytest.mark.parametrize('change',[
    {'queue_sha256':'bad'},{'queue_sha256':'d'*64},{'frame_sha256':'c'*64},{'evidence_sha256':False},
    {'reason':'quality_pass'},{'schema':'other'},{'labels':[]},{'requested_at':'2026-11-17'},
    {'requested_at':'2026-02-30T00:00:00Z'},{'requested_at':True},
])
def test_invalid_contract_refuses(tmp_path,change):
    _,rr,q,item,_=fixture(tmp_path);write(rr,dict(item,**change))
    with pytest.raises(ReviewRequestRefused):load_requests([q],rr)


def test_duplicate_target_and_duplicate_json_key_refuse(tmp_path):
    _,rr,q,item,_=fixture(tmp_path);a=write(rr,item);b=write(rr,dict(item,requested_at='2026-11-17T01:00:00Z'))
    with pytest.raises(ReviewRequestRefused,match='duplicate_request'):load_requests([q],rr)
    a.unlink();b.unlink();raw=b'{"schema":"review-request/1","schema":"review-request/1"}'
    bad=rr/(hashlib.sha256(raw).hexdigest()+'.json');bad.write_bytes(raw);bad.chmod(0o600)
    with pytest.raises(ReviewRequestRefused,match='duplicate_key'):load_requests([q],rr)


@pytest.mark.parametrize('kind',['symlink','root_symlink','oversized','fifo','writable','digest'])
def test_unsafe_files_refuse(tmp_path,kind):
    _,rr,q,item,_=fixture(tmp_path)
    path=write(rr,item)
    if kind=='symlink':
        target=tmp_path/'target';path.rename(target);path.symlink_to(target)
    elif kind=='root_symlink':
        link=tmp_path/'link';link.symlink_to(rr,target_is_directory=True);rr=link
    elif kind=='oversized':path.write_bytes(b' '*4097)
    elif kind=='fifo':path.unlink();os.mkfifo(path)
    elif kind=='writable':path.chmod(0o666)
    else:path.write_bytes(path.read_bytes()+b' ')
    with pytest.raises(ReviewRequestRefused):load_requests([q],rr)


def test_missing_default_empty_and_missing_configured_are_distinct(tmp_path,monkeypatch):
    monkeypatch.delenv('KURA_REVIEW_REQUEST_DIR',raising=False);monkeypatch.setenv('HOME',str(tmp_path))
    assert load_requests([]) is None
    assert load_requests([],tmp_path)=={}
    monkeypatch.setenv('KURA_REVIEW_REQUEST_DIR',str(tmp_path/'missing'))
    with pytest.raises(ReviewRequestRefused,match='directory_missing'):load_requests([])


@pytest.mark.parametrize('kind',['valid','invalid','empty'])
def test_endpoint_keeps_pin_semantics_and_exposes_unknown_request_state(tmp_path,monkeypatch,kind):
    qr,rr,q,item,_=fixture(tmp_path)
    if kind!='empty':write(rr,item if kind=='valid' else dict(item,frame_sha256='c'*64))
    monkeypatch.setenv('KURA_REVIEW_QUEUE_DIR',str(qr));monkeypatch.setenv('KURA_REVIEW_REQUEST_DIR',str(rr))
    db=SimpleNamespace(execute=AsyncMock(side_effect=[
        SimpleNamespace(scalar_one=lambda: False),
        SimpleNamespace(all=lambda:[(41,'fixture')]),
        SimpleNamespace(all=lambda:[(3,'present')]),
    ]))
    result=asyncio.run(pools.review_queues(db,SimpleNamespace(name='reader')));got,=result['queues']
    assert got['queue_sha256']==q['queue_sha256'] and got['unjudged_url']==q['unjudged_url']
    assert got['remaining']==1 and got['judged']=={'present':1}
    assert got['request']==(item if kind=='valid' else None)
    assert result['requests_state']==('unavailable' if kind=='invalid' else 'available')
    assert got['request_unavailable']==(kind=='invalid')
    assert (result['request_unavailable'] is not None)==(kind=='invalid')
    assert all(str(call.args[0]).startswith('SELECT ') for call in db.execute.call_args_list)


def test_directory_read_failure_refuses(tmp_path,monkeypatch):
    from api.services import review_requests as service
    _,rr,q,_,_=fixture(tmp_path)
    with monkeypatch.context() as patch:
        def unavailable(*args):raise PermissionError('private path must not escape')
        patch.setattr(service.os,'listdir',unavailable)
        with pytest.raises(ReviewRequestRefused,match='^directory_unreadable$'):load_requests([q],rr)


def test_request_for_non_training_queue_refuses(tmp_path):
    _,rr,q,item,_=fixture(tmp_path);write(rr,item)
    q['purpose']='fixed_evaluation'
    with pytest.raises(ReviewRequestRefused,match='queue_kind_invalid'):load_requests([q],rr)


@pytest.mark.parametrize('value',['','relative/path','~kura_nonexistent_review_user/requests'])
def test_configured_path_must_resolve_absolute_nonblank(tmp_path,monkeypatch,value):
    monkeypatch.setenv('KURA_REVIEW_REQUEST_DIR',value)
    with pytest.raises(ReviewRequestRefused,match='directory_path_invalid'):load_requests([])


def test_writable_directory_is_refused(tmp_path):
    _,rr,q,item,_=fixture(tmp_path);write(rr,item);rr.chmod(0o770)
    with pytest.raises(ReviewRequestRefused,match='directory_invalid'):load_requests([q],rr)


@pytest.mark.parametrize('unexpected',[False,True])
def test_invalid_metadata_preserves_existing_purpose_request(tmp_path,monkeypatch,unexpected):
    from api.services import review_requests as service
    qr,rr,q,item,_=fixture(tmp_path);write(rr,dict(item,frame_sha256='c'*64))
    write(qr,dict(queue(),tag='existing_request',purpose='boundary_recheck',model_sha256='f'*64))
    monkeypatch.setenv('KURA_REVIEW_QUEUE_DIR',str(qr));monkeypatch.setenv('KURA_REVIEW_REQUEST_DIR',str(rr))
    if unexpected:
        def failed(*args):raise RuntimeError('private operator path')
        monkeypatch.setattr(service,'load_requests',failed)
    async def query(statement, params=None):
        sql = str(statement)
        if 'to_regclass' in sql:
            return SimpleNamespace(scalar_one=lambda: True)
        if 'SELECT EXISTS(SELECT FROM tagger_review_registry_activation)' in sql:
            return SimpleNamespace(scalar_one=lambda: False)
        if 'canonical.name' in sql:return SimpleNamespace(all=lambda:[(41,params['tag'])])
        return SimpleNamespace(all=lambda:[(3,'present')])
    db=SimpleNamespace(execute=AsyncMock(side_effect=query))
    result=asyncio.run(pools.review_queues(db,SimpleNamespace(name='reader')))
    assert len(result['queues'])==2 and result['requests_state']=='unavailable'
    training=next(q for q in result['queues'] if q['purpose']=='training_support')
    existing=next(q for q in result['queues'] if q['purpose']=='boundary_recheck')
    assert training['request_unavailable'] and not existing['request_unavailable']
    assert all(q['remaining']==1 and q['judged']=={'present':1} for q in result['queues'])
    assert existing['unjudged_url'].startswith('/?q=')
    assert result['request_unavailable']=={'reason':'request_metadata_unavailable'}
