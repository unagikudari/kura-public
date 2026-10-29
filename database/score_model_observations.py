#!/usr/bin/env python3
"""P0 答案合わせ — モデルの観測を人間の観測と軸ごとに突き合わせる。

plan kura1-model-observation の P0 (user 承認 2026-07-31「実施」)。
**この script が一致率の定義そのもの** — 合格ラインの fact はこの script の
commit を参照する。結果を見てから採点規則を変えないため、先に固定する。

── 採点の定義 ──
* answer sheet = producer_kind='human' の**生きている**観測 (撤回済みを除く)。
  resolved ではなく生の観測を使う — resolver は human>model を混ぜるので、
  「モデルが単独で何を書けたか」は生の観測でしか測れない。
* model 側 = 指定 producer_ref の生きている観測。
* 軸: identity.species / presentation / region / pose /
  attribute (値・部位込み) / relation (左右は見ない、0072 と同じ) /
  scene_descriptor / entity_count (involvement 込み)。
* **entity の対応付けはモデルに甘い**: 人物 ref どうしの全単射のうち
  **一致合計が最大になるもの**を総当たりで選ぶ (scene の人物は高々 5 想定)。
  位置 (anchor) を要求しない代わりに、この甘さを限界として fact に明記する。
* precision = モデルが書いた所見のうち人間と一致した割合 (誤りの少なさ)
  recall    = 人間の所見のうちモデルも書いた割合 (拾えた割合)
* 関係軸は**人物 2 人以上の scene だけの内訳**を別掲する (主体の結び付けが最弱と予想)。

使い方:
  python3 database/score_model_observations.py --producer 'qwen2.5-vl@<ver>'
  python3 database/score_model_observations.py --producer '<ref>' --scenes 90,91
"""
from __future__ import annotations

import argparse
import itertools
import os
from collections import defaultdict

import psycopg

DSN = os.environ.get("KURA1_DSN", "postgresql://desk@127.0.0.1:5541/kura1")
# 2026-08-01 anchor 最近傍対応を実装 (fact 30fab4d1、user 指示)。合否 (--for-pass-fail)
# は anchor 対応でのみ算出し、best_alignment (max-match、モデルに甘い) は探索モード専用。
_ANCHOR_READY = True
# 合否の対応付けで「同一人物」とみなす最大距離 (正規化座標)。モデル anchor は
# 0.1 刻みの粗さがある (S2 実測) が、別人の頭はそれ以上離れているのが通常
_ANCHOR_MAX_DIST = 0.4
# 合格ライン (結果より先に固定 — 親 plan kura1-model-observation P0/P4 の線)
_PASS_PRECISION = 0.90
_PASS_RECALL = 0.50
_MIN_N = 50   # 軸ごと最低 N (human facet 数)。未満は判定保留 (nodec review 30fab4d1)


def repo_commit() -> str:
    """一致率の定義の版 = この repo の HEAD (未 commit 変更があれば -dirty)。"""
    import pathlib
    import subprocess
    repo = pathlib.Path(__file__).resolve().parent.parent
    run = lambda *args: subprocess.run(["git", "-C", str(repo), *args],  # noqa: E731
                                       capture_output=True, text=True).stdout.strip()
    c = run("rev-parse", "--short", "HEAD") or "unknown"
    return c + ("-dirty" if run("status", "--porcelain", "--untracked-files=no",
                                "--", "database/") else "")

AXES = ("species", "presentation", "region", "pose", "attribute",
        "relation", "scene_descriptor", "entity_count")


def load(cur, producer_kind: str, producer_ref: str | None) -> dict:
    """scene_id -> {axis: set(キー), 'refs': set(人物 ref)} を返す。撤回済みは除く。"""
    cond = "o.producer_kind = %s"
    args: list = [producer_kind]
    if producer_ref:
        cond += " AND o.producer_ref = %s"
        args.append(producer_ref)
    cur.execute(f"""
        SELECT o.scene_id, o.subject_ref, o.domain,
               oi.species_slug, oi.presentation,
               br.region_slug, op.pose_slug,
               oa.attribute_slug, oa.value_slug, oa.region_slug,
               orl.relation_slug, orl.subject_ref, orl.subject_region_slug,
               orl.object_ref, orl.object_region_slug, orl.object_surface_slug,
               osd.descriptor_slug,
               oec.presentation, oec.count_min, oec.count_exact, oec.involvement
          FROM observation o
          LEFT JOIN obs_identity         oi  ON oi.observation_id  = o.id
          LEFT JOIN obs_body_region      br  ON br.observation_id  = o.id
          LEFT JOIN obs_pose             op  ON op.observation_id  = o.id
          LEFT JOIN obs_attribute        oa  ON oa.observation_id  = o.id
          LEFT JOIN obs_relation         orl ON orl.observation_id = o.id
          LEFT JOIN obs_scene_descriptor osd ON osd.observation_id = o.id
          LEFT JOIN obs_entity_count     oec ON oec.observation_id = o.id
         WHERE {cond}
           AND o.retracts_id IS NULL
           AND NOT EXISTS (SELECT 1 FROM observation r WHERE r.retracts_id = o.id)
    """, args)
    out: dict = defaultdict(lambda: {a: set() for a in AXES} | {"refs": set()})
    for (sid, ref, dom, sp, pres, reg, pose, aslug, aval, areg,
         rel, rsub, rsubr, robj, robjr, rsurf, desc,
         ecp, ecmin, ecex, ecinv) in cur.fetchall():
        s = out[sid]
        if ref:
            s["refs"].add(ref)
        if sp:
            s["species"].add((ref, sp))
        if pres:
            s["presentation"].add((ref, pres))
        if reg:
            s["region"].add((ref, reg))
        if pose:
            s["pose"].add((ref, pose))
        if aslug:
            s["attribute"].add((ref, aslug, aval, areg))
        if rel:
            # 左右は見ない (0072 の照合と同じ理由 — 指定に限界がある)
            s["relation"].add((rsub, rel, rsubr, robj, robjr, rsurf))
        if desc:
            s["scene_descriptor"].add(desc)
        if ecp:
            s["entity_count"].add((ecp, ecmin, ecex, ecinv))

    # entity_count は**キーを正規化して**比較する (2026-08-01、user 指摘
    # 「人数記録はすでにあるよね」):
    #   human 側 = 個体化した人物 (subjects) の列挙が人数記録そのもの —
    #              presentation ごとの ref 数から導出する。明示の obs_entity_count
    #              行は書かれていない (0079 の欄は個体化しない背景の観衆用)
    #   model 側 = (presentation, count) に正規化。bystander は 'bystander:' 前置き
    #              (human 側に対応物が無い主張として model のみに数える)
    # ※ この定義は entity_count 軸が一度も測れていない時点 (human 0 件) で固定した —
    #   結果を見てからの採点規則変更ではない。
    for s in out.values():
        raw = s["entity_count"]
        s["entity_count"] = set()
        for (p, cmin, cex, cinv) in raw:
            s["entity_count"].add((("bystander:" if cinv == "bystander" else "") + p,
                                   cmin))
        if producer_kind == "human":
            per: dict = defaultdict(set)
            for (ref, pres) in s["presentation"]:
                per[pres].add(ref)
            for pres, refs in per.items():
                s["entity_count"].add((pres, len(refs)))
    return out


def remap(keys: set, mapping: dict, positions: tuple) -> set:
    """キーの中の ref を mapping で読み替える。positions = ref が入る位置。"""
    out = set()
    for k in keys:
        if isinstance(k, tuple):
            k = tuple(mapping.get(x, x) if i in positions else x for i, x in enumerate(k))
        out.add(k)
    return out


REF_POS = {"species": (0,), "presentation": (0,), "region": (0,), "pose": (0,),
           "attribute": (0,), "relation": (0, 3), "scene_descriptor": (), "entity_count": ()}


def load_anchors(cur, producer_kind: str, producer_ref: str | None) -> dict:
    """scene_id -> {ref: (x, y)} を返す (生きている観測の最新 anchor)。"""
    cond = "o.producer_kind = %s"
    args: list = [producer_kind]
    if producer_ref:
        cond += " AND o.producer_ref = %s"
        args.append(producer_ref)
    cur.execute(f"""
        SELECT DISTINCT ON (o.scene_id, o.subject_ref)
               o.scene_id, o.subject_ref, ar.anchor_x, ar.anchor_y
          FROM observation o
          JOIN asset_region ar ON ar.id = o.subject_asset_region_id
         WHERE {cond} AND ar.anchor_x IS NOT NULL
           AND o.retracts_id IS NULL
           AND NOT EXISTS (SELECT 1 FROM observation r WHERE r.retracts_id = o.id)
         ORDER BY o.scene_id, o.subject_ref, o.id DESC
    """, args)
    out: dict = defaultdict(dict)
    for sid, ref, x, y in cur.fetchall():
        out[sid][ref] = (float(x), float(y))
    return out


def anchor_alignment(model: dict, human: dict, ma: dict, ha: dict):
    """anchor 最近傍の対応付け (fact 30fab4d1)。**幾何だけで対応を決める**ので、
    best_alignment (一致最大化) の「難しい scene ほどモデルに甘い」バイアスが無い。

    戻り: (mapping, ok)。ok=False = 必要な anchor が欠けており、この scene は
    合否分母から**除外**する (fallback で甘く数えない)。
    片側 1 人どうしは anchor 不要 (対応が自明)。人数不一致は距離最小の部分対応 —
    余った側の facet はそのまま FP/FN として数える (見落とし・幻視の実態)。
    """
    import math
    mrefs, hrefs = sorted(model["refs"]), sorted(human["refs"])
    if not mrefs or not hrefs:
        return {}, True
    if len(mrefs) == 1 and len(hrefs) == 1:
        return {mrefs[0]: hrefs[0]}, True
    if any(r not in ma for r in mrefs) or any(r not in ha for r in hrefs):
        return {}, False
    small, big, m_small = ((mrefs, hrefs, True) if len(mrefs) <= len(hrefs)
                          else (hrefs, mrefs, False))
    best, best_cost = [], None
    for perm in itertools.permutations(big, len(small)):
        pairs = list(zip(small, perm))
        cost = 0.0
        for s_, b_ in pairs:
            mref, href = (s_, b_) if m_small else (b_, s_)
            cost += math.dist(ma[mref], ha[href])
        if best_cost is None or cost < best_cost:
            best_cost, best = cost, pairs
    mapping = {}
    for s_, b_ in best:
        mref, href = (s_, b_) if m_small else (b_, s_)
        if math.dist(ma[mref], ha[href]) <= _ANCHOR_MAX_DIST:
            mapping[mref] = href
    return mapping, True


def best_alignment(model: dict, human: dict) -> dict:
    """一致合計が最大になる model ref -> human ref の対応。**モデルに甘い**。

    P0 の合否には使えない placeholder。review 5ed1d22d CRITICAL のとおり最大一致は
    難しい scene ほどモデル有利。anchor 最近傍対応 (fact 30fab4d1) を P1 で実装する。
    """
    mrefs, hrefs = sorted(model["refs"]), sorted(human["refs"])
    if not mrefs or not hrefs:
        return {}
    best, best_score = {}, -1
    for perm in itertools.permutations(hrefs, min(len(mrefs), len(hrefs))):
        mp = dict(zip(mrefs, perm))
        score = sum(len(remap(model[a], mp, REF_POS[a]) & human[a]) for a in AXES)
        if score > best_score:
            best, best_score = mp, score
    return best


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--producer", required=True,
                    help="model 側の producer_ref (例 qwen2.5-vl@7b-2026xx)")
    ap.add_argument("--scenes", help="scene_id をカンマ区切りで限定 (省略で全 scene)")
    ap.add_argument("--for-pass-fail", action="store_true",
                    help="合否判定として実行。anchor 対応の実装まで拒否される")
    ap.add_argument("--no-persist", action="store_true",
                    help="observer_score_run へ書かない (お試し実行用)")
    ap.add_argument("--dsn", default=DSN)
    a = ap.parse_args()
    if a.for_pass_fail and not _ANCHOR_READY:
        raise SystemExit(
            "拒否: 合否は anchor 最近傍対応 (fact 30fab4d1) が要る。現在の best_alignment は "
            "max-match で難しい scene ほどモデル有利 (review 5ed1d22d CRITICAL)。"
            "P1 で anchor を実装し _ANCHOR_READY=True にすること。")

    with psycopg.connect(a.dsn) as conn, conn.cursor() as cur:
        cur.execute("SET search_path = kura, public")
        human = load(cur, "human", None)
        model = load(cur, "model", a.producer)
        h_anchor = load_anchors(cur, "human", None) if a.for_pass_fail else {}
        m_anchor = load_anchors(cur, "model", a.producer) if a.for_pass_fail else {}

    scenes = sorted(set(human) & set(model))
    if a.scenes:
        want = {int(x) for x in a.scenes.split(",")}
        scenes = [s for s in scenes if s in want]
    if not scenes:
        print(f"model ({a.producer}) と human の両方に観測がある scene が無い")
        return 1

    tp: dict = defaultdict(int)   # 一致
    fp: dict = defaultdict(int)   # model のみ (誤り側)
    fn: dict = defaultdict(int)   # human のみ (見逃し側)
    rel_multi = {"tp": 0, "fp": 0, "fn": 0}
    excluded = 0   # 合否モードで anchor 欠けにより分母から除外した scene
    for sid in scenes:
        h, m = human[sid], model[sid]
        if a.for_pass_fail:
            # 合否は anchor 最近傍対応のみ (fact 30fab4d1)。anchor が無い scene は
            # 甘い fallback で数えず、分母から除外する
            mp, ok = anchor_alignment(m, h, m_anchor.get(sid, {}), h_anchor.get(sid, {}))
            if not ok:
                excluded += 1
                continue
        else:
            mp = best_alignment(m, h)
        for ax in AXES:
            mm = remap(m[ax], mp, REF_POS[ax])
            tp[ax] += len(mm & h[ax])
            fp[ax] += len(mm - h[ax])
            fn[ax] += len(h[ax] - mm)
            if ax == "relation" and len(h["refs"]) >= 2:
                rel_multi["tp"] += len(mm & h[ax])
                rel_multi["fp"] += len(mm - h[ax])
                rel_multi["fn"] += len(h[ax] - mm)

    # **P/R は合否モード (anchor 対応) でのみ出す** (review 08668e40 #1)。
    # 探索モードの max-match 件数を P/R 化すると合否に流用されるため。
    show_pr = a.for_pass_fail and _ANCHOR_READY
    print(f"model = {a.producer} / 対象 scene {len(scenes)}"
          + (f" (anchor 欠けで除外 {excluded}、判定 {len(scenes) - excluded})"
             if a.for_pass_fail else
             "  ⚠️ 探索モード: precision/recall は出さない "
             "(max-match 対応は合否に使えない)"))
    hdr = f"{'軸':18} {'一致':>5} {'modelのみ':>8} {'humanのみ':>8}"
    print(hdr + (f" {'precision':>9} {'recall':>7}" if show_pr else "   [P/R は非表示]"))

    def prf(ttp, tfp, tfn):
        """(precision, recall, passed)。合格ライン (P≥0.90 ∧ R≥0.50) は結果より先に
        固定した値 (_PASS_*)。N = human facet 数 < _MIN_N は判定保留 (passed=None)。"""
        pr = ttp / (ttp + tfp) if ttp + tfp else None
        rc = ttp / (ttp + tfn) if ttp + tfn else None
        n = ttp + tfn
        passed = None
        if n >= _MIN_N and pr is not None and rc is not None:
            passed = pr >= _PASS_PRECISION and rc >= _PASS_RECALL
        return pr, rc, passed

    axis_pr: dict = {}
    def prow(name, ttp, tfp, tfn):
        base = f"{name:18} {ttp:5} {tfp:8} {tfn:8}"
        if not show_pr:
            print(base + "   —"); return
        pr, rc, passed = prf(ttp, tfp, tfn)
        axis_pr[name] = (pr, rc, passed)
        verdict = ("N<%d 保留" % _MIN_N) if passed is None else ("合格" if passed else "不合格")
        print(base + f" {pr if pr is not None else float('nan'):9.2f}"
                     f" {rc if rc is not None else float('nan'):7.2f}  {verdict}")

    for ax in AXES:
        prow(ax, tp[ax], fp[ax], fn[ax])
    d = rel_multi
    prow("relation(2人以上)", d["tp"], d["fp"], d["fn"])
    if show_pr:
        print(f"\n合格ライン (事前固定): precision ≥ {_PASS_PRECISION} かつ recall ≥ "
              f"{_PASS_RECALL}、N (human facet) ≥ {_MIN_N}。"
              "\n注: trivial baseline の併記 (30fab4d1 §3) は未実装 — 別途出す。")
    if not show_pr:
        print("\n※ これは探索用の件数のみ。合否判定は --for-pass-fail "
              "(anchor 実装後) でしか出せない (review 08668e40 #1)。")

    # ── 採点結果の永続化 (0086) — dashboard と世代間比較の SoT ──
    # producer_ref は observer_version に登録済みであることを FK で強制する
    # (未登録の版は採点で発覚する、という向きの検査)。
    if not a.no_persist:
        with psycopg.connect(a.dsn) as conn, conn.cursor() as cur:
            cur.execute("SET search_path = kura, public")
            try:
                cur.execute(
                    "INSERT INTO observer_score_run(producer_ref, scorer_commit,"
                    " mode, scene_count) VALUES (%s,%s,%s,%s) RETURNING id",
                    (a.producer, repo_commit(),
                     "pass_fail" if a.for_pass_fail else "exploratory",
                     len(scenes) - excluded))
                run_id = cur.fetchone()[0]
                def _pr3(name, ttp, tfp, tfn):
                    if not a.for_pass_fail:
                        return (None, None, None)
                    return prf(ttp, tfp, tfn)
                rows = []
                for ax in AXES:
                    pr, rc, ps = _pr3(ax, tp[ax], fp[ax], fn[ax])
                    rows.append((run_id, ax, tp[ax], fp[ax], fn[ax], pr, rc, ps))
                pr, rc, ps = _pr3("relation_multi", rel_multi["tp"],
                                  rel_multi["fp"], rel_multi["fn"])
                rows.append((run_id, "relation_multi", rel_multi["tp"],
                             rel_multi["fp"], rel_multi["fn"], pr, rc, ps))
                cur.executemany(
                    "INSERT INTO observer_score_axis(run_id, axis, matched,"
                    " model_only, human_only, match_precision, match_recall, passed)"
                    " VALUES (%s,%s,%s,%s,%s,%s,%s,%s)", rows)
                conn.commit()
                print(f"永続化: observer_score_run {run_id} ({repo_commit()})")
            except psycopg.errors.ForeignKeyViolation:
                conn.rollback()
                print(f"永続化せず: {a.producer} が observer_version に未登録 "
                      "(runner を通していない版は採点結果を残さない)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
