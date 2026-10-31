"""Relational service, shared by HTTP/UI and MCP adapters.

SQL is intentionally visible: narrow projections, keyset paging, serialized append.
"""
import hashlib
import json
import re
from uuid import UUID, uuid4

from sqlalchemy import text

from .storage import StoredMedia
from .errors import Conflict, NotFound, InvalidPath
from . import label_service


def validate_relative_path(path: str | None, name: str) -> str | None:
    if path is None:
        return None
    if (not path or len(path) > 4096 or "\\" in path or "\x00" in path
            or re.match(r"^[A-Za-z]:", path) or any(p in ("", ".", "..") for p in path.split("/"))
            or path.rsplit("/", 1)[-1] != name):
        raise InvalidPath("フォルダ内の相対パスが不正か、ファイル名と一致しません。")
    return path


async def collection_page(db,owner:int,after:int|None=None,limit:int=100):
    if not 1<=limit<=100 or (after is not None and after<=0):raise ValueError("作品一覧の範囲を確認してください。")
    cursor=" AND id<:after" if after is not None else ""
    rows=[dict(r) for r in (await db.execute(text(
        "SELECT id,title FROM collector_next.collection WHERE owner_id=:owner"+cursor+" ORDER BY id DESC LIMIT :limit"
    ),{"owner":owner,"after":after,"limit":limit+1})).mappings()]
    return {"items":rows[:limit],"next_cursor":rows[limit-1]['id'] if len(rows)>limit else None}


async def collections(db,owner:int):
    return (await collection_page(db,owner))['items']


async def create_collection(db, owner: int, title: str, request_key: UUID | None = None):
    params = {"owner": owner, "title": title, "key": request_key or uuid4()}
    created = (await db.execute(text("""
        INSERT INTO collector_next.collection(owner_id,title,request_key) VALUES(:owner,:title,:key)
        ON CONFLICT(owner_id,request_key) DO NOTHING RETURNING id,title
    """), params)).mappings().first()
    if created is None:
        created = (await db.execute(text("""
            SELECT id,title FROM collector_next.collection WHERE owner_id=:owner AND request_key=:key
        """), params)).mappings().one()
        if created["title"] != title:
            raise Conflict("同じ受付キーで異なる作品名が送信されました")
    return dict(created)


async def list_media(db, owner: int, after: int | None = None, limit: int = 40, tags=None, kind=None):
    if not 1 <= limit <= 100:
        raise ValueError("一覧は1～100件ずつ取得してください。")
    # Separate SQL shapes keep the cursor predicate indexable, without OR/NULL plans.
    cursor = " AND m.asset_id < :after" if after is not None else ""
    tag_ids = await label_service.filter_tags(db, owner, tags)
    if tag_ids is None:
        return {"items": [], "next_cursor": None}
    params = {"owner": owner, "after": after, "limit": limit + 1}
    source = "collector_next.media_current m"
    pivot = ""
    if tag_ids:
        source = "collector_next.media_tag_current pivot JOIN collector_next.media_current m ON m.asset_id=pivot.asset_id"
        params["pivot"] = tag_ids[0]
        pivot = " AND pivot.owner_id=:owner AND pivot.tag_id=:pivot"
    filters = label_service.additional_filters(tag_ids, kind, params)
    rows = [dict(r) for r in (await db.execute(text(f"""
        SELECT m.asset_id AS id,m.original_name,m.media_type,m.width,m.height
        FROM {source} WHERE m.owner_id=:owner {cursor} {pivot} {filters}
        ORDER BY m.asset_id DESC LIMIT :limit
    """), params)).mappings()]
    return {"items": await label_service.enrich(db, owner, rows[:limit]), "next_cursor": rows[limit - 1]["id"] if len(rows) > limit else None}


async def entries(db, owner: int, collection: int, after: int = 0, limit: int = 40, tags=None, kind=None):
    if not 1 <= limit <= 100:
        raise ValueError("一覧は1～100件ずつ取得してください。")
    if not (await db.execute(text("SELECT id FROM collector_next.collection WHERE owner_id=:o AND id=:c"),
                             {"o": owner, "c": collection})).first():
        raise NotFound("作品が見つかりません")
    tag_ids = await label_service.filter_tags(db, owner, tags)
    if tag_ids is None:
        return {"items": [], "next_cursor": None}
    params = {"o": owner, "owner": owner, "c": collection, "after": after, "lim": limit + 1}
    pivot_join = ""
    pivot = ""
    if tag_ids:
        pivot_join = "JOIN collector_next.media_tag_current pivot ON pivot.asset_id=e.asset_id"
        pivot = " AND pivot.owner_id=:owner AND pivot.tag_id=:pivot"
        params["pivot"] = tag_ids[0]
    filters = label_service.additional_filters(tag_ids, kind, params)
    rows = [dict(r) for r in (await db.execute(text(f"""
        SELECT e.id AS entry_id,e.position,m.asset_id AS id,COALESCE(r.original_name,m.original_name) AS original_name,
               r.relative_path,m.media_type,m.width,m.height
        FROM collector_next.collection_entry e JOIN collector_next.media_current m ON m.asset_id=e.asset_id
        {pivot_join}
        LEFT JOIN collector_next.receipt r ON r.entry_id=e.id
        WHERE e.owner_id=:o AND e.collection_id=:c AND e.position>:after {pivot} {filters}
        ORDER BY e.position LIMIT :lim
    """), params)).mappings()]
    return {"items": await label_service.enrich(db, owner, rows[:limit]), "next_cursor": rows[limit - 1]["position"] if len(rows) > limit else None}


async def lock_import_plans(db,owner):
    lock=int.from_bytes(hashlib.sha256(f'collector-plan:{owner}'.encode()).digest()[:8],'big',signed=True)
    await db.execute(text('SELECT pg_advisory_xact_lock(:key)'),{'key':lock})


async def register(db, owner: int, request_key: UUID, image: StoredMedia, name: str, collection: int | None,
                   relative_path: str | None = None, *, reserved_position: int | None = None, operation_key: UUID | None = None):
    relative_path = validate_relative_path(relative_path, name)
    if operation_key is None:
        await lock_import_plans(db,owner)
        if (await db.execute(text('SELECT 1 FROM collector_next.import_item WHERE owner_id=:o AND request_key=:k'),{'o':owner,'k':request_key})).first():
            raise Conflict('保存済みの取り込み番号を付けて再試行してください。')
    identity = [image.digest.hex(), image.size, name, collection]
    # Preserve the original four-field fingerprint for requests issued before folders.
    if relative_path is not None:
        identity.append(relative_path)
    fingerprint = hashlib.sha256(json.dumps(
        identity, ensure_ascii=False,
        separators=(",", ":"),
    ).encode()).digest()
    # Same request only: lock scope is bounded to the DB transaction, not hashing/I/O.
    lock = int.from_bytes(hashlib.sha256(f"collector:{owner}:{request_key}".encode()).digest()[:8],
                          "big", signed=True)
    await db.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": lock})
    params = {"o": owner, "key": request_key}
    old = (await db.execute(text("""
        SELECT fingerprint,asset_id,entry_id FROM collector_next.receipt WHERE owner_id=:o AND request_key=:key
    """), params)).mappings().first()
    if old:
        if old["fingerprint"] != fingerprint:
            raise Conflict("同じ受付キーで異なる内容が送信されました")
        return {"asset_id": old["asset_id"], "entry_id": old["entry_id"], "replayed": True}
    if collection is not None:
        # Serialize append positions and reject cross-owner references before inserting anything.
        if not (await db.execute(text("""
            SELECT id FROM collector_next.collection WHERE owner_id=:o AND id=:c FOR UPDATE
        """), {"o": owner, "c": collection})).first():
            raise NotFound("作品が見つかりません")
    blob = (await db.execute(text("""
        INSERT INTO collector_next.blob(sha256,byte_size) VALUES(:sha,:size)
        ON CONFLICT(sha256) DO NOTHING RETURNING id,byte_size
    """), {"sha": image.digest, "size": image.size})).mappings().first()
    if blob is None:
        blob = (await db.execute(text("SELECT id,byte_size FROM collector_next.blob WHERE sha256=:sha"),
                                 {"sha": image.digest})).mappings().one()
    if blob["byte_size"] != image.size:
        raise Conflict("原本のサイズが一致しません")
    await db.execute(text("""
        INSERT INTO collector_next.blob_location(blob_id,storage_key) VALUES(:id,:key)
        ON CONFLICT(blob_id) DO NOTHING
    """), {"id": blob["id"], "key": image.key})
    asset = (await db.execute(text("""
        INSERT INTO collector_next.asset(owner_id,blob_id) VALUES(:o,:b)
        ON CONFLICT(owner_id,blob_id) DO NOTHING RETURNING id
    """), {"o": owner, "b": blob["id"]})).scalar_one_or_none()
    reused = asset is None
    if reused:
        asset = (await db.execute(text("SELECT id FROM collector_next.asset WHERE owner_id=:o AND blob_id=:b"),
                                  {"o": owner, "b": blob["id"]})).scalar_one()
    await db.execute(text("""
        INSERT INTO collector_next.media_current(asset_id,owner_id,original_name,media_type,width,height)
        VALUES(:a,:o,:n,:m,:w,:h) ON CONFLICT(asset_id) DO NOTHING
    """), {"a": asset, "o": owner, "n": name, "m": image.media_type, "w": image.width, "h": image.height})
    entry = None
    if collection is not None:
        entry = (await db.execute(text("""
            INSERT INTO collector_next.collection_entry(owner_id,collection_id,asset_id,position)
            SELECT :o,:c,:a,COALESCE(CAST(:reserved AS bigint),GREATEST(COALESCE(MAX(position),0),
                COALESCE((SELECT max(collection_position) FROM collector_next.import_item WHERE collection_id=:c),0))+1)
            FROM collector_next.collection_entry WHERE collection_id=:c
            RETURNING id
        """), {"o": owner, "c": collection, "a": asset, "reserved":reserved_position})).scalar_one()
    await db.execute(text("""
        INSERT INTO collector_next.receipt(owner_id,request_key,fingerprint,original_name,relative_path,asset_id,entry_id)
        VALUES(:o,:key,:fp,:name,:path,:a,:e)
    """), {**params, "fp": fingerprint, "name": name, "path": relative_path, "a": asset, "e": entry})
    return {"asset_id": asset, "entry_id": entry, "replayed": False, "reused_original": reused}


async def original(db, owner: int, asset: int):
    row = (await db.execute(text("""
        SELECT b.sha256,b.byte_size,m.original_name,m.media_type
        FROM collector_next.asset a JOIN collector_next.blob b ON b.id=a.blob_id
        JOIN collector_next.media_current m ON m.asset_id=a.id WHERE a.owner_id=:o AND a.id=:a
    """), {"o": owner, "a": asset})).mappings().first()
    if row is None:
        raise NotFound("画像が見つかりません")
    return row
