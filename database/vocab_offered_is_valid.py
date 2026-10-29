#!/usr/bin/env python3
"""**画面が出す語を validator が弾かない**ことを確かめる。

なぜ要るか (2026-07-31 の実害):
  0059 で `horse_girl` を非推奨にしたとき、**validator は弾くのに UI は候補に
  出したまま**にした。注釈者は選び続け、**確定が毎回落ちた**。画面には
  「押しても何も起きない」としか見えず、切り分けに 3 往復かかった。

個別に「非推奨を外した」と直しても同じ形はまた作れる。**規則そのものを検査する** —
UI が出す語を 1 つずつ最小の所見にして validator にかけ、1 つでも弾かれたら落とす。

使い方: python3 database/vocab_offered_is_valid.py
"""
from __future__ import annotations

import os
import sys
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).parent))

import annotate_scene as A  # noqa: E402
import annotate_server as S  # noqa: E402
import psycopg  # noqa: E402

DSN = os.environ.get("KURA1_DSN", "postgresql://desk@127.0.0.1:5541/kura1")


def base(subject: dict) -> dict:
    # **必須項目は埋めておく。** 埋めないと出る「minutes が無い …
    # minutes_unmeasured に理由を書く」という文に `red` が含まれ、
    # eye_color=red が弾かれたことになっていた (2026-07-31、部分一致の罠)
    return {"kura_post_id": 1, "annotator": "check", "minutes_unmeasured": "検査",
            "subjects": [subject],
            "relations": [], "scene_descriptors": [], "entity_counts": [],
            "unresolved": []}


def main() -> int:
    with psycopg.connect(DSN) as conn, conn.cursor() as cur:
        cur.execute("SET search_path = kura, public")
        V = S.vocab()
        ont = A.Ontology(cur)

        cases: list[tuple[str, str, dict]] = []
        for sp in V["species"]:
            cases.append(("species", sp["slug"],
                          base({"ref": "person_1", "identity": {"species": sp["slug"]}})))
        for r in V["region"]:
            cases.append(("region", r["slug"],
                          base({"ref": "person_1", "regions": [r["slug"]]})))
        for pz in V["pose"]:
            cases.append(("pose", pz["slug"],
                          base({"ref": "person_1", "poses": [{"pose": pz["slug"]}]})))
        for d in V["descriptor"]:
            a = base({"ref": "person_1"})
            a["scene_descriptors"] = [d["slug"]]
            cases.append(("descriptor", d["slug"], a))
        for o in V["object"]:
            slug = o["slug"] if isinstance(o, dict) else o
            cases.append(("object", slug,
                          base({"ref": "person_1", "objects": [{"object": slug}]})))
        for rel in V["relation"]:
            a = base({"ref": "person_1"})
            a["relations"] = [{"relation": rel["slug"],
                               "subject": {"ref": "person_1"}, "object": {"ref": "person_1"}}]
            cases.append(("relation", rel["slug"], a))
        for at in V["attribute"]:
            reg = at["applies_key"] if at["applies_kind"] == "region" else (
                "breast" if at["applies_kind"] in ("any_region", "class") else None)
            for v in at.get("values") or []:
                s = {"ref": "person_1",
                     "attributes": [{"attribute": at["slug"], "value": v["slug"]}]}
                if reg:
                    s["attributes"][0]["region"] = reg
                    s["regions"] = [reg]
                cases.append((f"attribute {at['slug']}", v["slug"], base(s)))

        bad = []
        for kind, slug, ann in cases:
            errs = A.validate(ann, ont)
            # **その語のせいで落ちたものだけ**を数える (他の必須項目の不足は無視)
            # **語として一致するものだけ**。素の部分一致は他の語の一部に当たる
            mine = [e for e in errs
                    if f"'{slug}'" in e or f"`{slug}`" in e or f" {slug} " in e]
            if mine:
                bad.append((kind, slug, mine[0][:110]))

    for kind, slug, why in bad:
        print(f"  NG  {kind} = {slug}\n      {why}")
    print(f"\n画面が出す語 {len(cases)} 件中、validator が弾くもの {len(bad)} 件")
    if bad:
        print("**画面が出す語を validator が弾いてはいけない** — "
              "注釈者は選べるのに確定できず、原因が画面から見えない (2026-07-31 の実害)")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
