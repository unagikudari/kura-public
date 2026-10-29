"""Single current tag summary: transaction-local deltas and explicit rebuild."""
from sqlalchemy import text


async def lock(db, owner, tags):
    if not tags:return
    await db.execute(text('''
      INSERT INTO collector_next.tag_usage(tag_id,owner_id)
      SELECT x,:o FROM unnest(CAST(:tags AS bigint[])) x ORDER BY x
      ON CONFLICT(tag_id) DO NOTHING
    '''),{'o':owner,'tags':tags})
    await db.execute(text('''SELECT tag_id FROM collector_next.tag_usage
      WHERE owner_id=:o AND tag_id=ANY(CAST(:tags AS bigint[])) ORDER BY tag_id FOR UPDATE'''),{'o':owner,'tags':tags})


# Keep a valid cover stable. On invalidation the partial membership index finds
# one replacement; no image/color/history scan or page-wide greedy selection.
COVER='''COALESCE((SELECT h.asset_id FROM collector_next.label_head h
 WHERE h.owner_id=u.owner_id AND h.tag_id=u.tag_id AND h.asset_id=u.representative_asset_id AND h.present),
 (SELECT h.asset_id FROM collector_next.label_head h
 WHERE h.owner_id=u.owner_id AND h.tag_id=u.tag_id AND h.present ORDER BY h.asset_id LIMIT 1))'''


async def apply(db, owner, counts, sizes):
    tags=sorted(t for t in counts if counts[t] or sizes[t])
    if not tags:return
    await lock(db,owner,tags)
    await db.execute(text(f'''
      UPDATE collector_next.tag_usage u SET positive_count=u.positive_count+x.n,
        logical_bytes=u.logical_bytes+x.bytes,representative_asset_id={COVER},revision=u.revision+1
      FROM unnest(CAST(:tags AS bigint[]),CAST(:counts AS bigint[]),CAST(:sizes AS bigint[])) x(tag,n,bytes)
      WHERE u.owner_id=:o AND u.tag_id=x.tag
    '''),{'o':owner,'tags':tags,'counts':[counts[t] for t in tags],'sizes':[sizes[t] for t in tags]})


async def rebuild(db, owner, tags):
    """Caller serializes affected assets; summary locks serialize other writers."""
    tags=sorted(set(tags))
    if not tags:return
    await lock(db,owner,tags)
    await db.execute(text(f'''
      UPDATE collector_next.tag_usage u SET positive_count=x.n,logical_bytes=x.bytes,
        representative_asset_id={COVER},revision=u.revision+1
      FROM (SELECT t.id,count(h.asset_id) AS n,COALESCE(sum(b.byte_size),0) AS bytes
        FROM unnest(CAST(:tags AS bigint[])) t(id)
        LEFT JOIN collector_next.label_head h ON h.tag_id=t.id AND h.owner_id=:o AND h.present
        LEFT JOIN collector_next.asset a ON a.id=h.asset_id
        LEFT JOIN collector_next.blob b ON b.id=a.blob_id GROUP BY t.id) x
      WHERE u.owner_id=:o AND u.tag_id=x.id AND
        (u.positive_count,u.logical_bytes,u.representative_asset_id) IS DISTINCT FROM (x.n,x.bytes,{COVER})
    '''),{'o':owner,'tags':tags})


async def catalog(db, owner, after=None, limit=40, *, safe_mode=True):
    if not 1<=limit<=100 or (after is not None and after<=0):
        raise ValueError('タグは1～100件ずつ取得してください。')
    rows=[dict(r) for r in (await db.execute(text('''
      SELECT t.id,t.name,COALESCE(u.positive_count,0) AS count,
        COALESCE(u.logical_bytes,0) AS logical_bytes,COALESCE(u.revision,0) AS revision,
        COALESCE(u.representative_policy,1) AS representative_policy,
        CASE WHEN :safe THEN NULL ELSE u.representative_asset_id END AS representative_asset_id
      FROM collector_next.tag t LEFT JOIN collector_next.tag_usage u ON u.tag_id=t.id AND u.owner_id=t.owner_id
      WHERE t.owner_id=:o AND (CAST(:after AS bigint) IS NULL OR t.id<CAST(:after AS bigint)) ORDER BY t.id DESC LIMIT :lim
    '''),{'o':owner,'after':after,'lim':limit+1,'safe':safe_mode})).mappings()]
    more=len(rows)>limit;rows=rows[:limit]
    for row in rows:
        asset=row['representative_asset_id']
        row['representative_state']='empty' if not row['count'] else 'hidden' if safe_mode else 'ready' if asset is not None else 'pending'
        row['thumbnail_url']=f'/m/collector-next/media/{asset}/thumbnail' if asset is not None else None
    return {'items':rows,'next_cursor':rows[-1]['id'] if more else None}
