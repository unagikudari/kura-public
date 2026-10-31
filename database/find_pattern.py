#!/usr/bin/env python3
"""型 (phrase_macro) で resolved_facet を照合し、**分解された所見を 1 語で引く**。

なぜ要るか (user 指摘 2026-07-31):
  「そういった語句は全体ポーズの関係で**一発で引けるようにしてほしい**。
    **右手左手で指定するのは限界がある**」
  「**従来のタグとの関係性にもつながる**」

0071 で羽交い締めを behind + restraining(arm→axilla、左右) の 3 本に分解した。
記録としては正しいが、引くときに 3 本を join して左右まで合わせることになる。
入力の手間は 0064 の型で消したのに、**検索の手間が残っていた**。

型は「羽交い締めとは何の組み合わせか」を既に書いてあるので、**同じ定義を照合にも使う**。
語彙は増やさない — 羽交い締めという所見は作らず、当たるかどうかをその都度導く
(0019/0049/0060 と同じ「導けるものは保存しない」)。

照合の規則:
  * **laterality は見ない** — 右手左手の指定には限界がある (user 指摘)。
    書いてあれば残るが、引くときの条件にはしない
  * 型の region が NULL の行は**部位を問わない**
  * `$s` / `$o` は scene の中の entity へ**全通り**割り当てて、1 つでも通れば当たり
  * 同じ ref (`$s` を 2 回など) は**同じ entity** に縛る

使い方:
  python3 database/find_pattern.py --macro full_nelson
  python3 database/find_pattern.py --all
  python3 database/find_pattern.py --all --compare   # 従来タグと突き合わせる
"""
from __future__ import annotations

import argparse
import itertools
import json
import os
import urllib.parse
import urllib.request

import psycopg

DSN = os.environ.get("KURA1_DSN", "postgresql://desk@127.0.0.1:5541/kura1")
KURA_API = os.environ.get("KURA_API", "http://127.0.0.1:8001")


def load_macros(cur) -> dict:
    cur.execute("SELECT slug, phrase, description, tag_name FROM phrase_macro"
                " ORDER BY display_order, slug")
    m = {s: {"slug": s, "phrase": ph, "d": d or "", "tag": tg, "rows": []}
         for s, ph, d, tg in cur.fetchall()}
    cur.execute("SELECT macro_slug, relation_slug, subject_ref, subject_region,"
                " object_ref, object_region FROM phrase_macro_relation ORDER BY macro_slug, ord")
    for ms, rel, sr, sg, orf, og in cur.fetchall():
        m[ms]["rows"].append({"rel": rel, "s": sr, "sg": sg, "o": orf, "og": og})
    return {k: v for k, v in m.items() if v["rows"]}


def load_scenes(cur) -> dict:
    """current な解決結果から、scene ごとの関係 facet と post 番号を読む。"""
    cur.execute(
        "SELECT rs.scene_id, s.external_id,"
        "       rf.relation_slug, rf.entity_id, rf.region_slug,"
        "       rf.object_entity_id, rf.object_region_slug"
        "  FROM resolved_facet rf"
        "  JOIN resolved_scene rs ON rs.id = rf.resolved_scene_id"
        "  JOIN asset a ON a.scene_id = rs.scene_id"
        "  JOIN asset_source s ON s.asset_id = a.id"
        " WHERE rs.status = 'current' AND rf.relation_slug IS NOT NULL")
    out: dict = {}
    for sid, post, rel, ent, reg, oent, oreg in cur.fetchall():
        sc = out.setdefault(sid, {"post": post, "facets": [], "entities": set()})
        sc["facets"].append({"rel": rel, "s": ent, "sg": reg, "o": oent, "og": oreg})
        sc["entities"].add(ent)
        if oent is not None:
            sc["entities"].add(oent)
    return out


def row_matches(row: dict, f: dict, bind: dict) -> bool:
    if row["rel"] != f["rel"]:
        return False
    if bind.get(row["s"]) != f["s"]:
        return False
    if row["o"] is not None and bind.get(row["o"]) != f["o"]:
        return False
    # **型の region が NULL なら部位を問わない。** laterality は一切見ない
    if row["sg"] is not None and row["sg"] != f["sg"]:
        return False
    if row["og"] is not None and row["og"] != f["og"]:
        return False
    return True


def scene_matches(macro: dict, sc: dict) -> bool:
    refs = sorted({r["s"] for r in macro["rows"]}
                  | {r["o"] for r in macro["rows"] if r["o"] is not None})
    ents = sorted(sc["entities"])
    if not ents:
        return False
    # $s / $o への割り当てを全通り試す。同じ ref は同じ entity に縛られる
    for combo in itertools.product(ents, repeat=len(refs)):
        bind = dict(zip(refs, combo))
        if all(any(row_matches(r, f, bind) for f in sc["facets"]) for r in macro["rows"]):
            return True
    return False


_TAGS: dict = {}


def post_tags(post: str) -> set | None:
    """その post に付いている従来タグを kura から読む (1 post 1 回、以後は使い回す)。

    **母集団を kura1 の注釈済み集合に限るためにこの形にした。**
    最初は `?query=<tag>` で tag 側から引いていたが、`sex` は 319,593 件あり
    limit 2000 で切れた**任意の 2000 件**と突き合わせていた — 「型のみ 12 / 両方 0」
    という、比較になっていない数字が出た (2026-07-31、出す前に気付いた)。
    両側が同じ母集団を見ていないと精度も再現率も意味を持たない。
    """
    if post in _TAGS:
        return _TAGS[post]
    try:
        with urllib.request.urlopen(f"{KURA_API}/api/post/{post}", timeout=20) as r:
            d = json.load(r)
        names = set()
        for x in d.get("tags") or []:
            names |= set(x.get("names") or ([x] if isinstance(x, str) else []))
        _TAGS[post] = names
    except Exception:
        _TAGS[post] = None
    return _TAGS[post]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--macro")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--compare", action="store_true",
                    help="tag_name の付いた post と突き合わせる")
    ap.add_argument("--dsn", default=DSN)
    a = ap.parse_args()

    with psycopg.connect(a.dsn) as conn, conn.cursor() as cur:
        cur.execute("SET search_path = kura, public")
        macros = load_macros(cur)
        scenes = load_scenes(cur)

    want = [macros[a.macro]] if a.macro else list(macros.values())
    if a.macro and a.macro not in macros:
        raise SystemExit(f"型 '{a.macro}' が無い。あるのは: {' '.join(sorted(macros))}")

    for m in want:
        hits = sorted(sc["post"] for sc in scenes.values() if scene_matches(m, sc))
        line = f"{m['phrase']:18} {len(hits):3} 件"
        if m["tag"]:
            line += f"  (従来タグ {m['tag']})"
        print(line + ("  " + " ".join(hits[:12]) if hits else ""))
        if a.compare and m["tag"]:
            # **母集団は kura1 の注釈済み scene だけ。** 両側が同じ集合を見ないと
            # 精度も再現率も意味を持たない
            pop = sorted({sc["post"] for sc in scenes.values()})
            h = set(hits)
            tagged, unknown = set(), 0
            for pid in pop:
                names = post_tags(pid)
                if names is None:
                    unknown += 1
                elif m["tag"] in names:
                    tagged.add(pid)
            print(f"      母集団 {len(pop)} 件中: 型のみ {len(h - tagged):3} /"
                  f" 両方 {len(h & tagged):3} / タグのみ {len(tagged - h):3}"
                  + (f" / 引けず {unknown}" if unknown else "")
                  + "  ← 型のみ=flat tag の取りこぼし、タグのみ=未注釈または定義のずれ")
    if not a.macro and not a.all:
        print("\n--macro <slug> か --all を指定してください")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
