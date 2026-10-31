"""v-next runner の escalation 述語 — LLM の唯一の入口 (INV-R6)。

plan tagger-vnext-deterministic-runner rev6 Step 17。runner は決定論で
PASS/HOLD を出すが、「決定論では分類できない状態」だけは人間/LLM に上げる。
述語をここ 1 箇所に集約するのは、runner 本体に散らすと「どの条件で上げるか」
が実装の副作用になり、あとから読めなくなるため。

分類:
  - **E0 (default-escalate)**: 分類できない状態はすべて上げる。特に測定前の
    構造前提チェック (release 行/artifact パス/タグの語彙からの消失/配備行の
    撤去/replica schema drift)。「知らない形」を黙って HOLD にしない。
  - **ブロック型** E1-E6: 測定を止める/判定を保留する事象。
  - **情報提供型** E7: above-chance かつ binding が支持数/recall 側 = 「正例を
    増やせば到達しうる」。HOLD の流れは止めず owner に通知するだけ
    (round 7 の失敗形がこれ。E3「定義不能」と混同すると誤った撤退をする)。

dedup 鍵は **対象タグの GT カウントを整数除算で bucket 化** する
(rev2 F3): corpus 全体の GT で鍵を作ると無関係タグの判定が動くたびに再発火し、
逆に鍵を固定にすると GT が育っても二度と上がらない。
"""
from __future__ import annotations

import math

from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

# GT bucket 幅 — 成長ゲート (正例 +20 / 負例 +50) と同じ粒度。これ未満の
# 変化では「状況が変わった」とみなさず再発火しない。
POS_BUCKET = 25
NEG_BUCKET = 50

# E4: floor 境界の僅差。ここに入る arm は「落ちた」ではなく「際どい」として上げる。
FLOOR_MARGIN = 0.05

# E6: HOLD の連続回数。hysteresis — GT bucket が動けばカウントはリセットされる
# (rev3 C2)。カウントだけで上げると同じ結論を毎回報告して context を汚す。
HOLD_STREAK_LIMIT = 3

# 「決定論が結論に至った」判定の語彙 (4 クラスのうちの 2 つ)。
#
# E4/E6/E7 は **結論に至らなかった測定** についてだけ意味を持つ:
#   E4 「floor に僅差で不達」  — 結論が出ているなら「不達」ではない
#   E6 「HOLD が N 回連続」    — 結論が出ているなら HOLD ではない
#   E7 「正例を増やせば届く」  — 結論が出ているなら足す必要が無い
#
# これらは長らく `verdict["any_arm_meets_floor"]` を「結論に至ったか」の代理に
# していた。2 値 (PASS/HOLD) の時代はそれで正しかったが、判定が 4 クラスに
# なり **規模 floor** が入った時点で代理は壊れた — `any_arm_meets_floor` は
# 絶対 (率) floor しか見ないので、規模 floor で `deployable` になった測定でも
# False のままになる。
#
# 2026-08-05 実測: smegma は `deployable` (lift 7.29x / 差し引き 169,284 件減)
# と判定されているのに `any_arm_meets_floor=False` のため、測定のたびに
# 「P>=0.90 が min_p_deploy に僅差で不達」の E4 が owner へ届き続けた。
# **配備が決まったタグについて「僅差で落ちた」と報告していた。**
#
# **この module は `any_arm_meets_floor` を読まない。** 判定は 4 クラスの
# disposition ただ 1 つで決める — 旧値への fallback を残すと真実の出所が
# 2 つになり、片方だけ直したときに今回と同じ食い違いが再発する。
CONCLUDED_DISPOSITIONS = frozenset({"deployable", "not-needed"})


# E2 の「偶然では説明できないほど 0.5 を下回っている」の判定。
#
# 2026-08-07 実測: 条件は `auc < 0.5` の裸の閾値だった。**点推定のばらつきを
# 見ていない。** foreskin は AUC=0.4996 (n_pos=71 / n_neg=224) で発火し、owner に
# 「corpus arm が逆相関。レシピの汚染が疑われる」と届いた。実際には 0.5 を
# **0.01 標準誤差**下回っただけで、偶然そのもの。
#
#     foreskin   auc=0.4996  SE=0.0394  → 0.01 SE 下  (信号が無いだけ)
#     dark_anus  auc=0.4050  SE=0.0519  → 1.83 SE 下  (本当に逆相関、corpus の
#                                          除外フィルタが 30,274 件中 0 件しか
#                                          除外できていなかった実害があった)
#
# 他の arm は permutation null (`above_chance = auc > null.max`) で偶然と区別して
# いる。C arm には null が計算されていないので、同じ問いを解析的に置く —
# AUC の帰無分布の標準誤差 sqrt((n_p+n_n+1)/(12 n_p n_n)) に対する片側検定。
# 有意水準は通例の 5% (z=1.645)。**新しい閾値を発明しない。**
#
# n が無ければ判定しない。E2 は「レシピを見直せ」という誘いなので、根拠が
# 欠けたまま誘うと owner の時間を捨てさせる (E4 と同じ扱い)。
_CHANCE = 0.5
_ONE_SIDED_5PCT_Z = 1.645


def _below_chance(auc, n_pos, n_neg) -> bool:
    """AUC が偶然の下振れでは説明できないほど 0.5 を下回っているか。"""
    if not isinstance(auc, (int, float)):
        return False
    if not isinstance(n_pos, int) or not isinstance(n_neg, int):
        return False
    if n_pos <= 0 or n_neg <= 0:
        return False
    se = math.sqrt((n_pos + n_neg + 1) / (12.0 * n_pos * n_neg))
    return auc < _CHANCE - _ONE_SIDED_5PCT_Z * se


def is_concluded(disposition: str | None) -> bool:
    """この測定は結論に至ったか。

    `hold` (測れなかった) と `cleanup-capable` (owner 判断待ち) は未確定。
    None は「判定そのものが無い」(例外で終わった測定など) で、これも未確定 —
    **判断がつかないときは鳴る側**に倒す。escalation は届かなければ存在しない
    のと同じなので、誤るなら「鳴りすぎ」であって「黙る」ではない。
    """
    return disposition in CONCLUDED_DISPOSITIONS


@dataclass
class Escalation:
    """1 件の escalation。`blocking` が False なら runner は処理を続ける。"""

    code: str
    blocking: bool
    summary: str
    detail: dict[str, Any] = field(default_factory=dict)

    def dedup_key(self, tag: str, gt_pos: int, gt_neg: int) -> str:
        """同一タグ・同一 code・同程度の GT では 1 回だけ上げる。"""
        return (f"vnext-escalate:{self.code}:{tag}:"
                f"p{gt_pos // POS_BUCKET}:n{gt_neg // NEG_BUCKET}")


def structural_preconditions(ctx: Mapping[str, Any]) -> list[Escalation]:
    """E0 — 測定を始める前に「世界が想定どおりか」を確かめる。

    ここで見るのは runner が制御できない外部構造で、崩れていたら測定結果は
    意味を持たない。ctx のキーが欠けている場合も「確認できなかった」として
    上げる (欠落を正常扱いにしない = fail-closed)。
    """
    out: list[Escalation] = []
    for key, why in (
        ("tag_in_vocab", "タグが tagger 語彙から消えている (rename/merge の疑い)"),
        ("release_row_present", "稼働 release 行が引けない"),
        ("replica_schema_matches", "replica の schema version が canonical と不一致 "
                                   "(migration 未伝播 — 2026-08-02 に replication を止めた形)"),
    ):
        value = ctx.get(key)
        if value is None:
            out.append(Escalation(
                "E0", True, f"構造前提を確認できなかった: {key}",
                {"missing_check": key, "why_it_matters": why}))
        elif value is False:
            out.append(Escalation(
                "E0", True, f"構造前提が崩れている: {key}", {"check": key, "why": why}))
    return out


def _binding_items(verdict: Mapping[str, Any]) -> list[str]:
    """verdict から binding な floor 項名を取り出す (実装差異に耐える)。"""
    for key in ("binding", "binding_floor_items", "binding_items"):
        v = verdict.get(key)
        if isinstance(v, str):
            return [v]
        if isinstance(v, (list, tuple)):
            return [str(x) for x in v]
    return []


def evaluate(
    *,
    tag: str,
    verdict: Mapping[str, Any],
    arms: Mapping[str, Mapping[str, Any]],
    floor: Mapping[str, float],
    hold_streak: int,
    gt_pos: int,
    gt_neg: int,
    disposition: str | None,
    structural: Mapping[str, Any] | None = None,
    exception: BaseException | None = None,
    api_errors: Sequence[str] = (),
) -> list[Escalation]:
    """測定 1 回分の escalation を全部返す。呼び出し側は blocking を見て止める。"""
    out: list[Escalation] = []

    # ── E0: 構造前提 (測定前に呼ぶ想定だが、ここでも受け取れる) ──
    if structural is not None:
        out.extend(structural_preconditions(structural))

    # ── E1: 例外 / primary API 非 2xx ──
    if exception is not None:
        out.append(Escalation("E1", True, f"測定が例外で終了: {type(exception).__name__}",
                              {"error": str(exception)[:500]}))
    for err in api_errors:
        out.append(Escalation("E1", True, "primary API が非 2xx を返した", {"error": err[:500]}))

    if not arms:
        # 測定結果が無いのに例外も無い = 分類不能 (E0 の精神)。
        if exception is None:
            out.append(Escalation("E0", True, "arm が 1 つも無い測定結果", {}))
        return out

    # ── E2: corpus arm が逆相関 = レシピが壊れている ──
    for name, arm in arms.items():
        auc = arm.get("auc")
        if name.startswith("C_") and _below_chance(auc, arm.get("n_pos"), arm.get("n_neg")):
            # **blocking にしない**: plan Step 17 の定義は「当該 arm を除外し
            # owner へ」であって測定の中止ではない。他の arm の判定は有効なので
            # 止めると健全な結果まで捨てることになる (2026-08-03: dark_anus で
            # 実際に発火し、blocking=True のままだと PASS 判定を捨てていた)。
            out.append(Escalation(
                "E2", False, f"corpus arm が逆相関 ({name} AUC={auc:.3f} < 0.5)",
                {"arm": name, "auc": auc,
                 "note": "当該 arm は判定に使わない。レシピの汚染が疑われるので "
                         "corpus サンプリングの見直しを owner に依頼する"}))

    # ── E3: 全 arm が permutation null 以下 = 現手法では定義不能 ──
    above = [n for n, a in arms.items() if a.get("above_chance") is True]
    if not above:
        out.append(Escalation(
            "E3", True, "全 arm が permutation null 以下 — 現手法では判別不能",
            {"arms": {n: a.get("auc") for n, a in arms.items()}}))
        return out  # 以降の floor 議論は意味を持たない

    # ── E5: metrics の形が分類できない ──
    for name, arm in arms.items():
        if arm.get("above_chance") and not isinstance(arm.get("nested"), Mapping):
            out.append(Escalation("E5", True, f"{name} の metrics 形状が分類不能",
                                  {"arm": name, "keys": sorted(arm.keys())}))

    # **結論に至ったか** — 4 クラスの判定だけで決める (上の
    # CONCLUDED_DISPOSITIONS の注記)。E4/E6/E7 はここが False のときだけ
    # 意味を持つ。
    concluded = is_concluded(disposition)
    binding = _binding_items(verdict)

    # ── E4: floor 境界の僅差 (落ちたが margin 以内) ──
    if not concluded:
        for name, arm in arms.items():
            nested = arm.get("nested")
            if not isinstance(nested, Mapping):
                continue
            for rule, metrics in nested.items():
                if not isinstance(metrics, Mapping):
                    continue
                # **その動作点の床を項目ごとに全部見てから決める。**
                #
                # 2026-08-06 実測 (phimosis): max_f1 は min_recall に 0.011 差
                # だったので E4 が「僅差で不達」と上げたが、**同じ動作点の
                # precision は 0.707 で床 0.85 から 0.143 離れていた**。
                # 0.011 だけを見せられた owner は「あと一息」と読む。
                # 項目 1 つを全体として報告していた — この module の docstring が
                # `any_arm_meets_floor` の代理について戒めているのと同じ形。
                #
                # 僅差と呼べるのは **他の床項目が全部満たされていて、1 つだけが
                # margin 以内で足りない** ときだけ。
                shortfalls, unmeasurable = {}, False
                for fkey, mkey in (("min_precision", "P"), ("min_recall", "R"),
                                   ("min_p_deploy", "P_deploy")):
                    need, got = floor.get(fkey), metrics.get(mkey)
                    # **床がその項目を定めていない = 該当なし**。満たすべき
                    # ものが無いので、判断材料が欠けているわけではない。
                    if not isinstance(need, (int, float)):
                        continue
                    # 床は定めているのに測れていない = **判断不能**。
                    if not isinstance(got, (int, float)):
                        unmeasurable = True
                        continue
                    if need - got > 0:
                        shortfalls[fkey] = (mkey, got, need)
                # 測れていない項目があるなら「他は満たしている」と言えない。
                # 判断がつかないときは鳴らす側ではなく **黙る** 側 — E4 は
                # 「あと一息だから手を入れろ」という誘いなので、根拠が欠けた
                # まま誘うと owner の時間を捨てさせる。
                if unmeasurable or len(shortfalls) != 1:
                    continue
                fkey, (mkey, got, need) = next(iter(shortfalls.items()))
                if need - got <= FLOOR_MARGIN:
                    out.append(Escalation(
                        "E4", False,
                        f"{name}/{rule} が {fkey} に僅差で不達 "
                        f"({mkey}={got:.3f} vs {need:.3f}、差 {need - got:.3f})。"
                        f"この動作点の他の床項目は満たしている",
                        {"arm": name, "rule": rule, "floor_key": fkey,
                         "observed": got, "required": need,
                         "other_floor_terms_met": True}))

    # ── E6: HOLD の連続 (hysteresis 込み — 呼び出し側が GT bucket 変化で 0 に戻す) ──
    if not concluded and hold_streak >= HOLD_STREAK_LIMIT:
        out.append(Escalation(
            "E6", True, f"HOLD が {hold_streak} 回連続 — 手法の見直しが要る",
            {"hold_streak": hold_streak,
             "note": "GT bucket が動けば streak はリセットされる (hysteresis)"}))

    # ── E7 (情報提供型): above-chance かつ binding が支持数/recall 側 ──
    if not concluded and above:
        support_or_recall = [b for b in binding
                             if "support" in b or "recall" in b]
        if support_or_recall:
            out.append(Escalation(
                "E7", False,
                "判別はできているが支持数/recall 側で floor 不達 — "
                "正例を増やせば到達しうる",
                {"above_chance_arms": above, "binding": binding,
                 "not_e3": "識別不能ではない。撤退判断の材料にしないこと"}))

    return out


def blocking(escalations: Sequence[Escalation]) -> list[Escalation]:
    return [e for e in escalations if e.blocking]


def next_hold_streak(prev_streak: int, prev_bucket: tuple[int, int] | None,
                     concluded: bool, gt_pos: int, gt_neg: int) -> tuple[int, tuple[int, int]]:
    """HOLD 連続回数の更新 (E6 の hysteresis)。

    GT bucket が前回と変われば「状況が変わった」とみなし streak を捨てる。
    結論が出ていれば当然リセット。戻り値は (次の streak, 今回の bucket)。

    `concluded` は **4 クラスの判定**から作る (`is_concluded`)。旧 2 値を渡すと
    規模 floor で `deployable` になったタグの streak が永久に増え続け、
    配備が決まっているのに「HOLD が 3 回連続 — 手法の見直しが要る」と鳴る。
    """
    bucket = (gt_pos // POS_BUCKET, gt_neg // NEG_BUCKET)
    if concluded or (prev_bucket is not None and bucket != prev_bucket):
        return (0 if concluded else 1), bucket
    return prev_streak + 1, bucket
