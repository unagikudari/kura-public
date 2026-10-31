"""Real PostgreSQL proves online copy/mirror semantics without production writes."""
import asyncio
from pathlib import Path
import pytest
from sqlalchemy import text
from test_collector_next import postgres,run  # noqa:F401
from test_pool_search_candidates import search_database  # noqa:F401
import schema_history
from api.claim_storage.migration import copy_batch,verify_batch

SCHEMA=Path(__file__).resolve().parents[1]/'api/claim_storage/schema.sql'


async def initialize(sessions):
    async with sessions.begin() as db:
        for sql in ('CREATE TABLE post(id int PRIMARY KEY)','CREATE TABLE tag(id int PRIMARY KEY)',
          'CREATE TABLE post_tag_actor_claim(post_id int REFERENCES post ON DELETE CASCADE,tag_id int REFERENCES tag,actor_id text,claimed_at timestamptz DEFAULT now(),PRIMARY KEY(post_id,tag_id,actor_id))',
          'INSERT INTO post SELECT generate_series(1,5)', 'INSERT INTO tag SELECT generate_series(1,5)',
          "INSERT INTO post_tag_actor_claim SELECT p,t,a,'2026-01-01Z' FROM generate_series(1,3)p,generate_series(1,3)t,unnest(ARRAY['actor-a','actor-b'])a"):
            await db.execute(text(sql))
        for statement in SCHEMA.read_text().split('-- statement-break'):
            if statement.strip():await db.execute(text(statement))


async def equal(db):
    old=(await db.execute(text('SELECT post_id,tag_id,actor_id,claimed_at FROM post_tag_actor_claim ORDER BY 1,2,3'))).all()
    new=(await db.execute(text('SELECT c.post_id,c.tag_id,a.actor_id,c.claimed_at FROM claim_storage.claim c JOIN claim_storage.actor a ON a.id=c.actor_key ORDER BY 1,2,3'))).all()
    assert old==new


def test_resume_mirrors_updates_merge_delete_and_cascade(search_database):
    async def scenario(sessions):
        await initialize(sessions)
        async with sessions.begin() as db:
            assert (await copy_batch(db,5))['rows']==5
        # A new session resumes; updates touch already copied AND uncopied rows.
        async with sessions.begin() as db:
            await db.execute(text("DELETE FROM post_tag_actor_claim WHERE post_id=1 AND actor_id='actor-a'"))
            await db.execute(text("INSERT INTO post_tag_actor_claim VALUES(1,4,'actor-a','2026-02-01Z'),(4,1,'new-actor','2026-02-01Z')"))
            await db.execute(text("UPDATE post_tag_actor_claim SET tag_id=5 WHERE tag_id=3"))
        while True:
            async with sessions.begin() as db:
                result=await copy_batch(db,5)
            if result['phase']!='copying':break
        async with sessions.begin() as db:
            await equal(db)
            await db.execute(text('DELETE FROM post WHERE id=2'))
            await equal(db)
        while True:
            async with sessions.begin() as db:
                result=await verify_batch(db,1)
            if result['phase']=='ready':break
        async with sessions.begin() as db:
            await db.execute(text('TRUNCATE post_tag_actor_claim'))
            await equal(db)
            assert (await copy_batch(db))['phase']=='verifying'
            assert (await verify_batch(db))['phase']=='ready'
    run(search_database,scenario)


def test_copy_cannot_resurrect_concurrent_delete(search_database):
    async def scenario(sessions):
        await initialize(sessions)
        async with sessions() as copying:
            await copy_batch(copying,1)
            async def remove():
                async with sessions.begin() as deleting:
                    await deleting.execute(text("DELETE FROM post_tag_actor_claim WHERE post_id=1 AND tag_id=1 AND actor_id='actor-a'"))
            task=asyncio.create_task(remove())
            await asyncio.sleep(0.05)
            assert not task.done()  # source SHARE lock is still held
            await copying.commit()
            await task
        while True:
            async with sessions.begin() as db:
                result=await copy_batch(db,50)
            if result['phase']!='copying':break
        async with sessions() as db:await equal(db)
    run(search_database,scenario)


def test_verification_refuses_extra_or_missing_claims(search_database):
    async def scenario(sessions):
        await initialize(sessions)
        async with sessions.begin() as db:
            await copy_batch(db,50)
            await copy_batch(db,50)
            await db.execute(text('DELETE FROM claim_storage.claim WHERE post_id=1 AND tag_id=1'))
        async with sessions.begin() as db:
            with pytest.raises(ValueError,match='verification failed'):
                await verify_batch(db)
        async with sessions() as db:
            assert (await db.execute(text('SELECT phase FROM claim_storage.migration'))).scalar_one()=='verifying'
    run(search_database,scenario)


def test_cutover_legacy_sql_and_immutable_archive(search_database):
    from api.claim_storage.cutover import activate
    async def scenario(sessions):
        await initialize(sessions)
        async with sessions.begin() as db:
            with pytest.raises(ValueError,match='ready'):
                await activate(db)
        async with sessions.begin() as db:
            await copy_batch(db,50)
            await copy_batch(db,50)
        while True:
            async with sessions.begin() as db:result=await verify_batch(db)
            if result['phase']=='ready':break
        async with sessions.begin() as db:
            from api.collector.adapters.legacy_provenance import capture
            original=await capture(db,[1,2,3],['post_tag_actor_claim'])
        async with sessions.begin() as db:
            assert (await activate(db))['changed']
            assert await capture(db,[1,2,3],['post_tag_actor_claim'])==original
            await equal(db)
            before=(await db.execute(text('SELECT count(*) FROM claim_storage.legacy_claim_archive'))).scalar_one()
        async with sessions.begin() as db:
            assert not (await activate(db))['changed']
            await db.execute(text("DELETE FROM post_tag_actor_claim WHERE post_id=1 AND actor_id='actor-a'"))
            await db.execute(text("INSERT INTO post_tag_actor_claim(post_id,tag_id,actor_id) VALUES(1,4,'actor-a'),(1,4,'actor-c')"))
            await db.execute(text("UPDATE post_tag_actor_claim SET tag_id=5 WHERE tag_id=3 AND NOT EXISTS(SELECT 1 FROM post_tag_actor_claim other WHERE other.post_id=post_tag_actor_claim.post_id AND other.actor_id=post_tag_actor_claim.actor_id AND other.tag_id=5)"))
            await equal(db)
            assert (await db.execute(text('SELECT count(*) FROM claim_storage.legacy_claim_archive'))).scalar_one()==before
            await db.execute(text('DELETE FROM post WHERE id=2'))
            assert not (await db.execute(text('SELECT 1 FROM post_tag_actor_claim WHERE post_id=2'))).first()
            # Per-actor retraction excludes another actor's same-tag support.
            candidates=(await db.execute(text("SELECT c.tag_id FROM post_tag_actor_claim c WHERE c.post_id=1 AND c.actor_id='actor-a' AND NOT EXISTS(SELECT 1 FROM post_tag_actor_claim o WHERE o.post_id=1 AND o.tag_id=c.tag_id AND o.actor_id<>'actor-a')"))).scalars().all()
            assert 4 not in candidates
        async with sessions() as db:
            with pytest.raises(Exception,match='immutable'):
                await db.execute(text('DELETE FROM claim_storage.legacy_claim_archive'))
            await db.rollback()
            with pytest.raises(Exception,match='immutable'):
                await db.execute(text("UPDATE claim_storage.actor SET actor_id='changed'"))
            await db.rollback()
    run(search_database,scenario)


def test_rollback_preserves_post_cutover_changes_and_can_reactivate(search_database):
    from api.claim_storage.cutover import activate,rollback
    async def scenario(sessions):
        await initialize(sessions)
        async with sessions.begin() as db:
            await copy_batch(db,50);await copy_batch(db,50)
        while True:
            async with sessions.begin() as db:result=await verify_batch(db)
            if result['phase']=='ready':break
        async with sessions.begin() as db:await activate(db)
        async with sessions.begin() as db:
            await db.execute(text("DELETE FROM post_tag_actor_claim WHERE post_id=1 AND actor_id='actor-a'"))
            await db.execute(text("INSERT INTO post_tag_actor_claim(post_id,tag_id,actor_id) VALUES(4,5,'new-actor')"))
            await db.execute(text('DELETE FROM post WHERE id=2'))
            expected=(await db.execute(text('SELECT * FROM post_tag_actor_claim ORDER BY 1,2,3'))).all()
        async with sessions.begin() as db:
            with pytest.raises(ValueError,match='budget'):await rollback(db,max_rows=1)
        async with sessions.begin() as db:
            result=await rollback(db)
            assert result['reconciled_posts']==3
            assert (await db.execute(text('SELECT * FROM post_tag_actor_claim ORDER BY 1,2,3'))).all()==expected
            await equal(db)
            await db.execute(text('DELETE FROM post WHERE id=4'))
            await equal(db)
        async with sessions.begin() as db:
            await activate(db)
            await equal(db)
    run(search_database,scenario)


def test_integer_service_and_legacy_rollback_share_snapshot_semantics(search_database):
    from api.claim_storage import service
    from api.claim_storage.cutover import activate,rollback
    async def scenario(sessions):
        await initialize(sessions)
        async with sessions.begin() as db:
            legacy_backend=await service.backend(db,'actor-a')
            assert not legacy_backend.normalized
            # actor-b protects all old actor-a claims until it retracts them.
            assert await service.candidates(db,legacy_backend,1,[1])==[]
            await service.replace(db,legacy_backend,1,[4])
            assert await service.candidates(db,legacy_backend,1,[1])==[4]
        while True:
            async with sessions.begin() as db:result=await copy_batch(db,50)
            if result['phase']!='copying':break
        while True:
            async with sessions.begin() as db:result=await verify_batch(db)
            if result['phase']=='ready':break
        async with sessions.begin() as db:await activate(db)
        async with sessions.begin() as db:
            source=await service.backend(db,'actor-a')
            assert source.normalized and source.key is not None
            assert await service.candidates(db,source,1,[1])==[4]
            await service.replace(db,source,1,[2,5])
            await equal(db)
            fresh=await service.backend(db,'unseen-source')
            assert fresh.key is None and await service.candidates(db,fresh,1,[1])==[]
            await service.replace(db,fresh,1,[5])
            assert await service.candidates(db,source,1,[2])==[] # fresh source protects 5
            with pytest.raises(ValueError,match='nonempty'):await service.replace(db,source,1,[])
        async with sessions.begin() as db:await rollback(db)
        async with sessions.begin() as db:
            source=await service.backend(db,'actor-a')
            assert not source.normalized
            await service.replace(db,source,1,[3])
            await equal(db)
    run(search_database,scenario)


def test_completion_backend_lease_fences_cutover(search_database):
    from api.claim_storage import service
    from api.claim_storage.cutover import activate
    async def scenario(sessions):
        await initialize(sessions)
        async with sessions.begin() as db:
            await copy_batch(db,50);await copy_batch(db,50)
        while True:
            async with sessions.begin() as db:result=await verify_batch(db)
            if result['phase']=='ready':break
        async with sessions() as completion:
            source=await service.backend(completion,'actor-a')
            async def switch():
                async with sessions.begin() as db:return await activate(db)
            task=asyncio.create_task(switch())
            await asyncio.sleep(0.05)
            assert not task.done()
            await service.replace(completion,source,1,[4])
            await completion.commit()
            assert (await task)['phase']=='active'
        async with sessions() as db:
            assert (await service.backend(db,'actor-a')).normalized
            await equal(db)
    run(search_database,scenario)


def test_arriving_completion_does_not_lock_actor_before_cutover_guard(search_database):
    from api.claim_storage import service
    from api.claim_storage.cutover import activate
    async def scenario(sessions):
        await initialize(sessions)
        async with sessions.begin() as db:
            await copy_batch(db,50);await copy_batch(db,50)
        while True:
            async with sessions.begin() as db:result=await verify_batch(db)
            if result['phase']=='ready':break
        async with sessions() as switching:
            await service.transition_lease(switching)
            async def arriving():
                async with sessions.begin() as db:return await service.backend(db,'actor-a')
            task=asyncio.create_task(arriving())
            await asyncio.sleep(0.05)
            assert not task.done()
            await activate(switching)
            await switching.commit()
            assert (await task).normalized
    run(search_database,scenario)


async def initialize_completion_database(sessions):
    import ast
    import sqlalchemy as sa
    from api.db.models import Base
    metadata=sa.MetaData()
    for model in Base.metadata.tables.values():
        table=model.to_metadata(metadata)
        table.constraints={c for c in table.constraints if not isinstance(c,sa.ForeignKeyConstraint)}
        table.foreign_keys.clear()
        for column in table.columns:column.foreign_keys.clear()
    async with sessions.begin() as db:
        connection=await db.connection()
        await connection.run_sync(metadata.create_all)
        statements=[
          'CREATE TABLE IF NOT EXISTS tagger_head_policy(tag text,tag_id int,patch_version int,apply_mode text)',
          'CREATE TABLE IF NOT EXISTS tagger_patch(tag_id int,version int,status text)',
          'ALTER TABLE tagger_patch ADD COLUMN IF NOT EXISTS tag_id int',
          # The policy in force (0186), as reap authority reads it; empty here, like the tables.
          "CREATE OR REPLACE VIEW tagger_current_head_policy AS SELECT p.tag_id,p.tag,p.patch_version,p.apply_mode,"
          "NULL::text AS artifact_kind FROM tagger_head_policy p JOIN tagger_patch t ON t.tag_id=p.tag_id "
          "AND t.version=p.patch_version WHERE t.status='approved'",
          'CREATE TABLE post_tag_pin(post_id int,tag_id int,state text,PRIMARY KEY(post_id,tag_id))',
          'CREATE TABLE post_tag_audit(id bigserial PRIMARY KEY,post_id int,actor_type text,actor_id text,source text,added_tags text[],removed_tags text[],host text,release_id int)',
          'CREATE TABLE post_tag_actor_claim(post_id int REFERENCES post(id) ON DELETE CASCADE,tag_id int REFERENCES tag(id),actor_id text,claimed_at timestamptz DEFAULT now(),PRIMARY KEY(post_id,tag_id,actor_id))',
          # The work table and the real stage 4a trigger function: ingest seeds the row from
          # post.type, completion reads attempts there and the governed functions write it.
          'CREATE TABLE post_tagging_work(post_id int PRIMARY KEY REFERENCES post(id) ON DELETE CASCADE,status text NOT NULL,lease_until timestamptz,attempts int NOT NULL DEFAULT 0,last_error text)',
          _mirror_function(),
          'CREATE TRIGGER post_tagging_work_mirror_ins AFTER INSERT ON post FOR EACH ROW EXECUTE FUNCTION trg_post_tagging_work_mirror()',
          "INSERT INTO tag_category(id,version,name,color,\"default\",\"order\") VALUES(1,1,'general','#fff',true,0)",
          "INSERT INTO tag(id,category_id,version,creation_time,post_count) SELECT i,1,1,now(),0 FROM generate_series(1,6)i",
          "INSERT INTO tag_name(tag_id,name,ord) VALUES(1,'locked',0),(2,'human-positive',0),(3,'shared',0),(4,'own-stale',0),(5,'new',0),(6,'human-negative',0)",
          "INSERT INTO post(id,version,creation_time,safety,type,checksum,file_size,status,flags,\"mime-type\") VALUES(1,1,now(),'safe','image','test',100,'ok','','image/png')",
          'INSERT INTO post_tag(post_id,tag_id,locked) VALUES(1,1,true),(1,2,false),(1,3,false),(1,4,false)',
          "INSERT INTO post_tag_pin VALUES(1,2,'present'),(1,6,'absent')",
          "INSERT INTO post_tag_actor_claim(post_id,tag_id,actor_id) VALUES(1,1,'actor-a'),(1,2,'actor-a'),(1,3,'actor-a'),(1,4,'actor-a'),(1,3,'actor-b')",
          "CREATE FUNCTION tagger_governed_complete_ok(p_pid int) RETURNS void LANGUAGE SQL AS $$UPDATE post_tagging_work SET status='done',lease_until=NULL,last_error=NULL WHERE post_id=p_pid$$",
          "CREATE FUNCTION tagger_governed_complete_retry(p_pid int,p_state text,p_error text) RETURNS void LANGUAGE SQL AS $$UPDATE post_tagging_work SET status=p_state,attempts=attempts+1,lease_until=NULL,last_error=p_error WHERE post_id=p_pid$$",
        ]
        for sql in statements:await db.execute(text(sql))
        # Reuse actual production pin trigger DDL, not a copied substitute.
        tree=ast.parse(schema_history.migration('0057_invm_human_pin.py').read_text())
        for node in ast.walk(tree):
            if isinstance(node,ast.Constant) and isinstance(node.value,str) and node.value.strip().startswith(('CREATE FUNCTION post_tag_pin_guard','CREATE TRIGGER "01_post_tag_pin_guard')):
                await db.execute(text(node.value))
        for sql in SCHEMA.read_text().split('-- statement-break'):
            if sql.strip():await db.execute(text(sql))


def _mirror_function():
    """The CREATE FUNCTION of scripts/sql/tagging_work_stage4a_insert_rule.sql, as production has it."""
    sql=(SCHEMA.parents[2]/'scripts/sql/tagging_work_stage4a_insert_rule.sql').read_text()
    start=sql.index('CREATE OR REPLACE FUNCTION public.trg_post_tagging_work_mirror()')
    end=sql.index('$function$;',start)+len('$function$')
    return sql[start:end]

@pytest.mark.parametrize('normalized',[False,True])
def test_real_completion_keeps_locks_human_pins_other_actors_and_audit(search_database,normalized,monkeypatch):
    """Actual completion service; private ORM tables and original pin triggers.

    Status-governance marking is a minimal test boundary, not a test of its
    unchanged executor authorization policy. No live API/GT/training writes.
    """
    from api.schemas.tagging import TagItem
    from api.services.tagging import complete_auto_tag
    monkeypatch.setattr('api.services.tagging._reap_cache',None)
    from api.claim_storage.cutover import activate
    async def scenario(sessions):
        await initialize_completion_database(sessions)
        if normalized:
            async with sessions.begin() as db:
                await copy_batch(db,50);await copy_batch(db,50)
            while True:
                async with sessions.begin() as db:result=await verify_batch(db)
                if result['phase']=='ready':break
            async with sessions.begin() as db:await activate(db)
        async with sessions() as db:
            result=await complete_auto_tag(db,1,'ok',[TagItem(name='new'),TagItem(name='human-negative')],None,source='actor-a',reap_scope='own')
            assert result[0]=='done' and result[3]==1
            assert (await db.execute(text('SELECT tag_id FROM post_tag WHERE post_id=1 ORDER BY tag_id'))).scalars().all()==[1,2,3,5]
            audit=(await db.execute(text('SELECT added_tags,removed_tags,actor_id,actor_type FROM post_tag_audit ORDER BY id DESC LIMIT 1'))).one()
            assert audit.added_tags==['new'] and audit.removed_tags==['own-stale']
            assert audit.actor_id=='actor-a' and audit.actor_type=='script'
            before=(await db.execute(text('SELECT * FROM post_tag_actor_claim ORDER BY 1,2,3'))).all()
            await complete_auto_tag(db,1,'ok',[],None,source='actor-a',reap_scope='own')
            assert (await db.execute(text('SELECT * FROM post_tag_actor_claim ORDER BY 1,2,3'))).all()==before
            await complete_auto_tag(db,1,'failed',[], 'test-only',source='actor-a',reap_scope='own')
            assert (await db.execute(text('SELECT * FROM post_tag_actor_claim ORDER BY 1,2,3'))).all()==before
            await complete_auto_tag(db,1,'ok',[TagItem(name='new')],None,source='actor-b',reap_scope='own')
            await complete_auto_tag(db,1,'ok',[TagItem(name='own-stale')],None,source='actor-a',reap_scope='own')
            assert (await db.execute(text('SELECT tag_id FROM post_tag WHERE post_id=1 ORDER BY tag_id'))).scalars().all()==[1,2,4,5]
    run(search_database,scenario)


def test_completing_a_post_deleted_after_its_claim_raises_post_gone_and_writes_nothing(search_database,monkeypatch):
    """A claimed post that was deleted (its work row cascaded) is PostGone, for ok and failed alike."""
    from api.schemas.tagging import TagItem
    from api.services.tagging import PostGone, complete_auto_tag
    monkeypatch.setattr('api.services.tagging._reap_cache',None)
    async def scenario(sessions):
        await initialize_completion_database(sessions)
        async with sessions() as db:
            await db.execute(text('DELETE FROM post_tag WHERE post_id=1'))
            await db.execute(text('DELETE FROM post_tag_actor_claim WHERE post_id=1'))
            await db.execute(text('DELETE FROM post WHERE id=1'))
            await db.commit()
            assert (await db.execute(text('SELECT count(*) FROM post_tagging_work WHERE post_id=1'))).scalar()==0
            audits=(await db.execute(text('SELECT count(*) FROM post_tag_audit'))).scalar()
            for status,tags in (('ok',[TagItem(name='new')]),('failed',[])):
                with pytest.raises(PostGone) as raised:
                    await complete_auto_tag(db,1,status,tags,'x',source='actor-a',reap_scope='own')
                assert raised.value.post_id==1
                await db.rollback()
            assert (await db.execute(text('SELECT count(*) FROM post_tag_audit'))).scalar()==audits
    run(search_database,scenario)


@pytest.mark.parametrize('other_post',[1,2])
def test_completion_serializes_same_post_but_not_independent_posts(search_database,monkeypatch,other_post):
    from api.claim_storage import service
    from api.claim_storage.cutover import activate
    from api.schemas.tagging import TagItem
    from api.services.tagging import complete_auto_tag
    monkeypatch.setattr('api.services.tagging._reap_cache',None)
    async def scenario(sessions):
        await initialize_completion_database(sessions)
        async with sessions.begin() as db:
            await db.execute(text("INSERT INTO post(id,version,creation_time,safety,type,checksum,file_size,status,flags,\"mime-type\") VALUES(2,1,now(),'safe','image','test2',100,'ok','','image/png')"))
            await copy_batch(db,50);await copy_batch(db,50)
        while True:
            async with sessions.begin() as db:result=await verify_batch(db)
            if result['phase']=='ready':break
        async with sessions.begin() as db:await activate(db)
        candidate_ready=asyncio.Event();resume=asyncio.Event()
        original=service.candidates
        async def paused(db,source,pid,keep):
            result=await original(db,source,pid,keep)
            if source.actor=='actor-a':
                candidate_ready.set();await resume.wait()
            return result
        monkeypatch.setattr(service,'candidates',paused)
        async def complete(actor,pid,name):
            async with sessions() as db:
                return await complete_auto_tag(db,pid,'ok',[TagItem(name=name)],None,source=actor,reap_scope='own')
        first=asyncio.create_task(complete('actor-a',1,'new'))
        await asyncio.wait_for(candidate_ready.wait(),2)
        second=asyncio.create_task(complete('actor-b',other_post,'own-stale'))
        try:
            if other_post==1:
                await asyncio.sleep(0.1)
                assert not second.done(), 'Concurrent post writers can invalidate a retraction decision'
            else:
                await asyncio.wait_for(asyncio.shield(second),2)
        finally:resume.set()
        await first;await second
        async with sessions() as db:
            assert (await db.execute(text('SELECT 1 FROM post_tag WHERE post_id=:pid AND tag_id=4'),{'pid':other_post})).first()
            assert (await db.execute(text("SELECT 1 FROM post_tag_actor_claim WHERE post_id=:pid AND actor_id='actor-b' AND tag_id=4"),{'pid':other_post})).first()
    run(search_database,scenario)


def test_bound_views_and_sql_functions_refuse_rename_cutover(search_database):
    from api.claim_storage.cutover import activate
    async def scenario(sessions):
        await initialize(sessions)
        async with sessions.begin() as db:
            await copy_batch(db,50);await copy_batch(db,50)
        while True:
            async with sessions.begin() as db:result=await verify_batch(db)
            if result['phase']=='ready':break
        async with sessions.begin() as db:
            await db.execute(text('CREATE VIEW bound_reader AS SELECT * FROM post_tag_actor_claim'))
            with pytest.raises(ValueError,match='Bound'):await activate(db)
            await db.execute(text('DROP VIEW bound_reader'))
            await db.execute(text('CREATE FUNCTION bound_reader() RETURNS SETOF post_tag_actor_claim LANGUAGE SQL BEGIN ATOMIC SELECT * FROM post_tag_actor_claim; END'))
            with pytest.raises(ValueError,match='Bound'):await activate(db)
            await db.execute(text('DROP FUNCTION bound_reader()'))
            assert (await activate(db))['phase']=='active'
    run(search_database,scenario)


def test_compatibility_grants_work_without_exposing_definer_to_other_triggers(search_database):
    async def scenario(sessions):
        from api.claim_storage.cutover import activate
        await initialize(sessions)
        async with sessions.begin() as db:
            await db.execute(text('CREATE ROLE claim_view_client'))
            await db.execute(text('GRANT SELECT,INSERT,UPDATE,DELETE ON public.post_tag_actor_claim TO claim_view_client'))
            await db.execute(text('GRANT CREATE ON SCHEMA public TO claim_view_client'))
            await db.execute(text('GRANT USAGE ON SCHEMA claim_storage TO claim_view_client'))
            await copy_batch(db,50);await copy_batch(db,50)
        while True:
            async with sessions.begin() as db:
                if (await verify_batch(db))['phase']=='ready':break
        async with sessions.begin() as db:
            await activate(db)
            await db.execute(text('SET LOCAL ROLE claim_view_client'))
            await db.execute(text("INSERT INTO public.post_tag_actor_claim(post_id,tag_id,actor_id) VALUES(5,5,'delegated-client')"))
            await db.execute(text("UPDATE public.post_tag_actor_claim SET tag_id=4 WHERE post_id=5 AND actor_id='delegated-client'"))
            assert (await db.execute(text("SELECT tag_id FROM public.post_tag_actor_claim WHERE post_id=5"))).scalar_one()==4
            await db.execute(text("DELETE FROM public.post_tag_actor_claim WHERE post_id=5"))
            await db.execute(text("CREATE VIEW public.fake_claim_writer AS SELECT 5::int AS post_id,5::int AS tag_id,'forged'::text AS actor_id,now() AS claimed_at"))
            await db.execute(text('CREATE TRIGGER fake_write INSTEAD OF INSERT ON public.fake_claim_writer FOR EACH ROW EXECUTE FUNCTION claim_storage.legacy_write()'))
            with pytest.raises(Exception,match='restricted to its public view'):
                async with db.begin_nested():
                    await db.execute(text("INSERT INTO public.fake_claim_writer VALUES(5,5,'forged',now())"))
            await db.execute(text('RESET ROLE'))
            assert (await db.execute(text('SELECT count(*) FROM claim_storage.claim WHERE post_id=5'))).scalar_one()==0
            await db.execute(text('DROP OWNED BY claim_view_client CASCADE'))
            await db.execute(text('DROP ROLE claim_view_client'))
    run(search_database,scenario)
