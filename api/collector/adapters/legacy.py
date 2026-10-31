"""Read-only bridge to the shared legacy library, not a migration/projection.

Only narrow metadata is paged; media bytes are fetched separately on demand.
No ownership inference from post.user_id: legacy library is shared, unlike the
new collector's private assets. HTTP adapter still requires an authenticated user.
"""
from sqlalchemy import text
from api.collector.errors import NotFound

# Keep the legacy default display set: status ok, no advertisement, explicit safe
# when requested. Unknown safety is never treated as safe.
def display_filter(safe: bool):
    return """p.status='ok' AND NOT EXISTS (
        SELECT 1 FROM post_tag pt WHERE pt.post_id=p.id
        AND pt.tag_id=(SELECT tag_id FROM tag_name WHERE name='advertisement'))""" + (" AND p.safety='safe'" if safe else '')


async def pool(db, pool_id: int):
    row = (await db.execute(text('''
        SELECT p.id,p.version,COALESCE((SELECT name FROM pool_name WHERE pool_id=p.id ORDER BY ord,pool_name_id LIMIT 1),
        'pool#' || p.id) AS title FROM pool p WHERE p.id=:id
    '''), {'id': pool_id})).mappings().first()
    if row is None:
        raise NotFound('作品が見つかりません。')
    return dict(row)


async def entries(db, pool_id: int, safe: bool, after: tuple[int, int] | None = None, limit: int = 40):
    if not 1 <= limit <= 100:
        raise ValueError('一覧は1～100件ずつ取得してください。')
    metadata = await pool(db, pool_id)
    cursor = 'AND (pp.ord,pp.post_id) > (:ord,:post)' if after is not None else ''
    rows = [dict(r) for r in (await db.execute(text(f'''
        SELECT pp.post_id AS id,pp.ord AS position,p.type,p.original_filename AS original_name
        FROM pool_post pp JOIN post p ON p.id=pp.post_id
        WHERE pp.pool_id=:pool AND {display_filter(safe)} {cursor}
        ORDER BY pp.ord,pp.post_id LIMIT :lim
    '''), {'pool': pool_id, 'ord': after[0] if after else None, 'post': after[1] if after else None,
           'lim': limit + 1})).mappings()]
    return {'collection': metadata, 'items': rows[:limit],
            'next_cursor': [rows[limit-1]['position'], rows[limit-1]['id']] if len(rows) > limit else None}


async def media(db, pool_id: int, post: int, safe: bool, kind: str):
    if kind not in ("thumbnail", "preview"):
        raise ValueError("Unsupported media kind")
    column = "thumbnail" if kind == "thumbnail" else "data"
    row = (await db.execute(text(f'''
        SELECT p.id,p."mime-type" AS media_type,p.media_location,p.{column} AS payload
        FROM pool_post pp JOIN post p ON p.id=pp.post_id
        WHERE pp.pool_id=:pool AND pp.post_id=:post AND {display_filter(safe)}
    '''), {'pool': pool_id, 'post': post})).mappings().first()
    if row is None:
        raise NotFound('表示できるメディアが見つかりません。')
    return row
