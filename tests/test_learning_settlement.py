"""A measured quality failure must leave training once, through real governed writes."""
import asyncio
import pytest
from test_tagger_learn_live import pg, world, _queue, _measure, _queued, _psql, TAG
from test_learning_dispatch import evidence, FLOOR
from tagger import learn, auto_review, reconciler


@pytest.mark.parametrize("fp",[201,10])
def test_real_qualification_registration_is_rejected_and_settled(world, monkeypatch, tmp_path,fp):
    event=asyncio.run(_queue(world.sessions))
    metrics=evidence(tmp_path,fp=fp)
    payload=dict(tag=TAG,source_host='offline-nodeb',artifact_ref='nodeb:/test/artifact',
        artifact_sha256='a'*64,gt={'positive':1480,'negative':8570},metrics=metrics)
    monkeypatch.setattr(learn,'run_child',lambda *a:{'outcome':'rejected','registration':payload})
    result,state=_measure(world,event)
    assert result=='registered' and not state['pending']
    async def judge():
        async with world.sessions() as db:
            review=await auto_review.review_pending(db)
            assert len(review['rejected' if fp==201 else 'owner_decision'])==1
        async with world.sessions() as db:
            await reconciler.settle_judged_training(db)
    asyncio.run(judge())
    assert _psql(world.url,"SELECT status||':'||rejected_reason FROM tagger_registration_request")==('rejected:serving_quality_failed:precision' if fp==201 else 'rejected:awaiting_production_precision_and_fleet_qualification')
    assert asyncio.run(_queued(world.sessions))==[]
    assert _psql(world.url,"SELECT count(*) FROM tagger_patch")== '0'

    async def dashboard():
        from api.routers.pools import tagger_dashboard
        async with world.sessions() as db:
            return (await tagger_dashboard(raw=True,db=db,include_nodes=False)).body.decode()
    html=asyncio.run(dashboard())
    assert '未配備候補の固定評価' in html
    if fp==201:assert '51.7%' in html and '1300件' in html and '不採用' in html
    else:assert '配備条件の確認待ち' in html



def test_canonical_snapshot_connection_has_effective_timeout_and_is_readonly(world,monkeypatch):
    import psycopg2
    from tagger import vnext_arms as a, remote_learning as remote
    c=psycopg2.connect(world.url.replace('postgresql+asyncpg:','postgresql:'))
    c.autocommit=True  # connect_primary's real contract.
    monkeypatch.setattr(a,'connect_primary',lambda:c)
    observed=[];original=a.q
    def query(conn,sql,args=None):
        observed.append((original(conn,'SHOW transaction_read_only')[0][0],
                         original(conn,'SHOW statement_timeout')[0][0]))
        return original(conn,sql,args)
    monkeypatch.setattr(a,'q',query)
    gt,baseline,floor=remote._gt_and_baseline(TAG)
    assert observed and set(observed)=={('on','30s')}
    assert baseline is None and gt['positive']==0


def test_stale_training_labels_reject_registration_and_settle(world, monkeypatch, tmp_path):
    event = asyncio.run(_queue(world.sessions))
    payload = dict(
        tag=TAG, source_host="offline-nodeb", artifact_ref="nodeb:/test/artifact",
        artifact_sha256="a" * 64,
        gt={"positive": 1480, "negative": 8570, "label_sha256": "b" * 64},
        metrics=evidence(tmp_path, fp=10),
    )
    monkeypatch.setattr(learn, "run_child", lambda *a: {
        "outcome": "measured", "registration": payload,
    })
    result, _ = _measure(world, event)
    assert result == "registered"

    async def judge():
        async with world.sessions() as db:
            review = await auto_review.review_pending(db)
            assert review["not_needed"] == [(TAG, "stale_training_labels")]
        async with world.sessions() as db:
            await reconciler.settle_judged_training(db)

    asyncio.run(judge())
    assert asyncio.run(_queued(world.sessions)) == []
    assert _psql(world.url, "SELECT count(*) FROM tagger_patch") == "0"
