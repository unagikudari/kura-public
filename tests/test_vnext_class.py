"""4 クラス判定のテスト (plan tagger-vnext-deterministic-runner rev8)。

**判定の入力は実測から切り出す** (tests/fixtures/vnext_measurement_*.json)。
初版はここを自作 dict で書き、`capability_gain` を「偶然を超えた」の意味だと
誤読したまま 7 件すべて green になった。実データに当てた瞬間、lift 5.9 倍の
smegma に「判別できない、定義を見直せ」という**正反対の助言**を出しているのが
判明した。自作 fixture は自分の誤解をそのまま固定する。

固定するのは、2 値判定では起きる**逆転**そのもの:
  - dark_anus (π=0.819): 絶対 floor 通過 (旧 PASS) だが改善 1.04 倍 → **不要**
  - smegma (π=0.129, corpus 201,369 件): floor 未達 (旧 HOLD) だが 5.95 倍の
    分離能があり誤検出を差し引き 16 万件消す → **掃除可**
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from tagger.vnext_arms import _classify  # noqa: E402
from tagger.vnext_class import (CLEANUP_CAPABLE, DEPLOYABLE, HOLD,  # noqa: E402
                                NOT_NEEDED, OperatingPoint, classify,
                                net_fp_removed_deploy)

FIXTURES = Path(__file__).parent / "fixtures"
TAGS = ("dark_anus", "smegma", "fellatio", "chastity_cage")


def load(tag: str) -> dict:
    return json.loads((FIXTURES / f"vnext_measurement_{tag}.json").read_text())


# ── 実測に対する判定 (この機能が存在する理由そのもの) ────────────────────
def test_clean_tag_that_passes_the_floor_is_not_needed():
    """dark_anus: 元から 82% 正しい → floor は自動的に通るが得るものが無い。"""
    m = load("dark_anus")
    assert m["verdict"]["any_arm_meets_floor"] is True, "旧判定は PASS だった"
    k = _classify(m)
    assert k.verdict == NOT_NEEDED, k.why
    assert k.lift is not None and k.lift < 1.25


def test_dirty_tag_that_fails_the_floor_is_deployed_not_handed_to_a_human():
    """smegma: floor 未達 (旧 HOLD) だが 20 万件の母集団を大きく掃除できる。

    **2026-08-05 に owner 判断を廃止** (user「掃除可なら判断はいらない。削除しろ」)。
    ここに来た時点で、偶然を超えている / 正当な動作点がある / 差し引きが正 /
    改善が negligible を超える、は全部確かめ済み。残る差は「絶対 floor という
    一律の閾値に届かない」だけで、それは効果の有無ではなく**閾値の粗さ**の話
    だった (lift 5.76x で 163,583 件減らす head が落ち、lift 8.07x で 358 件の
    head が残る逆転が実測された)。回された側に増える判断材料は無く、実際
    smegma / fellatio / chastity_cage は掃除可のまま滞留した。
    """
    m = load("smegma")
    assert m["verdict"]["any_arm_meets_floor"] is False, "旧判定は HOLD だった"
    k = _classify(m)
    assert k.verdict == DEPLOYABLE, k.why
    assert k.lift > 5 and k.net_fp_removed_deploy > 100_000
    assert "min_p_deploy_lift" in " ".join(k.binding), "何が足りないかを必ず添える"


def test_deploying_a_head_is_not_deleting_tags():
    """**配備 ≠ 削除**。head を載せて今後の誤検出を抑えることと、既に付いた
    タグを消すことは別の権限 — 削除は reap 側が GT 正例 recall で別途認可する
    (tagger/reap_authorized.json)。再現率の低いタグで削除すると、タガー自身の
    見落としを誤検出と誤認して真のタグを消す。判定の why がそこを言い切らないと
    「配備可 = 消してよい」と読まれる。"""
    k = _classify(load("smegma"))
    assert "配備する" in k.why
    assert "削除" not in k.why, "判定の文面が削除の許可と読める"


def test_real_improvement_from_own_head_is_deployable():
    """自前 head (feat1024/logit) が floor を通っていれば配備可。"""
    m = load("dark_anus")
    assert set(m["verdict"]["arms_clearing_full_floor"]) == {"feat1024", "logit"}
    # dark_anus 自体は lift 1.04 で「不要」に落ちるので、lift だけを有意にして
    # 「自前 head の通過 → 配備可」の経路そのものを確かめる (実測の腕構成は保つ)。
    for op in m["verdict"]["legitimate_operating_points"]:
        op["P_deploy_lift"] = 2.0
    k = _classify(m)
    assert k.verdict == DEPLOYABLE, k.why


def test_baseline_only_pass_is_not_deployable():
    """fellatio: floor を通ったのは stock 腕だけ = **自前 head は全滅**。

    ここを配備可と呼ぶと「head の開発失敗」が「配備可」に化ける
    (primary-gemini-primary のレビュー指摘 3)。改善 1.78 倍・差し引き +43,658 件は
    本物なので却下もせず、**閾値の見直し**として owner 判断へ回す。
    """
    m = load("fellatio")
    assert list(m["verdict"]["arms_clearing_full_floor"]) == ["E_stock_pixai"]
    assert m["verdict"]["any_arm_meets_floor"] is True, "絶対 floor 自体は通っている"
    k = _classify(m)
    assert k.verdict == CLEANUP_CAPABLE, k.why
    assert "自前 head は 1 つも届いていない" in k.why
    assert k.lift > 1.25 and k.net_fp_removed_deploy > 0 and k.at


def test_small_population_is_judged_by_effect_not_by_size():
    """chastity_cage: 母集団 414 件と小さくても、差し引きが正なら配備可。

    **規模の小ささは却下の理由にならない** — 差し引き 358 件は小さいが正で、
    改善は 8.07 倍。逆に規模の大きさも自動的な合格理由にはならない
    (negligible_lift が効く)。判定するのは効果であって母集団の大きさではない。
    """
    k = _classify(load("chastity_cage"))
    assert k.verdict == DEPLOYABLE, k.why
    assert 0 < k.net_fp_removed_deploy < 10_000, "規模 floor を下回る側の実例"


def test_every_classification_names_the_operating_point_it_used():
    """「どの腕のどの動作点での話か」が無い判定は検証できない。"""
    for tag in TAGS:
        k = _classify(load(tag))
        assert k.at and "@" in k.at, f"{tag}: {k}"


# ── スキーマ契約: 無いキーを「偽」と読まない ─────────────────────────────
@pytest.mark.parametrize("missing",
                         ["any_arm_above_chance", "legitimate_operating_points"])
def test_old_schema_measurement_is_refused_not_silently_held(missing):
    """欠けている = 安全ではなく未確認。

    この 2 キーを黙って False/[] と読むと「どの腕も偶然を超えていない」という
    **断定的に誤った**結論になる。旧スキーマの測定は再測定させる。
    """
    m = load("smegma")
    del m["verdict"][missing]
    with pytest.raises(ValueError, match=missing):
        _classify(m)


def test_fixtures_declare_the_schema_that_carries_the_new_keys():
    for tag in TAGS:
        assert load(tag)["schema"] == "vnext-measurement/2"


@pytest.mark.parametrize("key", ["any_arm_meets_floor", "arms_clearing_full_floor",
                                 "any_arm_above_chance", "legitimate_operating_points"])
def test_fixtures_carry_every_key_the_classifier_reads(key):
    """fixture の欠落は「テストが通る」という形で現れる — だから機械で見張る。

    2026-08-03 に arms_clearing_full_floor を fixture に入れ忘れ、baseline 専用
    通過の新しい分岐を fellatio のテストが素通りした (green のまま無検証)。
    """
    for tag in TAGS:
        assert key in load(tag)["verdict"], f"{tag} の fixture に {key} が無い"


# ── 純関数としての性質 ──────────────────────────────────────────────────
def test_n_is_only_a_scale_factor_never_a_sign():
    """符号は角括弧だけで決まる (INV-R12)。"""
    a = net_fp_removed_deploy(pi=0.129, recall=0.75, fpr=0.02, n_flagged=201369)
    b = net_fp_removed_deploy(pi=0.129, recall=0.75, fpr=0.02, n_flagged=1000)
    assert a > 0 and b > 0 and a / b == pytest.approx(201369 / 1000)


def test_net_goes_negative_when_the_tag_is_already_mostly_correct():
    """π が高いほど「TP を捨てる」側が効く — dark_anus の高精度点で実際に負。"""
    assert net_fp_removed_deploy(pi=0.819, recall=0.5756, fpr=0.2895,
                                 n_flagged=30274) < 0


def test_below_chance_is_hold_and_says_so_plainly():
    k = classify(any_arm_above_chance=False, absolute_pass=False,
                 legit_points=[], pi=0.1, n_flagged=1000)
    assert k.verdict == HOLD and "判別できない" in k.why


def test_above_chance_without_a_legitimate_point_is_hold_not_cleanup():
    """発火をほぼ止めれば precision は上がる — それを掃除可と呼ばない。"""
    k = classify(any_arm_above_chance=True, absolute_pass=False, legit_points=[],
                 pi=0.1, n_flagged=1000, binding_terms=["min_recall (R=0.02 < 0.7)"])
    assert k.verdict == HOLD and "min_recall" in k.why


def test_negative_net_overrides_a_passing_floor():
    """絶対 floor を満たしていても、差し引きマイナスなら配備しない。"""
    k = classify(any_arm_above_chance=True, absolute_pass=True,
                 legit_points=[OperatingPoint("feat1024", "P>=0.90", 0.5756,
                                              0.2895, lift=9.9)],
                 pi=0.819, n_flagged=30274)
    assert k.verdict == NOT_NEEDED and k.net_fp_removed_deploy < 0


def test_missing_population_still_classifies_without_inventing_a_net():
    k = classify(any_arm_above_chance=True, absolute_pass=True,
                 legit_points=[OperatingPoint("feat1024", "max_f1", 0.8, 0.1,
                                              lift=2.0)],
                 pi=None, n_flagged=None)
    assert k.verdict == DEPLOYABLE and k.net_fp_removed_deploy is None


# ── fixture に依存しない性質テスト (nodec-cc-primary の推奨、fixture 免疫) ──
# 上のテストは実測 fixture を load するので、**fixture を書き換えれば assertion が
# 黙って反転する**。実際 e0b7b0d で `arms_clearing_full_floor` を入れ忘れ、fellatio
# のテストが新しい分岐を素通りしたまま green だった。ここは逆転の 2 方向を
# **一次原理から組んだ入力**で固定する — ファイルを触っても壊せない。
def test_property_negative_net_always_overrides_a_passing_floor():
    """差し引きが負なら、絶対 floor を通っていても **必ず**不要。

    「π が高ければ負」ではない — **符号は (1−π)(1−FPR) − π(1−R) が決める**
    (π=0.60, R=0.55, FPR=0.30 は +300 で正)。そこを手で当てずに、式で負になる
    組み合わせだけを選んで性質を固定する。
    """
    checked = 0
    for pi in (0.30, 0.50, 0.60, 0.75, 0.819, 0.95):
        for recall in (0.40, 0.55, 0.75, 0.95):
            for fpr in (0.05, 0.30, 0.70):
                if net_fp_removed_deploy(pi=pi, recall=recall, fpr=fpr,
                                         n_flagged=30_000) >= 0:
                    continue
                k = classify(any_arm_above_chance=True, absolute_pass=True,
                             legit_points=[OperatingPoint("feat1024", "P>=0.90",
                                                          recall, fpr, lift=9.9)],
                             pi=pi, n_flagged=30_000)
                assert k.verdict == NOT_NEEDED, f"π={pi} R={recall} FPR={fpr}: {k.why}"
                checked += 1
    assert checked >= 10, f"負になる組み合わせが {checked} 件しか無い — 前提を疑う"


def test_property_dirty_tag_with_a_legitimate_point_is_deployable():
    """floor 未達でも、偶然を超え・正当な動作点で実利があれば **必ず**配備可。

    smegma 型 (π が低い = 汚染タグ)。**lift floor (10 倍) 未満でも届く**こと
    自体が要点 — ここに lift floor を混ぜると hold に落ちて逆転が復活する。
    """
    for lift in (1.5, 3.0, 5.95, 9.99):
        k = classify(any_arm_above_chance=True, absolute_pass=False,
                     legit_points=[OperatingPoint("feat1024", "max_f1",
                                                  recall=0.80, fpr=0.05, lift=lift)],
                     pi=0.129, n_flagged=200_000)
        assert k.verdict == DEPLOYABLE, f"lift={lift}: {k.why}"
        assert k.net_fp_removed_deploy > 0


def test_only_baseline_only_pass_still_reaches_a_human():
    """**掃除可が残る唯一の経路**を明示する。owner 判断を廃止したあと、この
    クラスが誰からも返らなくなると「4 クラス」が実質 3 クラスになる — 判定器の
    語彙と実際の出力が食い違ったまま気づけない状態を作らない。

    baseline (stock) 腕だけが floor を通る = 自前 head は全滅。打ち手は
    **既存モデルの閾値変更**であって head の登録ではないので、機械には
    実行できるものが無く、人に回すのが正しい (fellatio が実例)。
    """
    k = classify(any_arm_above_chance=True, absolute_pass=True,
                 absolute_pass_is_baseline_only=True,
                 legit_points=[OperatingPoint("E_stock_pixai", "max_f1",
                                              recall=0.80, fpr=0.05, lift=5.0)],
                 pi=0.129, n_flagged=200_000)
    assert k.verdict == CLEANUP_CAPABLE, k.why
    assert "自前 head は 1 つも届いていない" in k.why


def test_the_scale_floor_no_longer_gates_the_verdict():
    """**規模 floor は判定を変えない** — 説明文を選ぶだけ。

    owner 判断を廃止した結果、規模 floor を通る入力も通らない入力も
    `deployable` になる。**これは policy dial が実質的に無効化されたという
    こと**で、`tagger_config.min_net_fp_removed` を「配備を絞る歯止め」だと
    思って触ると何も起きない。効いている歯止めは negligible_lift と
    「差し引きが正」の 2 つだけ。ここを test で言い切っておかないと、
    設定値を下げて安心する事故になる。
    """
    kw = dict(any_arm_above_chance=True, absolute_pass=False,
              legit_points=[OperatingPoint("feat1024", "max_f1",
                                           recall=0.80, fpr=0.05, lift=3.0)],
              pi=0.129, n_flagged=200_000)
    over = classify(**kw, min_net_fp_removed=10_000)      # 規模 floor を通る
    under = classify(**kw, min_net_fp_removed=10_000_000)  # 通らない
    assert over.verdict == under.verdict == DEPLOYABLE
    assert "規模の floor を満たす" in over.why
    assert "規模の floor" not in under.why


def test_property_a_legitimate_point_is_recall_and_support_only():
    """lift が floor 未満でも「正当な動作点が無い」にはならない。

    `vnext_arms.clears_lift` は lift floor を含むが、**正当性の判定に混ぜてはいけない**。
    混ぜた実装では下の入力が hold になる — それがまさに直した逆転。
    """
    k = classify(any_arm_above_chance=True, absolute_pass=False,
                 legit_points=[OperatingPoint("feat1024", "max_f1",
                                              recall=0.80, fpr=0.05, lift=2.0)],
                 pi=0.10, n_flagged=100_000)
    assert k.verdict != HOLD, "lift が floor 未満でも動作点は正当"


def test_property_baseline_only_pass_never_deployable_at_any_lift():
    """baseline だけの通過は、lift がどれだけ高くても配備可にしない。"""
    for lift in (1.5, 5.0, 50.0):
        k = classify(any_arm_above_chance=True, absolute_pass=True,
                     absolute_pass_is_baseline_only=True,
                     legit_points=[OperatingPoint("E_stock_pixai", "P>=0.85",
                                                  recall=0.85, fpr=0.05, lift=lift)],
                     pi=0.484, n_flagged=117_664)
        assert k.verdict == CLEANUP_CAPABLE, f"lift={lift}: {k.why}"


# ── 抽出そのものを検査する (ここが検査されていなかった) ──────────────────
# 2026-08-04: 逆転を復活させる改変 (抽出条件に lift floor を混ぜる) を実際に
# 入れてみたところ、**テストが 1 件も落ちなかった**。fixture には抽出済みの
# legitimate_operating_points が焼き込まれており、抽出は誰も見ていなかった。
# 抽出を関数に切り出し、**腕データそのもの**を実測から与えて検査する。
def test_extraction_keeps_points_whose_lift_is_below_the_lift_floor():
    """smegma の腕データ: lift は 5.62〜6.55 で floor (10.0) に全て届かない。

    それでも **正当な動作点は残らなければならない**。ここに lift 条件を混ぜると
    0 件になり、smegma が hold に落ちて逆転が復活する — この module が存在する
    理由そのものが消える。
    """
    from tagger.vnext_arms import legitimate_operating_points

    m = json.loads((FIXTURES / "vnext_arms_smegma.json").read_text())
    f = m["floor"]
    pts = legitimate_operating_points(
        list(m["arms"].items()), min_recall=f["min_recall"],
        min_val_pos=f["min_val_pos_support"], min_pred_pos=f["min_pred_pos_support"])

    assert pts, "lift が floor 未満でも動作点は残る"
    assert all(p["P_deploy_lift"] < f["min_p_deploy_lift"] for p in pts), \
        "この fixture は全点が lift floor 未満 — 前提が崩れたら意味が無い"
    assert all(p["R"] >= f["min_recall"] for p in pts)


def test_extraction_drops_points_that_break_the_recall_floor():
    """recall floor は**効いていなければならない** (発火をほぼ止める点を排除)。"""
    from tagger.vnext_arms import legitimate_operating_points

    m = json.loads((FIXTURES / "vnext_arms_smegma.json").read_text())
    f = m["floor"]
    strict = legitimate_operating_points(
        list(m["arms"].items()), min_recall=0.999,
        min_val_pos=f["min_val_pos_support"], min_pred_pos=f["min_pred_pos_support"])
    assert strict == [], "recall floor を上げれば点は消える (条件が効いている証拠)"


def test_extraction_drops_points_with_thin_support():
    from tagger.vnext_arms import legitimate_operating_points

    m = json.loads((FIXTURES / "vnext_arms_smegma.json").read_text())
    f = m["floor"]
    assert legitimate_operating_points(
        list(m["arms"].items()), min_recall=f["min_recall"],
        min_val_pos=10**9, min_pred_pos=f["min_pred_pos_support"]) == []
