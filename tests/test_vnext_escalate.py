"""vnext_escalate の述語テスト (plan Step 17 / 23)。

述語は LLM の唯一の入口 (INV-R6) なので、「上げるべきものを上げる」と
「上げるべきでないものを上げない」の両側を固定する。特に E7 と E3 の
取り違え (round 7 の失敗形を「識別不能」と読むと誤った撤退をする) は
名指しでテストする。
"""
from __future__ import annotations

import pytest

from tagger import vnext_escalate as ve


def _arm(auc=0.9, above=True, nested=None, n_pos=200, n_neg=200):
    # n_pos/n_neg は E2 の判定に要る (2026-08-07)。AUC が 0.5 を下回ったことを
    # 「逆相関」と呼べるかは標本の大きさで決まる — foreskin は 0.4996 で
    # 0.01 標準誤差下でしかなく、偶然そのものだった。
    return {"auc": auc, "above_chance": above, "n_pos": n_pos, "n_neg": n_neg,
            "nested": nested if nested is not None else {"max_f1": {"P": 0.8, "R": 0.8}}}


# ── E0: 構造前提 ────────────────────────────────────────────────────────
def test_e0_missing_check_is_escalated_not_assumed_ok():
    """確認できなかった項目を「正常」と読まない (fail-closed)。"""
    out = ve.structural_preconditions({"tag_in_vocab": True})
    codes = {e.code for e in out}
    assert codes == {"E0"}
    assert all(e.blocking for e in out)
    missing = {e.detail.get("missing_check") for e in out}
    assert {"release_row_present", "replica_schema_matches"} <= missing


def test_e0_all_present_and_true_is_silent():
    assert ve.structural_preconditions({
        "tag_in_vocab": True, "release_row_present": True,
        "replica_schema_matches": True}) == []


def test_e0_false_check_is_blocking():
    out = ve.structural_preconditions({
        "tag_in_vocab": True, "release_row_present": True,
        "replica_schema_matches": False})
    assert [e.code for e in out] == ["E0"] and out[0].blocking


# ── E2 / E3 ─────────────────────────────────────────────────────────────
def test_e2_fires_only_for_corpus_arm_inverse_correlation():
    out = ve.evaluate(tag="t", verdict={},
                      arms={"C_dispatch_recipe:corpus_aug": _arm(auc=0.42),
                            "B_veto_ceiling:feat1024": _arm(auc=0.95)},
                      floor={}, hold_streak=0, gt_pos=100, gt_neg=1000,
                      disposition="hold")
    assert "E2" in {e.code for e in out}
    detail = next(e for e in out if e.code == "E2").detail
    assert detail["arm"].startswith("C_")


def test_e3_when_no_arm_beats_null_and_stops_further_floor_talk():
    out = ve.evaluate(tag="t", verdict={},
                      arms={"B_veto_ceiling:feat1024": _arm(auc=0.48, above=False)},
                      floor={"min_precision": 0.85}, hold_streak=9,
                      gt_pos=100, gt_neg=1000, disposition="hold")
    codes = [e.code for e in out]
    assert codes == ["E3"], "E3 の後に floor 議論を続けない"


# ── E7 と E3 の取り違え防止 (round 7 の失敗形) ──────────────────────────
def test_e7_is_informational_and_distinct_from_e3():
    """above-chance かつ binding が支持数側 = 「正例を増やせば届く」。

    ここを E3 (定義不能) と読むと、実際には学習できているタグから撤退する。
    """
    out = ve.evaluate(
        tag="licking_testicle",
        verdict={"binding": ["min_pred_pos_support", "min_recall"]},
        arms={"B_veto_ceiling:feat1024": _arm(auc=0.96)},
        floor={"min_precision": 0.85, "min_recall": 0.7}, hold_streak=0,
        gt_pos=384, gt_neg=9154, disposition="hold")
    e7 = [e for e in out if e.code == "E7"]
    assert e7, "支持数/recall 側 binding では E7 が出る"
    assert not e7[0].blocking, "E7 は情報提供型 — HOLD の流れを止めない"
    assert "E3" not in {e.code for e in out}


def test_e7_not_raised_when_precision_is_binding():
    out = ve.evaluate(tag="t", verdict={"binding": ["min_precision"]},
                      arms={"B_veto_ceiling:feat1024": _arm(auc=0.96)},
                      floor={"min_precision": 0.85}, hold_streak=0,
                      gt_pos=100, gt_neg=1000, disposition="hold")
    assert "E7" not in {e.code for e in out}


# ── E4: 僅差 ────────────────────────────────────────────────────────────
def test_e4_near_miss_is_reported_but_not_blocking():
    out = ve.evaluate(
        tag="t", verdict={},
        arms={"B_veto_ceiling:feat1024": _arm(
            nested={"P>=0.85": {"P": 0.83, "R": 0.75}})},
        floor={"min_precision": 0.85, "min_recall": 0.7}, hold_streak=0,
        gt_pos=100, gt_neg=1000, disposition="hold")
    e4 = [e for e in out if e.code == "E4"]
    assert e4 and not e4[0].blocking
    assert e4[0].detail["floor_key"] == "min_precision"


def test_e4_silent_when_miss_is_wide():
    out = ve.evaluate(
        tag="t", verdict={},
        arms={"B_veto_ceiling:feat1024": _arm(
            nested={"P>=0.85": {"P": 0.40, "R": 0.75}})},
        floor={"min_precision": 0.85, "min_recall": 0.7}, hold_streak=0,
        gt_pos=100, gt_neg=1000, disposition="hold")
    assert "E4" not in {e.code for e in out}


# ── E6 の hysteresis ────────────────────────────────────────────────────
def test_e6_fires_on_streak():
    out = ve.evaluate(tag="t", verdict={},
                      arms={"B_veto_ceiling:feat1024": _arm()},
                      floor={}, hold_streak=3, gt_pos=100, gt_neg=1000,
                      disposition="hold")
    assert "E6" in {e.code for e in out}


def test_hold_streak_resets_when_gt_bucket_moves():
    """GT が育ったら「同じ結論の繰り返し」ではない — streak を捨てる。"""
    streak, bucket = ve.next_hold_streak(2, (4, 20), concluded=False,
                                         gt_pos=100, gt_neg=1000)
    assert (streak, bucket) == (3, (4, 20)), "同一 bucket なら加算"
    streak2, bucket2 = ve.next_hold_streak(2, (1, 1), concluded=False,
                                           gt_pos=100, gt_neg=1000)
    assert streak2 == 1 and bucket2 == (4, 20), "bucket が動けばリセット"


def test_hold_streak_zero_on_pass():
    # 引数は「結論に至ったか」— 旧 2 値 (any_arm_meets_floor) ではない。
    # 規模 floor で deployable になった測定は旧 2 値では False のままなので、
    # そちらを渡すと配備可のタグの streak が永久に増える
    # (tests/test_escalate_uses_the_four_class_verdict.py)。
    assert ve.next_hold_streak(5, (4, 20), concluded=True,
                               gt_pos=100, gt_neg=1000)[0] == 0


# ── dedup 鍵 ────────────────────────────────────────────────────────────
def test_dedup_key_is_stable_within_bucket_and_moves_across():
    e = ve.Escalation("E7", False, "x")
    assert e.dedup_key("t", 100, 1000) == e.dedup_key("t", 110, 1010), \
        "bucket 内の小さな変動では再発火しない"
    assert e.dedup_key("t", 100, 1000) != e.dedup_key("t", 200, 1000), \
        "GT がまとまって育てば再発火する"
    assert e.dedup_key("t", 100, 1000) != e.dedup_key("other", 100, 1000)


# ── 例外系 ──────────────────────────────────────────────────────────────
def test_exception_becomes_blocking_e1():
    out = ve.evaluate(tag="t", verdict={}, arms={}, floor={}, hold_streak=0,
                      gt_pos=0, gt_neg=0, disposition=None,
                      exception=RuntimeError("boom"))
    assert [e.code for e in out] == ["E1"] and out[0].blocking


def test_empty_arms_without_exception_is_e0_not_silence():
    out = ve.evaluate(tag="t", verdict={}, arms={}, floor={}, hold_streak=0,
                      gt_pos=0, gt_neg=0, disposition=None)
    assert [e.code for e in out] == ["E0"]


def test_blocking_filter():
    es = [ve.Escalation("E7", False, "info"), ve.Escalation("E1", True, "stop")]
    assert [e.code for e in ve.blocking(es)] == ["E1"]


def test_e2_is_informational_not_a_measurement_abort():
    """corpus arm の逆相関で測定全体を止めない。

    plan Step 17 の定義は「当該 arm を除外し owner へ」。2026-08-03 に
    dark_anus で実際に発火したが、blocking=True のままだと健全な他 arm の
    配備可 判定まで捨てることになる。
    """
    out = ve.evaluate(tag="t", verdict={},
                      arms={"C_dispatch_recipe:corpus_aug": _arm(auc=0.405),
                            "B_veto_ceiling:feat1024": _arm(auc=0.95)},
                      floor={}, hold_streak=0, gt_pos=100, gt_neg=100,
                      disposition="deployable")
    e2 = [e for e in out if e.code == "E2"]
    assert e2 and e2[0].blocking is False
