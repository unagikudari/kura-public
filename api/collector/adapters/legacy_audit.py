"""Bounded read-contract evidence; never assign owners or manufacture label intent."""
from collections import Counter,defaultdict
import hashlib,json
from sqlalchemy import text
from api.collector.labels import normalize_label
from . import legacy, legacy_provenance


async def inspect_pool(db, pool_id:int, *, safe:bool=False, max_entries:int=2000, max_memberships:int=250000, include_provenance:bool=False, max_provenance_rows:int=250000, max_provenance_bytes:int=64*1024*1024):
    if pool_id<=0 or not 1<=max_entries<=10000 or not 1<=max_memberships<=1000000:
        raise ValueError('Invalid pool or inspection budget')
    if (await db.execute(text('SHOW transaction_read_only'))).scalar_one()!='on' or (await db.execute(text('SHOW transaction_isolation'))).scalar_one() not in ('repeatable read','serializable'):
        raise ValueError('Inspection requires a read-only repeatable snapshot')
    collection=await legacy.pool(db,pool_id)
    if (await db.execute(text('''SELECT pp.post_id FROM pool_post pp LEFT JOIN post p ON p.id=pp.post_id
        WHERE pp.pool_id=:pool AND p.id IS NULL LIMIT 1'''),{'pool':pool_id})).first():
        raise ValueError('Collection has dangling media references; inspection is incomplete')
    rows=[dict(r) for r in (await db.execute(text('''
      SELECT p.id,pp.ord AS position,p.version,p.type,p.original_filename AS original_name,
        p.status,p.safety,p."mime-type" AS media_type,p.file_size AS declared_bytes,
        encode(p.sha256,'hex') AS declared_sha256
      FROM pool_post pp JOIN post p ON p.id=pp.post_id
      WHERE pp.pool_id=:pool ORDER BY pp.ord,pp.post_id LIMIT :lim
    '''),{'pool':pool_id,'lim':max_entries+1})).mappings()]
    if len(rows)>max_entries:raise ValueError('Entry budget exceeded; choose a smaller collection or an explicit larger budget')
    ids=[r['id'] for r in rows]
    links=[dict(r) for r in (await db.execute(text('''
      SELECT post_id,tag_id,locked FROM post_tag WHERE post_id=ANY(CAST(:ids AS integer[]))
      ORDER BY post_id,tag_id LIMIT :lim
    '''),{'ids':ids,'lim':max_memberships+1})).mappings()]
    if len(links)>max_memberships:raise ValueError('Membership budget exceeded; inspection is incomplete')
    evidence_tables=(await db.execute(text("SELECT c.relname AS tablename FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname='public' AND c.relkind IN ('r','p','v') AND c.relname IN ('post_tag_pin','post_tag_audit','post_tag_actor_claim') ORDER BY c.relname"))).scalars().all()
    provenance = await legacy_provenance.capture(db, ids, evidence_tables,
        max_rows=max_provenance_rows, max_bytes=max_provenance_bytes) if include_provenance else None
    tag_ids={r['tag_id'] for r in links}
    if provenance is not None:
        for table in ('post_tag_pin','post_tag_actor_claim'):
            tag_ids.update(r['tag_id'] for r in provenance['tables'].get(table, []))
    tag_ids=sorted(tag_ids)
    dictionary={}
    name_rows=(await db.execute(text('''
      SELECT t.id,c.name AS category,n.name FROM tag t
      LEFT JOIN tag_category c ON c.id=t.category_id
      LEFT JOIN tag_name n ON n.tag_id=t.id
      WHERE t.id=ANY(CAST(:tags AS integer[])) ORDER BY t.id,n.ord,n.tag_name_id LIMIT :lim
    '''),{'tags':tag_ids,'lim':max_memberships+1})).mappings().all()
    if len(name_rows)>max_memberships:raise ValueError('Dictionary budget exceeded; inspection is incomplete')
    for r in name_rows:
        tag=dictionary.setdefault(r['id'],{'id':r['id'],'category':r['category'],'names':[]})
        if r['name'] is not None:tag['names'].append(r['name'])
    memberships=defaultdict(list)
    for r in links:memberships[r['post_id']].append({'tag_id':r['tag_id'],'locked':r['locked']})
    ad_ids={tid for tid,t in dictionary.items() if 'advertisement' in t['names']}
    for row in rows:
        row['tags']=memberships[row['id']]
        row['displayable']=row['status']=='ok' and not ad_ids.intersection(t['tag_id'] for t in row['tags']) and (not safe or row['safety']=='safe')
    # Independent page contract: the bridge must agree with the manifest's full
    # membership-derived eligibility and stable (ord, legacy post ID) order.
    bridged=[];after=None
    while True:
        page=await legacy.entries(db,pool_id,safe,after,100)
        bridged.extend(page['items'])
        if page['next_cursor'] is None:break
        next_after=tuple(page['next_cursor'])
        if next_after==after or len(bridged)>max_entries:raise ValueError('Invalid bridge pagination')
        after=next_after
    expected=[{k:r[k] for k in ('id','position','type','original_name')} for r in rows if r['displayable']]
    if bridged!=expected:raise ValueError('Legacy bridge result/order differs from the inspection snapshot')
    normalized=defaultdict(set);invalid=[]
    for tag in dictionary.values():
        for name in tag['names']:
            try:normalized[normalize_label(name)[1]].add(tag['id'])
            except ValueError:invalid.append({'tag_id':tag['id'],'name':name})
    collisions=[{'normalized_name':name,'tag_ids':sorted(tags)} for name,tags in sorted(normalized.items()) if len(tags)>1]
    positions=Counter(r['position'] for r in rows)
    document={'format':'kura-legacy-read-audit-v1','collection':collection,'safe_mode':safe,
      'ownership':'shared-unassigned','label_semantics':'legacy-membership-not-account-intent',
      'entries':rows,'dictionary':list(dictionary.values()),
      'readiness':{'ready_for_import':False,'original_bytes_verified':False,'provenance_converted':False,
        'unmapped_provenance_tables':list(evidence_tables),'unsupported_names':invalid,'normalized_collisions':collisions,
        'missing_tag_ids':sorted(set(tag_ids)-set(dictionary)),
        'blockers':['explicit target ownership required','verify original bytes and media identity','preserve provenance and label meaning before conversion']},
      'summary':{'entries':len(rows),'displayable_entries':len(expected),'tags':len(dictionary),'memberships':len(links),
        'locked_memberships':sum(bool(r['locked']) for r in links),'tied_position_groups':sum(n>1 for n in positions.values()),
        'status_counts':dict(Counter(r['status'] for r in rows)),'read_contract_verified':True}}
    if provenance is not None:
        document['format']='kura-legacy-read-audit-v2'
        document['provenance']=provenance
        document['readiness']['provenance_captured']=True
        document['summary']['provenance_rows']=provenance['summary']['rows']
    document['snapshot_sha256']=hashlib.sha256(json.dumps(document,sort_keys=True,ensure_ascii=False,separators=(',',':')).encode()).hexdigest()
    return document
