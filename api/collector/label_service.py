"""Atomic label commands, incremental heads, and indexed current-state reads.

No per-tag history scan, row-level aggregation trigger, or JSON system of record.
"""
from collections import Counter
import hashlib
import json
from uuid import UUID

from sqlalchemy import text

from .errors import Conflict, NotFound
from . import tag_summary
from .labels import MAX_LABELS, MAX_TARGETS, changes, normalize_label


async def operation(db, owner: int, key: UUID):
    command = (await db.execute(text("""
        SELECT o.fingerprint,o.asset_count,o.tag_count,p.version AS policy
        FROM collector_next.label_operation o JOIN collector_next.label_policy p ON p.id=o.policy_id
        WHERE o.owner_id=:o AND o.request_key=:k
    """), {"o": owner, "k": key})).mappings().first()
    if command is None:
        raise NotFound("タグ操作が見つかりません。")
    targets = [dict(row) for row in (await db.execute(text("""
        SELECT asset_id,after_revision AS revision FROM collector_next.label_target
        WHERE owner_id=:o AND request_key=:k ORDER BY asset_id
    """), {"o": owner, "k": key})).mappings()]
    return {"request_key": str(key), "targets": targets,
            "observations": command["asset_count"] * command["tag_count"], "resolver_version": command["policy"]}


async def apply(db, owner: int, key: UUID, targets: list[tuple[int, int]], add: list[str], remove: list[str], *, origin: str = "ui"):
    if origin not in ("ui","mcp"):raise ValueError("Unknown label command origin")
    edits = changes(add, remove)
    expected = dict(targets)
    if not 1 <= len(expected) <= MAX_TARGETS or len(expected) != len(targets) or any(a <= 0 or rev < 0 for a, rev in targets):
        raise ValueError("重複のない1～100件を選択してください。")
    asset_ids = sorted(expected)
    fingerprint = hashlib.sha256(json.dumps([sorted(targets), [(k, v) for _, k, v in edits]],
                                           ensure_ascii=False, separators=(",", ":")).encode()).digest()
    lock = int.from_bytes(hashlib.sha256(f"collector-label:{owner}:{key}".encode()).digest()[:8], "big", signed=True)
    await db.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": lock})
    old = (await db.execute(text("SELECT fingerprint FROM collector_next.label_operation WHERE owner_id=:o AND request_key=:k"),
                            {"o": owner, "k": key})).scalar_one_or_none()
    if old is not None:
        if old != fingerprint:
            raise Conflict("同じ受付キーで異なるタグ操作が送信されました。")
        return {**await operation(db, owner, key), "replayed": True}
    locked = (await db.execute(text("""
        SELECT a.id,b.byte_size FROM collector_next.asset a JOIN collector_next.blob b ON b.id=a.blob_id
        WHERE a.owner_id=:o AND a.id=ANY(CAST(:ids AS bigint[])) ORDER BY a.id FOR UPDATE OF a
    """), {"o": owner, "ids": asset_ids})).mappings().all()
    byte_sizes = {r["id"]: r["byte_size"] for r in locked}
    if len(locked) != len(asset_ids):
        raise NotFound("選択したメディアが見つかりません。")
    await db.execute(text("""
        INSERT INTO collector_next.asset_label_state(asset_id,owner_id)
        SELECT unnest(CAST(:ids AS bigint[])),:o ON CONFLICT(asset_id) DO NOTHING
    """), {"o": owner, "ids": asset_ids})
    states = {row["asset_id"]: row for row in (await db.execute(text("""
        SELECT asset_id,revision,positive_count,last_observed_at FROM collector_next.asset_label_state
        WHERE owner_id=:o AND asset_id=ANY(CAST(:ids AS bigint[]))
    """), {"o": owner, "ids": asset_ids})).mappings()}
    if any(states[a]["revision"] != expected[a] for a in asset_ids):
        raise Conflict("選択したメディアのタグが別の操作で更新されています。一覧を再読み込みして選び直してください。")
    # Dictionary writes and usage locks always follow normalized key / integer order.
    await db.execute(text("""
        INSERT INTO collector_next.tag(owner_id,name,normalized_name)
        SELECT :o,x.name,x.key FROM unnest(CAST(:names AS text[]),CAST(:keys AS text[])) x(name,key)
        ORDER BY x.key COLLATE "C" ON CONFLICT(owner_id,normalized_name) DO NOTHING
    """), {"o": owner, "names": [e[0] for e in edits], "keys": [e[1] for e in edits]})
    tags = {row["normalized_name"]: row["id"] for row in (await db.execute(text("""
        SELECT id,normalized_name FROM collector_next.tag WHERE owner_id=:o AND normalized_name=ANY(CAST(:keys AS text[]))
    """), {"o": owner, "keys": [e[1] for e in edits]})).mappings()}
    tag_ids = sorted(tags.values())
    desired = {tags[k]: present for _, k, present in edits}
    heads = {(row["asset_id"], row["tag_id"]): row for row in (await db.execute(text("""
        SELECT asset_id,tag_id,observation_id,present FROM collector_next.label_head
        WHERE asset_id=ANY(CAST(:ids AS bigint[])) AND tag_id=ANY(CAST(:tags AS bigint[]))
    """), {"ids": asset_ids, "tags": tag_ids})).mappings()}
    deltas, asset_deltas, size_deltas = Counter(), Counter(), Counter()
    for asset in asset_ids:
        for tag in tag_ids:
            delta = int(desired[tag]) - int(heads.get((asset, tag), {}).get("present", False))
            deltas[tag] += delta
            size_deltas[tag] += delta * byte_sizes[asset]
            asset_deltas[asset] += delta
        if states[asset]["positive_count"] + asset_deltas[asset] > MAX_LABELS:
            raise ValueError("1件に付けられるタグは128個までです。不要なタグを外してから追加してください。")
    producer_params = {"o": owner, "ref": str(owner), "version": "collector-label-mcp-v1" if origin=="mcp" else "collector-label-api-v1"}
    await db.execute(text("""
        INSERT INTO collector_next.label_producer(owner_id,kind_id,reference,version)
        VALUES(:o,1,:ref,:version) ON CONFLICT(owner_id,kind_id,reference,version) DO NOTHING
    """), producer_params)
    producer = (await db.execute(text("""
        SELECT id FROM collector_next.label_producer WHERE owner_id=:o AND kind_id=1 AND reference=:ref AND version=:version
    """), producer_params)).scalar_one()
    await db.execute(text("""
        INSERT INTO collector_next.label_operation(owner_id,request_key,fingerprint,producer_id,asset_count,tag_count)
        VALUES(:o,:k,:fp,:p,:ac,:tc)
    """), {"o": owner, "k": key, "fp": fingerprint, "p": producer, "ac": len(asset_ids), "tc": len(tag_ids)})
    now = (await db.execute(text("SELECT clock_timestamp()"))).scalar_one()
    times = {a: max(now, states[a]["last_observed_at"]) for a in asset_ids}
    pairs = [(a, t) for a in asset_ids for t in tag_ids]
    evidence = [dict(row) for row in (await db.execute(text("""
        INSERT INTO collector_next.label_observation(owner_id,asset_id,tag_id,producer_id,request_key,present,prior_present,based_on_id,observed_at)
        SELECT :o,x.asset,x.tag,:p,:k,x.present,x.prior,x.base,x.at
        FROM unnest(CAST(:assets AS bigint[]),CAST(:tags AS bigint[]),CAST(:values AS boolean[]),
                    CAST(:priors AS boolean[]),CAST(:bases AS bigint[]),CAST(:times AS timestamptz[])) x(asset,tag,present,prior,base,at)
        RETURNING id,asset_id,tag_id,present
    """), {"o": owner, "p": producer, "k": key,
           "assets": [a for a, t in pairs], "tags": [t for a, t in pairs],
           "values": [desired[t] for a, t in pairs], "priors": [heads.get(pair, {}).get("present", False) for pair in pairs],
           "bases": [heads.get(pair, {}).get("observation_id") for pair in pairs], "times": [times[a] for a, t in pairs]})).mappings()]
    await db.execute(text("""
        INSERT INTO collector_next.label_head(owner_id,asset_id,tag_id,observation_id,present)
        SELECT :o,x.asset,x.tag,x.id,x.present FROM unnest(CAST(:assets AS bigint[]),CAST(:tags AS bigint[]),
            CAST(:observations AS bigint[]),CAST(:values AS boolean[])) x(asset,tag,id,present)
        ON CONFLICT(asset_id,tag_id) DO UPDATE SET observation_id=EXCLUDED.observation_id,present=EXCLUDED.present
    """), {"o": owner, "assets": [e["asset_id"] for e in evidence], "tags": [e["tag_id"] for e in evidence],
           "observations": [e["id"] for e in evidence], "values": [e["present"] for e in evidence]})
    await tag_summary.apply(db, owner, deltas, size_deltas)
    await db.execute(text("""
        UPDATE collector_next.asset_label_state s SET revision=s.revision+1,
          positive_count=s.positive_count+x.delta,last_observed_at=x.at,policy_id=1
        FROM unnest(CAST(:assets AS bigint[]),CAST(:deltas AS integer[]),CAST(:times AS timestamptz[])) x(asset,delta,at)
        WHERE s.owner_id=:o AND s.asset_id=x.asset
    """), {"o": owner, "assets": asset_ids, "deltas": [asset_deltas[a] for a in asset_ids], "times": [times[a] for a in asset_ids]})
    await db.execute(text("""
        INSERT INTO collector_next.label_target(owner_id,request_key,asset_id,before_revision,after_revision)
        SELECT :o,:k,x.asset,x.prior,x.prior+1 FROM unnest(CAST(:assets AS bigint[]),CAST(:revisions AS bigint[])) x(asset,prior)
    """), {"o": owner, "k": key, "assets": asset_ids, "revisions": [expected[a] for a in asset_ids]})
    return {**await operation(db, owner, key), "replayed": False}


async def enrich(db, owner, rows):
    if not rows:
        return rows
    ids = sorted({row["id"] for row in rows})
    metadata = {row["asset_id"]: row for row in (await db.execute(text("""
        SELECT a.id AS asset_id,COALESCE(s.revision,0) AS revision,
               COALESCE(array_agg(t.name ORDER BY t.normalized_name) FILTER(WHERE t.id IS NOT NULL),ARRAY[]::varchar[]) AS tags
        FROM unnest(CAST(:ids AS bigint[])) a(id)
        LEFT JOIN collector_next.asset_label_state s ON s.asset_id=a.id AND s.owner_id=:o
        LEFT JOIN collector_next.media_tag_current c ON c.asset_id=a.id AND c.owner_id=:o
        LEFT JOIN collector_next.tag t ON t.id=c.tag_id
        GROUP BY a.id,s.revision
    """), {"o": owner, "ids": ids})).mappings()}
    return [{**row, "tag_revision": metadata[row["id"]]["revision"], "tags": metadata[row["id"]]["tags"]} for row in rows]


async def filter_tags(db, owner, names):
    if not names:
        return []
    if len(names) > 16:
        raise ValueError("検索するタグは16個までです。")
    keys = sorted({normalize_label(name)[1] for name in names})
    if len(keys) > 16:
        raise ValueError("検索するタグは16個までです。")
    rows = (await db.execute(text("""
        SELECT t.id,t.normalized_name,COALESCE(u.positive_count,0) AS count
        FROM collector_next.tag t LEFT JOIN collector_next.tag_usage u ON u.tag_id=t.id
        WHERE t.owner_id=:o AND t.normalized_name=ANY(CAST(:keys AS text[]))
        ORDER BY count,t.id
    """), {"o": owner, "keys": keys})).mappings().all()
    if len(rows) != len(keys) or any(row["count"] == 0 for row in rows):
        return None
    return [row["id"] for row in rows]


def additional_filters(tag_ids, kind, params, asset="m.asset_id"):
    clauses = []
    for index, tag in enumerate(tag_ids[1:]):
        key = f"label_{index}"
        params[key] = tag
        clauses.append(f"EXISTS(SELECT 1 FROM collector_next.media_tag_current f WHERE f.owner_id=:owner AND f.asset_id={asset} AND f.tag_id=:{key})")
    if kind is not None:
        if kind not in ("image", "video"):
            raise ValueError("種別は画像または動画を指定してください。")
        clauses.append(f"m.media_type LIKE '{kind}/%'")
    return "".join(" AND " + clause for clause in clauses)


async def history(db, owner, asset, limit=30):
    if not 1 <= limit <= 100:
        raise ValueError("履歴は1～100件ずつ取得してください。")
    if not (await db.execute(text("SELECT id FROM collector_next.asset WHERE owner_id=:o AND id=:a"), {"o": owner, "a": asset})).first():
        raise NotFound("メディアが見つかりません。")
    return [dict(row) for row in (await db.execute(text("""
        SELECT e.id,t.name,e.present,e.prior_present,e.observed_at,e.request_key,p.version AS producer_version
        FROM collector_next.label_observation e JOIN collector_next.tag t ON t.id=e.tag_id
        JOIN collector_next.label_producer p ON p.id=e.producer_id
        WHERE e.owner_id=:o AND e.asset_id=:a ORDER BY e.id DESC LIMIT :lim
    """), {"o": owner, "a": asset, "lim": limit})).mappings()]


async def rebuild_asset(db, owner, asset):
    """Explicit maintenance path: rebuild one asset from immutable evidence.

    SQL performs the history reduction; normal edits never run this path. Affected
    posting counts are reconciled while locked, so concurrent edits on other
    assets can safely apply their deltas after this transaction commits.
    """
    params = {"o": owner, "a": asset}
    if not (await db.execute(text("SELECT id FROM collector_next.asset WHERE owner_id=:o AND id=:a FOR UPDATE"), params)).first():
        raise NotFound("メディアが見つかりません。")
    if (await db.execute(text("""
        SELECT 1 FROM collector_next.label_observation e JOIN collector_next.label_operation o
          ON o.owner_id=e.owner_id AND o.request_key=e.request_key
        WHERE e.owner_id=:o AND e.asset_id=:a AND o.policy_id<>1 LIMIT 1
    """), params)).first():
        raise ValueError("この再構築版で扱えないタグ方針が含まれています。")
    await db.execute(text("DROP TABLE IF EXISTS pg_temp.collector_label_rebuild_tags"))
    await db.execute(text("""
        CREATE TEMP TABLE collector_label_rebuild_tags ON COMMIT DROP AS
        SELECT tag_id FROM collector_next.label_head WHERE owner_id=:o AND asset_id=:a
        UNION SELECT tag_id FROM collector_next.label_observation WHERE owner_id=:o AND asset_id=:a
    """), params)
    await db.execute(text("DELETE FROM collector_next.label_head WHERE owner_id=:o AND asset_id=:a"), params)
    await db.execute(text("""
        INSERT INTO collector_next.label_head(owner_id,asset_id,tag_id,observation_id,present)
        SELECT :o,x.asset_id,x.tag_id,x.id,x.present FROM (
          SELECT DISTINCT ON(asset_id,tag_id) asset_id,tag_id,id,present
          FROM collector_next.label_observation WHERE owner_id=:o AND asset_id=:a
          ORDER BY asset_id,tag_id,observed_at DESC,id DESC
        ) x
    """), params)
    await db.execute(text("""
        INSERT INTO collector_next.asset_label_state(asset_id,owner_id,revision,positive_count,last_observed_at,policy_id)
        SELECT :a,:o,count(DISTINCT request_key),
          (SELECT count(*) FROM collector_next.media_tag_current WHERE owner_id=:o AND asset_id=:a),
          COALESCE(max(observed_at),'epoch'::timestamptz),1
        FROM collector_next.label_observation WHERE owner_id=:o AND asset_id=:a
        ON CONFLICT(asset_id) DO UPDATE SET revision=EXCLUDED.revision,positive_count=EXCLUDED.positive_count,
          last_observed_at=EXCLUDED.last_observed_at,policy_id=EXCLUDED.policy_id
    """), params)
    affected = (await db.execute(text("SELECT tag_id FROM pg_temp.collector_label_rebuild_tags"))).scalars().all()
    await tag_summary.rebuild(db, owner, affected)
    row = (await db.execute(text("SELECT revision,positive_count FROM collector_next.asset_label_state WHERE owner_id=:o AND asset_id=:a"), params)).mappings().one()
    return {"asset_id": asset, "revision": row["revision"], "labels": row["positive_count"]}
