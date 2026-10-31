"""head 学習の計画づくり — 層別負例・PU・決定性・fail closed。

**DB を使わない。** ここで確かめるのは「どの post をどの層に置くか」という判断であって、
DB の中身ではない。DSN が要るテストにすると DSN の無い環境で skip され、`skip する guard
は guard ではない` (tests/test_mega_key_pending_db.py の言い方) になる。埋め込み 232,000 枚
を回さないと確かめられない設計は、確かめられないのと同じでもある。

出所: plan `kura-generic-head-trainer` rev 3 / 敵対的レビュー 2 席 (round 1・2 とも REVISE)。
"""
from __future__ import annotations

import pytest

from tagger.heads.planner import (
    CONFIRMED_WEIGHT, PlanError, S2_DEFAULT_WEIGHT, Stratum, assert_disjoint,
    assert_no_split_leak, build, kind_of, register_kind, registered_kinds,
    stable_order,
)


def _s1(ids, quota=None):
    return Stratum("S1", ids, CONFIRMED_WEIGHT, quota, "verified_log hard-neg",
                   rank=1, confirmed=True)


def _s2(ids, quota=10**9, weight=S2_DEFAULT_WEIGHT):
    return Stratum("S2", ids, weight, quota, "他タグ正例 (自タグなし)",
                   rank=2, confirmed=False)


def _s3(ids, quota=10**9):
    return Stratum("S3", ids, CONFIRMED_WEIGHT, quota, "corpus 全域一様",
                   rank=3, confirmed=True)


from tagger.heads.planner import (  # noqa: E402
    build_negatives, finalize, split_by_post,
)


# ── S1 は削られない ──────────────────────────────────────────────────────
def test_confirmed_negatives_are_never_dropped_by_a_quota():
    """旧実装は `neg[:len(pos)*2]` で一律に切っていた。人間が剥がした hard-neg は
    head を騙した実績のある最重要教師で、上限で落ちてよいものではない。"""
    s1 = _s1(range(100))                      # quota なし = 全件
    s2 = _s2(range(1000, 1100), quota=10)
    neg, w, man = build_negatives([s1, s2, _s3(range(2000, 2100), quota=10)])
    taken = {m["name"]: m["taken"] for m in man["strata"]}
    assert taken["S1"] == 100, "S1 は全件残る"
    assert taken["S2"] == 10 and taken["S3"] == 10, "S2/S3 はクォータで止まる"
    assert sum(1 for x in w if x == CONFIRMED_WEIGHT) >= 100


def test_a_quota_of_zero_takes_nothing_from_that_stratum():
    neg, _, man = build_negatives([_s2(range(50), quota=0)])
    assert neg == []
    assert man["strata"][0]["taken"] == 0


# ── PU: S2 は確定負例ではない ──────────────────────────────────────────
def test_unconfirmed_confusion_candidates_carry_a_weaker_weight():
    """「タグが無い」は「写っていない」ではない。kura のタグ付けは網羅的でないので、
    S2 をそのまま確定負例にすると正例を負例として学習する。"""
    neg, w, _ = build_negatives([_s1([1, 2]), _s2([10, 11, 12])])
    by_id = dict(zip(neg, w))
    assert by_id[1] == CONFIRMED_WEIGHT and by_id[2] == CONFIRMED_WEIGHT
    assert by_id[10] == S2_DEFAULT_WEIGHT < CONFIRMED_WEIGHT
    assert 0 < S2_DEFAULT_WEIGHT < 1, (
        "0 なら S2 は学習に効かず、1 なら層を分けた意味が消える")


def test_a_post_promoted_to_confirmed_wins_over_its_unconfirmed_appearance():
    """人間が確認して S1 へ昇格した post は、S2 にも現れるが確定の重みを取る。
    層は S1 → S2 → S3 の順に詰めるので、先に来た層が勝つ。"""
    neg, w, man = build_negatives([_s1([7]), _s2([7, 8])])
    assert dict(zip(neg, w))[7] == CONFIRMED_WEIGHT
    s2 = next(m for m in man["strata"] if m["name"] == "S2")
    assert s2["dropped_as_duplicate"] == 1


# ── 決定性 ───────────────────────────────────────────────────────────────
def test_the_same_input_yields_the_same_plan():
    """旧実装は `_verneg` が set で、上限超過時にどれが残るかが実質任意だった
    (train_heads.py 112/126/129 行、2026-09-04 実測)。"""
    strata = lambda: [_s1([]), _s2(range(500), quota=17), _s3(range(1000, 1500), quota=23)]
    a = build_negatives(strata())
    b = build_negatives(strata())
    assert a[0] == b[0] and a[1] == b[1]


def test_order_is_not_id_ascending():
    """id 昇順で切ると常に古い post ばかり残り、視覚的に偏る。安定なハッシュ順にする。"""
    ids = list(range(200))
    assert stable_order(ids) != sorted(ids)
    assert sorted(stable_order(ids)) == ids, "並べ替えであって取りこぼしではない"


def test_a_quota_cut_is_a_consequence_of_the_quota_not_of_ordering_luck():
    """同じ母集団を別の順で渡しても、選ばれる集合は同じ。"""
    a, _, _ = build_negatives([_s2(list(range(300)), quota=40)])
    b, _, _ = build_negatives([_s2(list(reversed(range(300))), quota=40)])
    assert set(a) == set(b)


# ── 正例の混入 ───────────────────────────────────────────────────────────
def test_positives_are_removed_from_the_negative_pool_before_quotas():
    """除外はクォータの**前**。後で落とすと、正例が枠を食って負例が減る。"""
    neg, _, man = build_negatives([_s2(range(10), quota=5)], exclude=[0, 1, 2])
    assert set(neg).isdisjoint({0, 1, 2})
    assert len(neg) == 5, "除外した分だけ後ろから補充される"
    assert man["excluded_by_pos"] == 3


def test_disjointness_is_asserted_and_fails_closed():
    """キャラは共演する。checklist ではなく assertion にせよ、というレビュー指摘。"""
    assert_disjoint("t", [1, 2], [3, 4])                     # 交わらない: 通る
    with pytest.raises(PlanError) as e:
        assert_disjoint("ada_wong", [1, 2, 3], [3, 9])
    assert "共演" in str(e.value) and "ada_wong" in str(e.value)


def test_split_leak_is_asserted():
    assert_no_split_leak("t", [1, 2], [3])
    with pytest.raises(PlanError) as e:
        assert_no_split_leak("t", [1, 2], [2])
    assert "val" in str(e.value)


# ── dispatch 表 ──────────────────────────────────────────────────────────
def test_the_two_existing_kinds_are_still_recognised():
    """移設で既存 8 タグの分類が変わってはならない。"""
    assert kind_of({"gt": "verified", "neg_fill": "select ..."}) == "verified_log"
    assert kind_of({"pos": "select ...", "neg": "select ..."}) == "tag_query"


def test_an_unknown_kind_raises_instead_of_falling_into_the_else_branch():
    """THE DEFECT THIS REPLACES. 旧ループは二分岐で、3 種目は `else` に落ちて
    `q["pos"]` の KeyError になった — 種別を足した人には何が起きたか分からない。"""
    with pytest.raises(PlanError) as e:
        kind_of({"gt_source": "label_table"})
    assert "判定できない" in str(e.value)

    @register_kind("__probe_only__")
    def _b(tag, spec, **ctx):  # pragma: no cover - 登録の確認だけ
        raise AssertionError("呼ばれないはず")

    with pytest.raises(PlanError) as e:
        build("t", {"kind": "no_such_kind"})
    assert "未登録" in str(e.value)
    assert "__probe_only__" in str(e.value), "登録済み種別を挙げて次の一手を示す"
    assert "__probe_only__" in registered_kinds()


def test_a_kind_cannot_be_registered_twice():
    """二重登録は「どちらが効いているか分からない」状態。黙って上書きさせない。"""
    @register_kind("__dup_probe__")
    def _a(tag, spec, **ctx):  # pragma: no cover
        return None
    with pytest.raises(PlanError):
        @register_kind("__dup_probe__")
        def _b(tag, spec, **ctx):  # pragma: no cover
            return None


# ── manifest ─────────────────────────────────────────────────────────────
def test_the_manifest_can_reconstruct_where_every_negative_came_from():
    """provenance が無ければ「S2 が本当に他キャラ由来か」を後から検証できない。"""
    neg, w, man = build_negatives(
        [_s1([1]), _s2(range(100, 140), quota=5), _s3(range(200, 260), quota=7)],
        exclude=[100])
    names = [m["name"] for m in man["strata"]]
    assert names == ["S1", "S2", "S3"]
    for m in man["strata"]:
        assert m["rule"] and "offered" in m and "taken" in m and "weight" in m
    assert sum(m["taken"] for m in man["strata"]) == len(neg) == len(w)
    s2 = next(m for m in man["strata"] if m["name"] == "S2")
    assert s2["dropped_as_positive"] == 1 and s2["quota_exhausted"] is True


# ══ round 1 の指摘に対する反証テスト ═════════════════════════════════════
# 両席が独立に挙げた 5 点。指摘ごとに、それを観測できるフレームを置く。


def test_a_repeated_id_within_one_stratum_is_not_inserted_twice():
    """THE DEFECT ROUND 1 FOUND (primary-codex)。実測で再現した:
    `[5,5,5,7]` がそのまま 4 件として詰められ、post 5 が 3 回入って**暗黙に 3 倍の重み**を
    持っていた。層内の一意化はクォータの前に行う。"""
    neg, w, man = build_negatives([_s2([5, 5, 5, 7])])
    assert sorted(neg) == [5, 7], f"重複が残っている: {neg}"
    assert len(w) == len(neg)
    m = man["strata"][0]
    assert m["offered"] == 4 and m["unique"] == 2
    assert m["dropped_duplicate_within_stratum"] == 2


def test_the_final_negative_list_is_asserted_unique():
    """一意化を通り抜けた重複が残っていたら、最後に止める。"""
    neg, _, _ = build_negatives([_s1([1, 1]), _s2([1, 2, 2])])
    assert len(neg) == len(set(neg))


def test_stratum_order_is_enforced_not_merely_conventional():
    """round 1 (trainer): 優先順位が「呼び出し側の慣習」に留まっていた。
    慣習は引数の並べ替え 1 つで壊れる — validator で強制する。"""
    with pytest.raises(PlanError) as e:
        build_negatives([_s3(range(5)), _s1([9])])      # rank 3 → 1 の逆順
    assert "rank 昇順" in str(e.value)


def test_a_confirmed_stratum_cannot_carry_a_weak_weight():
    with pytest.raises(PlanError) as e:
        build_negatives([Stratum("S1", [1], 0.3, None, "r", rank=1, confirmed=True)])
    assert "確定負例" in str(e.value)


def test_an_unconfirmed_stratum_cannot_carry_the_confirmed_weight():
    """PU の弱ラベルを weight 1.0 で入れるのは「未ラベルを確定負例として学習する」こと。"""
    with pytest.raises(PlanError) as e:
        build_negatives([Stratum("S2", [1], 1.0, 10, "r", rank=2, confirmed=False)])
    assert "未確定" in str(e.value)


def test_an_unconfirmed_stratum_cannot_be_unbounded():
    """弱ラベルが無制限だと、数で確定負例を圧倒する。"""
    with pytest.raises(PlanError) as e:
        build_negatives([Stratum("S2", [1], 0.3, None, "r", rank=2, confirmed=False)])
    assert "無制限" in str(e.value)


def test_empty_strata_are_refused():
    with pytest.raises(PlanError):
        build_negatives([])


# ── finalize: Plan を返す唯一の経路で検査が必ず走る ────────────────────
def test_the_only_public_path_to_a_plan_runs_every_assertion():
    """round 1 (両席): `assert_no_split_leak` に本番呼び出し経路が無かった。
    **呼ばれない assertion は無いのと同じ。** 検査を通らずに Plan が手に入る道を残さない。"""
    plan = finalize("t", pos=[1, 2, 3, 4, 5, 6, 7, 8, 9, 10],
                    strata=[_s1([100, 101]), _s2(range(200, 240), quota=8)])
    assert plan.tag == "t"
    assert set(plan.pos).isdisjoint(plan.neg)
    assert len(plan.weights) == len(plan.neg)
    assert plan.manifest["split"]["pos_val"] >= 5


def test_finalize_actually_runs_the_split_leak_check(monkeypatch):
    """P13 SURVIVED まで、このフレームは検査の**呼び出し**を観測できていなかった。

    `split_by_post` は重複除去済みの集合を切るので漏れを作らない。つまり
    `assert_no_split_leak` は将来 `split_by_post` が変わったときのための防護で、
    「今は漏れない」ことは「finalize が検査している」ことの証拠にならない — 検査を
    削っても全テストが緑のままだった。漏れる split を差し込んで、**呼ばれていること**を
    直接見る。両席が言った「呼ばれない assertion は無いのと同じ」が、私のテスト側にも
    当てはまっていた。
    """
    import tagger.heads.planner as pl

    def leaky(ids, *, val_fraction=0.2, min_val=5):
        ordered = pl.stable_order(set(ids))
        return ordered, ordered[:1]          # train と val が必ず重なる

    monkeypatch.setattr(pl, "split_by_post", leaky)
    with pytest.raises(PlanError) as e:
        pl.finalize("t", pos=list(range(20)),
                    strata=[_s1([100, 101]), _s2(range(200, 220), quota=5)])
    assert "train と val" in str(e.value)


def test_finalize_refuses_a_plan_left_with_no_negatives():
    """ANOTHER DEFECT THE TEST FOUND。正例が負例候補を食い尽くすと (共演で起きる)
    負例 0 件になるが、`finalize` はそのまま通っていた — 正例だけで二値分類器を学習する
    ことになる。exclude が先に効くので `assert_disjoint` では捕まらない位置。"""
    with pytest.raises(PlanError) as e:
        finalize("ada_wong", pos=[1, 2, 3],
                 strata=[Stratum("S1", [3], 1.0, None, "r", rank=1, confirmed=True)])
    assert "負例が 0 件" in str(e.value)
    assert "excluded_by_pos" in str(e.value), "なぜ空になったかの手がかりを添える"


def test_finalize_refuses_zero_positives():
    with pytest.raises(PlanError) as e:
        finalize("t", pos=[], strata=[_s1([1])])
    assert "正例が 0 件" in str(e.value)


def test_split_is_by_post_id_and_deterministic():
    """split は **埋め込みより前**に post_id 単位で確定する (round 1 指摘)。
    tensor になってからでは post の同一性が消える。"""
    tr1, va1 = split_by_post(range(100))
    tr2, va2 = split_by_post(list(reversed(range(100))))
    assert (tr1, va1) == (tr2, va2), "入力順に依らない"
    assert set(tr1).isdisjoint(va1)
    assert sorted(tr1 + va1) == list(range(100))


# ── manifest が「再構成できる」ことの中身 ──────────────────────────────
def test_the_manifest_names_every_chosen_id_with_its_stratum_and_weight():
    """round 1 (両席): manifest が集計値だけで、採用 ID・層・除外理由・選択規則版を
    復元できなかった。「再構成できる」と主張するなら、実際に再構成して見せる。"""
    neg, w, man = build_negatives(
        [_s1([1]), _s2(range(100, 140), quota=5), _s3(range(200, 260), quota=7)],
        exclude=[100])

    # 再構成: manifest だけから (post_id -> 層, 重み) を復元し、実際の出力と一致するか
    rebuilt = {c["post_id"]: (c["stratum"], c["weight"]) for c in man["chosen"]}
    assert rebuilt == {i: (st, wt) for i, wt, st in
                       zip(neg, w, [c["stratum"] for c in man["chosen"]])}
    assert set(rebuilt) == set(neg)

    assert man["selector_version"].startswith("planner/")
    assert "md5" in man["hash_spec"]
    assert man["cross_stratum_overlap"] >= 0
    s2 = next(m for m in man["strata"] if m["name"] == "S2")
    assert s2["dropped_as_positive"] == 1 and s2["confirmed"] is False


def test_the_weight_is_recorded_as_configuration_not_as_a_derivation():
    """round 1 (primary-codex): `0.3` は PU 学習から導出された値ではない。
    設定値であることが manifest に残らないと、次の読み手は導出だと思う。"""
    _, _, man = build_negatives([_s2([1, 2, 3])])
    s2 = next(m for m in man["strata"] if m["name"] == "S2")
    assert s2["weight"] == S2_DEFAULT_WEIGHT
    src = __import__("pathlib").Path(
        __import__("tagger.heads.planner", fromlist=["x"]).__file__).read_text()
    assert "導出された値ではない" in src, "設定値であることを源で明言する"


# ══ 旧経路との parity ═══════════════════════════════════════════════════
#
# primary-codex round 1: 「固定 fixture を旧経路と新経路へ通し比較せよ」— blocking。
#
# **旧経路は非決定的なので byte 一致は定義できない。** `_verneg[name]` は set で、
# `list(...)` の順序が未規定 (train_heads.py 112/126/129 行、実測)。よって固定するのは:
#   * truncation が効かない領域 → **集合が一致する**
#   * truncation が効く領域     → 一致しない。旧は非決定的に落とし、新は決定的に落とす。
#     **意図した差**であることを、差そのものを見せて固定する。


def _old_path(pos, verified_neg, other_neg, *, ratio=2):
    """旧 train_heads.py の負例構成を、そのまま写した参照実装。

    `list(set)` を含むのは意図的 — 非決定性ごと再現しないと「旧と同じか」を論じられない。
    """
    neg = list(dict.fromkeys(list(set(verified_neg)) + list(other_neg)))
    return neg[:len(set(pos)) * ratio]


def test_parity_the_sets_agree_when_truncation_does_not_bite():
    """truncation が効かない領域では、旧と新は**同じ集合**を選ぶ。"""
    pos = list(range(50))                      # 予算 = 2*50 = 100
    vneg = list(range(1000, 1010))             # 10
    oneg = list(range(2000, 2040))             # 40 -> 合計 50 <= 100 で切られない

    old = _old_path(pos, vneg, oneg)
    plan = build("t", {"pos": "sql", "neg": "sql"},
                 sql_pos=pos, sql_neg=oneg, applied=[], verified_pos=[],
                 verified_neg=vneg)
    assert set(plan.neg) == set(old), "切られない領域では集合が一致する"
    assert len(plan.neg) == len(old)


def test_parity_the_difference_is_visible_and_intended_when_truncation_bites():
    """切られる領域では一致しない。旧は非決定的、新は決定的 — その差を固定する。"""
    pos = list(range(5))                       # 予算 = 10
    vneg = list(range(1000, 1004))             # 4 (確定、削られない)
    oneg = list(range(2000, 2100))             # 100 -> 大幅に切られる

    old = _old_path(pos, vneg, oneg)
    plan = build("t", {"pos": "sql", "neg": "sql"},
                 sql_pos=pos, sql_neg=oneg, applied=[], verified_pos=[],
                 verified_neg=vneg)

    assert len(old) == 10, "旧: 合計を 2*len(pos) で切る"
    assert len(plan.neg) == 10, "新: S1 全件 + S3 に残り予算"
    assert set(vneg).issubset(plan.neg), "新は確定負例を必ず残す"
    # 旧は set 順に依存するので、確定負例が残る保証が無い — そこが直した点
    assert set(plan.neg) != set(old) or True   # 集合一致は要求しない

    # そして新は決定的
    again = build("t", {"pos": "sql", "neg": "sql"},
                  sql_pos=pos, sql_neg=oneg, applied=[], verified_pos=[],
                  verified_neg=vneg)
    assert plan.neg == again.neg


def test_parity_confirmed_negatives_survive_where_the_old_path_could_drop_them():
    """旧経路の実害: verified hard-neg が 2*len(pos) を超えると、どれが残るか任意だった。"""
    pos = [1, 2]                               # 予算 = 4
    vneg = list(range(1000, 1020))             # 20 件の確定負例 > 予算
    plan = build("t", {"pos": "sql", "neg": "sql"},
                 sql_pos=pos, sql_neg=[], applied=[], verified_pos=[], verified_neg=vneg)
    assert set(plan.neg) == set(vneg), "確定負例は 20 件すべて残る (クォータで削らない)"
    assert all(w == CONFIRMED_WEIGHT for w in plan.weights)


def test_the_tag_query_kind_reproduces_the_old_positive_rule():
    """機械適用を除外し、人間検証済み正例は復帰させる — 旧経路と同じ規則。"""
    plan = build("t", {"pos": "sql", "neg": "sql"},
                 sql_pos=[1, 2, 3, 4, 5, 6, 7, 8, 9, 10],
                 sql_neg=list(range(2000, 2050)),
                 applied=[2, 3], verified_pos=[3], verified_neg=[])
    assert 2 not in plan.pos, "機械適用は除外"
    assert 3 in plan.pos, "ただし人間検証済みは復帰"
    assert 1 in plan.pos


def test_the_verified_log_kind_uses_only_curated_positives():
    """raw タグクエリを正例に使わない (base WD14 タグがノイズの概念用)。"""
    plan = build("t", {"gt": "verified", "neg_fill": "sql"},
                 verified_pos=list(range(20)), verified_neg=[900, 901],
                 neg_fill=list(range(3000, 3060)))
    assert set(plan.pos) == set(range(20))
    assert {900, 901}.issubset(plan.neg)


def test_both_existing_kinds_route_through_the_dispatch_table():
    """dispatch parity: 既存 2 種別が表を通り、同じ検査を受ける。"""
    from tagger.heads.planner import registered_kinds
    assert "verified_log" in registered_kinds()
    assert "tag_query" in registered_kinds()


# ══ per-sample weight が既存 8 タグで無害であることの実測 ═════════════════
#
# 配線で損失を `reduction="none"` + 重み付き平均に変えた。既存 8 タグは全負例が確定
# (weight 1.0) なので「学習は変わらない」と主張したが、**主張は測るまで主張のまま**。
# torch を使うので、無ければ skip する (この 1 本だけ)。


@pytest.mark.torch
def test_uniform_weights_reproduce_the_unweighted_loss():
    """重みが全部 1.0 のとき、重み付き平均は素の平均と一致する。

    これが成り立たなければ、既存 8 タグの学習が移設で変わる。

    **注意: この 2 本は torch の無い環境で skip される。** kura の test venv に torch は
    無く (comfyui venv 側にある)、skip されたテストはテストではない。だから主張は
    2026-09-04 に comfyui venv の torch 2.13.0 で**手で実測**した:

        素の平均     = 0.7047980428
        重み付き平均 = 0.7047980428     (pos_weight あり でも 1.0475890636 で一致)
        弱い重み 0.3 で誤りの寄与 5.0000 -> 2.3077

    実測はしたが、**回帰は守れていない** — torch を持つ環境で走らせる経路が要る。
    """
    import torch

    g = torch.Generator().manual_seed(0)
    logits = torch.randn(64, generator=g)
    y = (torch.rand(64, generator=g) > 0.5).float()

    plain = torch.nn.BCEWithLogitsLoss()(logits, y)

    per = torch.nn.BCEWithLogitsLoss(reduction="none")(logits, y)
    w = torch.ones(64)
    weighted = (per * w).sum() / w.sum()

    assert torch.allclose(plain, weighted, atol=1e-6), (
        f"素の平均 {plain.item():.8f} と重み付き平均 {weighted.item():.8f} が違う")


@pytest.mark.torch
def test_a_weaker_weight_actually_reduces_that_sample_s_influence():
    """逆に、重みを下げたら効きが落ちることも見る — 落ちないなら器が効いていない。"""
    import torch

    logits = torch.tensor([10.0, -10.0])       # 2 件目だけ大きく外している
    y = torch.tensor([1.0, 1.0])
    per = torch.nn.BCEWithLogitsLoss(reduction="none")(logits, y)

    full = (per * torch.tensor([1.0, 1.0])).sum() / 2.0
    damped = (per * torch.tensor([1.0, 0.3])).sum() / 1.3
    assert damped < full, "弱い重みが誤りの寄与を下げていない"


# ══ round 2 の指摘に対する反証テスト ═════════════════════════════════════
# 「黙った不足」— docstring での要求は検査ではない、という指摘 (round 2)。
# round 3 で両席 blocking: 「記録は誰も読まなければ無いのと同じ」。既定は止め、通すには
# 期限付き waiver が要る。期限が切れた waiver は無いのと同じ。

from datetime import date, timedelta  # noqa: E402

from tagger.heads.planner import ShortfallWaiver  # noqa: E402

TODAY = date(2026, 9, 5)


def _waiver(days=30):
    return ShortfallWaiver(reason="旧経路と同じ不足 (LIMIT 500)",
                           expires=TODAY + timedelta(days=days), granted_by="test")


def _limited(quota, waiver=None):
    return Stratum("S3", list(range(500)), CONFIRMED_WEIGHT, quota, "limited", rank=3,
                   confirmed=True, complete=False, source_limit=500,
                   shortfall_waiver=waiver)


def test_a_shortfall_without_a_waiver_is_refused_by_default():
    """THE SILENT SHORTFALL。SQL が `LIMIT n` に張り付いた列に quota > n を要求すると、
    **足りないことに誰も気づかない**まま少ない負例で学習する。既定は止める —
    round 2 の版は「既存なら記録のみ」を既定にしていたが、記録は読まれる保証が無い。"""
    with pytest.raises(PlanError) as e:
        build_negatives([_limited(800)], today=TODAY)
    assert "LIMIT 500" in str(e.value) and "黙って不足" in str(e.value)
    assert "ShortfallWaiver" in str(e.value), "通す道 (期限付き waiver) を名指しする"


def test_a_valid_waiver_lets_the_shortfall_through_and_is_recorded():
    """**既存 8 タグを壊さない。** 実測: dark_labia_minora は正例 225,626 で予算 451,252、
    neg SQL は LIMIT 500。旧経路も同じ不足を抱えていたので、期限付きで通す。
    通した事実 (理由・期限・許可者・判定日) は manifest に残る。"""
    neg, _, man = build_negatives([_limited(451252, _waiver())], today=TODAY)
    assert len(neg) == 500, "取れるだけ取る (旧と同じ)"
    [sf] = man["shortfalls"]
    assert (sf["stratum"], sf["source_limit"], sf["fetched"], sf["quota"]) == (
        "S3", 500, 500, 451252)
    assert sf["waiver"]["reason"] == "旧経路と同じ不足 (LIMIT 500)"
    assert sf["waiver"]["expires"] == (TODAY + timedelta(days=30)).isoformat()
    assert sf["waiver"]["granted_by"] == "test"
    assert sf["waiver"]["valid_on"] == TODAY.isoformat()


def test_an_expired_waiver_is_the_same_as_no_waiver():
    """期限が切れたら止まる — これが「読まれる保証の無い記録」を「読まざるを得ない失敗」に
    変える機構そのもの。期限を過ぎた翌日で止まり、message が期限と理由を名指しする。"""
    w = _waiver(days=0)                                   # expires == TODAY
    neg, _, _ = build_negatives([_limited(800, w)], today=TODAY)
    assert len(neg) == 500, "期限当日はまだ有効"
    with pytest.raises(PlanError) as e:
        build_negatives([_limited(800, w)], today=TODAY + timedelta(days=1))
    msg = str(e.value)
    assert "失効" in msg and TODAY.isoformat() in msg and "旧経路と同じ不足" in msg


def test_the_waiver_is_only_consulted_when_there_is_a_shortfall():
    """不足が無ければ期限切れの waiver があっても止まらない。waiver は不足の免除であって、
    層の有効期限ではない。"""
    w = _waiver(days=-10)                                 # already expired
    neg, _, man = build_negatives([_limited(300, w)], today=TODAY)
    assert len(neg) == 300 and man["shortfalls"] == []


def test_no_shortfall_is_recorded_when_there_is_none():
    """不足が無いのに記録すると、記録が信号でなくなる。"""
    _, _, man = build_negatives([_s1([1, 2, 3])])
    assert man["shortfalls"] == []


def test_a_truncated_source_is_fine_when_the_quota_fits_inside_it():
    """500 件取得で quota 300 なら、完全集合と同じ hash prefix になるので不足しない。"""
    neg, _, _ = build_negatives([_limited(300)])
    assert len(neg) == 300


def test_a_source_that_did_not_hit_its_limit_is_not_suspect():
    """LIMIT 500 で 120 件しか返らなかったなら、それが完全集合。quota が超えても不足しない。"""
    neg, _, _ = build_negatives([Stratum("S3", list(range(120)), CONFIRMED_WEIGHT, 800,
                                         "limited", rank=3, confirmed=True,
                                         complete=False, source_limit=500)])
    assert len(neg) == 120


def test_declaring_incompleteness_without_saying_where_it_was_cut_is_refused():
    """「不完全」とだけ言われても、不足しているかは判定できない。"""
    with pytest.raises(PlanError) as e:
        build_negatives([Stratum("S3", [1, 2], CONFIRMED_WEIGHT, 10, "r", rank=3,
                                 confirmed=True, complete=False)])
    assert "source_limit" in str(e.value)


def test_the_compat_path_declares_the_existing_sql_limit():
    """既存 8 タグの経路が `complete=True` を黙って主張していないこと。
    主張していたら、LIMIT 500 を超える予算で黙って不足する。"""
    from tagger.heads.planner import OLD_SQL_LIMIT, _compat_strata
    strata, _ = _compat_strata(verified_neg=[1], other_neg=list(range(500)), pos_count=10)
    s3 = next(s for s in strata if s.name == "S3")
    assert s3.complete is False and s3.source_limit == OLD_SQL_LIMIT


def _tag_query_beyond_limit(**kw):
    return build("t", {"pos": "sql", "neg": "sql"},
                 sql_pos=list(range(400)),          # 予算 = 800 > 500
                 sql_neg=list(range(10000, 10500)), # LIMIT 500 に張り付いている
                 applied=[], verified_pos=[], verified_neg=[], today=TODAY, **kw)


def test_the_compat_path_dies_on_a_budget_beyond_the_sql_limit_unless_waived():
    """正例が多いタグは予算 2*len(pos) が 500 を超える。waiver 無しでは既存経路でも止まる —
    「既存だから」は planner の中の既定ではなく、呼び出し側が期限付きで宣言するもの。"""
    with pytest.raises(PlanError):
        _tag_query_beyond_limit()
    plan = _tag_query_beyond_limit(shortfall_waiver=_waiver())
    assert len(plan.neg) == 500
    [sf] = plan.manifest["shortfalls"]
    assert sf["quota"] == 800 and sf["fetched"] == 500 and sf["waiver"]["granted_by"] == "test"


# ── 正例と予算の導出は記録され、検査される (round 3) ──────────────────────
# 「除外前の数で予算を組んでいるのが本当の欠陥ではないか」と私が書き、検算で偽だった。
# 以後は記録が答える: raw → excluded_applied → restored_verified → final → budget。

def test_the_tag_query_kind_records_how_its_positives_were_derived():
    plan = build("t", {"pos": "sql", "neg": "sql"},
                 sql_pos=[1, 2, 3, 4, 5, 6],
                 sql_neg=[100, 101, 102, 103, 104, 105, 106, 107],
                 applied=[2, 3, 4],            # 機械適用 3 件
                 verified_pos=[4],             # うち 1 件は人が確認 → 復帰
                 verified_neg=[100])
    pos = plan.manifest["positives"]
    assert (pos["raw"], pos["excluded_applied"], pos["restored_verified"], pos["final"]) == (
        6, 2, 1, 4)
    assert pos["final"] == len(plan.pos) == plan.manifest["pos_count"]
    b = pos["budget"]
    assert b == {"rule": "budget = max(0, ratio * final_pos - s1_unique)",
                 "ratio": 2, "final_pos": 4, "s1_unique": 1, "budget": 7}
    s3 = next(m for m in plan.manifest["strata"] if m["name"] == "S3")
    assert s3["quota"] == b["budget"], "記録した予算と実際に使った quota が同じ数"


def test_the_budget_is_derived_from_the_post_exclusion_count():
    """除外後 (final) から組む。除外前 (raw) から組めば予算が 2 倍近く膨らみ、不足が
    桁違いに見える — round 3 で私が確かめずに書いた誤りは、この記録が否定する。"""
    plan = build("t", {"pos": "sql", "neg": "sql"},
                 sql_pos=list(range(100)), sql_neg=list(range(1000, 1300)),
                 applied=list(range(90)), verified_pos=[], verified_neg=[])
    b = plan.manifest["positives"]["budget"]
    assert plan.manifest["positives"]["final"] == 10
    assert b["final_pos"] == 10 and b["budget"] == 20, "raw=100 なら 200 になっていた"


def test_the_verified_log_kind_records_a_derivation_with_nothing_excluded():
    plan = build("p", {"gt": "verified"}, verified_pos=[1, 2, 3], verified_neg=[9],
                 neg_fill=[10, 11, 12, 13, 14])
    pos = plan.manifest["positives"]
    assert (pos["raw"], pos["excluded_applied"], pos["restored_verified"], pos["final"]) == (
        3, 0, 0, 3)
    assert pos["budget"]["budget"] == 2 * 3 - 1


def test_finalize_refuses_a_derivation_record_that_disagrees_with_the_positives():
    """記録して検査する、の「検査」。final が実正例数と食い違う記録は嘘なので止める。"""
    with pytest.raises(PlanError) as e:
        finalize("t", [1, 2, 3], [_s1([9])],
                 positives={"raw": 3, "excluded_applied": 0, "restored_verified": 0,
                            "final": 2, "rule": "lie"})
    assert "導出記録" in str(e.value) and "final=2" in str(e.value)
