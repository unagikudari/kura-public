"""backfill 登録は baseline に **GT 総数** を載せる — 測定 doc の 'gt' ではなく。

**なぜこのテストが要るか** (2026-09-04 の実測):
reconciler の成長ゲートは「直近 registration の gt」を baseline に、live の
tagger_gt_label **総数**と比較する。gt_full 化 (dcbfb12) 以前の測定 doc は
eval-set cap 後の件数を 'gt' に持つ (greyscale: doc 2490/1500 vs 総数 3294/4859)。
backfill がその doc の 'gt' をそのまま再登録すると、baseline が総数に永遠に
一致せず、ゲートは毎サイクル真:

  dispatch → runner gt-unchanged → backfill (stale gt) → auto_review validated
  → promote → GC/再入 → dispatch …

greyscale は 24h に 37 回 candidate→training を回り、毎回 auto-promote まで
完走して repo に commit していた (`auto-promote tagger/patches/greyscale/v2.json`
の連発)。歯止め (MAX_TRAINING_REOPENS は GC 専用 / auto-hold は新 metrics 形式で
不発 / AttemptLedger は event id 更新で毎回リセット) はどれも効かない。

backfill は prev_gt == cur_gt の分岐でしか呼ばれないので、cur_gt (総数) は
前回測定時点の総数そのものでもある — これを baseline に書くのは嘘ではない。
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from tagger import vnext_runner as vr  # noqa: E402

STALE_DOC = {
    "gt": {"positive": 2490, "negative": 1500},  # eval-set cap 後 (旧世代)
    "classification": {"verdict": "deployable", "binding": "x"},
    "verdict": "deployable",
    "artifact_sha256": "ab" * 32,
    "artifact_ref": "heads/gt_greyscale.safetensors",
}


def _capture(monkeypatch):
    sent: list[tuple[str, dict]] = []
    monkeypatch.setattr(vr, "kura_post",
                        lambda path, body: sent.append((path, body)) or {"id": 7})
    monkeypatch.setattr(vr, "current_stage", lambda tag: ("candidate", 4242))
    monkeypatch.setattr(vr, "_upload_artifact", lambda sha, ref: None)
    return sent


def test_backfill_submits_the_store_total_not_the_docs_capped_gt(monkeypatch):
    """成長ゲートの baseline = cur_gt (総数)。doc の cap 済み 'gt' は使わない。"""
    sent = _capture(monkeypatch)

    vr._try_backfill_registration(
        "greyscale", STALE_DOC, Path("/nonexistent"),
        {"positive": 3294, "negative": 4859})

    path, body = sent[0]
    assert path == "/tagger/registration-request"
    assert body["gt"] == {"positive": 3294, "negative": 4859}, \
        "stale な doc gt を再登録すると成長ゲートが永遠に真のまま (24h/37 回の実測)"
    assert body["lifecycle_event_id"] == 4242


def test_backfill_does_not_mutate_the_measurement_doc(monkeypatch):
    """prev_doc は <tag>.measurement.json の内容 — 上書きは新しい嘘を作る。"""
    _capture(monkeypatch)
    doc = dict(STALE_DOC)

    vr._try_backfill_registration(
        "greyscale", doc, Path("/nonexistent"),
        {"positive": 3294, "negative": 4859})

    assert doc["gt"] == {"positive": 2490, "negative": 1500}
