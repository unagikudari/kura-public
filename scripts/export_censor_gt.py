#!/usr/bin/env python3
"""Export censor-detection GT for tagger-correction plan kura-tag-correction-heads-resume
(rev3 Step 5). Unifies three raw sources into one per-post record:

  1. censor_annotation (live rows) — region+type ground truth (the "本命" GT).
  2. censor_pair (confirmed) — censored/uncensored contrastive pairs, same artwork.
  3. judgment/verification pools (5252/6190/6191/6187/6188/6192/6207) — human-reviewed
     posts. Per INV-O the label is read from the post's CURRENT tags, never the pool
     name/expectation (a pool named "mosaic verification" can hold posts where the
     *type* claim was falsified but the *censored* tag survived review).

Output (versioned schema, content-addressed):
  <out>/labels.jsonl   — one JSON record per unique post
  <out>/images/<sha256>.<ext>  — image bytes, deduped by content hash
  <out>/manifest.json  — schema_version, counts, sha256 of labels.jsonl, split seed,
                          per-image sha256 list, conflict/drop counts (no silent caps)

Split: deterministic, seeded, computed over connected components of the pair graph
(so a censored/uncensored pair never straddles train/val/test — that would leak the
same artwork across the split boundary).

Usage:
  export_censor_gt.py --out /path/to/export/dir [--seed 1337]
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import asyncpg  # noqa: E402

from api import config  # noqa: E402
from api.services.files import content_path, MIME_TO_EXT  # noqa: E402

SCHEMA_VERSION = "censor-gt-v1"
JUDGMENT_POOL_IDS = [5252, 6190, 6191, 6187, 6188, 6192, 6207]
CENSORED_TAGS = {"censored", "mosaic_censoring", "bar_censor"}
UNCENSORED_TAGS = {"uncensored"}
SPLIT_SEED_DEFAULT = 1337
SPLIT_RATIOS = (("train", 0, 80), ("val", 80, 90), ("test", 90, 100))


def split_for(key: str, seed: int) -> str:
    h = hashlib.sha256(f"{seed}:{key}".encode()).digest()
    bucket = h[0] % 100
    for name, lo, hi in SPLIT_RATIOS:
        if lo <= bucket < hi:
            return name
    return "test"


class UnionFind:
    def __init__(self):
        self.parent = {}

    def find(self, x):
        self.parent.setdefault(x, x)
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
            x = self.parent[x]
        return x

    def union(self, a, b):
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[ra] = rb


async def fetch_all(conn, target_ids: list[int]) -> dict[int, dict]:
    """post_id -> {status, mime, media_location, has_data, tags, db_sha256}"""
    rows = await conn.fetch(
        """
        select id, status, "mime-type" as mime, media_location,
               (data is not null) as has_data, tags, encode(sha256, 'hex') as db_sha256,
               checksum_md5, checksum, file_size
        from post where id = any($1::int[])
        """,
        target_ids,
    )
    return {r["id"]: dict(r) for r in rows}


def read_image_bytes(post_id: int, mime: str, media_location: str | None, has_data: bool,
                      conn_row_data: bytes | None, identity=None) -> bytes | None:
    if has_data and conn_row_data is not None:
        return conn_row_data
    # legacy on-disk (media_location is null/'hdd' and no inline data). On a node without the
    # archive disk an archived original is fetched and verified against the post's identity
    # (SHA-256, else MD5; api/services/originals.py); unverifiable or unreachable → None.
    p = content_path(post_id, mime or "")
    from api.services import originals
    loc = originals.classify(p)
    if loc is None:
        return None
    if loc.kind == "local" or not originals.remote():
        return p.read_bytes() if p.exists() else None
    if identity is None or not (identity.sha256 or identity.md5):
        return None
    try:
        return originals.original_bytes(loc, identity)
    except originals.OriginalUnavailable:
        return None


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--seed", type=int, default=SPLIT_SEED_DEFAULT)
    args = ap.parse_args()

    out_dir = Path(args.out)
    images_dir = out_dir / "images"
    images_dir.mkdir(parents=True, exist_ok=True)

    dsn = config.DATABASE_URL_SYNC.replace("postgresql://", "postgresql://", 1)
    conn = await asyncpg.connect(dsn)

    # ── 1. censor_annotation (live) ──────────────────────────────────────
    ann_rows = await conn.fetch(
        """
        select id, post_id, censor_type, shape_type, geometry, negative,
               region_version, (mask is not null) as has_mask
        from censor_annotation
        where deleted_at is null
        order by post_id, id
        """
    )
    regions_by_post: dict[int, list[dict]] = {}
    for r in ann_rows:
        regions_by_post.setdefault(r["post_id"], []).append({
            "id": r["id"],
            "censor_type": r["censor_type"],
            "shape_type": r["shape_type"],
            "geometry": json.loads(r["geometry"]) if isinstance(r["geometry"], str) else r["geometry"],
            "negative": r["negative"],
            "region_version": r["region_version"],
            "has_mask": r["has_mask"],
        })

    # ── 2. censor_pair (confirmed) ───────────────────────────────────────
    pair_rows = await conn.fetch(
        """
        select cp.id as pair_id, cp.censored_post_id, cp.uncensored_post_id,
               cp.method, cp.off_censor_pct,
               pc.status as c_status, pu.status as u_status
        from censor_pair cp
        join post pc on pc.id = cp.censored_post_id
        join post pu on pu.id = cp.uncensored_post_id
        where cp.confirmed
        """
    )
    pair_by_post: dict[int, dict] = {}
    uf = UnionFind()
    for r in pair_rows:
        quality = "both_ok" if (r["c_status"] == "ok" and r["u_status"] == "ok") else "partial"
        base = {
            "pair_id": r["pair_id"], "method": r["method"],
            "off_censor_pct": r["off_censor_pct"], "pair_quality": quality,
        }
        pair_by_post[r["censored_post_id"]] = {
            **base, "role": "censored", "partner_post_id": r["uncensored_post_id"],
        }
        pair_by_post[r["uncensored_post_id"]] = {
            **base, "role": "uncensored", "partner_post_id": r["censored_post_id"],
        }
        uf.union(r["censored_post_id"], r["uncensored_post_id"])

    # ── 3. judgment pools ─────────────────────────────────────────────────
    pool_name_rows = await conn.fetch(
        "select pool_id, name from pool_name where pool_id = any($1::int[]) and ord = 0",
        JUDGMENT_POOL_IDS,
    )
    pool_names = {r["pool_id"]: r["name"] for r in pool_name_rows}
    pool_member_rows = await conn.fetch(
        "select pool_id, post_id from pool_post where pool_id = any($1::int[])",
        JUDGMENT_POOL_IDS,
    )
    pools_by_post: dict[int, list[dict]] = {}
    for r in pool_member_rows:
        pools_by_post.setdefault(r["post_id"], []).append({
            "pool_id": r["pool_id"], "pool_name": pool_names.get(r["pool_id"]),
        })

    # ── union universe ───────────────────────────────────────────────────
    target_ids = sorted(set(regions_by_post) | set(pair_by_post) | set(pools_by_post))
    print(f"target universe: {len(target_ids)} posts", file=sys.stderr)

    post_meta = await fetch_all(conn, target_ids)

    # split groups: connected components over the pair graph; solo posts get
    # their own component keyed by post_id.
    components: dict[int, list[int]] = {}
    for pid in target_ids:
        root = uf.find(pid)
        components.setdefault(root, []).append(pid)
    split_of: dict[int, str] = {}
    for root, members in components.items():
        key = ",".join(str(m) for m in sorted(members))
        s = split_for(key, args.seed)
        for m in members:
            split_of[m] = s

    records = []
    dropped_no_image = []
    conflicts = 0
    image_sha_index: dict[str, str] = {}  # sha256 -> ext, for tar dedup

    for pid in target_ids:
        meta = post_meta.get(pid)
        if meta is None:
            dropped_no_image.append({"post_id": pid, "reason": "post_row_missing"})
            continue

        # fetch raw data bytes only when needed (avoid loading all 5527 blobs twice)
        data_row = await conn.fetchrow('select data from post where id = $1', pid)
        from api.services.originals import Identity
        raw = read_image_bytes(pid, meta["mime"], meta["media_location"],
                                meta["has_data"], data_row["data"] if data_row else None,
                                Identity(meta.get("db_sha256"),
                                         meta.get("checksum_md5") or (meta.get("checksum")
                                             if len(meta.get("checksum") or "") == 32 else None),
                                         meta.get("file_size")))
        if raw is None:
            dropped_no_image.append({"post_id": pid, "reason": "image_bytes_unavailable"})
            continue

        sha256_hex = hashlib.sha256(raw).hexdigest()
        ext = MIME_TO_EXT.get(meta["mime"], "dat")
        img_path = images_dir / f"{sha256_hex}.{ext}"
        if sha256_hex not in image_sha_index:
            img_path.write_bytes(raw)
            image_sha_index[sha256_hex] = ext

        tags = meta["tags"] or []
        tag_set = set(tags)
        if tag_set & CENSORED_TAGS:
            tag_derived = True
        elif tag_set & UNCENSORED_TAGS:
            tag_derived = False
        else:
            tag_derived = None

        regions = regions_by_post.get(pid, [])
        positive_regions = [r for r in regions if not r["negative"]]
        annotation_derived = True if positive_regions else (None if not regions else None)
        censor_types = sorted({r["censor_type"] for r in positive_regions})

        pair_info = pair_by_post.get(pid)
        pair_derived = None
        if pair_info:
            pair_derived = pair_info["role"] == "censored"

        if annotation_derived is not None:
            final, final_source = annotation_derived, "annotation"
        elif pair_derived is not None:
            final, final_source = pair_derived, "pair"
        elif tag_derived is not None:
            final, final_source = tag_derived, "tag"
        else:
            final, final_source = None, "none"

        signals = [s for s in (annotation_derived, pair_derived, tag_derived) if s is not None]
        agreement = len(set(signals)) <= 1
        if not agreement:
            conflicts += 1

        records.append({
            "post_id": pid,
            "image_sha256": sha256_hex,
            "image_ext": ext,
            "post_status": meta["status"],
            "split": split_of[pid],
            "current_tags": tags,
            "labels": {
                "tag_derived": tag_derived,
                "annotation_derived": annotation_derived,
                "pair_derived": pair_derived,
                "final": final,
                "final_source": final_source,
                "agreement": agreement,
            },
            "censor_types": censor_types,
            "regions": regions,
            "pair": pair_info,
            "pool_memberships": pools_by_post.get(pid, []),
        })

    await conn.close()

    labels_path = out_dir / "labels.jsonl"
    with labels_path.open("w") as f:
        for rec in records:
            f.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")

    if dropped_no_image:
        dropped_path = out_dir / "dropped.json"
        dropped_path.write_text(json.dumps(dropped_no_image, ensure_ascii=False, indent=2))
        print(f"WARNING: {len(dropped_no_image)} posts dropped (no image bytes) — "
              f"see {dropped_path}", file=sys.stderr)

    split_counts = {}
    for rec in records:
        split_counts[rec["split"]] = split_counts.get(rec["split"], 0) + 1
    label_counts = {}
    for rec in records:
        k = rec["labels"]["final"]
        label_counts[str(k)] = label_counts.get(str(k), 0) + 1
    type_counts = {}
    for rec in records:
        for t in rec["censor_types"]:
            type_counts[t] = type_counts.get(t, 0) + 1

    summary = {
        "schema_version": SCHEMA_VERSION,
        "seed": args.seed,
        "total_records": len(records),
        "dropped": len(dropped_no_image),
        "label_conflicts": conflicts,
        "split_counts": split_counts,
        "final_label_counts": label_counts,
        "censor_type_counts": type_counts,
        "pairs": sum(1 for r in records if r["pair"]),
        "annotated": sum(1 for r in records if r["regions"]),
        "pool_only": sum(1 for r in records if not r["regions"] and not r["pair"]),
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    (out_dir / "export_summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
