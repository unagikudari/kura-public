#!/usr/bin/env python3
"""plan kura-1.0-implementation S-8 — 人手注釈 JSON を kura1 の append-only 観測に書く。

注釈者の仕事は「画像を見て JSON を書く」だけ。DDL の作法 (append-only / entity は
resolver 出力 / 非捏造の scope / ontology FK) は本 writer が担う。

## 設計上の要点
* **entity を作らない。** 0002 以降 entity は resolver の出力なので、注釈は
  `observation.subject_ref` (例 'person_1') で主体を指すだけ。どの entity を指すかは
  S-9 の resolver が run ごとに決める。
* **relation も ref で書く** (0004)。`obs_relation.subject_ref` + `subject_region_slug`
  で「person_2 の hand」を entity 無しに表せる。
* **分からないものは埋めない。** 主体が特定できない person-scoped な事実は
  `subject_scope='unresolved'` で入れる。候補集合の列挙は resolver の仕事
  (`unresolved_subject_candidate` は resolver_run と entity を要求するため)。
  → **既知の限界**: 注釈者が「3 人のうち 2 人に絞れる」と分かっても絞り込みは保存
     されない。resolver は entity_count から候補を導くので、1-2 人の scene では
     情報損失ゼロ、3 人以上で絞れる場合のみ損失する。plan S-12 に記録する。
* **ontology に無い語は書けない。** FK が弾くが、それだと原因が読み取りにくいので
  投入前に検証して「どの語が無いか / 近い候補は何か」を出す (裸のエラーは禁止)。

## JSON 形式 (1 ファイル = 1 scene)
{
  "kura_post_id": 338998,
  "annotator": "primary-cc-primary",
  "minutes": 6.5,
  "subjects": [
    {"ref": "person_1", "presentation": "female",
     "bbox": [0.10, 0.05, 0.45, 0.95],          // 任意 [x_min,y_min,x_max,y_max] 0..1
     "regions": ["breast", "nipple"],            // 見えている body_region
     "attributes": [{"attribute": "breast_size", "value": "huge"},
                    {"attribute": "wetness", "numeric": 0.7}],
     "poses": ["standing"],
     "identity": {"species": "human", "character": "lili_(tekken)"},
     "objects": [{"object": "dress", "state": "worn"}]}
  ],
  "relations": [
    {"relation": "touching",
     "subject": {"ref": "person_2", "region": "hand"},
     "object":  {"ref": "person_1", "region": "breast"}}
  ],
  "scene_descriptors": ["from_above"],
  "entity_counts": [{"presentation": "female", "count_min": 1, "count_exact": true}],
  "unresolved": [
    {"domain": "attribute", "attribute": "breast_size", "value": "large",
     "note": "2 人いてどちらの胸か判別できない"}
  ],
  "notes": "自由記述 (任意)"
}

使い方:
    python3 database/annotate_scene.py --file ann/338998.json --validate-only
    python3 database/annotate_scene.py --file ann/338998.json
    python3 database/annotate_scene.py --dir ann/          # ディレクトリ一括
"""
from __future__ import annotations

import argparse
import collections
import difflib
import json
import pathlib
import sys

DSN_DEFAULT = "host=127.0.0.1 port=5541 dbname=kura1 user=desk"


def anchor_region(anc: dict) -> str:
    """点が指す部位。0028 で `kind`(head_center 等) から body_region 参照へ移した。

    古い下書きは `kind` を持つので `_center` を落として読む — 移行前に書かれたものを
    捨てないため (下書きは手作業の成果で、作り直させる理由が無い)。
    """
    if anc.get("region"):
        return anc["region"]
    return (anc.get("kind") or "head_center").removesuffix("_center")


class Ontology:
    """kura1 に実在する語彙。未知語の検出と「近い候補」の提示に使う。"""

    def __init__(self, cur):
        q = lambda sql: {r[0] for r in cur.execute(sql).fetchall()}  # noqa: E731
        self.regions = q("SELECT slug FROM body_region")
        self.attributes = q("SELECT slug FROM attribute")
        self.attr_values = {(a, v) for a, v in cur.execute(
            "SELECT attribute_slug, value_slug FROM attribute_value").fetchall()}
        self.poses = q("SELECT slug FROM pose")
        self.relations = q("SELECT slug FROM relation")
        self.descriptors = q("SELECT slug FROM scene_descriptor")
        # 0019: 機械で決まるファセット (orientation 等) は**人手で書かせない**。
        # UI で出さないだけでは、手書きの JSON や古い下書きから入りうるので
        # 検証側で弾く (原則は仕組みで守る)。
        self.derived_descriptors = {
            r[0]: r[1] for r in cur.execute(
                "SELECT d.slug, coalesce(d.derived_from, f.derived_from)"
                " FROM scene_descriptor d JOIN scene_facet f ON f.slug = d.facet"
                " WHERE d.derived_from IS NOT NULL OR f.derived_from IS NOT NULL").fetchall()}
        self.objects = q("SELECT slug FROM object_type")
        self.object_states = q("SELECT slug FROM object_state")
        self.object_variants = q("SELECT slug FROM object_variant")
        self.surfaces = q("SELECT slug FROM scene_surface")
        self.species = q("SELECT slug FROM species")
        # 非推奨は**データから引く** — script 側に一覧を持つと ontology と二重になる
        self.deprecated_species = q(
            "SELECT slug FROM species WHERE display_name LIKE '%[非推奨]%'")
        self.characters = q("SELECT slug FROM character")
        self.presentations = q("SELECT slug FROM gender_presentation")
        self.entity_kinds = q("SELECT slug FROM entity_kind")
        self.domains = q("SELECT slug FROM observation_domain")


    def check(self, kind: str, value: str, pool: set[str], errs: list[str]) -> None:
        if value in pool:
            return
        near = difflib.get_close_matches(value, sorted(pool), n=3, cutoff=0.6)
        fix = f"近い語: {', '.join(near)}" if near else \
              f"この語彙は {len(pool)} 語しかない。seed 追加が必要なら plan に上げる"
        errs.append(f"{kind} '{value}' は ontology に無い — {fix}")


def validate(ann: dict, onto: Ontology) -> list[str]:
    errs: list[str] = []
    if "kura_post_id" not in ann:
        errs.append("kura_post_id が無い")
    if not ann.get("annotator"):
        errs.append("annotator が無い (producer_ref に入る — 誰の観測か辿れなくなる)")

    refs = set()
    for s in ann.get("subjects", []):
        ref = s.get("ref")
        if not ref:
            errs.append("subjects[].ref が無い (主体を指す語が無いと観測を書けない)")
            continue
        refs.add(ref)
        # 0030: 主体は人とは限らない (触手の主 等)。人でない主体に presentation は付かない
        kind = s.get("kind", "person")
        if kind not in onto.entity_kinds:
            errs.append(f"kind '{kind}' は ontology に無い — "
                        f"取れる値: {', '.join(sorted(onto.entity_kinds))}")
        if s.get("presentation"):
            if kind != "person":
                errs.append(f"kind={kind} の主体に presentation は付かない "
                            f"— 人としての属性を非人間に付けることになる")
            onto.check("presentation", s["presentation"], onto.presentations, errs)
        for r in s.get("regions", []):
            onto.check("body_region", r, onto.regions, errs)
        for a in s.get("attributes", []):
            slug = a.get("attribute", "")
            if a.get("region"):
                onto.check("body_region", a["region"], onto.regions, errs)
            if a.get("laterality") and a["laterality"] not in ("na", "left", "right"):
                errs.append(f"attribute '{slug}' の laterality は na/left/right: {a['laterality']}")
            onto.check("attribute", slug, onto.attributes, errs)
            if a.get("value") is not None and slug in onto.attributes:
                if (slug, a["value"]) not in onto.attr_values:
                    vals = sorted(v for (s2, v) in onto.attr_values if s2 == slug)
                    errs.append(f"attribute '{slug}' に値 '{a['value']}' は無い — 取れる値: {', '.join(vals)}")
            if a.get("value") is None and a.get("numeric") is None:
                errs.append(f"attribute '{slug}' に value も numeric も無い")
        for p in s.get("poses", []):
            # pose は 'slug' でも {"pose": slug, "laterality": "left"} でもよい
            pv = p.get("pose", "") if isinstance(p, dict) else p
            onto.check("pose", pv, onto.poses, errs)
            if isinstance(p, dict) and p.get("laterality") \
                    and p["laterality"] not in ("na", "left", "right"):
                errs.append(f"pose '{pv}' の laterality は na/left/right: {p['laterality']}")
        ident = s.get("identity") or {}
        if ident.get("species"):
            onto.check("species", ident["species"], onto.species, errs)
            # 0040: `_girl` 系は「人である」「動物の耳がある」「どの動物か」
            # 「女性に見える」を 1 語に混ぜており、最後は presentation と二重になる。
            # **新しく書かせない** — 既存の観測は残るが、書き方は ontology が教える。
            if ident["species"] in onto.deprecated_species:
                errs.append(
                    f"species '{ident['species']}' は非推奨 — "
                    f"species='human' + identity.motif_species='"
                    f"{ident['species'].replace('_girl','').replace('_boy','')}' + "
                    f"見えている部位 (animal_ear / tail) + presentation に分けて書く")
        if ident.get("motif_species"):
            onto.check("species", ident["motif_species"], onto.species, errs)
        if ident.get("character"):
            onto.check("character", ident["character"], onto.characters, errs)
        for o in s.get("objects", []):
            onto.check("object_type", o.get("object", ""), onto.objects, errs)
            if o.get("state"):
                onto.check("object_state", o["state"], onto.object_states, errs)
            # 0045: どこに付いているか。ピアスは部位が本体なので語彙で検査する
            if o.get("region"):
                onto.check("body_region", o["region"], onto.regions, errs)
            # 0046: 形と数。形は語彙、数は正の整数 (0 は「無い」ではなく書かないで表す)
            if o.get("variant"):
                onto.check("object_variant", o["variant"], onto.object_variants, errs)
            if o.get("laterality") and o["laterality"] not in ("na","left","right"):
                errs.append(f"objects[].laterality は na/left/right: {o['laterality']!r}")
            if o.get("count") is not None and not (
                    isinstance(o["count"], int) and o["count"] > 0):
                errs.append(f"objects[].count は正の整数: {o['count']!r} "
                            f"— 「無い」は所見を書かないことで表す")
        bb = s.get("bbox")
        if bb is not None:
            if len(bb) != 4 or not all(isinstance(v, (int, float)) for v in bb):
                errs.append(f"bbox は [x_min,y_min,x_max,y_max] の 4 数値: {bb}")
            elif not (0 <= bb[0] < bb[2] <= 1 and 0 <= bb[1] < bb[3] <= 1):
                errs.append(f"bbox は 0..1 で min<max: {bb}")
        # 0021: 位置は box でも点でもよい。点は「何を指したか」まで含めて 1 つの事実。
        anc = s.get("anchor")
        if anc:
            if not isinstance(anc, dict) or anc.get("x") is None or anc.get("y") is None:
                errs.append(f"anchor は {{x, y, region}} の形: {anc}")
            else:
                if not all(0 <= float(anc[k]) <= 1 for k in ("x", "y")):
                    errs.append(f"anchor の x/y は 0..1 の相対座標: {anc}")
                # 0028: 点は body_region の中心を指す。旧 draft の kind='head_center' も読む。
                onto.check("body_region", anchor_region(anc), onto.regions, errs)

    for rel in ann.get("relations", []):
        onto.check("relation", rel.get("relation", ""), onto.relations, errs)
        for role in ("subject", "object"):
            side = rel.get(role) or {}
            if side.get("ref") and side["ref"] not in refs:
                errs.append(f"relation の {role}.ref '{side['ref']}' が subjects に無い"
                            f" — subjects に足すか、参加者を省く (片側だけの relation は許される)")
            if side.get("laterality") and side["laterality"] not in ("na", "left", "right"):
                errs.append(f"relation の {role}.laterality は na/left/right: {side['laterality']}")
            if side.get("laterality") and not side.get("region"):
                errs.append(f"relation の {role} に laterality だけあって region が無い "
                            f"— 左右は部位の属性なので、どの部位かが要る")
            # 0052: 対象が環境 (地面・空中) のとき。**人体と両方は書けない**
            if side.get("surface"):
                if role != "object":
                    errs.append("surface は object 側にしか書けない (主体は必ず部位)")
                onto.check("scene_surface", side["surface"], onto.surfaces, errs)
                if side.get("ref") or side.get("region"):
                    errs.append("object に surface と 部位 の両方は書けない — "
                                "「顔に、かつ地面に」は解釈不能。両方に飛んでいるなら 2 所見で書く")
            if side.get("region"):
                onto.check("body_region", side["region"], onto.regions, errs)
                if not side.get("ref"):
                    errs.append(f"relation の {role} に region だけあって ref が無い"
                                f" — region は「誰の region か」を ref が担うので両方必要")
    for d in ann.get("scene_descriptors", []):
        onto.check("scene_descriptor", d, onto.descriptors, errs)
        if d in onto.derived_descriptors:
            errs.append(f"scene_descriptor '{d}' は機械で決まるので人手で書けない — "
                        f"{onto.derived_descriptors[d]}")
    for ec in ann.get("entity_counts", []):
        onto.check("presentation", ec.get("presentation", ""), onto.presentations, errs)
        if not isinstance(ec.get("count_min"), int):
            errs.append(f"entity_counts[].count_min は整数: {ec}")
        # 0079: 参加者と傍観者を分ける。NULL は「言っていない」
        if nz(ec.get("involvement")) not in (None, "participant", "bystander"):
            errs.append(f"entity_counts[].involvement は participant / bystander: {ec}")
    for s in ann.get("subjects", []):
        for a in s.get("attributes", []):
            if a.get("text") and a.get("attribute") != "surface_marking":
                errs.append(
                    f"attribute '{a.get('attribute')}' に text は書けない — "
                    "自由入力は surface_marking の「書いてある文字」だけ (0070)。"
                    "**メモ欄ではない**")
            if a.get("text") and len(a["text"]) > 200:
                errs.append("text は 200 字まで — 文章を書く場所ではない (0070)")

    for r in ann.get("relations", []):
        obj = r.get("object") or {}
        # **対象は人体か環境のどちらか** (DB の ck_obs_relation_object_one_kind と同じ線)。
        # ここで言わないと INSERT で 500 になり、画面には「投入中に例外」としか出ない
        if nz(obj.get("surface")) and (nz(obj.get("ref")) or nz(obj.get("region"))):
            errs.append(
                f"関係 '{r.get('relation')}': 対象に環境 ({obj.get('surface')}) と"
                f" 人体 ({obj.get('ref')}/{obj.get('region')}) の両方が入っている — "
                "どちらか一方にする (両方に飛んでいるなら 2 所見で書く)")

    for u in ann.get("unresolved", []):
        dom = u.get("domain", "")
        onto.check("domain", dom, onto.domains, errs)
        # **domain ごとに中身も検査する。** ここを domain だけの検査にしていたため、
        # `{domain:'body_region'}` に region が無い行が validate を通り、write() が
        # KeyError で落ちていた (2026-07-28 実測)。**validate が通ったら write は通る**
        # という不変条件が破れていたので塞ぐ — 部分適用の transaction を作らないため。
        if dom == "attribute":
            onto.check("attribute", u.get("attribute", ""), onto.attributes, errs)
            if u.get("attribute") and u.get("value") is not None and \
                    (u["attribute"], u["value"]) not in onto.attr_values:
                errs.append(f"unresolved: attribute '{u['attribute']}' に値 "
                            f"'{u['value']}' は無い")
        elif dom == "body_region":
            onto.check("body_region", u.get("region", ""), onto.regions, errs)
        elif dom == "pose":
            onto.check("pose", u.get("pose", ""), onto.poses, errs)
        elif dom:
            errs.append(f"unresolved の domain '{dom}' は書き込み経路が無い "
                        f"(attribute / body_region / pose のみ)")
        if not (u.get("note") or "").strip():
            errs.append("unresolved には note (なぜ特定できないか) が要る — "
                        "理由の無い『特定不能』は後から検算できない")
    if ann.get("minutes") is None:
        # **「測っていない」は理由つきで記録できる。** 適当な値を入れると後で計測値として
        # 読まれてしまう (minutes は DB に入らず下書きにだけ残るので検算もできない)。
        # 測れなかったことを事実として残す方が、非捏造の原則に沿う (2026-07-28)。
        why = (ann.get("minutes_unmeasured") or "").strip()
        if not why:
            errs.append("minutes が無い (1 scene あたりの注釈コストは acquisition 見積りの根拠)。"
                        "測れなかった場合は minutes_unmeasured に**理由**を書く — "
                        "適当な値を入れない")
    return errs


def existing_keys(cur, scene_id: int, producer_kind: str = 'human',
                  producer_ref: str | None = None) -> tuple[set, dict]:
    """この scene に既に入っている観測の「内容キー」集合と、ref ごとの bbox を返す。

    **同じ producer の観測とだけ重複判定する** (plan kura1-model-observation P1)。
    人間と同内容のモデル所見を skip すると P0 で「一致」が消える (2026-07-31 発見)。
    モデルは producer_ref 単位で見る (fine-tune の rev が違えば別)。

    観測は append-only で消せないので、**同じ内容を二度書かない**ことでしか
    「少しずつ確定する」を実現できない。キーは (種別, 主体 ref, 中身) で、
    同一内容の再投入だけを弾く (値が違えば別の観測として正しく積まれる)。
    """
    keys: set = set()
    bboxes: dict[str, tuple] = {}
    cur.execute("""
        SELECT o.id, o.subject_scope, o.subject_ref, o.domain, o.subject_asset_region_id,
               br.region_slug, oa.attribute_slug, oa.value_slug, oa.value_numeric,
               op.pose_slug, oi.species_slug, oi.character_slug,
               oi.motif_species_slug, oi.entity_kind,
               oi.presentation AS ipres,
               oa.region_slug, oa.laterality, op.laterality,
               oo.object_slug, oo.state_slug, oo.region_slug AS o_region,
               oo.variant_slug, oo.item_count, oo.laterality AS o_lat,
               osd.descriptor_slug,
               oec.presentation, oec.count_min, oec.count_exact, oec.involvement,
               orl.relation_slug, orl.subject_ref, orl.subject_region_slug,
               orl.subject_laterality, orl.object_laterality,
               orl.object_ref, orl.object_region_slug, orl.object_surface_slug
        FROM observation o
        LEFT JOIN obs_body_region      br  ON br.observation_id  = o.id
        LEFT JOIN obs_attribute        oa  ON oa.observation_id  = o.id
        LEFT JOIN obs_pose             op  ON op.observation_id  = o.id
        LEFT JOIN obs_identity         oi  ON oi.observation_id  = o.id
        LEFT JOIN obs_object           oo  ON oo.observation_id  = o.id
        LEFT JOIN obs_scene_descriptor osd ON osd.observation_id = o.id
        LEFT JOIN obs_entity_count     oec ON oec.observation_id = o.id
        LEFT JOIN obs_relation         orl ON orl.observation_id = o.id
        WHERE o.scene_id = %s
          -- **撤回されたものは「既にある」に数えない。** 数えていた版では、
          -- 一度撤回した事実を入れ直しても**黙って skip され、入らなかった**
          -- (2026-07-31 発見。worklist の件数で直したのと同じ形の欠陥)。
          -- 撤回は「その所見を取り消す」意思表示なので、入れ直しは新しい所見。
          AND NOT EXISTS (SELECT 1 FROM observation r WHERE r.retracts_id = o.id)
          -- 撤回そのものも内容キーを持たない (retracts_id が本体)
          AND o.retracts_id IS NULL
          -- **同じ producer とだけ重複判定** (P1)。human は human と、
          -- model は同じ producer_ref と。跨ぐと一致が消える / 別 rev が消える。
          AND o.producer_kind = %s
          AND (%s::text IS NULL OR o.producer_ref = %s::text)
    """, (scene_id, producer_kind, producer_ref, producer_ref))
    rows = cur.fetchall()
    for (_id, scope, ref, dom, arid, region, aslug, aval, anum, pose, sp, ch,
         motif, ek, ipres,
         areg, alat, plat,
         obj, ost, oreg, ovar, ocnt, olat, desc, pres, cmin, cex, einv,
         rel, rsub, rsubr, rsl, rol, robj, robjr, rsurf) in rows:
        if dom == "body_region" and region:
            keys.add(("body_region", scope, ref, region))
        elif dom == "attribute" and aslug:
            keys.add(("attribute", scope, ref, aslug, aval,
                      float(anum) if anum is not None else None, areg, alat))
        elif dom == "pose" and pose:
            keys.add(("pose", scope, ref, pose, plat))
        elif dom == "identity" and (sp or ch or motif or ek or ipres):
            # 0040: motif もキーに入れる。**入れないと motif だけ違う観測が
            # 「同一内容」に見えて弾かれる** (再投入で消える)。
            keys.add(("identity", scope, ref, sp, ch, motif, ek, ipres))
        elif dom == "object" and obj:
            keys.add(("object", scope, ref, obj, ost, oreg, ovar, ocnt, olat))
        elif dom == "scene_descriptor" and desc:
            keys.add(("scene_descriptor", desc))
        elif dom == "entity_count" and pres:
            # 0079: involvement もキーに入れる — **参加者 3 人と傍観者 3 人は別の事実**
            keys.add(("entity_count", pres, cmin, cex, einv))
        elif dom == "relation" and rel:
            # 0025: 左右もキーに入れる — 右手と左手が「同じ関係」とみなされると
            # 片方が二重投入の判定で飛ばされる
            # 0052: 環境もキーに入れる (地面へと空中へは別の事実)
            keys.add(("relation", rel, rsub, rsubr, rsl, robj, robjr, rol, rsurf))
        if ref and arid and ref not in bboxes:
            # 0021: 位置は box 4 値 + 点 3 値の 7 つ組。write() 側のキーと形を合わせる
            # (合っていないと「同じ位置」を毎回作り直して asset_region が増える)。
            cur.execute("SELECT x_min,y_min,x_max,y_max,anchor_x,anchor_y,anchor_region_slug"
                        " FROM asset_region WHERE id=%s", (arid,))
            got = cur.fetchone()
            if got:
                bboxes[ref] = (arid, tuple(
                    None if v is None else (v if isinstance(v, str) else round(float(v), 4))
                    for v in got))
    return keys, bboxes


def nz(v):
    """**空文字を NULL にする。** 画面から来る「選んでいない」は '' で届く。

    そのまま渡していたので
      * `object_surface_slug=''` が「環境を指定した」と見なされ
        ck_obs_relation_object_one_kind に当たる
      * `subject_region_slug=''` が body_region への FK に当たる
    という 500 が出ていた (2026-07-31、下書き 6 件が確定できなかった実害)。
    **NULL は「言っていない」**という既定を守るには、'' を入口で落とすしかない。
    """
    if v is None:
        return None
    s = str(v).strip()
    return s or None


def write(cur, ann: dict, skip_existing: bool = True) -> dict:
    """観測を書いて件数を返す。呼び手が commit する。

    **producer は ann から取る** (plan kura1-model-observation P1)。既定は human。
    モデルの素案は ann に producer_kind='model' / producer_ref='<model>@<rev>' を入れて
    渡す — **人間の下書きに流し込まず、model 名義でそのまま commit する** (出所の洗浄禁止)。
    """
    pid = ann["kura_post_id"]
    who = ann["annotator"]
    pkind = ann.get("producer_kind", "human")
    pref = ann.get("producer_ref") or who
    pevent = "model_annotation" if pkind == "model" else "human_annotation"
    cur.execute(
        "SELECT a.id, a.scene_id FROM asset a JOIN asset_source s ON s.asset_id=a.id"
        " WHERE s.source_slug='kura_post' AND s.external_id=%s", (str(pid),))
    row = cur.fetchone()
    if not row:
        raise SystemExit(f"post {pid} は kura1 に import されていません"
                         f" (import_probe_skeleton.py を先に実行)")
    asset_id, scene_id = row
    n = {"observation": 0, "asset_region": 0, "relation": 0, "unresolved": 0, "skipped": 0}

    # 既に入っている内容は書かない。append-only で消せない以上、これが
    # 「少しずつ確定する」を成立させる唯一の方法 (同一内容の再投入だけを弾き、
    #  値が違えば別の観測として正しく積まれる)。
    seen, old_bbox = (existing_keys(cur, scene_id, pkind,
                                    pref if pkind == 'model' else None)
                      if skip_existing else (set(), {}))

    def dup(key) -> bool:
        if key in seen:
            n["skipped"] += 1
            return True
        seen.add(key)
        return False

    def obs(scope: str, domain: str, ref: str | None = None, region_id: int | None = None) -> int:
        cur.execute(
            "INSERT INTO observation(scene_id, subject_scope, subject_entity_id, subject_ref,"
            " subject_asset_region_id, domain, event_type, producer_kind, producer_ref)"
            " VALUES (%s,%s,NULL,%s,%s,%s,%s,%s,%s) RETURNING id",
            (scene_id, scope, ref, region_id, domain, pevent, pkind, pref))
        n["observation"] += 1
        return cur.fetchone()[0]

    for s in ann.get("subjects", []):
        ref = s["ref"]
        region_id = None
        # 0021: 位置は box でも点でもよい。絡み合った人物では box がほぼ同一になり
        # 主体を同定できないので、頭の中心などの点を受けられるようにしてある。
        bb = tuple(round(float(v), 4) for v in s["bbox"]) if s.get("bbox") else (None,) * 4
        anc = s.get("anchor") or {}
        ac = ((round(float(anc["x"]), 4), round(float(anc["y"]), 4), anchor_region(anc))
              if anc.get("x") is not None and anc.get("y") is not None else (None, None, None))
        if bb[0] is not None or ac[0] is not None:
            key = bb + ac
            prev = old_bbox.get(ref)
            if prev and prev[1] == key:
                region_id = prev[0]          # 同じ位置は使い回す (毎回増やさない)
            else:
                cur.execute("INSERT INTO asset_region(asset_id, x_min, y_min, x_max, y_max,"
                            " anchor_x, anchor_y, anchor_region_slug)"
                            " VALUES (%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id",
                            (asset_id, *bb, *ac))
                region_id = cur.fetchone()[0]
                old_bbox[ref] = (region_id, key)
                n["asset_region"] += 1
        elif ref in old_bbox:
            region_id = old_bbox[ref][0]     # 既に付けた bbox を維持
        for r in s.get("regions", []):
            if dup(("body_region", "entity", ref, r)):
                continue
            oid = obs("entity", "body_region", ref, region_id)
            cur.execute("INSERT INTO obs_body_region(observation_id, region_slug, present)"
                        " VALUES (%s,%s,true)", (oid, r))
        for a in s.get("attributes", []):
            num = a.get("numeric")
            areg, alat = a.get("region"), a.get("laterality")
            if dup(("attribute", "entity", ref, a["attribute"], a.get("value"),
                    float(num) if num is not None else None, areg, alat)):
                continue
            oid = obs("entity", "attribute", ref, region_id)
            # 0070: 書いてある文字。**surface_marking のときだけ** (DB の CHECK と同じ線)
            atxt = (a.get("text") or None) if a["attribute"] == "surface_marking" else None
            cur.execute("INSERT INTO obs_attribute(observation_id, attribute_slug, value_slug,"
                        " value_numeric, region_slug, laterality, text_content)"
                        " VALUES (%s,%s,%s,%s,%s,%s,%s)",
                        (oid, a["attribute"], a.get("value"), num, areg, alat, atxt))
        for p in s.get("poses", []):
            pv = p.get("pose") if isinstance(p, dict) else p
            plat = p.get("laterality") if isinstance(p, dict) else None
            if dup(("pose", "entity", ref, pv, plat)):
                continue
            oid = obs("entity", "pose", ref, region_id)
            cur.execute("INSERT INTO obs_pose(observation_id, pose_slug, laterality)"
                        " VALUES (%s,%s,%s)", (oid, pv, plat))
        ident = s.get("identity") or {}
        kind = s.get("kind") if s.get("kind") not in (None, "person") else None
        # 0031: species が無くても **kind が person でなければ書く** — でないと
        # 「触手の主 (species 不明)」が観測として一切残らない
        pres = s.get("presentation") or None
        # 0037: presentation は**検証していたのに書いていなかった** (乖離の 9 件目)。
        # species も character も無くても、presentation だけで identity 観測を作る。
        if (ident.get("species") or ident.get("character")
                or ident.get("motif_species") or kind or pres):
            if not dup(("identity", "entity", ref, ident.get("species"),
                        ident.get("character"), ident.get("motif_species"),
                        kind, pres)):
                oid = obs("entity", "identity", ref, region_id)
                cur.execute("INSERT INTO obs_identity(observation_id, species_slug,"
                            " character_slug, motif_species_slug,"
                            " entity_kind, presentation)"
                            " VALUES (%s,%s,%s,%s,%s,%s)",
                            (oid, ident.get("species"), ident.get("character"),
                             ident.get("motif_species"),
                             # 0031: 人以外のときだけ書く。person は既定なので、
                             # 「人だと言った」と「言っていない」を混ぜない
                             kind, pres))
        for o in s.get("objects", []):
            # 0045: 部位もキーに入れる。**耳のピアスと乳首のピアスは別の事実**
            if dup(("object", "entity", ref, o["object"], o.get("state"),
                    o.get("region"), o.get("variant"), o.get("count"),
                    o.get("laterality"))):
                continue
            oid = obs("entity", "object", ref, region_id)
            cur.execute("INSERT INTO obs_object(observation_id, object_slug, state_slug,"
                        " region_slug, variant_slug, item_count, laterality)"
                        " VALUES (%s,%s,%s,%s,%s,%s,%s)",
                        (oid, o["object"], o.get("state"), o.get("region"),
                         o.get("variant"), o.get("count"), o.get("laterality")))

    for rel in ann.get("relations", []):
        sub = rel.get("subject") or {}
        obj = rel.get("object") or {}
        # 観測の主体は relation の subject。ref が無ければ scene-scoped として記録する。
        if dup(("relation", rel["relation"], sub.get("ref"), sub.get("region"),
                sub.get("laterality"), obj.get("ref"), obj.get("region"),
                obj.get("laterality"), obj.get("surface"))):
            continue
        scope, ref = ("entity", sub["ref"]) if sub.get("ref") else ("scene", None)
        oid = obs(scope, "relation", ref)
        cur.execute(
            "INSERT INTO obs_relation(observation_id, relation_slug, subject_ref,"
            " subject_region_slug, subject_laterality, object_ref, object_region_slug,"
            " object_laterality, object_surface_slug) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (oid, rel["relation"], nz(sub.get("ref")), nz(sub.get("region")),
             nz(sub.get("laterality")), nz(obj.get("ref")), nz(obj.get("region")),
             nz(obj.get("laterality")), nz(obj.get("surface"))))
        n["relation"] += 1

    for d in ann.get("scene_descriptors", []):
        if dup(("scene_descriptor", d)):
            continue
        oid = obs("scene", "scene_descriptor")
        cur.execute("INSERT INTO obs_scene_descriptor(observation_id, descriptor_slug)"
                    " VALUES (%s,%s)", (oid, d))
    for ec in ann.get("entity_counts", []):
        if dup(("entity_count", ec["presentation"], ec["count_min"], ec.get("count_exact", True))):
            continue
        oid = obs("scene", "entity_count")
        cur.execute("INSERT INTO obs_entity_count(observation_id, presentation, count_min,"
                    " count_exact, involvement) VALUES (%s,%s,%s,%s,%s)",
                    (oid, ec["presentation"], ec["count_min"],
                     ec.get("count_exact", True), nz(ec.get("involvement"))))

    # 主体が特定できない person-scoped な事実。候補集合の列挙は resolver の仕事。
    for u in ann.get("unresolved", []):
        oid = obs("unresolved", u["domain"])
        dom = u["domain"]
        if dom == "attribute":
            cur.execute("INSERT INTO obs_attribute(observation_id, attribute_slug, value_slug,"
                        " value_numeric) VALUES (%s,%s,%s,%s)",
                        (oid, u["attribute"], u.get("value"), u.get("numeric")))
        elif dom == "body_region":
            cur.execute("INSERT INTO obs_body_region(observation_id, region_slug, present)"
                        " VALUES (%s,%s,true)", (oid, u["region"]))
        elif dom == "pose":
            cur.execute("INSERT INTO obs_pose(observation_id, pose_slug) VALUES (%s,%s)",
                        (oid, u["pose"]))
        else:
            raise SystemExit(f"unresolved の domain '{dom}' は未対応 (attribute / body_region /"
                             f" pose のみ)。必要なら writer を拡張する")
        n["unresolved"] += 1
    return n


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--file", type=pathlib.Path)
    ap.add_argument("--dir", type=pathlib.Path)
    ap.add_argument("--dsn", default=DSN_DEFAULT)
    ap.add_argument("--validate-only", action="store_true")
    args = ap.parse_args()
    if not args.file and not args.dir:
        ap.error("--file か --dir が必要")

    paths = [args.file] if args.file else sorted(args.dir.glob("*.json"))
    if not paths:
        raise SystemExit("対象 JSON がありません")

    import psycopg
    # **キーを列挙しない。** write() が返すキーをここでも書くと 2 か所に同じ表が住み、
    # 実際 write() が 'skipped' を足したときここが追従せず KeyError で落ちた
    # (2026-07-29、206141 の投入時)。Counter なら追従が要らない。
    total: collections.Counter[str] = collections.Counter()
    with psycopg.connect(args.dsn, autocommit=False) as conn, conn.cursor() as cur:
        cur.execute("SET search_path = kura, public")
        onto = Ontology(cur)
        anns = []
        bad = 0
        for p in paths:
            ann = json.loads(p.read_text(encoding="utf-8"))
            errs = validate(ann, onto)
            if errs:
                bad += 1
                print(f"✗ {p.name}")
                for e in errs:
                    print(f"    - {e}")
            else:
                print(f"✓ {p.name} (post {ann['kura_post_id']}, {ann.get('minutes')} 分)")
                anns.append(ann)
        if bad:
            print(f"\n{bad} ファイルに問題があります。直してから再実行してください"
                  f" (1 件でも不正なら何も書きません — 部分適用で追跡不能になるため)。")
            return 1
        if args.validate_only:
            print("\n--validate-only なので何も書いていません。")
            return 0
        for ann in anns:
            got = write(cur, ann)
            for k, v in got.items():
                total[k] += v
        conn.commit()
    print("\n投入: " + " / ".join(f"{k} {v}" for k, v in sorted(total.items())))
    return 0


if __name__ == "__main__":
    sys.exit(main())
