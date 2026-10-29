"""nodec_autotag_worker が content head を適用することを固定する。

このワーカーは 2026-08-04 まで WD14 専用で content head を一切 load していなかった。
実測された帰結: release 9 の audit 334,572 件のうち **99.4% をこのワーカーが処理**
しており、**v2 head でタグ付けされた post は primary の 584 件だけ**だった。head を
更新しても、コーパスのほぼ全体はその head を一度も通らない。

適用規則は 2026-08-05 に **policy 由来**へ変わった (user:「マジックナンバーや
特殊変数で管理するな。スキーマを整えろ」)。旧版は `_HEAD_SPECS` の 3 タグ・
全 head 共通の `GREYOWL_ADD_THRESH = 0.5`・`"penis"` というタグ名のゲートを
持っており、**昇格しても書き足すまで適用されなかった** (monochrome /
greyscale が patch まで到達して永久に効かなかったのがこれ)。

いま固定するのは: ADD-ONLY / **policy の閾値** / **policy の gate_tags** /
既存タグを上書きしない / 閾値の無い head は当てない。
"""
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "tagger"))

from tagger import nodec_autotag_worker as w  # noqa: E402


class _Head:
    img_size = 448

    def __init__(self, prob):
        self.prob = prob


@pytest.fixture(autouse=True)
def _stub_prob(monkeypatch):
    monkeypatch.setattr(w, "_content_head_prob", lambda img, m: m.prob)


# policy 由来の head 束を作る。**タグ名の表はテスト側にも置かない** —
# 「どの head を積むか」は policy が決めるので、テストも policy を渡す。
_TAGS = ("small_penis", "dark_penis", "dark_perineum")


def _heads(*, thr=0.5, gates=None, **probs):
    d = {}
    for t in _TAGS:
        d[t] = _Head(probs[t]) if t in probs else None
        d[f"__policy__{t}"] = {"threshold": thr, "apply_mode": "add_only",
                               "gate_tags": list(gates or []),
                               "operating_point": "test@fixed"}
    for t, v in probs.items():
        if t not in _TAGS:
            d[t] = _Head(v)
            d[f"__policy__{t}"] = {"threshold": thr, "apply_mode": "add_only",
                                   "gate_tags": list(gates or []),
                                   "operating_point": "test@fixed"}
    return d


def test_gate_comes_from_the_policy_not_a_tag_name_in_code():
    """**gate は policy の `gate_tags`。** 旧版は `"penis" not in general` という
    タグ名の前提をコードに持っており、無関係なタグの head が永久に発火しなかった。"""
    heads = _heads(gates=["penis"], smegma=0.99)
    assert w.apply_content_heads(["cat"], None, heads) == ["cat"], "gate が閉じるべき"
    assert "smegma" in w.apply_content_heads(["penis"], None, heads), "gate が開くべき"


def test_a_head_without_gates_fires_anywhere():
    """gate_tags が空 = 無条件。monochrome のような全体タグがここに入る。"""
    assert "monochrome" in w.apply_content_heads(["cat"], None, _heads(monochrome=0.9))


def test_adds_tag_above_threshold():
    out = w.apply_content_heads(["penis"], None, _heads(small_penis=0.9))
    assert out == ["penis", "small_penis"]


def test_the_threshold_comes_from_the_policy():
    """**測定が選んだ閾値をそのまま使う。** 全 head 共通の既定値は持たない。"""
    assert "small_penis" in w.apply_content_heads(
        ["penis"], None, _heads(thr=0.3, small_penis=0.35))
    assert "small_penis" not in w.apply_content_heads(
        ["penis"], None, _heads(thr=0.9, small_penis=0.35))


def test_a_head_without_a_threshold_is_not_applied():
    """既定値で埋めない (fail-closed) — 測っていない閾値を配備に混ぜない。"""
    h = _heads(small_penis=0.99)
    h["__policy__small_penis"] = {"apply_mode": "add_only", "gate_tags": []}
    assert w.apply_content_heads(["penis"], None, h) == ["penis"]


def test_does_not_add_below_threshold():
    out = w.apply_content_heads(["penis"], None, _heads(small_penis=0.49))
    assert out == ["penis"]


def test_threshold_boundary_is_inclusive():
    """`prob >= threshold`。境界の向きを固定する。"""
    assert "small_penis" in w.apply_content_heads(
        ["penis"], None, _heads(thr=0.5, small_penis=0.5))


def test_is_add_only_never_removes():
    """削除安全性を校正するデータが無いので、head は削除に使わない。"""
    out = w.apply_content_heads(["penis", "smegma"], None, _heads(smegma=0.01))
    assert out == ["penis", "smegma"], "低い確率でも既存タグを消してはいけない"


def test_does_not_override_existing_tag():
    out = w.apply_content_heads(["penis", "smegma"], None, _heads(smegma=0.99))
    assert out.count("smegma") == 1


def test_missing_head_is_skipped_not_fatal():
    out = w.apply_content_heads(["penis"], None, _heads(small_penis=0.9))
    assert out == ["penis", "small_penis"]  # 他の 3 つは None


def test_no_heads_at_all_is_harmless():
    assert w.apply_content_heads(["penis"], None, None) == ["penis"]


def test_inference_failure_does_not_lose_other_heads(monkeypatch):
    """1 つの head が落ちても、残りの head とタグ付け全体は生き残る。"""
    def flaky(img, m):
        if m.prob == 0.9:
            raise RuntimeError("boom")
        return m.prob

    monkeypatch.setattr(w, "_content_head_prob", flaky)
    out = w.apply_content_heads(["penis"], None, _heads(small_penis=0.9, dark_penis=0.8))
    assert out == ["penis", "dark_penis"]


# ── provenance ───────────────────────────────────────────────────────────

def test_source_records_which_heads_were_actually_loaded():
    """固定文字列 `nodec-autotag-rN` では「head が当たったか」を後から判定できない
    — それが 2026-08-04 まで起きていた状態そのもの。"""
    h = _heads(small_penis=0.9)
    h["__sha__small_penis"] = "77ad0165809d"
    src = w.ensemble_source_id(12, h)
    assert src.startswith("nodec-autotag-r12;")
    assert "small_penis=77ad0165809d" in src
    assert "dark_penis=absent" in src


def test_source_marks_absent_heads_explicitly():
    """「無かった」を「書かない」で表さない — 黙っていると積んだのか判別できない。"""
    src = w.ensemble_source_id(12, _heads())
    for tag in _TAGS:
        assert f"{tag}=absent" in src


def test_source_without_release_id():
    assert w.ensemble_source_id(None, None).startswith("nodec-autotag;")


def test_source_is_stable_for_the_same_ensemble():
    h = _heads(small_penis=0.9)
    h["__sha__small_penis"] = "77ad0165809d"
    assert w.ensemble_source_id(12, h) == w.ensemble_source_id(12, dict(h))


# ── skin model (smegma-foreskin-phimosis) ────────────────────────────────
# ContentHeadModel ではなく skin/smegma/precum の 3 出力を持つ別アーキテクチャ。
# content head として load しようとすると state_dict が合わずに落ちる
# (2026-08-04 nodec で実測)。release がある限り、このワーカーが供給するのは
# foreskin だけ — smegma/precum/phimosis は release の patched 行が所有する。

def test_smegma_is_loaded_by_the_skin_model_not_as_a_content_head():
    """smegma は ContentHeadModel ではなく skin/smegma/precum の 3 出力を持つ
    別アーキテクチャ。content head として load すると state_dict が合わずに落ちる
    (2026-08-04 nodec で実測)。

    **head の表がコードから消えた今、これは「policy に smegma を入れるな」では
    なく「別の loader が担当する」という事実**なので、skin 側の窓口が
    生きていることで固定する。policy に smegma の content_head が現れたら
    load 時に失敗して `missing` に落ちる (黙って壊れない)。
    """
    assert hasattr(w, "load_skin_model")
    assert hasattr(w, "skin_verdict")


def test_skin_verdict_supplies_foreskin():
    """**本物の判定関数**を呼ぶ (stub ではない — stub を検証しても意味が無い)。"""
    assert w.skin_verdict(["penis"], "foreskin") == ["penis", "foreskin"]


def test_skin_verdict_does_not_supply_other_classes():
    """release がある限り smegma/precum/phimosis は release の patched 行が所有。
    同じタグに補正器を 2 つ走らせない。"""
    for cls in ("phimosis", "bare", None):
        assert w.skin_verdict(["penis"], cls) == ["penis"], cls


def test_skin_verdict_does_not_duplicate():
    assert w.skin_verdict(["penis", "foreskin"], "foreskin") == ["penis", "foreskin"]


def test_apply_skin_model_gates_without_touching_torch():
    """ゲートが閉じているときは model に一切触れない (torch を import しない)。"""
    class Boom:
        def __call__(self, *a):
            raise AssertionError("ゲートが閉じているのに推論した")

    assert w.apply_skin_model(["cat"], None, Boom()) == ["cat"]
    assert w.apply_skin_model(["penis", "foreskin"], None, Boom()) == ["penis", "foreskin"]
    assert w.apply_skin_model(["penis"], None, None) == ["penis"]


def test_source_records_skin_model(monkeypatch):
    monkeypatch.setattr(w, "SKIN_MODEL_SHA", "0c2b5be1e6d8")
    assert "skin=0c2b5be1e6d8" in w.ensemble_source_id(12, None)
    monkeypatch.setattr(w, "SKIN_MODEL_SHA", None)
    assert "skin=absent" in w.ensemble_source_id(12, None)
