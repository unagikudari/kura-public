"""Explicit one-writer cutover after bounded full reconciliation."""
from pathlib import Path
from sqlalchemy import text
from .service import transition_lease


async def activate(db):
    await db.execute(text("SET LOCAL lock_timeout='2s'"))
    await db.execute(text("SET LOCAL statement_timeout='10s'"))
    await transition_lease(db)
    # Every source statement and its mirrors must have committed first. No
    # full scan, index build or byte copy is performed under this short lock.
    state=(await db.execute(text('SELECT phase FROM claim_storage.migration FOR UPDATE'))).scalar_one()
    if state=='active':
        return {'phase':'active','changed':False}
    if state!='ready':
        raise ValueError('Full reconciliation must reach ready before cutover')
    await db.execute(text('LOCK TABLE public.post_tag_actor_claim IN ACCESS EXCLUSIVE MODE'))
    if (await db.execute(text("SELECT count(*) FROM pg_trigger WHERE tgrelid='public.post_tag_actor_claim'::regclass AND tgname=ANY(ARRAY['claim_mirror_insert','claim_mirror_delete','claim_mirror_update','claim_mirror_truncate']) AND tgenabled='O'"))).scalar_one()!=4:
        raise ValueError('Live mirrors are not intact')
    if (await db.execute(text("SELECT EXISTS(SELECT 1 FROM pg_constraint WHERE confrelid='public.post_tag_actor_claim'::regclass) OR EXISTS(SELECT 1 FROM pg_publication_tables WHERE (schemaname='public' AND tablename='post_tag_actor_claim') OR schemaname='claim_storage')"))).scalar_one():
        raise ValueError('Unexpected claim dependencies or publication')
    if (await db.execute(text("""SELECT EXISTS(SELECT 1 FROM pg_depend d
      WHERE d.refclassid='pg_class'::regclass AND d.refobjid='public.post_tag_actor_claim'::regclass
        AND d.classid IN ('pg_rewrite'::regclass,'pg_proc'::regclass,'pg_policy'::regclass))
      OR EXISTS(SELECT 1 FROM pg_depend d JOIN pg_class c ON c.reltype=d.refobjid
        WHERE c.oid='public.post_tag_actor_claim'::regclass AND d.refclassid='pg_type'::regclass
          AND d.classid='pg_proc'::regclass)"""))).scalar_one():
        raise ValueError('Bound database readers must be migrated before cutover')
    grants=(await db.execute(text("SELECT grantee,privilege_type FROM information_schema.role_table_grants WHERE table_schema='public' AND table_name='post_tag_actor_claim' AND grantee<>current_user"))).all()
    # Prevent direct normalized writes while the source relation changes name.
    await db.execute(text('LOCK TABLE claim_storage.claim IN ACCESS EXCLUSIVE MODE'))
    for name in ('insert','delete','update','truncate'):
        await db.execute(text(f'DROP TRIGGER claim_mirror_{name} ON public.post_tag_actor_claim'))
    await db.execute(text('ALTER TABLE public.post_tag_actor_claim SET SCHEMA claim_storage'))
    await db.execute(text('ALTER TABLE claim_storage.post_tag_actor_claim RENAME TO legacy_claim_archive'))
    # It is a fixed rollback checkpoint, not another mutable child of posts.
    # Keeping cascades here would either mutate the checkpoint or block a
    # legitimate post deletion once the archive is write-protected.
    constraints=(await db.execute(text("SELECT conname FROM pg_constraint WHERE conrelid='claim_storage.legacy_claim_archive'::regclass AND contype='f'"))).scalars().all()
    quote=db.bind.dialect.identifier_preparer.quote
    for name in constraints:
        await db.execute(text(f'ALTER TABLE claim_storage.legacy_claim_archive DROP CONSTRAINT {quote(name)}'))
    for statement in Path(__file__).with_name('legacy_view.sql').read_text().split('-- statement-break'):
        if statement.strip():await db.execute(text(statement))
    for role,privilege in grants:
        if privilege in ('SELECT','INSERT','UPDATE','DELETE','REFERENCES','TRIGGER'):
            # Privileges come from PostgreSQL metadata; identifiers are quoted.
            await db.execute(text(f'GRANT {privilege} ON public.post_tag_actor_claim TO {quote(role)}'))
    await db.execute(text("UPDATE claim_storage.migration SET phase='active',updated_at=clock_timestamp()"))
    return {'phase':'active','changed':True,'legacy_archive_preserved':True}


async def rollback(db, max_rows=250000):
    """Restore changed posts into retained checkpoint before returning the writer.

    Refuse an oversized online rollback rather than holding writer locks through
    an unbounded reconstruction. The current normalized writer remains intact
    if ANY statement here fails; the caller must rollback the transaction.
    """
    if not 1<=max_rows<=1000000:raise ValueError('Invalid rollback budget')
    await db.execute(text("SET LOCAL lock_timeout='2s'"))
    await db.execute(text("SET LOCAL statement_timeout='30s'"))
    await transition_lease(db)
    phase=(await db.execute(text('SELECT phase FROM claim_storage.migration FOR UPDATE'))).scalar_one()
    if phase!='active':raise ValueError('Normalized claims are not active')
    if not (await db.execute(text("SELECT to_regclass('claim_storage.legacy_claim_archive') IS NOT NULL"))).scalar_one():
        raise ValueError('Legacy checkpoint is retired; restore a verified backup instead')
    await db.execute(text('LOCK TABLE public.post_tag_actor_claim IN ACCESS EXCLUSIVE MODE'))
    await db.execute(text('LOCK TABLE claim_storage.claim IN ACCESS EXCLUSIVE MODE'))
    posts=(await db.execute(text('SELECT post_id FROM claim_storage.changed_post ORDER BY post_id LIMIT 5001'))).scalars().all()
    if len(posts)>5000:raise ValueError('Online rollback post budget exceeded; current writer preserved')
    count=(await db.execute(text('''SELECT count(*) FROM (
        SELECT post_id FROM claim_storage.legacy_claim_archive WHERE post_id=ANY(CAST(:posts AS int[]))
        UNION ALL SELECT post_id FROM claim_storage.claim WHERE post_id=ANY(CAST(:posts AS int[]))
        LIMIT :lim) q'''),{'posts':posts,'lim':max_rows+1})).scalar_one()
    if count>max_rows:raise ValueError('Online rollback row budget exceeded; current writer preserved')
    await db.execute(text('DROP TRIGGER claim_archive_read_only ON claim_storage.legacy_claim_archive'))
    await db.execute(text('DELETE FROM claim_storage.legacy_claim_archive WHERE post_id=ANY(CAST(:posts AS int[]))'),{'posts':posts})
    await db.execute(text('''INSERT INTO claim_storage.legacy_claim_archive(post_id,tag_id,actor_id,claimed_at)
      SELECT c.post_id,c.tag_id,a.actor_id,c.claimed_at FROM claim_storage.claim c
      JOIN claim_storage.actor a ON a.id=c.actor_key WHERE c.post_id=ANY(CAST(:posts AS int[]))'''),{'posts':posts})
    # Changes since cutover are now in the old layout, including deleted posts.
    await db.execute(text('DROP VIEW public.post_tag_actor_claim'))
    await db.execute(text('ALTER TABLE claim_storage.legacy_claim_archive RENAME TO post_tag_actor_claim'))
    await db.execute(text('ALTER TABLE claim_storage.post_tag_actor_claim SET SCHEMA public'))
    await db.execute(text('''ALTER TABLE public.post_tag_actor_claim
      ADD CONSTRAINT post_tag_actor_claim_post_id_fkey FOREIGN KEY(post_id) REFERENCES public.post(id) ON DELETE CASCADE NOT VALID,
      ADD CONSTRAINT post_tag_actor_claim_tag_id_fkey FOREIGN KEY(tag_id) REFERENCES public.tag(id) NOT VALID'''))
    for name in ('insert','delete','update'):
        await db.execute(text(f'DROP TRIGGER claim_track_{name} ON claim_storage.claim'))
    await db.execute(text('DROP TRIGGER claim_block_truncate ON claim_storage.claim'))
    await db.execute(text('DROP TABLE claim_storage.changed_post'))
    await db.execute(text('''CREATE TRIGGER claim_mirror_insert AFTER INSERT ON public.post_tag_actor_claim
      REFERENCING NEW TABLE AS inserted FOR EACH STATEMENT EXECUTE FUNCTION claim_storage.mirror_insert()'''))
    await db.execute(text('''CREATE TRIGGER claim_mirror_delete AFTER DELETE ON public.post_tag_actor_claim
      REFERENCING OLD TABLE AS deleted FOR EACH STATEMENT EXECUTE FUNCTION claim_storage.mirror_delete()'''))
    await db.execute(text('''CREATE TRIGGER claim_mirror_update AFTER UPDATE ON public.post_tag_actor_claim
      REFERENCING OLD TABLE AS deleted NEW TABLE AS inserted FOR EACH STATEMENT EXECUTE FUNCTION claim_storage.mirror_update()'''))
    await db.execute(text('''CREATE TRIGGER claim_mirror_truncate AFTER TRUNCATE ON public.post_tag_actor_claim
      FOR EACH STATEMENT EXECUTE FUNCTION claim_storage.mirror_truncate()'''))
    await db.execute(text("UPDATE claim_storage.migration SET phase='ready',updated_at=clock_timestamp()"))
    return {'phase':'ready','writer':'legacy-with-mirror','reconciled_posts':len(posts),'reconciled_rows':count}
