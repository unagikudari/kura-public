"""One bounded sidebar read, without per-category HTTP/count/image fan-out."""
from sqlalchemy import text


async def sidebar(db):
    rows = (await db.execute(text('''
        SELECT c.name,c.color,c."default",COALESCE((
            SELECT jsonb_agg(jsonb_build_object(
                'id',t.id,'names',COALESCE((SELECT jsonb_agg(n.name ORDER BY n.ord,n.tag_name_id)
                    FROM tag_name n WHERE n.tag_id=t.id),'[]'::jsonb),
                'localizedNames',COALESCE((SELECT jsonb_object_agg(l.locale,l.name)
                    FROM tag_localization l WHERE l.tag_id=t.id),'{}'::jsonb),
                'category',c.name,'categoryColor',c.color,'usages',COALESCE(t.post_count,0)
            ) ORDER BY t.post_count DESC,t.id DESC)
            FROM (SELECT id,post_count FROM tag WHERE category_id=c.id
                  ORDER BY post_count DESC,id DESC LIMIT 40) t
        ),'[]'::jsonb) AS tags
        FROM tag_category c ORDER BY c."order",c.id
    '''))).mappings().all()
    return {'categories': [{'name': r['name'], 'color': r['color'], 'default': r['default']} for r in rows],
            'tagsByCat': {r['name']: r['tags'] for r in rows}}
