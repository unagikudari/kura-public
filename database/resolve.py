#!/usr/bin/env python3
"""Kura 1.0 Resolver — 最小実装 (plan kura-1.0-implementation S-9)。

観測 (append-only) を 1 つの resolved scene に畳む。仕様は
docs/standard/resolver-specification.md v0.2.0、entity の扱いは
database/ENTITY-RESOLUTION.md。**方針は data から読む** (seed/resolver/*.tsv) —
policy を code に焼くと「data-driven」を名乗れなくなる (spec §10)。

── この実装が守るもの (仕様の番号) ──
  §1 決定性: 同じ入力 → 同じ出力。2 回走らせて resolved_facet が一致することを
     `--check-determinism` で実際に確かめる。
  §2 総順序: observed_at 昇順 → observation.id 昇順。
  §3 競合: retraction > producer precedence (human > model) > recency。
     同 tier で値が矛盾したら **contested** — 勝手に選ばない。
  §4 entity: **Resolver の出力**。run ごとに作り直し、複合 FK で run をまたがせない。
  §6 contested: 対立した観測を resolved_facet_evidence に残す (根拠を捨てない)。
  §8 provenance: 採用した観測を resolved_observation に記録。
  §9 projection: tag_projection は resolved scene からのみ再構築可能。

── 帰属 (attachment) の判断は provenance で変わる ──
ENTITY-RESOLUTION.md §4 の曖昧規則は **flat tag 由来**の観測のためのもの。
人手注釈は `observation.subject_ref` で主語を名指しているので、multi-person scene でも
曖昧ではない — 「誰のものか判らない」場合、注釈者は subject_scope='unresolved' に
落としている (捏造しない仕組みが入力側で既に効いている)。
よって:
  * subject_scope='entity' かつ subject_ref あり → その ref の entity へ束縛 (曖昧でない)
  * subject_scope='unresolved'                  → 束縛せず候補集合を書く
この区別を無視して「multi-person だから全部 unresolved」にすると、人手注釈が持って
いる情報を捨てることになる。逆に unresolved を勝手に束縛すれば捏造になる。
"""
from __future__ import annotations

import argparse
import collections
import pathlib
import sys

import psycopg

DSN_DEFAULT = "host=127.0.0.1 port=5541 dbname=kura1 user=desk"
SEED = pathlib.Path(__file__).resolve().parent / "seed" / "resolver"
RESOLVER_VERSION = "0.1.0-minimal"

# 属性のうち「同時に複数の値を取りうる」もの (0016 の attribute.is_multivalued) は
# 矛盾ではなく併存なので contested にしない。DB から読む — ここに slug を書かない。


def read_tsv(name: str) -> list[dict]:
    rows = []
    for i, line in enumerate((SEED / name).read_text().splitlines()):
        if not line.strip():
            continue
        parts = line.split("\t")
        if i == 0:
            head = parts
            continue
        rows.append(dict(zip(head, parts)))
    return rows


def policy() -> dict:
    prec = {r["producer_kind"]: int(r["rank"]) for r in read_tsv("producer_precedence.tsv")}
    dom = {r["domain"]: r["policy"] for r in read_tsv("per_domain_policy.tsv")}
    return {"precedence": prec, "domain": dom}


# ── 1 観測を「解決キー」と「値」に開く ────────────────────────────
# 解決キー = その値が付く (主体, 述語)。同じキーに違う値が来たら競合になる。
def facet_of(row: dict) -> tuple[tuple | None, dict]:
    d = row["domain"]
    if d == "body_region":
        return ("body_region", row["region_slug"]), {"region_slug": row["region_slug"]}
    if d == "attribute":
        # 部位つき属性は **部位ごとに別のキー** (乳輪の色と顔の色は競合しない)。
        # 左右も別 — 0010 で laterality を足したのはこのため。
        return (("attribute", row["attribute_slug"], row["a_region"], row["a_lat"]),
                {"attribute_slug": row["attribute_slug"], "value_slug": row["value_slug"],
                 "value_numeric": row["value_numeric"],
                 "region_slug": row["a_region"],
                 # 0010 で観測には左右があったが解決形へ運んでいなかった (0025 で列追加)
                 "region_laterality": row["a_lat"]})
    if d == "pose":
        return ("pose", row["pose_slug"], row["p_lat"]), {"pose_slug": row["pose_slug"]}
    if d == "identity":
        # **identity は 1 つの事実ではない。** kind / presentation / species / character は
        # 独立に観測できる (「人だ」と「女性表現だ」と「人間種だ」は互いに矛盾しない)。
        # 1 つの解決キーにまとめていたため、presentation だけの観測と species だけの
        # 観測が**互いを contested にしていた** (2026-07-29 実測)。
        # kind と presentation は **entity 行の列**へ行くので facet にしない —
        # facet にも出すと同じ事実が 2 か所になる。ここは species/character だけ。
        # 0040: motif ("何に似せてあるか") も facet に運ぶ。**観測に書けるだけで
        # 解決形に出ないと query が引けない** — presentation を検証だけして列が無かった
        # 0037 の失敗を繰り返さないため。
        if not (row["species_slug"] or row["character_slug"]
                or row["motif_species_slug"]):
            return None, {}
        return ("identity",), {"species_slug": row["species_slug"],
                               "character_slug": row["character_slug"],
                               "motif_species_slug": row["motif_species_slug"]}
    if d == "relation":
        # **参加者を値に含める。** 0024 まで resolved_facet に相手の列が無く、
        # 「person_2.penis → person_1.vaginal_opening」が「entity 77 が
        # inserting_into している」に潰れていた (2026-07-29 実測)。
        # region_slug は domain='relation' のとき **subject の部位**を意味する。
        # 0025: 左右もキーに入れる。**入れないと「右手で A」「左手で A」が同じキーに
        # なり、片方が「同じ値の重複」として畳まれて消える。**
        # 0052: 環境もキーに入れる — 「地面へ」と「空中へ」は別の事実で、
        # 含めないと片方が他方を上書きする。
        return (("relation", row["relation_slug"], row["r_sub"], row["r_subr"], row["r_sl"],
                 row["r_obj"], row["r_objr"], row["r_ol"], row["r_surf"]),
                {"relation_slug": row["relation_slug"],
                 "region_slug": row["r_subr"],
                 "region_laterality": row["r_sl"],
                 "_object_ref": row["r_obj"],
                 "object_region_slug": row["r_objr"],
                 "object_surface_slug": row["r_surf"],
                 "object_region_laterality": row["r_ol"]})
    if d == "object":
        # **0045 で足した分岐。** それまで object の所見は key が None を返して
        # **確定所見にならなかった** — `resolved_facet.object_slug` 列はあるのに
        # 何も書かれない状態 (宣言と着地の乖離、0037 と同型)。
        # キーに部位を含める — **「耳のピアス」と「乳首のピアス」は別の事実**で、
        # 含めないと片方が他方を上書きする。
        # **形も部位もキーに入れる** — 「耳のリング」と「耳のバー」は別の事実。
        # 数はキーに入れない (同じ物の個数が違うのは矛盾であって別の事実ではない)。
        # 0047: **左右もキーに入れる** — 「右耳のリング」と「左耳のリング」は別の事実。
        # 入れないと片方が他方を上書きし、「両耳に 1 個ずつ」が消える。
        return ("object", row["object_slug"], row["o_region"],
                row["o_variant"], row["o_lat"]), {
            "object_slug": row["object_slug"],
            "region_slug": row["o_region"],
            "region_laterality": row["o_lat"],
            "object_variant_slug": row["o_variant"],
            "object_count": row["o_count"],
            "value_slug": row["state_slug"]}
    if d == "scene_descriptor":
        return ("scene_descriptor", row["facet"]), {"descriptor_slug": row["descriptor_slug"]}
    if d == "entity_count":
        # 0079: **参加者と傍観者は別の事実**なので解決キーにも入れる。
        # 同じ presentation でも involvement が違えば競合ではなく別々に立つ
        return (("entity_count", row["presentation"], row.get("involvement")),
                {"presentation": row["presentation"], "involvement": row.get("involvement")})
    return None, {}


VALUE_COLS = ("region_slug", "attribute_slug", "pose_slug", "relation_slug",
              "descriptor_slug", "object_slug", "species_slug", "character_slug",
              "motif_species_slug",
              "presentation", "value_slug", "value_numeric",
              "object_variant_slug", "object_count", "object_surface_slug",
              "object_entity_id", "object_region_slug",
              "region_laterality", "object_region_laterality", "involvement",
              # 0070: 書いてある文字。**宣言だけして着地させないのを繰り返さない** —
              # ここに無いと query が引けず、0044 で object が 1 件も解決されなかったのと
              # 同じことになる
              "text_content")


def load(cur, scene_id: int) -> list[dict]:
    """§2 の総順序で観測を読む。domain ごとの中身は LEFT JOIN で 1 行に畳む。"""
    cur.execute("""
        SELECT o.id, o.subject_scope, o.subject_ref, o.domain, o.event_type,
               o.producer_kind, o.observed_at, o.retracts_id,
               obr.region_slug, obr.present,
               oa.attribute_slug, oa.value_slug, oa.value_numeric, oa.text_content,
               oa.region_slug AS a_region, oa.laterality AS a_lat,
               op.pose_slug, op.laterality AS p_lat,
               oi.species_slug, oi.character_slug, oi.motif_species_slug,
               oi.entity_kind,
               -- **別名が要る**: obs_body_region / obs_attribute にも region_slug があり、
               -- dict(zip(cols,row)) だと後の列が前を黙って上書きする (0037 で踏んだ)
               oo.object_slug, oo.state_slug, oo.region_slug AS o_region,
               oo.variant_slug AS o_variant, oo.item_count AS o_count,
               oo.laterality AS o_lat,
               -- **別名が要る**: obs_entity_count にも presentation があり、
               -- 同名だと dict(zip(cols,row)) で後勝ちに潰れる (2026-07-29 実測)
               oi.presentation AS i_presentation,
               orl.relation_slug, orl.subject_ref AS r_sub, orl.subject_region_slug AS r_subr,
               orl.subject_laterality AS r_sl, orl.object_laterality AS r_ol,
               orl.object_ref AS r_obj, orl.object_region_slug AS r_objr,
               orl.object_surface_slug AS r_surf,
               osd.descriptor_slug, sd.facet,
               oec.presentation,
               oec.involvement
          FROM observation o
          LEFT JOIN obs_body_region      obr ON obr.observation_id = o.id
          LEFT JOIN obs_attribute        oa  ON oa.observation_id  = o.id
          LEFT JOIN obs_pose             op  ON op.observation_id  = o.id
          LEFT JOIN obs_identity         oi  ON oi.observation_id  = o.id
          LEFT JOIN obs_relation         orl ON orl.observation_id = o.id
          LEFT JOIN obs_object           oo  ON oo.observation_id  = o.id

          LEFT JOIN obs_scene_descriptor osd ON osd.observation_id = o.id
          LEFT JOIN scene_descriptor     sd  ON sd.slug = osd.descriptor_slug
          LEFT JOIN obs_entity_count     oec ON oec.observation_id = o.id
         WHERE o.scene_id = %s
         ORDER BY o.observed_at, o.id
    """, (scene_id,))
    cols = [c.name for c in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def resolve_scene(cur, scene_id: int, run_id: int, pol: dict) -> dict:
    obs = load(cur, scene_id)
    if not obs:
        return {"facets": 0, "entities": 0, "unresolved": 0}

    cur.execute("SELECT slug FROM attribute WHERE is_multivalued")
    multi = {r[0] for r in cur.fetchall()}

    # ── §3.1 撤回。**キー単位ではなく scene 単位で先に集める。**
    # 撤回観測は中身 (obs_relation 等) を持たないので `facet_of` が別の解決キーを返し、
    # キー単位で突き合わせても対象に届かない (2026-07-29 実測: 撤回したのに resolved に
    # 残っていた)。append-only の DB で誤りを無効にする唯一の経路なので、ここが効かないと
    # **訂正手段が存在しない**ことになる。
    retracted = {o["retracts_id"] for o in obs
                 if o["event_type"] == "retraction" and o["retracts_id"]}
    obs = [o for o in obs if o["event_type"] != "retraction" and o["id"] not in retracted]

    # ── §4 entity: 人手注釈が名指した主語ごとに 1 entity ──────────
    # entity_count 観測があればそちらも見るが、subject_ref の方が具体的なので
    # ref の集合を優先する (ref は「この 3 人」と言っている。count は「3 人いる」だけ)。
    refs = sorted({o["subject_ref"] for o in obs
                   if o["subject_scope"] == "entity" and o["subject_ref"]})
    # 0030: 主体は person とは限らない。ref の接頭辞では決めず、**注釈が言った kind** を
    # 使う。注釈側は subject ごとに kind を持つが観測行には無いので、identity 観測に
    # 現れない ref は person 既定 (従来と同じ) にし、下書き由来の kind は
    # obs_identity の有無では決めない — ここは resolver が推測しない箇所。
    # 0031: kind は identity 観測が言う。**NULL は「言っていない」なので person 既定** —
    # ただしそれは resolver の既定であって観測の主張ではない (列コメント参照)。
    kinds = {o["subject_ref"]: o["entity_kind"] for o in obs
             if o["domain"] == "identity" and o["subject_ref"] and o["entity_kind"]}
    # 0037: presentation を entity に載せる。**人数はここから数える** —
    # entity_count を別に持つと同じ事実が 2 か所になる。
    pres_of = {o["subject_ref"]: o["i_presentation"] for o in obs
               if o["domain"] == "identity" and o["subject_ref"] and o["i_presentation"]}
    pres = {}
    for o in obs:
        if o["domain"] == "identity" and o["subject_ref"]:
            pres.setdefault(o["subject_ref"], None)
    ent = {}
    for ref in refs:
        cur.execute("INSERT INTO entity(scene_id, kind, gender_presentation,"
                    " resolver_run_id) VALUES (%s,%s,%s,%s) RETURNING id",
                    (scene_id, kinds.get(ref, "person"), pres_of.get(ref), run_id))
        ent[ref] = cur.fetchone()[0]

    cur.execute("INSERT INTO resolved_scene(scene_id, resolver_run_id, status)"
                " VALUES (%s,%s,'current') RETURNING id", (scene_id, run_id))
    rs_id = cur.fetchone()[0]

    # ── §3 競合解決: 解決キーごとに候補を集める ───────────────────
    buckets: dict = collections.defaultdict(list)
    accepted: set = set()
    n_unres = 0
    for o in obs:
        key, val = facet_of(o)
        if key is None:
            continue
        if o["subject_scope"] == "unresolved":
            # 束縛しない。候補集合を書く (§4.3) — 「判らない」を記録として残す。
            # 候補は同 scene の person entity 全部で、排他かどうかは観測からは
            # 決まらないので inclusive (弱い方) にする。**推測しない**。
            for e in ent.values():
                cur.execute(
                    "INSERT INTO unresolved_subject_candidate"
                    "(resolver_run_id, observation_id, entity_id, rank, exclusivity)"
                    " VALUES (%s,%s,%s,%s,'inclusive')", (run_id, o["id"], e, 1))
            n_unres += 1
            continue
        eid = ent.get(o["subject_ref"]) if o["subject_scope"] == "entity" else None
        if o["subject_scope"] == "entity":
            if eid is None:
                continue
            cur.execute("INSERT INTO resolved_entity_binding"
                        "(resolver_run_id, observation_id, entity_id) VALUES (%s,%s,%s)",
                        (run_id, o["id"], eid))
        buckets[(o["subject_scope"], eid, key)].append((o, val))

    n_facet = 0
    for (scope, eid, key), cands in buckets.items():
        dom = key[0]
        live = [(o, v) for o, v in cands if o["id"] not in retracted]
        if not live:
            continue
        # precedence → recency。live は §2 の総順序で入っているので最後が最新。
        top = max(pol["precedence"].get(o["producer_kind"], 0) for o, _ in live)
        tier = [(o, v) for o, v in live if pol["precedence"].get(o["producer_kind"], 0) == top]

        # pose / relation / 多値属性は **集合** — 別の値は矛盾ではない (§5)
        is_set = dom in ("pose", "relation") or \
            (dom == "attribute" and key[1] in multi)
        if is_set:
            # **値ごとに 1 facet。** 多値属性 (surface_substance) は「汗と涙が同時に
            # 付いている」が正しいので、最後の 1 つだけ書くと片方が消える
            # (2026-07-28 実測: sweat が消えて tears だけ残った)。
            byval: dict = collections.OrderedDict()
            for o, v in tier:
                byval.setdefault(tuple(sorted((k, str(x)) for k, x in v.items())),
                                 []).append((o, v))
            groups = list(byval.values())
            status = "resolved"
        else:
            # 同 tier に**異なる値**が並んだら contested。同じ値の重複は競合ではない。
            distinct = {tuple(sorted((k, str(x)) for k, x in v.items())) for _, v in tier}
            if len(distinct) > 1:
                groups, status = [tier], "contested"
            else:
                groups, status = [tier[-1:]], "resolved"

        for g in groups:
            o0, v0 = g[-1]
            cols = {k: v0.get(k) for k in VALUE_COLS}
            if status == "contested":
                # **contested に値を書かない。** どれか 1 つを入れたら「選んだ」ことに
                # なり、contested を名乗る意味が消える。schema の
                # ck_resolved_facet_contested が実際にこれを弾いた (2026-07-28)。
                # 残すのは「どのキーが争われているか」だけで、対立した値は
                # resolved_facet_evidence 経由で観測から読む。
                cols["value_slug"] = cols["value_numeric"] = None
            # 関係の相手 ref を entity id に解決する (相手が unresolved なら NULL)
            if o0["domain"] == "relation":
                cols["object_entity_id"] = ent.get(v0.get("_object_ref"))
            names = ", ".join(VALUE_COLS)
            marks = ", ".join(["%s"] * len(VALUE_COLS))
            cur.execute(
                f"INSERT INTO resolved_facet(resolved_scene_id, domain, entity_id, status,"
                f" {names}) VALUES (%s,%s,%s,%s,{marks}) RETURNING id",
                (rs_id, o0["domain"], eid, status, *[cols[c] for c in VALUE_COLS]))
            fid = cur.fetchone()[0]
            n_facet += 1
            # §6/§8 根拠を残す。contested なら**対立した観測を全部**残す —
            # 根拠を捨てた「contested」は後から検算できない。
            for o, _ in (g if status == "contested" else [g[-1]]):
                cur.execute("INSERT INTO resolved_facet_evidence"
                            "(resolved_facet_id, observation_id, role) VALUES (%s,%s,%s)"
                            " ON CONFLICT DO NOTHING",
                            (fid, o["id"], "conflicting" if status == "contested" else "accepted"))
                accepted.add(o["id"])

    for oid in sorted(accepted):
        cur.execute("INSERT INTO resolved_observation(resolved_scene_id, observation_id)"
                    " VALUES (%s,%s) ON CONFLICT DO NOTHING", (rs_id, oid))

    # ── §9 projection: resolved scene からのみ作る ────────────────
    cur.execute("""
        INSERT INTO tag_projection(resolved_scene_id, tag)
        SELECT DISTINCT %s, coalesce(region_slug, pose_slug, relation_slug,
                                     descriptor_slug, species_slug, value_slug)
          FROM resolved_facet
         WHERE resolved_scene_id = %s AND status = 'resolved'
           AND coalesce(region_slug, pose_slug, relation_slug, descriptor_slug,
                        species_slug, value_slug) IS NOT NULL
        ON CONFLICT DO NOTHING
    """, (rs_id, rs_id))
    return {"facets": n_facet, "entities": len(ent), "unresolved": n_unres}


def run(cur, pol: dict, scenes: list[int]) -> int:
    cur.execute("INSERT INTO resolver_run(resolver_version) VALUES (%s) RETURNING id",
                (RESOLVER_VERSION,))
    run_id = cur.fetchone()[0]
    # 既存の current を superseded にする (§7。ux_resolved_scene_current が 1 本を強制)
    cur.execute("UPDATE resolved_scene SET status='superseded'"
                " WHERE status='current' AND scene_id = ANY(%s)", (scenes,))
    tot = collections.Counter()
    for sid in scenes:
        for k, v in resolve_scene(cur, sid, run_id, pol).items():
            tot[k] += v
    print(f"run {run_id} ({RESOLVER_VERSION}): scene {len(scenes)} / entity {tot['entities']}"
          f" / facet {tot['facets']} / unresolved 観測 {tot['unresolved']}")
    return run_id


def snapshot(cur, run_id: int) -> list[tuple]:
    """決定性の比較に使う指紋。**entity id は run ごとに変わるので使わない** —
    代わりに scene 内での entity の序数を使う (中身が同じなら一致する)。"""
    cur.execute("""
        SELECT rs.scene_id,
               dense_rank() OVER (PARTITION BY rs.scene_id ORDER BY rf.entity_id) AS ent_ord,
               rf.domain, rf.status, rf.region_slug, rf.attribute_slug, rf.pose_slug,
               rf.relation_slug, rf.descriptor_slug, rf.species_slug, rf.value_slug,
               dense_rank() OVER (PARTITION BY rs.scene_id ORDER BY rf.object_entity_id)
                 AS obj_ord,
               rf.object_region_slug, rf.region_laterality, rf.object_region_laterality
          FROM resolved_facet rf JOIN resolved_scene rs ON rs.id = rf.resolved_scene_id
         WHERE rs.resolver_run_id = %s
         ORDER BY 1,2,3,4,5,6,7,8,9,10,11,12,13,14,15
    """, (run_id,))
    return cur.fetchall()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dsn", default=DSN_DEFAULT)
    ap.add_argument("--scene", type=int, action="append")
    ap.add_argument("--check-determinism", action="store_true",
                    help="2 回走らせて結果が一致することを確かめ、**両方ロールバックする**")
    a = ap.parse_args()
    pol = policy()

    with psycopg.connect(a.dsn, autocommit=False) as conn, conn.cursor() as cur:
        cur.execute("SET search_path = kura, public")
        if a.scene:
            scenes = a.scene
        else:
            cur.execute("SELECT DISTINCT scene_id FROM observation ORDER BY 1")
            scenes = [r[0] for r in cur.fetchall()]
        if not scenes:
            print("観測のある scene がありません", file=sys.stderr)
            return 1

        if a.check_determinism:
            r1 = run(cur, pol, scenes)
            s1 = snapshot(cur, r1)
            r2 = run(cur, pol, scenes)
            s2 = snapshot(cur, r2)
            conn.rollback()      # **書き込まない** — 決定性の確認だけ
            same = s1 == s2
            print(f"\n決定性: run{r1} {len(s1)} 行 vs run{r2} {len(s2)} 行 → "
                  f"{'一致 (PASS)' if same else '不一致 (FAIL)'}")
            if not same:
                for x, y in zip(s1, s2):
                    if x != y:
                        print("  最初の差:", x, "|", y)
                        break
            print("(ロールバック済 — DB は変更していません)")
            return 0 if same else 1

        run(cur, pol, scenes)
        conn.commit()
    return 0


if __name__ == "__main__":
    sys.exit(main())
