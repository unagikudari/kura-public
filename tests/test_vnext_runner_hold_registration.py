"""測定に到達できなかった HOLD も登録する (INV-R3 / 成長ゲートの baseline)。

**なぜこのテストが要るか** (2026-08-04 の実測):
reconciler の成長ゲートは `_last_registration_positive()` =「直近 registration の
gt->>'positive'」を baseline に読む。登録が 1 件も無いと baseline が None になり、
**毎サイクル同じタグが再 dispatch される**。reconciler 自身の docstring がこの
失敗形を名指ししている ("a tag rejected for insufficient GT would get redispatched
every single cycle it stays candidate")。

実害: deepthroat は登録 0 件のまま 1 日 8 回の reopen 枠 (MAX_TRAINING_REOPENS) を
使い切り、14 タグが training に滞留した。**登録を出さないことが暴走を招いていた。**
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from tagger import vnext_runner as vr  # noqa: E402


def test_hold_registration_carries_the_gt_counts_the_growth_gate_reads(monkeypatch):
    """成長ゲートが読む契約キーは `gt.positive` / `gt.negative` の int。"""
    sent: list[dict] = []
    monkeypatch.setattr(vr, "kura_post", lambda path, body: sent.append((path, body)) or {"id": 99})

    out = vr.submit_hold_registration(
        "deepthroat", {"positive": 11, "negative": 33},
        reason="insufficient_gt_for_cv", detail="外側 fold 0 が pos=0")

    assert out["id"] == 99
    path, body = sent[0]
    assert path == "/tagger/registration-request"
    assert body["gt"] == {"positive": 11, "negative": 33}, "baseline はここから読まれる"
    assert body["tag"] == "deepthroat"


def test_hold_registration_is_marked_hold_not_deployable(monkeypatch):
    """4 クラスのうち「保留」であること — 誤って配備側へ流れない。"""
    sent: list[dict] = []
    monkeypatch.setattr(vr, "kura_post", lambda path, body: sent.append(body) or {"id": 1})

    vr.submit_hold_registration("t", {"positive": 3, "negative": 9},
                                reason="insufficient_gt_for_cv", detail="x")
    m = sent[0]["metrics"]
    assert m["disposition"] == "hold"
    assert m["classification"]["verdict"] == "hold"
    assert m["classification"]["binding"], "何が足りないかを必ず添える"


def test_hold_registration_artifact_fields_cannot_be_mistaken_for_a_hash(monkeypatch):
    """artifact_ref / sha256 は NOT NULL。**実体が無いと読んで分かる**値を入れる。

    ここに本物らしい 64 桁を入れると、後段が検証できるつもりで検証できない。
    """
    sent: list[dict] = []
    monkeypatch.setattr(vr, "kura_post", lambda path, body: sent.append(body) or {"id": 1})

    vr.submit_hold_registration("t", {"positive": 1, "negative": 1},
                                reason="insufficient_gt_for_cv", detail="x")
    b = sent[0]
    for field in ("artifact_ref", "artifact_sha256"):
        v = b[field]
        assert "none" in v or "no artifact" in v, f"{field}={v!r}"
        assert not v.strip().isalnum(), "16 進ハッシュと見分けが付く形であること"


def test_hold_registration_refuses_gt_without_the_contract_keys(monkeypatch):
    """gt の契約キーが欠けたら落とす — baseline が 0 と読まれると成長ゲートが壊れる。"""
    monkeypatch.setattr(vr, "kura_post", lambda path, body: {"id": 1})
    with pytest.raises(ValueError, match="positive"):
        vr.submit_hold_registration("t", {"pos": 3, "neg": 9},
                                    reason="insufficient_gt_for_cv", detail="x")


def test_normal_registration_still_refuses_a_measurement_without_classification(monkeypatch):
    """HOLD 用の入口を足しても、通常経路の fail-closed は緩めない。"""
    monkeypatch.setattr(vr, "kura_post", lambda path, body: {"id": 1})
    with pytest.raises(ValueError, match="classification"):
        vr.submit_registration("t", {"gt": {"positive": 1, "negative": 1}}, "ref", "sha")


def test_manifest_error_block_does_not_nest_an_except(monkeypatch):
    """ManifestError の処理に `except` を入れ子にしない。

    2026-08-04: 入れ子にしたら test_vnext_runner_gate の走査が境界を誤って
    落ちた。読む側 (人も走査も) が「どこまでが ManifestError の処理か」を
    見失う形は避ける — 登録の try/except は _try_hold_registration に置く。
    """
    import inspect

    src = inspect.getsource(vr.process_task)
    block = src.split("except ManifestError")[1].split("except Exception")[0]
    assert "_try_hold_registration" in block, "登録は helper 経由で呼ぶ"
    assert "binding" in block, "どの floor 項が binding かは同じブロックに残す"


def test_registration_failure_does_not_stop_the_caller(monkeypatch):
    """登録に失敗しても task を閉じられるよう、helper は None を返して続行する。"""
    def _boom(path, body):
        raise RuntimeError("broker 落ちてる")

    monkeypatch.setattr(vr, "kura_post", _boom)
    logged: list = []
    monkeypatch.setattr(vr, "log", lambda ev, **kw: logged.append((ev, kw)))

    assert vr._try_hold_registration("t", {"positive": 1, "negative": 1},
                                     reason="r", detail="d") is None
    assert any(ev == "registration.failed" for ev, _ in logged), "黙らないこと"


def test_missing_gt_counts_are_reported_not_guessed(monkeypatch):
    """GT が読めないときに 0 と書かない — baseline が 0 だと成長ゲートが壊れる。"""
    logged: list = []
    monkeypatch.setattr(vr, "log", lambda ev, **kw: logged.append((ev, kw)))
    monkeypatch.setattr(vr, "kura_post", lambda p, b: pytest.fail("登録してはいけない"))

    assert vr._try_hold_registration("t", None, reason="r", detail="d") is None
    assert any(ev == "registration.skipped" for ev, _ in logged)
