"""0154 review_class — 判定 5 分類の型昇格と auto-hold の復活 (#55)。

**なぜこのテストが要るか** (census #55 §1-2 / §2, 2026-09-04 実測):
- `rejected` 1010 行のうち本当の却下は 71 行 (7%)。残りは「測れなかった (999)」
  「不要 (64)」「owner 決裁待ち (22)」が rejected_reason の前置 prose に潰し込ま
  れていた。
- auto-hold (respawn ダンパー) は metrics["val"] 前提の分類器が新形式 1007 行に
  RESUBMIT を返し、**一度も発火しない dead 状態**だった。review_class を第一
  ソースにした _hold_class_from_registration がその復活点。
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from tagger.auto_review import BUCKET_TO_REVIEW_CLASS, Decision, decide  # noqa: E402
from tagger.reconciler import _hold_class_from_registration  # noqa: E402
from api.tagger_core.transitions import RESUBMIT  # noqa: E402


def test_every_decide_bucket_has_a_review_class():
    """decide() が返しうる bucket と写像の完全一致 — bucket を増やしたら
    0154 の CHECK と map を同 commit で広げる (ズレたらここで落ちる)。"""
    # decide() の全経路の bucket を実際に踏んで列挙する
    class _Cfg(dict):
        pass
    cfg = _Cfg()
    buckets = {
        decide({"disposition": "not-needed"}, None, cfg).bucket,
        decide({"disposition": "hold"}, None, cfg).bucket,
        decide({"disposition": "cleanup-capable"}, None, cfg).bucket,
        decide({"disposition": "deployable"}, None, cfg).bucket,  # 根拠なし→rejected
        Decision(True).bucket,  # ok=True の既定 bucket
    }
    assert buckets <= set(BUCKET_TO_REVIEW_CLASS), \
        f"map に無い bucket: {buckets - set(BUCKET_TO_REVIEW_CLASS)}"
    assert set(BUCKET_TO_REVIEW_CLASS.values()) == {
        "deployable", "not_needed", "unmeasurable", "owner_decision",
        "floor_rejected"}, "0154 の CHECK 語彙と一致させること"


def test_unmeasurable_holds_as_insufficient_gt():
    """新形式 1007 行の主クラス — metrics 形式に依存せず hold できる。"""
    assert _hold_class_from_registration("unmeasurable", {"anything": 1}, {}) \
        == "insufficient_gt"


def test_not_needed_and_owner_decision_do_not_hold():
    for rc in ("not_needed", "owner_decision"):
        assert _hold_class_from_registration(rc, {}, {}) is None, \
            f"{rc} は hold ではない (前者は成長ゲート、後者は人間の決裁待ち)"


def test_floor_rejected_and_legacy_fall_through_to_the_metrics_classifier():
    """floor_rejected と NULL (旧世代) は従来の structured metrics 分類へ。
    val の無い metrics は RESUBMIT (呼び出し側の membership guard が捨てる)。"""
    for rc in ("floor_rejected", None):
        out = _hold_class_from_registration(rc, {"no": "val"}, {})
        assert out == RESUBMIT, f"rc={rc}: 旧分類器へ落ちていない (got {out})"
