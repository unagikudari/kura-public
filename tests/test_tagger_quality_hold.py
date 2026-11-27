"""Real private PostgreSQL: abstention never becomes an add or a retraction."""
import pytest
from sqlalchemy import text
from test_collector_next import postgres, run  # noqa: F401
from test_pool_search_candidates import search_database  # noqa: F401
from test_claim_storage import initialize_completion_database
from api.claim_storage.migration import copy_batch, verify_batch
from api.claim_storage.cutover import activate
from api.schemas.tagging import TagItem
from api.services import tagging


@pytest.mark.parametrize('hold_source', ['emergency', 'majora_incident', 'dark_penis_incident', 'minora_incident', 'database'])
@pytest.mark.parametrize('normalized', [False, True])
@pytest.mark.parametrize('existing', [False, True])
@pytest.mark.parametrize('source', ['actor-a', None])
def test_hold_blocks_alias_and_implication_preserves_existing_and_claims(
        search_database, monkeypatch, normalized, existing, source, hold_source):
    held_tag = {'emergency': 'smegma', 'majora_incident': 'dark_labia_majora',
                'dark_penis_incident': 'dark_penis', 'minora_incident': 'dark_labia_minora', 'database': 'measured-bad-tag'}[hold_source]
    monkeypatch.setattr(tagging, '_reap_cache', None)
    async def authority(db):
        return frozenset({held_tag, 'held-alias', 'own-stale'})
    monkeypatch.setattr(tagging, '_reap_authorized_tags', authority)

    async def scenario(sessions):
        await initialize_completion_database(sessions)
        async with sessions.begin() as db:
            await db.execute(text("UPDATE tag_name SET name=:name WHERE tag_id=4"), {"name": held_tag})
            if hold_source == "database":
                await db.execute(text("INSERT INTO tag_quarantine(tag,reason) VALUES(:name,'measured fixture')"), {"name": held_tag})
            await db.execute(text("INSERT INTO tag_name(tag_id,name,ord) VALUES(4,'held-alias',1)"))
            await db.execute(text('INSERT INTO tag_implication(parent_id,child_id) VALUES(5,4)'))
            if not existing:
                await db.execute(text('DELETE FROM post_tag WHERE tag_id=4'))
                await db.execute(text('DELETE FROM post_tag_actor_claim WHERE tag_id=4'))
        if normalized:
            async with sessions.begin() as db:
                await copy_batch(db,50); await copy_batch(db,50)
            while True:
                async with sessions.begin() as db:
                    result = await verify_batch(db)
                if result['phase'] == 'ready': break
            async with sessions.begin() as db: await activate(db)
        async with sessions() as db:
            held_before = (await db.execute(text('SELECT * FROM post_tag_actor_claim WHERE tag_id=4 ORDER BY 1,2,3'))).all()
            for names in (['new', 'held-alias', 'human-negative'], ['new'], [held_tag]):
                await tagging.complete_auto_tag(db,1,'ok',[TagItem(name=n) for n in names],None,
                    source=source,reap_scope='own',host='test',release_id=14,
                    veto_gates=[held_tag,'held-alias'])
                ids = set((await db.execute(text('SELECT tag_id FROM post_tag WHERE post_id=1'))).scalars())
                assert (4 in ids) == existing
                assert 5 in ids  # unrelated prediction still accepted
                assert 6 not in ids  # negative human pin still respected
                assert {1,2,3} <= ids  # locked/human-positive/other actor retained
                assert (await db.execute(text('SELECT * FROM post_tag_actor_claim WHERE tag_id=4 ORDER BY 1,2,3'))).all() == held_before
            audits = (await db.execute(text('SELECT added_tags,removed_tags FROM post_tag_audit'))).all()
            assert all(not {held_tag,'held-alias'} & set(a.added_tags+a.removed_tags) for a in audits)
            # Manual editing is outside the machine completion hold.
            if not existing:
                await db.execute(text('INSERT INTO post_tag(post_id,tag_id,locked) VALUES(1,4,true)'))
                await db.commit()
                await tagging.complete_auto_tag(db,1,'ok',[TagItem(name='new')],None,source=source,reap_scope='own')
                assert (await db.execute(text('SELECT locked FROM post_tag WHERE post_id=1 AND tag_id=4'))).scalar() is True
    run(search_database,scenario)


def test_database_hold_refreshes_without_restart_and_does_not_clear_incident_floor(search_database):
    from api.tagger_core.quality_hold import held_names
    async def scenario(sessions):
        await initialize_completion_database(sessions)
        async with sessions() as db:
            assert await held_names(db) == frozenset({'smegma', 'dark_labia_majora', 'dark_penis', 'dark_labia_minora'})
        async with sessions.begin() as db:
            await db.execute(text("INSERT INTO tag_quarantine(tag,reason) VALUES('new','measured bad predictions')"))
        async with sessions() as db:
            assert await held_names(db) == frozenset({'smegma','dark_labia_majora','dark_penis','dark_labia_minora','new'})
        async with sessions.begin() as db:
            await db.execute(text("UPDATE tag_quarantine SET active=false"))
        async with sessions() as db:
            assert await held_names(db) == frozenset({'smegma', 'dark_labia_majora', 'dark_penis', 'dark_labia_minora'})
            await db.execute(text('DROP TABLE tag_quarantine'))
            with pytest.raises(Exception):
                await tagging.complete_auto_tag(db,1,'ok',[TagItem(name='new')],None,source='actor-a')
            await db.rollback()
            assert not (await db.execute(text('SELECT 1 FROM post_tag WHERE post_id=1 AND tag_id=5'))).first()
    run(search_database,scenario)


@pytest.mark.parametrize('held_tag', ['dark_labia_majora', 'dark_penis', 'dark_labia_minora'])
@pytest.mark.parametrize('use_alias', [False, True])
@pytest.mark.parametrize('existing', [False, True])
def test_pool_meta_respects_hold_by_resolved_identity(search_database, held_tag, use_alias, existing):
    name = 'held-alias' if use_alias else held_tag
    async def scenario(sessions):
        await initialize_completion_database(sessions)
        async with sessions.begin() as db:
            await db.execute(text("UPDATE tag_name SET name=:name WHERE tag_id=4"), {"name":held_tag})
            await db.execute(text("INSERT INTO tag_name(tag_id,name,ord) VALUES(4,'held-alias',1)"))
            await db.execute(text("INSERT INTO pool(id,category_id,version,creation_time,last_edit_time,meta_tagging_status,meta_tagging_attempts) VALUES(1,1,1,now(),now(),'pending',0)"))
            await db.execute(text('INSERT INTO pool_post(pool_id,post_id,ord) VALUES(1,1,0)'))
            if not existing:
                await db.execute(text('DELETE FROM post_tag WHERE tag_id=4'))
        async with sessions() as db:
            claims = (await db.execute(text('SELECT * FROM post_tag_actor_claim ORDER BY 1,2,3'))).all()
            pins = (await db.execute(text('SELECT * FROM post_tag_pin ORDER BY 1,2'))).all()
            assert await tagging.complete_pool_meta(db,1,'ok',[
                TagItem(name=name),TagItem(name='new'),TagItem(name='human-negative')],None) == ('done',2,0)
            ids=set((await db.execute(text('SELECT tag_id FROM post_tag WHERE post_id=1'))).scalars())
            assert (4 in ids) == existing
            assert 5 in ids and 6 not in ids
            assert (await db.execute(text('SELECT * FROM post_tag_actor_claim ORDER BY 1,2,3'))).all() == claims
            assert (await db.execute(text('SELECT * FROM post_tag_pin ORDER BY 1,2'))).all() == pins
    run(search_database,scenario)
