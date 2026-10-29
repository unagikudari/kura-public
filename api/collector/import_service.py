"""Durable import contract, shared by browser and MCP adapters.

Planning stores no original bytes. Registration and progress share a transaction.
Only owner+operation keyed reads; disk/decoder work is outside DB transactions.
"""
import hashlib
import json
from uuid import UUID, uuid5
from sqlalchemy import text
from . import service
from .errors import Conflict, NotFound
from .storage import MAX_BYTES


def summary(row):
    r = dict(row)
    remaining = r['item_count']-r['registered_count']-r['rejected_count']
    r['remaining_count'] = remaining
    r['state'] = 'awaiting_files' if remaining else 'needs_attention' if r['rejected_count'] else 'completed'
    return r


async def operation(db, owner, key):
    row = (await db.execute(text('SELECT * FROM collector_next.import_operation WHERE owner_id=:o AND request_key=:k'),
                           {'o':owner,'k':key})).mappings().first()
    if not row:
        raise NotFound('取り込みが見つかりません。')
    result = summary(row);result.pop('fingerprint')
    return result


async def recent(db, owner, after=None):
    params={'o':owner,'after':after};pivot=''
    if after:
        previous=await operation(db,owner,after);params['date']=previous['created_at']
        pivot='AND (created_at,request_key)<(:date,:after)'
    rows=list((await db.execute(text(f'''SELECT request_key,item_count,registered_count,rejected_count,created_at,updated_at
        FROM collector_next.import_operation WHERE owner_id=:o {pivot} ORDER BY created_at DESC,request_key DESC LIMIT 21'''),params)).mappings())
    return {'items':[summary(r) for r in rows[:20]],'next_cursor':rows[19]['request_key'] if len(rows)>20 else None}


async def items(db, owner, key, after=0, limit=200):
    await operation(db, owner, key)
    rows = [dict(r) for r in (await db.execute(text('''
        SELECT i.position,i.request_key,i.original_name,i.relative_path,i.byte_size,encode(i.sha256,'hex') AS sha256,
               i.collection_id,i.collection_position,CASE i.state WHEN 0 THEN 'pending' WHEN 1 THEN 'registered' ELSE 'rejected' END AS state,i.error_code,i.error_message,r.asset_id,r.entry_id
        FROM collector_next.import_item i LEFT JOIN collector_next.receipt r
          ON r.owner_id=i.owner_id AND r.request_key=i.request_key
        WHERE i.owner_id=:o AND i.operation_key=:k AND i.position>:after ORDER BY i.position LIMIT :lim
    '''), {'o':owner,'k':key,'after':after,'lim':limit+1})).mappings()]
    return {'items':rows[:limit], 'next_cursor':rows[limit-1]['position'] if len(rows)>limit else None}


async def plan(db, owner, key: UUID, collection, entries):
    if not 1 <= len(entries) <= 10000:
        raise ValueError('1回の取り込みは1〜10,000ファイルです。')
    normalized=[];seen=set();requests=set()
    for position,e in enumerate(entries,1):
        name=e['original_name'];path=service.validate_relative_path(e.get('relative_path'),name)
        if not name or len(name)>255 or '/' in name or '\\' in name or '\x00' in name:
            raise ValueError('ファイル名を確認してください。')
        identity=path or name
        if identity in seen or str(e['request_key']) in requests:
            raise ValueError('同じパスまたは受付キーが重複しています。')
        seen.add(identity);requests.add(str(e['request_key']))
        size=e['byte_size'];sha=e.get('sha256')
        if size<0 or size>2**53-1:
            raise ValueError('ファイルサイズを確認してください。')
        if size<=MAX_BYTES and (not isinstance(sha,str) or len(sha)!=64 or any(c not in '0123456789abcdef' for c in sha)):
            raise ValueError('原本のSHA-256を確認してください。')
        if size>MAX_BYTES:sha=None
        normalized.append(dict(position=position,request_key=str(e['request_key']),original_name=name,relative_path=path,byte_size=size,sha256=sha))
    fp=hashlib.sha256(json.dumps([collection,normalized],ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()).digest()
    await service.lock_import_plans(db,owner)
    existing=(await db.execute(text('SELECT fingerprint FROM collector_next.import_operation WHERE owner_id=:o AND request_key=:k'),{'o':owner,'k':key})).scalar_one_or_none()
    if existing is not None:
        if existing!=fp:raise Conflict('同じ取り込み番号で異なる計画が送られました。')
        return await operation(db,owner,key)
    if collection is not None and not (await db.execute(text('SELECT id FROM collector_next.collection WHERE owner_id=:o AND id=:c'),{'o':owner,'c':collection})).first():
        raise NotFound('作品が見つかりません。')
    groups={e['relative_path'].split('/')[0]:None for e in normalized
            if collection is None and e['relative_path'] and '/' in e['relative_path']}
    if groups:
        group_rows=[{'title':g[:200].strip() or 'フォルダ','request_key':str(uuid5(key,g))} for g in groups]
        await db.execute(text('''INSERT INTO collector_next.collection(owner_id,title,request_key)
            SELECT :o,x.title,x.request_key FROM jsonb_to_recordset(CAST(:data AS jsonb)) AS x(title text,request_key uuid)
            ON CONFLICT(owner_id,request_key) DO NOTHING'''),{'o':owner,'data':json.dumps(group_rows,ensure_ascii=False)})
        found={str(r['request_key']):r for r in (await db.execute(text('''SELECT id,title,request_key FROM collector_next.collection
            WHERE owner_id=:o AND request_key=ANY(CAST(:keys AS uuid[]))'''),{'o':owner,'keys':[r['request_key'] for r in group_rows]})).mappings()}
        for group in groups:
            row=found[str(uuid5(key,group))]
            if row['title']!=(group[:200].strip() or 'フォルダ'):raise Conflict('作品の受付キーが既存の別作品と重複しています。')
            groups[group]=row['id']
    for e in normalized:
        group=e['relative_path'].split('/')[0] if collection is None and e['relative_path'] and '/' in e['relative_path'] else None
        e['collection_id']=groups.get(group,collection)
    collection_ids=sorted({e['collection_id'] for e in normalized if e['collection_id'] is not None})
    positions={}
    if collection_ids:
        # Reserve stable page positions. Normal append also skips these reservations.
        await db.execute(text('SELECT id FROM collector_next.collection WHERE owner_id=:o AND id=ANY(CAST(:ids AS bigint[])) ORDER BY id FOR UPDATE'),{'o':owner,'ids':collection_ids})
        positions={r['id']:r['last'] for r in (await db.execute(text('''SELECT c.id,GREATEST(
            COALESCE((SELECT max(position) FROM collector_next.collection_entry WHERE collection_id=c.id),0),
            COALESCE((SELECT max(collection_position) FROM collector_next.import_item WHERE collection_id=c.id),0)) AS last
            FROM collector_next.collection c WHERE c.owner_id=:o AND c.id=ANY(CAST(:ids AS bigint[]))'''),{'o':owner,'ids':collection_ids})).mappings()}
    for e in normalized:
        c=e['collection_id']
        if c is not None:positions[c]+=1
        e['collection_position']=positions[c] if c is not None else None
    # Refuse request keys already owned by another plan or legacy receipt.
    keys=[e['request_key'] for e in normalized]
    collision=(await db.execute(text('''SELECT EXISTS(SELECT 1 FROM collector_next.import_item WHERE owner_id=:o AND request_key=ANY(CAST(:keys AS uuid[])))
        OR EXISTS(SELECT 1 FROM collector_next.receipt WHERE owner_id=:o AND request_key=ANY(CAST(:keys AS uuid[])))'''),{'o':owner,'keys':keys})).scalar_one()
    if collision:raise Conflict('受付キーを新しくして取り込みを作り直してください。')
    await db.execute(text('''INSERT INTO collector_next.import_operation(owner_id,request_key,fingerprint,item_count,rejected_count)
        VALUES(:o,:k,:fp,:n,:rejected)'''),{'o':owner,'k':key,'fp':fp,'n':len(normalized),'rejected':sum(e['byte_size']>MAX_BYTES for e in normalized)})
    await db.execute(text('''INSERT INTO collector_next.import_item(owner_id,operation_key,position,request_key,original_name,relative_path,byte_size,sha256,collection_id,collection_position,state,error_code,error_message)
        SELECT :o,:k,x.position,x.request_key,x.original_name,x.relative_path,x.byte_size,decode(x.sha256,'hex'),x.collection_id,x.collection_position,
          CASE WHEN x.byte_size>:max THEN 2 ELSE 0 END,
          CASE WHEN x.byte_size>:max THEN 'file_too_large' END,
          CASE WHEN x.byte_size>:max THEN 'ファイルは64MiB以下にしてください' END
        FROM jsonb_to_recordset(CAST(:data AS jsonb)) AS x(position integer,request_key uuid,original_name text,relative_path text,byte_size bigint,sha256 text,collection_id bigint,collection_position bigint)
    '''),{'o':owner,'k':key,'max':MAX_BYTES,'data':json.dumps(normalized,ensure_ascii=False)})
    return await operation(db,owner,key)


async def planned_item(db, owner, key, request_key, lock=False):
    row=(await db.execute(text('''SELECT * FROM collector_next.import_item
        WHERE owner_id=:o AND operation_key=:k AND request_key=:r''' + (' FOR UPDATE' if lock else '')),
        {'o':owner,'k':key,'r':request_key})).mappings().first()
    if not row:raise NotFound('取り込み対象が見つかりません。')
    return row


def validate_upload(item, name, path, collection):
    if item['original_name']!=name or item['relative_path']!=path or item['collection_id']!=collection:
        raise Conflict('保存した取り込み計画と対象が一致しません。元のファイルを選び直してください。')
    if item['state']==2:raise Conflict('このファイルは入力エラーとして記録済みです。修正後は新しい取り込みで追加してください。')


async def register(db, owner, key, request_key, stored, name, collection, path):
    item=await planned_item(db,owner,key,request_key,True)
    validate_upload(item,name,path,collection)
    if item['sha256']!=stored.digest or item['byte_size']!=stored.size:
        raise Conflict('原本の内容が取り込み計画から変わっています。元のファイルを選び直してください。')
    result=await service.register(db,owner,request_key,stored,name,collection,path,reserved_position=item['collection_position'],operation_key=key)
    if item['state']==0:
        await db.execute(text("UPDATE collector_next.import_item SET state=1 WHERE owner_id=:o AND operation_key=:k AND request_key=:r"),{'o':owner,'k':key,'r':request_key})
        await db.execute(text('UPDATE collector_next.import_operation SET registered_count=registered_count+1,updated_at=now() WHERE owner_id=:o AND request_key=:k'),{'o':owner,'k':key})
    return result


async def reject(db, owner, key, request_key, code, message):
    item=await planned_item(db,owner,key,request_key,True)
    if item['state']!=0:return
    await db.execute(text("UPDATE collector_next.import_item SET state=2,error_code=:code,error_message=:message WHERE owner_id=:o AND operation_key=:k AND request_key=:r"),{'o':owner,'k':key,'r':request_key,'code':code,'message':message[:500]})
    await db.execute(text('UPDATE collector_next.import_operation SET rejected_count=rejected_count+1,updated_at=now() WHERE owner_id=:o AND request_key=:k'),{'o':owner,'k':key})
