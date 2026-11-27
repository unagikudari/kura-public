"""Private canonical PG, actual response writes and actual population builders."""
import asyncio
import importlib
from types import SimpleNamespace
import psycopg2
from sqlalchemy import text
from api.tagger_core import training_eligibility as e
from tagger import dedicated_protocol as d, train_base_finetune as base, vnext_arms as arms
from test_human_review_live import pg, url, setup as review_setup, submit, snapshot, queue

async def setup(url):
    admin=connect(url.replace("kura@", "postgres@"))
    with admin.cursor() as cur:cur.execute("DO $$ BEGIN CREATE ROLE replicator NOLOGIN; EXCEPTION WHEN duplicate_object THEN NULL; END $$")
    admin.close()
    engine,ss=await review_setup(url)
    async with ss() as db:
        for sql in importlib.import_module("api.alembic.versions.0198_tag_merge_identity").STATEMENTS:
            await db.execute(text(sql))
        await db.commit()
    return engine,ss


def connect(url):
    c = psycopg2.connect(url.replace('postgresql+asyncpg:', 'postgresql:'))
    c.autocommit = True
    return c


def query(c, sql, args=None):
    with c.cursor() as cur:
        cur.execute(sql,args)
        return cur.fetchall()


def test_uncertain_preserves_gt_and_real_builders_exclude_then_restore(url,monkeypatch):
    async def go():
        engine, ss = await setup(url)
        c=connect(url)
        try:
            async with ss() as db: before=await snapshot(db)
            await submit(ss,queue(),101,'uncertain')
            async with ss() as db: assert await snapshot(db)==before
            db=SimpleNamespace(conn=c,rows=lambda sql,args=None:query(c,sql,args))
            assert base.gt_labels(db,0)[0]==[(102,'fixture',0.0)]
            overlay=read_overlay(c,'fixture')
            assert overlay['counts']=={'uncertain':1}
            draws=SimpleNamespace(rows=lambda *args:[(101,),(102,)])
            assert base.kd_post_ids(draws,2,set(),training_eligibility={'fixture':overlay})==[102]
            assert e.eligible_ids(overlay,[(101,'positive'),(102,'negative')])=={102}
            # Actual vnext builder, with replica observations controlled to force
            # the rejected ID into every augmentation/recovered path.
            realq=arms.q
            def q(conn,sql,args=None):
                if conn is c:
                    if 'select distinct a.post_id' in sql:return [(101,)]
                    return query(c,sql,args)
                if 'count(*)' in sql:return [(2,)]
                return [(101,),(102,)]
            monkeypatch.setattr(arms,'q',q)
            pop=arms.load_populations('fixture',c,object(),seed=1)
            assert pop['gt_full_pos_n']==1 and pop['gt_full_neg_n']==1
            for key in ('ig_pos','ig_neg','oog_neg','rand','corp_pre','recovered'):
                assert 101 not in pop[key],key
            monkeypatch.setattr(arms,'q',realq)
            # A later explicit definite answer binds its new audit/pin to GT.
            await submit(ss,queue(),101,'present')
            assert (101,'fixture',1.0) in base.gt_labels(db,0)[0]
            # A changed label before GT synchronization must not reuse old GT.
            await submit(ss,queue(),101,'absent')
            overlay=read_overlay(c,'fixture')
            assert overlay['counts']=={'gt_not_synced_to_response':1}
            assert (101,'fixture',1.0) not in base.gt_labels(db,0)[0]
            async with ss() as syncdb:
                await syncdb.execute(text('SELECT tagger_governed_sync_neg_gt_from_pins()'));await syncdb.commit()
            assert (101,'fixture',0.0) in base.gt_labels(db,0)[0]
        finally:c.close();await engine.dispose()
    asyncio.run(go())


def test_ordinary_pin_cannot_invent_order_and_queue_counts_are_not_gt(url):
    async def go():
        engine,ss=await setup(url);c=connect(url)
        try:
            await submit(ss,queue(),101,'uncertain')
            # Actual legacy pin representation: no audit ID, changed actor/time.
            async with ss() as db:
                await db.execute(text("UPDATE post_tag_pin SET pinned_by='later',pinned_at=now(),audit_id=NULL WHERE post_id=101"));await db.commit()
            ov=read_overlay(c,'fixture')
            assert ov['counts']=={'uncertain_current_pin_order_unresolved':1}
            report=e.readiness(ov,ov,'a'*64,[101,102],{101:'same',102:'same'})
            assert report['counts']=={'uncertain':1,'unseen':1}
            assert report['groups']=={'uncertain':1,'unseen':1}
            other=dict(queue(),queue_sha256='d'*64)
            await submit(ss,other,101,'present')
            current=read_overlay(c,'fixture')
            responses=read_overlay(c,'fixture',queue_sha256='a'*64)
            assert e.readiness(current,responses,'a'*64,[101,102],{101:1,102:2})['counts']=={'uncertain':1,'unseen':1}
            assert e.eligible_ids(current,[(101,'positive')])=={101}
        finally:c.close();await engine.dispose()
    asyncio.run(go())


def test_new_dedicated_snapshot_uses_overlay_but_old_snapshot_remains_valid(monkeypatch,tmp_path):
    labels={i:int(i<15) for i in range(60)}
    records=[dict(id=i,verdict='positive' if v else 'negative',derived_from='human_correction',media_exclusion=None) for i,v in labels.items()]
    doc=dict(tag='fixture',status='prepared',graph_nodes=list(labels),graph_edges=[],gt=records,seed=1,
             parameters=dict(positive=3,negative=10,tolerance=.1),grouping_policy=d.POLICY)
    doc['split']=d.fixed_split(labels,{i:i for i in labels},positive=3,negative=10,seed=1)
    assert d.validate_snapshot(doc)[0]==labels
    ov=sealed([dict(post_id=1,event_id=1,queue_sha256='a'*64,decision='uncertain',observed_tag_id='1',state='uncertain',verdict='positive')])
    doc['training_eligibility']=ov
    import pytest
    with pytest.raises(d.ProtocolError):d.validate_snapshot(doc)
    filtered={i:v for i,v in labels.items() if i!=1}
    doc['split']=d.fixed_split(filtered,{i:i for i in labels},positive=3,negative=10,seed=1)
    assert d.validate_snapshot(doc)[0]==filtered


def sealed(entries):
    import hashlib,json
    from collections import Counter
    body=dict(schema=e.SCHEMA,tag='fixture',entries=entries,counts=dict(Counter(x['state'] for x in entries)),historical_labels_unchanged=True,fresh_quality_granted=False,identity_policy='canonical_merge_identity_only',transaction_snapshot='1:2:')
    return dict(body,sha256=hashlib.sha256(json.dumps(body,sort_keys=True,separators=(',',':')).encode()).hexdigest())


def test_digest_duplicate_and_state_mismatch_refuse():
    import copy,pytest
    row=dict(post_id=1,event_id=1,queue_sha256='a'*64,decision='uncertain',observed_tag_id='1',state='uncertain',verdict='positive')
    ov=sealed([row]); bad=copy.deepcopy(ov);bad['entries'][0]['state']='current_definite'
    with pytest.raises(ValueError,match='digest'):e.eligible_ids(bad,[(1,'positive')])
    with pytest.raises(ValueError,match='entry_shape'):e.eligible_ids(sealed([row,row]),[(1,'positive')])
    with pytest.raises(ValueError,match='state_mismatch'):e.eligible_ids(sealed([dict(row,state='current_definite')]),[(1,'positive')])


def test_authoritative_merge_and_missing_ledger_fail_closed(url):
    async def go():
        engine,ss=await setup(url);c=connect(url)
        try:
            await submit(ss,queue(),101,'uncertain')
            async with ss() as db:
                await db.execute(text("INSERT INTO tag(id,category_id,creation_time,version) SELECT 2,category_id,now(),1 FROM tag WHERE id=1"))
                await db.execute(text("INSERT INTO tag_name(tag_name_id,tag_id,name,ord) VALUES(2,2,'merged_fixture',0)"))
                await db.execute(text("INSERT INTO tag_merge_identity(source_id,target_id,source_name,target_name,actor_user_id) VALUES(1,2,'fixture','merged_fixture',42)"))
                await db.commit()
            with e.read_snapshot(c):
                ov=e.load(lambda s,a:query(c,s,a),'merged_fixture')
            assert ov['entries'][0]['observed_tag_id']=='1'
            assert 101 in e.excluded(ov)
            async with ss() as db:
                await db.execute(text('ALTER TABLE tagger_human_review_event RENAME TO fixture_unavailable_ledger'));await db.commit()
            import pytest
            with pytest.raises(psycopg2.errors.UndefinedTable):
                with e.read_snapshot(c):e.load(lambda s,a:query(c,s,a),'merged_fixture')
            assert c.autocommit
        finally:c.close();await engine.dispose()
    asyncio.run(go())


def test_repeatable_snapshot_and_timeout_restoration(url):
    async def go():
        engine,ss=await setup(url);c=connect(url)
        try:
            with e.read_snapshot(c):
                before=e.load(lambda s,a:query(c,s,a),'fixture')
                assert query(c,'SHOW statement_timeout')[0][0]=='0'
                await submit(ss,queue(),101,'uncertain')
                assert e.load(lambda s,a:query(c,s,a),'fixture')==before
            with e.read_snapshot(c):
                query(c,"SELECT set_config('statement_timeout','7000',true)")
                assert e.load(lambda s,a:query(c,s,a),'fixture')['counts']=={'uncertain':1}
                assert query(c,'SHOW statement_timeout')[0][0]=='7s'
        finally:c.close();await engine.dispose()
    asyncio.run(go())


def test_actual_dedicated_prepare_keeps_historical_gt_excludes_new_labels(url,monkeypatch,tmp_path):
    async def go():
        engine,ss=await setup(url)
        try:
            await submit(ss,queue(),101,'uncertain')
            monkeypatch.setattr(arms,'connect_primary',lambda:connect(url))
            # Group source expansion is configuration unrelated to this fixture.
            monkeypatch.setattr(d,'work_edge_sql',lambda:{})
            seen=[]; original=d.fixed_split
            def split(labels,groups,**kw):
                seen.append(dict(labels));return original(labels,groups,**kw)
            monkeypatch.setattr(d,'fixed_split',split)
            import pytest,json
            target=tmp_path/'next.json'
            with pytest.raises(d.ProtocolError):
                d.prepare('fixture',target,positive=1,negative=1)
            receipt=json.loads(target.read_text())
            assert {r['id'] for r in receipt['gt']}=={101,102}
            assert receipt['training_eligibility']['counts']=={'uncertain':1}
            assert seen==[{102:0}]
            assert receipt['status']=='blocked'  # insufficient groups, not quality failure
        finally:await engine.dispose()
    asyncio.run(go())


def read_overlay(c,tag,**kw):
    with e.read_snapshot(c):
        return e.load(lambda s,a:query(c,s,a),tag,**kw)


def test_snapshot_contract_and_budget_refuse(url,monkeypatch):
    async def go():
        engine,ss=await setup(url);c=connect(url)
        try:
            import pytest
            with pytest.raises(ValueError,match='readonly_snapshot'):
                e.load(lambda s,a:query(c,s,a),'fixture')
            await submit(ss,queue(),101,'uncertain')
            monkeypatch.setattr(e,'MAX_REVIEWED_POSTS',0)
            with pytest.raises(ValueError,match='read_budget'):
                read_overlay(c,'fixture')
        finally:c.close();await engine.dispose()
    asyncio.run(go())


def test_all_uncertain_is_answered_but_not_ready(url):
    async def go():
        engine,ss=await setup(url);c=connect(url)
        try:
            for pid in [101,102]:await submit(ss,queue(),pid,'uncertain')
            ov=read_overlay(c,'fixture')
            report=e.readiness(ov,ov,'a'*64,[101,102],{101:'same',102:'same'})
            assert report['counts']=={'uncertain':2}
            assert report['groups']=={'uncertain':1}
            assert report['state']=='blocked_uncertain' and report['quality_granted'] is False
            assert e.eligible_ids(ov,[(101,'positive'),(102,'negative')])==set()
        finally:c.close();await engine.dispose()
    asyncio.run(go())
