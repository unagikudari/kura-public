"""process_task の**全終了経路**を実際に走らせ、ループを止める契約を確かめる。

**なぜこのファイルが要るか** (2026-08-04):
既存の runner テストは全部 `inspect.getsource` によるソース文字列の走査で、
**process_task を一度も実行していなかった**。だから「insufficient_gt の経路が
registration を出さない」= 成長ゲートの baseline が生まれず毎サイクル再 dispatch
される、という欠陥が本番で初めて出た。deepthroat は登録 0 件のまま 1 日 8 回の
reopen 枠を使い切り、14 タグが training に滞留していた。

**契約**: どの終了経路も、次のどちらかを必ず満たす。
  (a) registration を出す  → 成長ゲートに baseline ができ、GT が育つまで再投入されない
  (b) task を閉じる        → queue から消え、毎 tick 拾っては何もしない空回りにならない
両方欠けた経路は「タグが前に進まないのに、また来る」= ループになる。

shadow (`--no-registration`) ではこの契約は**原理的に検証できない** — (a) を
止めるのが shadow の定義だから。**出力を止める shadow はフィードバックループの
検証に使えない**、というのがこの事故の一般化。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from tagger import vnext_runner as vr  # noqa: E402


# 測定の差し替え口 (process_task が関数内 import するため module 属性を経由する)
_MEASURE = [lambda tag: {}]


class _Ledger:
    """判定済みかどうかだけを持つ最小の台帳。"""

    def __init__(self, attempt=True):
        self._attempt = attempt
        self.attempts: list = []
        self.done: list = []

    def should_attempt(self, key):
        return self._attempt

    def is_dead(self, key):
        return False

    def record_attempt(self, key, outcome, detail=None):
        self.attempts.append((outcome, detail))

    def mark_done(self, key):
        self.done.append(True)


@pytest.fixture
def harness(monkeypatch, tmp_path):
    """process_task を I/O 無しで走らせ、broker / kura への書き込みを記録する。"""
    calls = {"complete": [], "registration": [], "broker": []}

    def _broker(method, path, body=None):
        calls["broker"].append((method, path, body))
        if path.endswith("/complete"):
            calls["complete"].append(body)
        return {}

    def _kura_post(path, body):
        calls["registration"].append((path, body))
        return {"id": 123}

    monkeypatch.setattr(vr, "broker", _broker)
    monkeypatch.setattr(vr, "kura_post", _kura_post)
    monkeypatch.setattr(vr, "STATE_DIR", tmp_path)
    monkeypatch.setattr(vr, "current_stage", lambda tag: ("training", 42))
    monkeypatch.setattr(vr, "_current_gt", lambda tag: {"positive": 11, "negative": 33})
    monkeypatch.setattr(vr, "log", lambda ev, **kw: None)
    monkeypatch.setattr(vr, "structural_context", lambda tag: {})
    monkeypatch.setattr(vr.vnext_escalate, "structural_preconditions", lambda ctx: [])
    # process_task は `from tagger import vnext_arms` を**関数の中で**行うので、
    # runner の属性ではなく module 本体を差し替える。
    import tagger.vnext_arms as arms
    monkeypatch.setattr(arms, "measure_tag", lambda tag: _MEASURE[0](tag))
    calls["escalated"] = []
    monkeypatch.setattr(vr, "_deliver_escalations",
                        lambda tag, esc, *a, **k: calls["escalated"].append((tag, esc)),
                        raising=False)
    return calls


def _run(ledger=None, deadline=None):
    import time
    return vr.process_task({"id": "t1", "_tag": "deepthroat"},
                           ledger or _Ledger(), deadline or (time.time() + 600),
                           dry_run=False)


# ── 契約: 登録するか、task を閉じるか、どちらかは必ずやる ────────────────
def test_insufficient_gt_registers_so_the_growth_gate_gets_a_baseline(harness, monkeypatch):
    """測定に到達できない HOLD。**ここが本番で欠けていた経路。**"""
    from tagger.vnext_manifest import ManifestError

    def _boom(tag):
        raise ManifestError("outer fold 0 has pos=0/neg=8 < floor 1/1")

    _MEASURE[0] = _boom
    out = _run()

    assert out == "hold(insufficient_gt)"
    assert harness["registration"], "登録が無いと baseline が生まれず毎サイクル再投入される"
    _path, body = harness["registration"][0]
    assert body["gt"] == {"positive": 11, "negative": 33}, "成長ゲートが読む値"
    assert harness["complete"], "task も閉じる"


def test_structural_precondition_block_does_not_leave_the_tag_looping(harness, monkeypatch):
    """E0 (構造前提) で止まる経路も、登録か task 完了のどちらかは要る。

    どちらも無いと、タグは前に進まないのに次の tick でまた来る = ループ。
    insufficient_gt と同じ形の欠陥がここに無いかを実際に走らせて確かめる。
    """
    from tagger import vnext_escalate

    blocking = [vnext_escalate.Escalation("E0", True, "release 行が無い")]
    monkeypatch.setattr(vr.vnext_escalate, "structural_preconditions",
                        lambda ctx: blocking)
    out = _run()

    assert out == "escalated"
    assert harness["complete"], (
        "登録も task 完了も無い経路は、タグが進まないまま毎 tick 再来する")
    # E0 は GT の問題ではないので登録は出さない — GT baseline を書くと
    # 「GT が育てば直る」という誤った含意になる。直し方は escalation 側にある。
    assert not harness["registration"], "構造前提の欠落を GT の不足として記録しない"
    body = harness["complete"][0]["result_payload"]
    assert body["reason"] == "structural_precondition"
    assert body["escalations"] == ["E0"], "どの述語で止まったかを残す"
    assert harness["escalated"], "log だけで終わらせず owner へ届ける"


def test_already_judged_closes_the_task(harness):
    """判定済みは登録しない (前回の判定で出ている) が、task は必ず閉じる。"""
    out = _run(ledger=_Ledger(attempt=False))
    assert out == "skipped"
    assert harness["complete"], "閉じないと queue に残り毎 tick 空回りする"


def test_wrong_stage_is_a_noop_that_still_closes_the_task(harness, monkeypatch):
    """kura の stage が training でなければ触らない — ただし task は閉じる。"""
    monkeypatch.setattr(vr, "current_stage", lambda tag: ("approved", 42))
    out = _run()
    assert out == "noop"
    assert harness["complete"], "他人の仕事でも、拾った task は閉じる"


# ── 5 本目の終了経路: gt-unchanged ────────────────────────────────────
# 2026-08-04: 契約テストを書いたとき 4 経路しか覆っておらず、この経路が漏れた。
# 実際 06:17 に chastity_cage がここを通り、登録を出さずに帰っている。
# **経路を数え落とすと契約は守られない**ので、5 本目を明示的に固定する。
def _write_prev(tmp_path, *, gt, classification=True, deployable_policy=True):
    """前回の測定結果と GT スタンプを置く (skip の前提を作る)。

    **`deployable_policy` が skip の前提**: 2026-08-06 実測で、配備 policy を
    作れない測定 (動作点の閾値が無い世代) を skip の根拠にすると、GC が
    「policy が無い」と戻すたびにここで省略され、**一度も測り直さない無限
    ループ**になった。skip してよいのは「前回結果が使える」ときだけ。
    """
    doc = {"schema": "vnext-measurement/2", "tag": "deepthroat",
           "verdict": {"verdict_absolute": "HOLD", "verdict_lift": "HOLD"},
           "gt": {"positive": 11, "negative": 33}}
    if deployable_policy:
        # 実測から切り出した形 (動作点と閾値を持つ)
        doc["classification"] = {"verdict": "deployable", "why": "x",
                                 "net_fp_removed_deploy": 1000.0, "lift": 9.9,
                                 "at": "feat1024@max_f1", "binding": []}
        # **配備可の測定は head を作っている。** 実測 2026-08-08: 配備可 8 件の
        # うち 6 件が artifact_sha256 を持ち、持たない 2 件 (anilingus /
        # dark_labia_majora) は**まさに詰まっていた側**だった。artifact を
        # 書かない fixture は「正常な skip」ではなく壊れた状態を模していて、
        # そのまま green にすると詰まりを仕様として固定してしまう。
        doc["artifact_sha256"] = "a" * 64
        doc["artifact_ref"] = str(tmp_path / "deepthroat-head.pt")
        doc["B_veto_ceiling"] = {"feat1024": {"nested": {"max_f1": {
            "P": 0.9, "R": 0.9, "FPR": 0.01, "thr": 0.35}}}}
        (tmp_path / "deepthroat.measurement.json").write_text(json.dumps(doc))
        (tmp_path / "deepthroat.gt.json").write_text(json.dumps(gt))
        return
    if classification:
        doc["classification"] = {"verdict": "hold", "why": "x",
                                 "net_fp_removed_deploy": None, "lift": None,
                                 "at": None, "binding": []}
    (tmp_path / "deepthroat.measurement.json").write_text(json.dumps(doc))
    (tmp_path / "deepthroat.gt.json").write_text(json.dumps(gt))


def test_gt_unchanged_skips_when_a_baseline_already_exists(harness, monkeypatch, tmp_path):
    """通常形: 直近登録の GT が現在と一致 → 再測定も再登録もしない。"""
    _write_prev(tmp_path, gt={"positive": 11, "negative": 33})
    monkeypatch.setattr(vr, "_last_registration_gt",
                        lambda tag: {"positive": 11, "negative": 33})
    out = _run()

    assert out.endswith("(gt-unchanged)")
    assert not harness["registration"], "何も変わっていないなら登録も増やさない"
    assert harness["complete"], "task は閉じる"


def test_gt_unchanged_backfills_the_missing_baseline(harness, monkeypatch, tmp_path):
    """**穴だった形**: 登録が 1 件も無いまま skip すると毎サイクル戻ってくる。

    shadow (--no-registration) 中の測定は登録を残していないので、この状態は
    実在する。GPU を使わずに前回結果をそのまま登録して baseline を作る。
    """
    _write_prev(tmp_path, gt={"positive": 11, "negative": 33})
    monkeypatch.setattr(vr, "_last_registration_gt", lambda tag: None)
    monkeypatch.setattr(vr, "_sha256_of", lambda p: "0" * 64)
    out = _run()

    assert out.endswith("(gt-unchanged)"), "再測定はしない (skip の目的は保つ)"
    assert harness["registration"], "baseline を作らないと再 dispatch が止まらない"
    _path, body = harness["registration"][0]
    assert body["gt"] == {"positive": 11, "negative": 33}


def test_gt_unchanged_remeasures_when_the_stored_result_cannot_be_registered(
        harness, monkeypatch, tmp_path):
    """登録できない結果は skip の根拠にならない → 測り直す。

    旧スキーマ (classification 無し) がここに来る。黙って skip すると
    baseline は永遠に生まれない。
    """
    _write_prev(tmp_path, gt={"positive": 11, "negative": 33},
                classification=False, deployable_policy=False)
    monkeypatch.setattr(vr, "_last_registration_gt", lambda tag: None)
    _MEASURE[0] = lambda tag: (_ for _ in ()).throw(
        __import__("tagger.vnext_manifest", fromlist=["ManifestError"]).ManifestError("stub"))
    out = _run()

    assert out == "hold(insufficient_gt)", "skip せず測定へ落ちる"


def test_an_unreadable_baseline_is_not_treated_as_present(harness, monkeypatch, tmp_path):
    """baseline が読めないとき、「ある」と仮定して skip しない (fail-closed)。"""
    _write_prev(tmp_path, gt={"positive": 11, "negative": 33})
    monkeypatch.setattr(vr, "_last_registration_gt", lambda tag: None)  # 読めない = None
    monkeypatch.setattr(vr, "_sha256_of", lambda p: "0" * 64)
    _run()
    assert harness["registration"], "None を「一致」と読まない"


def test_gt_unchanged_remeasures_when_no_policy_can_be_derived(harness, monkeypatch, tmp_path):
    """**配備 policy を作れない測定は skip の根拠にならない。**

    2026-08-06 実測: greyscale は GC が「policy が無い」と candidate へ戻す
    たびに、ここで gt-unchanged と判定され **測定を省略して古い測定を再登録**
    していた。その測定は動作点の閾値 (thr) を持たない世代のものなので policy は
    作れず、また GC が戻す — **一度も測り直さない無限ループ**。GPU も再 dispatch
    枠も消費し続ける。

    reconciler の成長ゲートには同じ条件を入れたが、runner 側の skip 経路が
    残っていた (今日 9 回目の「片方だけ直す」)。
    """
    _write_prev(tmp_path, gt={"positive": 11, "negative": 33},
                deployable_policy=False)
    monkeypatch.setattr(vr, "STATE_DIR", tmp_path)
    monkeypatch.setattr(vr, "_current_gt", lambda tag: {"positive": 11, "negative": 33})
    from tagger.vnext_manifest import ManifestError
    measured = []
    def remeasure(tag):
        measured.append(tag)
        raise ManifestError("outer fold 0 has pos=0/neg=8 < floor 1/1")
    _MEASURE[0] = remeasure
    out = _run()
    assert measured == ["deepthroat"]
    assert not out.endswith("(gt-unchanged)"), "policy を作れないのに skip している"


# ─────────────────────────────────────────────────────────────────────────
# 配備可と言いながら head を作れていない測定は skip の根拠にならない
#
# 実測 2026-08-08 (dark_labia_majora): classification=deployable /
# head_policy.derive() は policy を作れる / **だが artifact_sha256 も
# artifact_ref も None**。backfill が「測定 JSON 自身」を artifact として
# 登録し、0139 の門 (kind='content_head_bundle' 必須) が policy を止め、
# 配備できないので GC が reopen し、また skip する。8/6 の測定のまま
# lifecycle だけが 2 日間回っていた。
# ─────────────────────────────────────────────────────────────────────────

def test_a_deployable_verdict_without_an_artifact_forces_a_remeasure():
    """**本題**: 節約した先が行き止まりなら節約になっていない。"""
    import re
    src = (REPO / "tagger" / "vnext_runner.py").read_text() if "REPO" in globals() \
        else (Path(__file__).resolve().parent.parent / "tagger" / "vnext_runner.py").read_text()
    i = src.index('why="前回結果から配備 policy を作れない')
    tail = src[i:i + 1400]
    assert 'get("artifact_sha256")' in tail, "artifact の有無を見ていない"
    assert '== "deployable"' in tail, "配備可の測定に限っていない"
    assert "prev_gt = None" in tail, "skip を外していない"
