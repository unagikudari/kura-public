import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock
import pytest
from api.services.review_comparison import load_scores, compare
from api.services.review_queues import load_queues, ReviewQueueRefused
from api.routers.pools import review_queues
from tests.test_review_queues import item, write


def frozen(q):
    return dict(schema='frozen-review-scores/1',queue_sha256=q['queue_sha256'],model_sha256=q['model_sha256'],frame_sha256=q['frame_sha256'],threshold=.5,scores={'3':.7,'8':.6,'12':.2})


def setup(tmp_path):
    qr=tmp_path/'queues';qr.mkdir();sr=tmp_path/'scores';sr.mkdir()
    write(qr,item());q,=load_queues(qr)
    return qr,sr,q


def test_current_pin_changes_recompute_without_touching_scores(tmp_path):
    _,sr,q=setup(tmp_path);path=write(sr,frozen(q));before=path.read_bytes()
    f=load_scores([q],sr)[q['queue_sha256']]
    a=compare(q,f,[(3,'present'),(8,'absent')])
    assert a['counts']==dict(tp=1,tn=0,fp=1,fn=0) and a['remaining']==1
    assert 'ids%3A8' in a['disagreement_url']
    b=compare(q,f,[(3,'absent'),(8,'present'),(12,'present')])
    assert b['counts']==dict(tp=1,tn=0,fp=1,fn=1)
    assert b['population_quality_claim'] is False and path.read_bytes()==before
    assert compare(q,f,[])['compared']==0


@pytest.mark.parametrize('change',[
 {'threshold':True},{'threshold':float('nan')},{'threshold':1.1},
 {'scores':{'3':.1,'8':.2}},{'scores':{'3':.1,'8':.2,'12':float('inf')}},
 {'scores':{'3':.1,'8':.2,'12':False}},{'model_sha256':'c'*64},
 {'frame_sha256':'c'*64},{'queue_sha256':'c'*64},{'queue_sha256':[]},
 {'labels':[1,0,0]}, {'schema':'other'},
])
def test_malformed_or_misbound_refuses(tmp_path,change):
    _,sr,q=setup(tmp_path);write(sr,dict(frozen(q),**change))
    with pytest.raises(ReviewQueueRefused):load_scores([q],sr)


def test_file_safety_and_duplicate_queue(tmp_path):
    _,sr,q=setup(tmp_path);p=write(sr,frozen(q));p.write_bytes(p.read_bytes()+b' ')
    with pytest.raises(ReviewQueueRefused,match='digest'):load_scores([q],sr)
    p.unlink();p=write(sr,frozen(q));write(sr,dict(frozen(q),threshold=.6))
    with pytest.raises(ReviewQueueRefused,match='queue_invalid'):load_scores([q],sr)
    for p in sr.iterdir():p.unlink()
    target=tmp_path/'file';target.write_text('{}');(sr/('a'*64+'.json')).symlink_to(target)
    with pytest.raises(ReviewQueueRefused,match='file_unreadable'):load_scores([q],sr)


def test_missing_score_is_not_perfect_score(tmp_path):
    _,sr,q=setup(tmp_path)
    assert load_scores([q],sr)=={} and load_scores([q],tmp_path/'missing')=={}


def test_api_compares_the_same_pin_rows_as_progress(tmp_path,monkeypatch):
    qr,sr,q=setup(tmp_path);write(sr,frozen(q))
    monkeypatch.setenv('KURA_REVIEW_QUEUE_DIR',str(qr));monkeypatch.setenv('KURA_REVIEW_SCORE_DIR',str(sr))
    db=SimpleNamespace(execute=AsyncMock(side_effect=[SimpleNamespace(all=lambda:[(41,'some_tag')]),SimpleNamespace(all=lambda:[(3,'present'),(8,'absent')])]))
    a=asyncio.run(review_queues(db,SimpleNamespace(name='reader')))['queues'][0]
    assert a['comparison']['compared']==2 and a['comparison']['remaining']==a['remaining']==1
    assert db.execute.call_count==2


@pytest.mark.parametrize('failure',[OSError('private path'),RecursionError('private content')])
def test_broken_scores_do_not_hide_live_review_progress(tmp_path,monkeypatch,failure):
    qr,sr,q=setup(tmp_path)
    monkeypatch.setenv('KURA_REVIEW_QUEUE_DIR',str(qr))
    def broken(*args):raise failure
    monkeypatch.setattr('api.services.review_comparison.load_scores',broken)
    db=SimpleNamespace(execute=AsyncMock(side_effect=[SimpleNamespace(all=lambda:[(41,'some_tag')]),SimpleNamespace(all=lambda:[(3,'present')])]))
    a=asyncio.run(review_queues(db,SimpleNamespace(name='reader')))['queues'][0]
    assert a['remaining']==2 and a['judged']=={'present':1}
    assert a['comparison']=={'state':'unavailable','reason':'score_read_failed'}


def test_api_absent_scores_are_explicitly_not_supplied(tmp_path,monkeypatch):
    qr,sr,q=setup(tmp_path)
    monkeypatch.setenv('KURA_REVIEW_QUEUE_DIR',str(qr));monkeypatch.setenv('KURA_REVIEW_SCORE_DIR',str(sr))
    db=SimpleNamespace(execute=AsyncMock(side_effect=[SimpleNamespace(all=lambda:[(41,'some_tag')]),SimpleNamespace(all=lambda:[])]))
    a=asyncio.run(review_queues(db,SimpleNamespace(name='reader')))['queues'][0]
    assert a['remaining']==3 and a['comparison']=={'state':'not_supplied'}


def test_disagreement_browse_does_not_activate_bulk_edit(tmp_path):
    _,sr,q=setup(tmp_path);write(sr,frozen(q));f=load_scores([q],sr)[q['queue_sha256']]
    assert compare(q,f,[(8,'absent')])['disagreement_url']=='/?q=ids%3A8'
