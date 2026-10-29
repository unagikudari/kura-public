"""絶対 floor が「率 OR 規模」であることを固定する。

■ なぜ変えたか (user 2026-08-05「バンドエイドはしない。決定論的に修正が回るようにせよ」)

`min_p_deploy_lift` (既定 10 倍) は **率** しか見ないので、規模の違う効果を
区別できない。実測 2026-08-05:

    smegma         lift 5.76x / 差し引き **163,583 件** 減 → 率で落ちる
    chastity_cage  lift 8.07x / 差し引き      **358 件** 減 → 率では smegma より上

率だけで判定すると「16 万件減らす head」が落ちて「358 件の head」が残りかける。
そして落ちた側は `cleanup-capable` として **owner の手作業**に回っていた —
機械が決められるものを人に投げていた。

`net_fp_removed_deploy` は **増える誤検出も勘定に入れた差し引き**なので、
これを floor に据えれば「率は届かないが規模で明らかに得」を機械が決められる。

規模経路にも `negligible_lift` は効く — 「ほぼ改善しないが母集団が巨大なので
net だけ大きい」を通さないため。
"""
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from tagger.vnext_class import (  # noqa: E402
    CLEANUP_CAPABLE, DEPLOYABLE, OperatingPoint, classify, net_fp_removed_deploy,
)

# 差し引き 174,200 件相当 (N=200,000 / pi=0.10 / R=0.80 / FPR=0.01)
BIG = dict(pi=0.10, n_flagged=200_000)
# 差し引き 871 件相当 — 同じ動作点で母集団だけ小さい
SMALL = dict(pi=0.10, n_flagged=1_000)


def _op(lift):
    return OperatingPoint(arm="feat1024", rule="max_f1", recall=0.80, fpr=0.01, lift=lift)


def _classify(lift, pop, floor=10_000.0):
    return classify(any_arm_above_chance=True, absolute_pass=False,
                    legit_points=[_op(lift)], binding_terms=["min_p_deploy_lift"],
                    min_net_fp_removed=floor, **pop)


def test_scale_floor_makes_a_low_lift_high_net_head_deployable():
    """**本題**: smegma 型 — 率は届かないが規模で明らかに得。"""
    r = _classify(5.76, BIG)
    assert r.verdict == DEPLOYABLE
    assert "規模の floor を満たす" in r.why
    assert f"{r.net_fp_removed_deploy:,.0f}" in r.why, "何件減るかが理由に無い"


def test_small_net_still_deploys_no_owner_queue():
    """**2026-08-05 に仕様が変わった** (user「掃除可なら判断はいらない」)。

    初版は chastity_cage 型 (率は高いが規模が小さい) を owner 判断へ回して
    いた。しかし**回された側に増える判断材料は無い** — 偶然を超え、正当な
    動作点があり、差し引きが正で、改善が negligible を超えることは既に
    確かめてある。残るのは「一律の閾値に届かない」だけで、それは効果の
    有無ではなく閾値の粗さの話だった。人を挟むと滞留するだけになる
    (実測: smegma / fellatio / chastity_cage が滞留していた)。
    """
    assert _classify(8.07, SMALL).verdict == DEPLOYABLE


def test_scale_path_still_respects_the_negligible_lift_bar():
    """母集団が巨大なだけの marginal な head を通さない。"""
    r = _classify(1.10, BIG)          # negligible_lift 既定 1.25 を下回る
    assert r.verdict != DEPLOYABLE


def test_scale_floor_absent_still_deploys_but_via_the_other_path():
    """config に規模 floor が無くても、差し引きが正で改善があるなら配備する。
    規模 floor は「率で落ちたものを拾う」ためのもので、**配備しない理由には
    ならない** (判断を挟まないので)。"""
    r = classify(any_arm_above_chance=True, absolute_pass=False,
                 legit_points=[_op(5.76)], min_net_fp_removed=None, **BIG)
    assert r.verdict == DEPLOYABLE


def test_rate_floor_alone_still_deploys():
    """既存の率経路は変わらない。"""
    r = classify(any_arm_above_chance=True, absolute_pass=True,
                 legit_points=[_op(12.0)], min_net_fp_removed=10_000.0, **BIG)
    assert r.verdict == DEPLOYABLE
    assert "絶対 floor を満たし" in r.why


def test_baseline_only_is_never_deployable_even_at_huge_scale():
    """自前 head が 1 つも届いていないケースは、規模がいくら大きくても
    「head の開発失敗」であって配備可ではない (レビュー指摘 3、fellatio が実例)。"""
    r = classify(any_arm_above_chance=True, absolute_pass=True,
                 absolute_pass_is_baseline_only=True,
                 legit_points=[_op(1.78)], min_net_fp_removed=10_000.0, **BIG)
    assert r.verdict == CLEANUP_CAPABLE
    assert "自前 head は 1 つも届いていない" in r.why


def test_scale_path_does_not_override_net_le_zero():
    """差し引きが負なら、規模 floor 以前に「不要」で落ちる。"""
    r = classify(any_arm_above_chance=True, absolute_pass=False,
                 legit_points=[OperatingPoint(arm="a", rule="r", recall=0.05,
                                              fpr=0.90, lift=5.0)],
                 pi=0.9, n_flagged=200_000, min_net_fp_removed=1.0)
    assert r.verdict != DEPLOYABLE


@pytest.mark.parametrize("floor", [100_000.0, 1_000_000.0])
def test_scale_floor_wording_reflects_which_path_fired(floor):
    """規模 floor を満たしたかどうかが理由文に出ること。判定はどちらも
    配備可だが、**どの経路で通ったかは記録に残す** (後から検証できるように)。"""
    r = _classify(5.76, BIG, floor=floor)
    assert r.verdict == DEPLOYABLE
    assert ("規模の floor を満たす" in r.why) == (r.net_fp_removed_deploy >= floor)


# ── config からの到達 ─────────────────────────────────────────────────────
# 「config に行を入れた」と「実際に判定へ届く」は別。2026-08-05 にまさにこれを
# 踏んだ: migration で `min_net_fp_removed` を投入し unit test も通っていたのに、
# `vnext_arms.FLOOR_KEYS` に載せ忘れていたため `load_floor()` が値を捨てており、
# **本番では規模経路が一度も働かなかった**。

def test_scale_floor_key_is_in_floor_keys():
    """FLOOR_KEYS に無いキーは load_floor が捨てる — config にあっても届かない。"""
    from tagger.vnext_arms import FLOOR_KEYS
    assert "min_net_fp_removed" in FLOOR_KEYS


def test_every_config_default_that_classify_reads_is_loadable():
    """classify が読む floor キーは、すべて FLOOR_KEYS 経由で届くこと。
    片方に足してもう片方を忘れる形を機械で塞ぐ。"""
    from api.tagger_core.config import DEFAULTS
    from tagger.vnext_arms import FLOOR_KEYS
    for k in ("min_p_deploy_lift", "min_net_fp_removed"):
        assert k in DEFAULTS, f"{k} が config.DEFAULTS に無い"
        assert k in FLOOR_KEYS, f"{k} が FLOOR_KEYS に無い (load_floor が捨てる)"


def test_arms_passes_the_scale_floor_to_classify():
    """_classify が floor から min_net_fp_removed を取り出して渡していること。"""
    src = (REPO / "tagger" / "vnext_arms.py").read_text()
    body = src[src.index("def _classify("):]
    body = body[:body.index("\ndef ", 10)]
    assert "min_net_fp_removed" in body, "classify へ渡していない"


def test_verdict_floor_carries_the_scale_floor_to_classify():
    """`verdict["floor"]` は明示列挙なので、そこに載せないと `_classify` に届かない。

    2026-08-05 実測でここを踏んだ: `load_floor()` は min_net_fp_removed を返して
    いたのに `verdict["floor"]` に載せていなかったため、smegma が
    **lift 7.61x / net 170,720 件** (規模 floor 10,000 を満たす) で測れても
    cleanup-capable のままだった。**「読み込めた」と「判定に届く」はまた別**。
    """
    src = (REPO / "tagger" / "vnext_arms.py").read_text()
    body = src[src.index('out["verdict"] = {'):]
    body = body[:body.index("\n    out[", 10)] if "\n    out[" in body[10:] else body[:4000]
    assert "min_net_fp_removed" in body, "verdict['floor'] に載っていない"


def test_scale_floor_is_omitted_when_absent_from_config():
    """DB に行が無ければキー自体を出さない — コードに既定値を焼かない。
    classify は None を受けて規模判定をしない = 今日までの挙動に戻るだけ。"""
    src = (REPO / "tagger" / "vnext_arms.py").read_text()
    assert 'if floor.get("min_net_fp_removed") is not None else {}' in src
