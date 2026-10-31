"""Display membership and totals do not share the large tagging row's lifecycle."""
from pathlib import Path
from sqlalchemy import text

SCHEMA=Path(__file__).with_name('post_listing_v1.sql')
# Replica gets its row shape before publication changes; origin-only triggers
# maintain it on the canonical writer. Subscriber applies the published rows.
_statements=SCHEMA.read_text().split('-- statement-break')
REPLICA_DDL=';\n'.join(s.strip().rstrip(';') for s in (_statements[0],_statements[3],_statements[4]))+';'


async def totals(db,safe=False):
    result=(await db.execute(text("""SELECT count(*) AS count,coalesce(sum(l.file_size),0) AS bytes
      FROM public.post_listing l WHERE NOT EXISTS (
        SELECT 1 FROM public.post_tag pt JOIN public.tag_name n ON n.tag_id=pt.tag_id
        WHERE pt.post_id=l.post_id AND n.name='advertisement')"""+
        (" AND l.safety='safe'" if safe else '')))).one()
    return int(result.count),int(result.bytes)


async def rebuild(db):
    # Lock the owning rows before the projection. Existing writers finish first;
    # subsequent inserts/attribute updates wait and cannot be lost during seed.
    await db.execute(text("SET LOCAL lock_timeout='2s'"))
    await db.execute(text("SET LOCAL statement_timeout='30s'"))
    await db.execute(text('LOCK TABLE public.post IN SHARE MODE'))
    await db.execute(text('TRUNCATE public.post_listing'))
    result=await db.execute(text("INSERT INTO public.post_listing SELECT id,safety,file_size FROM public.post WHERE status='ok'"))
    return {'rows':result.rowcount}
