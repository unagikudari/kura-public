#!/usr/bin/env python3
"""probe 母集団を **結果を見ずに** 選ぶ。plan kura-1.0-implementation の第 2 ラウンド用。

## なぜ必要か
第 1 ラウンド (pool 7846, 58 枚) では 14 query 中 4 本が**測定不能**で終わった。
理由は kura1 が解けなかったからではなく、**その構造を持つ画像が母集団に無かった**から:
A2 (口の行為の対象部位) は cunnilingus が 0 枚で 3 種類の対象が揃わず、A5/A6 は
腕・陰茎の持ち主が 2 人以上の scene が 3 枚しかなかった。
「解けなかった」と「測れなかった」は別物なので、後者を潰すには**構造で選び直す**しかない。

## 選定が満たすこと
* **規則が先、結果は後。** 選定規則は TSV に置き、この script は規則を機械適用するだけ。
  kura1 側の結果を見て条件を足す余地を残さない (S-1 で選定規則を fact 固定したのと同じ)。
* **タグで選ぶが、タグは持ち込まない。** 選定に使うのは production kura の tag だが、
  import_probe_skeleton.py は tag を import しない。観測は人手注釈だけで入る
  — でないと「情報量の差」と「表現力の差」が混ざる (層1 / 層2 の分離)。
* **等間隔で拾う。** id 降順の先頭 N 件だと直近の取り込みに偏り、1 人の作家の連投が
  そのまま母集団になりうる。stride で拾って corpus 全体に散らす。
* **多めに出して順位を付ける。** タグは構造を保証しない (`disembodied_limb` でも腕 2 本が
  同じ 1 人のことがある)。候補を n×oversample 件、**順位付きで**出し、採用は
  「順位順に見て構造条件を満たす先頭 n 件」とする。落とした候補も TSV に残るので、
  何を見て落としたかが後から辿れる。**満たすかどうかだけを見る — kura1 の結果は見ない。**
* **再現の SoT は出力 TSV。** corpus は増え続けるので同じ規則でも走らせた日で total が
  変わり、stride がずれる。規則は「どう選んだか」の記録で、母集団の実体は出力 TSV。
* **既存 pool を除く。** 第 1 ラウンドと混ぜない。混ぜると事前登録済みの測定が
  事後に変わる。
* **画像がローカルに無い post は採らない。** 注釈できないものを母集団に入れない。

## 使い方
    python3 database/select_probe_corpus.py --rules database/probe/round2-rules.tsv \\
        --exclude-pool 7846 --out database/probe/round2-selection.tsv
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import urllib.parse
import urllib.request

KURA_API = "http://127.0.0.1:8001"
DATA_IMAGES = pathlib.Path(os.environ.get(
    "KURA_DATA_IMAGES",
    pathlib.Path(__file__).resolve().parent.parent / "data-images" / "posts"))


def search(query: str, limit: int, offset: int) -> tuple[int, list[int]]:
    url = f"{KURA_API}/api/posts?" + urllib.parse.urlencode(
        {"query": query, "limit": limit, "offset": offset})
    with urllib.request.urlopen(url, timeout=60) as r:
        d = json.loads(r.read())
    return d.get("total", 0), [p["id"] for p in (d.get("results") or [])]


# 静止画だけ。**動画は母集団に入れない** — 注釈 UI は <img> で 1 枚を見せる作りで、
# 「どの時点の観測か」も表せない。glob だけで判定していた最初の版は mp4 を拾った
# (2026-07-30、Q15 の候補 579560)。
STILL_EXT = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"}


def has_image(post_id: int) -> bool:
    return any(f.suffix.lower() in STILL_EXT for f in DATA_IMAGES.glob(f"{post_id}_*"))


def pool_members(pool_id: int) -> set[int]:
    ids: set[int] = set()
    off = 0
    while True:
        total, page = search(f"pool:{pool_id}", 100, off)
        ids.update(page)
        off += len(page)
        if not page or off >= total:
            return ids


def pick(query: str, want: int, exclude: set[int]) -> list[int]:
    """id 降順に等間隔で want 件。欠けたら次の位置へずらして埋める。"""
    total, _ = search(query, 1, 0)
    if total == 0:
        return []
    stride = max(1, total // max(1, want))
    out: list[int] = []
    seen_offsets: set[int] = set()
    off = 0
    while len(out) < want and off < total:
        if off not in seen_offsets:
            seen_offsets.add(off)
            _, page = search(query, 1, off)
            for pid in page:
                if pid not in exclude and pid not in out and has_image(pid):
                    out.append(pid)
        # stride で進み、埋まらなければ 1 件ずつずらして隙間を拾う
        off += stride if len(seen_offsets) <= want else 1
        if off >= total and len(out) < want:
            off = 1 if 1 not in seen_offsets else max(seen_offsets) + 1
            if off >= total:
                break
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--rules", required=True, help="bucket \\t query \\t n の TSV")
    ap.add_argument("--exclude-pool", type=int, action="append", default=[])
    ap.add_argument("--oversample", type=int, default=3,
                    help="n の何倍の候補を出すか。採用は目視で構造条件を満たす先頭 n 件")
    ap.add_argument("--out", help="選定結果 TSV の書き出し先")
    a = ap.parse_args()

    exclude: set[int] = set()
    for p in a.exclude_pool:
        m = pool_members(p)
        print(f"除外 pool {p}: {len(m)} 件")
        exclude |= m

    rows: list[tuple[str, str, int]] = []
    for ln in pathlib.Path(a.rules).read_text(encoding="utf-8").splitlines():
        if not ln.strip() or ln.startswith("#"):
            continue
        bucket, query, n = ln.split("\t")
        rows.append((bucket, query, int(n)))

    out_lines = ["# bucket\trank\tpost_id\tquery  — select_probe_corpus.py が規則から機械生成。"
                 "採用は rank 順に構造条件を満たす先頭 n 件 (満たすかだけを目視、結果は見ない)"]
    for bucket, query, n in rows:
        ids = pick(query, n * a.oversample, exclude)
        exclude |= set(ids)                     # bucket 間でも重複させない
        print(f"{bucket:6} {query:42} → 候補 {len(ids):2} 件 (採用 {n})  {ids}")
        for rank, pid in enumerate(ids, 1):
            out_lines.append(f"{bucket}\t{rank}\t{pid}\t{query}")

    if a.out:
        pathlib.Path(a.out).write_text("\n".join(out_lines) + "\n", encoding="utf-8")
        print(f"\n書き出し: {a.out} ({len(out_lines)-1} 件)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
