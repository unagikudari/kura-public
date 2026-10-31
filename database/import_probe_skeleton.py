#!/usr/bin/env python3
"""plan kura-1.0-implementation S-7 — kura の post から kura1 へ scene/asset/blob の骨格だけ import。

**tag は import しない。** 観測は全て S-8 の人手注釈で入る (plan invariant)。tag を
持ち込むと「情報量の差」と「表現力の差」が混ざり、utility 測定が confound する
(broker fact b71cbc93 / 層1 vs 層2)。

**legacy は経由しない。** S-1 は kura の `pool:` 検索で選定したので選定結果は最初から
kura post id であり、legacy↔post の対応付け (rev8 までの S-8) は不要になった (rev9 で削除)。
副次効果として control arm (production kura の pool:7846) と kura1 の母集団が
構成上同一になる。

読み取りは production kura に対して **SELECT のみ**。書き込みは kura1 だけ。
blob は data-images を参照するだけでコピーしない (external_ref にパスを持つ)。

使い方:
    python3 database/import_probe_skeleton.py --pool 7846
    python3 database/import_probe_skeleton.py --pool 7846 --dry-run
"""
from __future__ import annotations

import argparse
import hashlib
import os
import pathlib
import re
import sys

KURA1_DSN_DEFAULT = "host=127.0.0.1 port=5541 dbname=kura1 user=desk"
DATA_IMAGES = pathlib.Path(os.environ.get(
    "KURA_DATA_IMAGES",
    pathlib.Path(__file__).resolve().parent.parent / "data-images" / "posts"))


def kura_dsn() -> str:
    """production kura の DSN を api/env の DATABASE_URL から作る (SELECT にのみ使う)。"""
    env = pathlib.Path(__file__).resolve().parent.parent / "api" / "env"
    for ln in env.read_text().splitlines():
        if ln.startswith("DATABASE_URL"):
            m = re.match(r"DATABASE_URL=postgresql\+asyncpg://([^:]+):([^@]+)@([^:/]+):(\d+)/(\S+)", ln.strip())
            if m:
                u, p, h, port, db = m.groups()
                return f"host={h} port={port} dbname={db} user={u} password={p}"
    raise SystemExit("api/env から DATABASE_URL を読めませんでした")


def blob_path(post_id: int, checksum: str, ext: str) -> pathlib.Path | None:
    """data-images/posts/<id>_<hash16>.<ext> を探す。hash 部分は post ごとに違うので glob。"""
    hits = sorted(DATA_IMAGES.glob(f"{post_id}_*"))
    return hits[0] if hits else None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pool", type=int, required=True, help="kura の pool id (probe 母集団)")
    ap.add_argument("--kura1-dsn", default=KURA1_DSN_DEFAULT)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    import psycopg

    # ── 1. production kura から骨格を読む (SELECT のみ) ──
    with psycopg.connect(kura_dsn()) as src, src.cursor() as cur:
        cur.execute(
            """
            -- 列名は DDL では "mime-type" (ハイフン) — ORM 側の mime_type とは別なので要引用符
            SELECT p.id, p.checksum, p."mime-type", p.type, p."image_width", p."image_height",
                   p.file_size, p.source, p.creation_time
            FROM post p
            JOIN pool_post pp ON pp.post_id = p.id
            WHERE pp.pool_id = %s
            ORDER BY p.id
            """,
            (args.pool,),
        )
        posts = cur.fetchall()
    print(f"production kura から {len(posts)} post を読みました (SELECT のみ)")
    if not posts:
        raise SystemExit(f"pool {args.pool} が空です")

    # ── 2. blob 実体の在処を解決 (コピーしない) ──
    resolved, missing = [], []
    for pid, checksum, mime, ptype, w, h, size, source, created in posts:
        path = blob_path(pid, checksum, "")
        (resolved if path else missing).append((pid, checksum, mime, ptype, w, h, size, source, created, path))
    if missing:
        print(f"  ! blob が見つからない post {len(missing)} 件: {[m[0] for m in missing[:5]]}", file=sys.stderr)
    print(f"  blob 解決 {len(resolved)} / {len(posts)}")

    if args.dry_run:
        for r in resolved[:3]:
            print(f"  (dry) post {r[0]} → {r[9].name} ({r[2]})")
        return 0

    # ── 3. kura1 に scene / asset / blob / asset_source を書く ──
    with psycopg.connect(args.kura1_dsn, autocommit=False) as dst, dst.cursor() as cur:
        cur.execute("SET search_path = kura, public")
        cur.execute("INSERT INTO blob_backend(slug, description) VALUES ('kura_data_images',"
                    " 'production kura の data-images/posts を参照 (コピーしない)')"
                    " ON CONFLICT (slug) DO NOTHING")
        cur.execute("INSERT INTO source(slug, kind) VALUES ('kura_post', 'legacy')"
                    " ON CONFLICT (slug) DO NOTHING")

        # **既に import 済の post は飛ばす。** この script は母集団が変わるたび同じ pool に
        # 対して再実行する使い方 (rev2 → rev3 で 14 → 18 件) なので、冪等でないと
        # **同じ post に scene が 2 つできる**。実際 14 件が二重になり、post 76 件に対して
        # scene が 90 件ある状態を作った (2026-07-30 に発見・掃除済)。
        # asset_source の ON CONFLICT DO NOTHING では防げない — 新しい asset_id は
        # 衝突しないので、provenance 行がもう 1 本増えるだけで通ってしまう。
        cur.execute("SELECT external_id FROM asset_source WHERE source_slug='kura_post'")
        already = {r[0] for r in cur.fetchall()}

        n_scene = n_asset = n_blob = n_skip = 0
        for pid, checksum, mime, ptype, w, h, size, source, created, path in resolved:
            if str(pid) in already:
                n_skip += 1
                continue
            # blob: sha256 は実体から計算する。kura の checksum は SHA-1 なので流用できない
            # (broker fact: kura post.checksum = hashlib.sha1)。Kura 1.0 の blob.sha256 は
            # 名前どおり sha256 でなければならないので、ここで計算するのが唯一正しい。
            sha256 = hashlib.sha256(path.read_bytes()).digest()
            cur.execute(
                "INSERT INTO blob(sha256, backend_slug, external_ref, byte_size, media_type)"
                " VALUES (%s,'kura_data_images',%s,%s,%s)"
                " ON CONFLICT (sha256) DO UPDATE SET external_ref = EXCLUDED.external_ref"
                " RETURNING id",
                (sha256, str(path), size, mime),
            )
            blob_id = cur.fetchone()[0]
            n_blob += 1

            cur.execute("INSERT INTO scene(created_at) VALUES (%s) RETURNING id", (created,))
            scene_id = cur.fetchone()[0]
            n_scene += 1

            cur.execute(
                "INSERT INTO asset(scene_id, role, blob_id, created_at)"
                " VALUES (%s,'source',%s,%s) RETURNING id",
                (scene_id, blob_id, created),
            )
            asset_id = cur.fetchone()[0]
            n_asset += 1

            # provenance: どの kura post 由来かを外部 id として残す (control arm と突き合わせる鍵)
            cur.execute(
                "INSERT INTO asset_source(asset_id, source_slug, external_id)"
                " VALUES (%s,'kura_post',%s) ON CONFLICT DO NOTHING",
                (asset_id, str(pid)),
            )
        dst.commit()

    print(f"kura1 へ投入: scene {n_scene} / asset {n_asset} / blob {n_blob}"
          f" / 既存で skip {n_skip}")
    print("tag は import していません (観測は S-8 の人手注釈のみ — plan invariant)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
