#!/usr/bin/env python3
"""S-11 — 14 query を kura1 と production kura で並列実行し、結果を並べて出す。

plan kura-1.0-implementation S-11。**判定は user が query 単位で行う**ので、この
スクリプトは判定しない。両方の結果を並べるだけ。集計は S-4 の決定規則を機械適用する
(それは別 step)。

── 両 arm の定義 ──
  * kura1  — resolved_facet (status='current' の resolved_scene 配下) への SQL。
             **観測ではなく解決形を見る** — S-11 は「解決後に何が引けるか」の測定。
  * control — production kura の検索 `pool:7846 <tags>`。母集団を kura1 と同一に
             限定する (S-10)。**kura に不利な向きに保守的**である旨は S-12 に明記済。

── この装置が判定しない理由 ──
「解けている」の基準は目で見た結果への判断であり、実行者が先に決めると S-11 の
検査が機能しない (rev9 で user の勝敗予想を撤去したのと同じ理由)。ここでは
**両方の件数と中身**を出すところまで。
"""
from __future__ import annotations

import argparse
import json
import ssl
import sys
import urllib.parse
import urllib.request

import psycopg

DSN = "host=127.0.0.1 port=5541 dbname=kura1 user=desk"
KURA_API = "http://127.0.0.1:8001"
POOL = 7846        # 第 1 ラウンド。第 2 ラウンドは --pool 7897

# ── 14 query。逐語は fact 786c0ce0 (Set A) / 9c879274 (Set B) ──
# 各行: (id, 逐語の要旨, 層, S-3 分類, kura1 側の SQL, control 側のタグ式)
# **SQL と タグ式は S-3 の「必要観測」欄から機械的に導く** — 有利な条件を後から
# 足さないため、分類表に無い列は使わない。
Q = [
    ("A1", "特定キャラ検索に他キャラが誤ヒット", "層1", "供給源が無い",
     """SELECT count(DISTINCT rs.scene_id) FROM resolved_facet rf
          JOIN resolved_scene rs ON rs.id=rf.resolved_scene_id
         WHERE rs.status='current' AND rf.character_slug IS NOT NULL""",
     None),
    ("A2", "行為の対象領域 (anilingus/fellatio/cunnilingus)", "層2", "additive migration",
     """SELECT count(*) FROM resolved_facet rf
          JOIN resolved_scene rs ON rs.id=rf.resolved_scene_id
         WHERE rs.status='current' AND rf.domain='relation'
           AND rf.region_slug IN (SELECT slug FROM pg_temp.descendants('mouth'))
           AND rf.object_region_slug IS NOT NULL""",
     "fellatio"),
    # **2026-07-31 修正 (測定の欠陥)。** 直す前は
    #   (1) kura1 側が pigmentation を**値を問わず**数えていた
    #       (light 21 / medium 7 / dark 13 / very_dark 17 = 58) のに、
    #   (2) control は `dark_areolae` だけ = 0 件。
    # 「濃いか」を問う query なのに**濃くないものまで**数え、control は
    # 乳輪だけに絞っていた (pigmentation は 0065 で any_region、陰唇等も含む)。
    # 同種の比較にするため kura1 を dark/very_dark に、control を
    # 色素沈着系タグの**和集合**にする。**判定を有利にする変更ではない** —
    # kura1 側は 58 → 30 に減り、control 側は 0 → 9 に増える。
    ("A3", "色素沈着の程度", "層1", "供給源が無い",
     """SELECT rf.value_slug, count(*) FROM resolved_facet rf
          JOIN resolved_scene rs ON rs.id=rf.resolved_scene_id
         WHERE rs.status='current' AND rf.attribute_slug='pigmentation'
           AND rf.value_slug IN ('dark','very_dark')
         GROUP BY 1 ORDER BY 2 DESC""",
     "dark_areolae|dark_nipples|dark_labia"),
    # **2026-07-31 修正 (測定の欠陥)。** 直す前は entity を gender_presentation で
    # GROUP BY し `IS NOT NULL` で絞っていたので、**presentation 未記入の人物 46/57 が
    # 丸ごと落ちていた**。817324 は kura1 に person が 4 人いるのに 0 人として扱われ、
    # 「最大 2 人 / 3 人以上 0 scene」という結果になっていた
    # (= kura1 がタグの 6+boys を否定した、という判定の根拠が成立していなかった)。
    # 人数は **presentation を問わず person entity を数える**のが意図。
    # **判定を有利にする変更ではない** — 3 人以上の scene が 0 → 4 件に増え、
    # 「タグが誤り」という主張はむしろ弱くなる。
    ("A4", "6boys 等の人数の間違い", "層1", "解ける (ただし層1)",
     """SELECT max(e.c) AS max_persons, count(*) FILTER (WHERE e.c >= 3) AS n_scenes_ge3
          FROM (SELECT scene_id, resolver_run_id, count(*) AS c
                  FROM entity WHERE kind='person' GROUP BY 1,2) e
          JOIN resolved_scene rs ON rs.resolver_run_id=e.resolver_run_id
                                AND rs.scene_id=e.scene_id AND rs.status='current'""",
     "6+boys"),
    ("A5", "腕がたくさん 1 キャラにアプローチ", "層2", "additive migration",
     """SELECT rs.scene_id, count(DISTINCT rf.entity_id) AS n_arm_owners
          FROM resolved_facet rf JOIN resolved_scene rs ON rs.id=rf.resolved_scene_id
         WHERE rs.status='current' AND rf.domain='relation'
           AND rf.region_slug IN (SELECT slug FROM pg_temp.descendants('arm')
                                  UNION SELECT 'tentacle')
         GROUP BY 1 HAVING count(DISTINCT rf.entity_id) >= 2""",
     "disembodied_limb"),
    ("A6", "ペニスがたくさん 1 キャラにアプローチ", "層2", "additive migration",
     """SELECT rs.scene_id, count(DISTINCT rf.entity_id) AS n_penis_owners
          FROM resolved_facet rf JOIN resolved_scene rs ON rs.id=rf.resolved_scene_id
         WHERE rs.status='current' AND rf.domain='relation'
           AND rf.region_slug IN (SELECT slug FROM pg_temp.descendants('penis'))
         GROUP BY 1 HAVING count(DISTINCT rf.entity_id) >= 2""",
     "multiple_penises"),
    ("A7", "ヨガ・土下座のような特殊ポーズ", "層2", "seed 1 行",
     """SELECT count(*) FROM resolved_facet rf
          JOIN resolved_scene rs ON rs.id=rf.resolved_scene_id
         WHERE rs.status='current' AND rf.pose_slug IN ('dogeza','prostration')""",
     "dogeza"),
    ("B1", "行為ポジションが複数出すぎ (定義未確定)", "層2", "seed 1 行",
     """SELECT rs.scene_id, count(*) AS n_whole_body_pose
          FROM resolved_facet rf JOIN resolved_scene rs ON rs.id=rf.resolved_scene_id
          JOIN pose p ON p.slug=rf.pose_slug
         WHERE rs.status='current' AND p.scope='whole_body'
         GROUP BY 1 HAVING count(*) >= 2""",
     "all_fours doggystyle sex_from_behind"),
    # **注釈者の恒久的な判断で eye_color は記録しない** (user 2026-07-31
    # 「目はやらない。今後もやらない」)。軸 (attribute eye_color) は残すが所見は積まれない。
    # したがって B2 は **kura1 が解けない** として数える — 「まだ注釈していない」ではなく
    # 「この運用では埋まらない」なので、未測定に逃がさない (自分の説に不利な側へ倒す)。
    ("B2", "blue_eyes はおかしい、red では [目は記録しない方針]", "層2", "kura1 解けない",
     """SELECT rf.value_slug, count(*) FROM resolved_facet rf
          JOIN resolved_scene rs ON rs.id=rf.resolved_scene_id
         WHERE rs.status='current' AND rf.attribute_slug='eye_color'
         GROUP BY 1 ORDER BY 2 DESC""",
     "blue_eyes red_eyes"),
    ("B3", "dark_areolae / huge vs large / nude の違い", "層2", "解ける",
     """SELECT rf.value_slug, count(*) FROM resolved_facet rf
          JOIN resolved_scene rs ON rs.id=rf.resolved_scene_id
         WHERE rs.status='current' AND rf.attribute_slug='breast_size'
         GROUP BY 1 ORDER BY 2 DESC""",
     "huge_breasts large_breasts"),
    # 0037: 人数は entity を数える。**同じ presentation に 2 つの数が立つことは
    # 構造上ありえない** ので、その不可能性そのものを見る query にする。
    ("B4", "1girl と 2girls が同時にヒット", "層2", "解ける",
     """SELECT count(*) AS n_conflicting FROM (
          SELECT e.scene_id, e.gender_presentation, count(DISTINCT c) AS n_kinds FROM (
            SELECT scene_id, resolver_run_id, gender_presentation, count(*) AS c
              FROM entity WHERE gender_presentation IS NOT NULL GROUP BY 1,2,3) e
          JOIN resolved_scene rs ON rs.resolver_run_id=e.resolver_run_id
                                AND rs.scene_id=e.scene_id AND rs.status='current'
          GROUP BY 1,2 HAVING count(DISTINCT c) > 1) x""",
     "1girl 2girls"),
    ("B5", "multiple_boys/girls の使い分け", "層2", "解ける",
     """SELECT e.gender_presentation, e.c AS n_persons, count(*) AS n_scenes FROM (
          SELECT scene_id, resolver_run_id, gender_presentation, count(*) AS c
            FROM entity WHERE gender_presentation IS NOT NULL GROUP BY 1,2,3) e
        JOIN resolved_scene rs ON rs.resolver_run_id=e.resolver_run_id
                              AND rs.scene_id=e.scene_id AND rs.status='current'
        GROUP BY 1,2 ORDER BY 1,2""",
     "multiple_boys"),
    ("B6", "all_fours/doggystyle/sex_from_behind が同時に出る", "層2", "seed 1 行",
     """SELECT rs.scene_id FROM resolved_facet rf
          JOIN resolved_scene rs ON rs.id=rf.resolved_scene_id
         WHERE rs.status='current' AND rf.pose_slug='all_fours'
         INTERSECT
        SELECT rs.scene_id FROM resolved_facet rf
          JOIN resolved_scene rs ON rs.id=rf.resolved_scene_id
         WHERE rs.status='current' AND rf.relation_slug='behind'""",
     "all_fours doggystyle"),
    ("B7", "big_belly と pregnant", "層2", "seed 1 行",
     """SELECT rf.attribute_slug, rf.value_slug, count(*) FROM resolved_facet rf
          JOIN resolved_scene rs ON rs.id=rf.resolved_scene_id
         WHERE rs.status='current'
           AND rf.attribute_slug IN ('belly_shape','pregnancy_state')
         GROUP BY 1,2 ORDER BY 1,3 DESC""",
     "pregnant"),
]


def pool_post_ids(pool: int) -> list[int]:
    """pool の post id を列挙する (control arm と kura1 側の母集団を一致させる鍵)。"""
    out, off = [], 0
    while True:
        url = f"{KURA_API}/api/posts?" + urllib.parse.urlencode(
            {"query": f"pool:{pool}", "limit": 100, "offset": off})
        with urllib.request.urlopen(url, timeout=60) as r:
            d = json.loads(r.read())
        page = [p["id"] for p in (d.get("results") or [])]
        out += page
        off += len(page)
        if not page or off >= d.get("total", 0):
            return out


def kura_search(tags: str, pool: int = POOL) -> tuple[int, list]:
    """control arm — production kura の `pool:<N> <tags>` 検索。読み取りのみ。

    **`|` 区切りは和集合。** kura の検索は複数タグを AND でしか扱えず
    (実測: huge_breasts 5 / large_breasts 22 / 両方 4)、OR 構文も無い
    (`~a ~b` / `a,b` / `or:a` いずれも 0 件)。
    kura1 側が「軸として持っている」ことを測る query に対しては、control も
    **その軸に相当するタグ全部**でなければ同種の比較にならない (2026-07-31、A3 の欠陥)。
    """
    if "|" in tags:
        ids: set = set()
        for one in tags.split("|"):
            _, got = kura_search(one.strip(), pool)
            ids |= {x for x in got if isinstance(x, int)}
        return len(ids), sorted(ids)[:12]
    q = f"pool:{pool} {tags}" if tags else f"pool:{pool}"
    url = f"{KURA_API}/api/posts?" + urllib.parse.urlencode({"query": q, "limit": 100})
    try:
        ctx = ssl._create_unverified_context()
        with urllib.request.urlopen(url, timeout=30, context=ctx) as r:
            d = json.loads(r.read())
        return d.get("total", 0), [p.get("id") for p in (d.get("results") or [])][:50]
    except Exception as e:                                  # noqa: BLE001
        return -1, [f"error: {e}"]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", help="query id を 1 つだけ実行")
    ap.add_argument("--pool", type=int, default=POOL,
                    help="母集団の pool。**kura1 側もこの pool の scene に限定する** — "
                         "ラウンドを混ぜると事前登録済みの測定が事後に変わる")
    a = ap.parse_args()
    pool = a.pool

    # **部位は slug を手で列挙せず body_region の親子から導く。**
    # A2 を region_slug='mouth' だけで書いたら upper_lip / tongue を取りこぼした
    # (2026-07-29 実測)。列挙は「語彙が増えたら直す」を前提にしていて、実際に外した。
    with psycopg.connect(DSN) as c, c.cursor() as cur:
        cur.execute("SET search_path = kura, public")
        # **kura1 側も母集団を pool に限定する。** これをしないと第 1 ラウンドの
        # 58 scene が混ざり、事前登録した「ラウンドを混ぜない」が壊れる。
        ids = pool_post_ids(pool)
        cur.execute("CREATE TEMP TABLE probe_scene AS"
                    " SELECT DISTINCT a.scene_id FROM asset a"
                    " JOIN asset_source s ON s.asset_id=a.id"
                    " WHERE s.source_slug='kura_post' AND s.external_id = ANY(%s)",
                    ([str(i) for i in ids],))
        cur.execute("SELECT count(*) FROM probe_scene")
        print(f"母集団: pool {pool} = {len(ids)} post / kura1 側 {cur.fetchone()[0]} scene\n")
        cur.execute("""
            CREATE OR REPLACE FUNCTION pg_temp.descendants(root text)
            RETURNS TABLE(slug text) LANGUAGE sql STABLE AS $$
              WITH RECURSIVE d AS (
                SELECT b.slug FROM kura.body_region b WHERE b.slug = root
                UNION ALL
                SELECT b.slug FROM kura.body_region b JOIN d ON b.parent_slug = d.slug)
              SELECT slug FROM d $$;""")
        for qid, gist, stratum, klass, sql, tags in Q:
            if a.only and qid != a.only:
                continue
            print("=" * 78)
            print(f"{qid}  {gist}")
            print(f"     {stratum} / S-3 分類: {klass}")
            print("-" * 78)
            try:
                # rs.status='current' の直後に母集団の限定を挟む
                cur.execute(sql.replace("rs.status='current'",
                                        "rs.status='current'"
                                        " AND rs.scene_id IN (SELECT scene_id FROM probe_scene)")
                            .replace("FROM entity WHERE",
                                     "FROM entity WHERE scene_id IN"
                                     " (SELECT scene_id FROM probe_scene) AND"))
                rows = cur.fetchall()
                cols = [d.name for d in cur.description]
            except Exception as e:                          # noqa: BLE001
                c.rollback()
                cur.execute("SET search_path = kura, public")
                rows, cols = [], [f"SQL エラー: {str(e)[:90]}"]
            print(f"  kura1   [{', '.join(cols)}]")
            if not rows:
                print("            (0 行)")
            for r in rows[:12]:
                print("           ", r)
            if len(rows) > 12:
                print(f"            … 他 {len(rows)-12} 行")
            if tags is None:
                print("  control  — 該当タグが無い (S-3 で「供給源が無い」と判定)")
            else:
                n, ids = kura_search(tags, pool)
                print(f"  control  pool:{pool} {tags}  →  {n} 件")
                if ids:
                    print("           ", ids)
            print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
