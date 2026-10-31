"""スキーマの規則が `infer.py` の定数と**同じ判断をする**ことを固定する。

■ なぜこのテストが要るか

migration 0138 は「挙動を変えない移行」が前提 (plan
slug:tagger-per-tag-decisions-as-rows の INV-5)。値を見直すなら測定 → 昇格の
既存経路に乗せるべきで、移行のついでに変えてはいけない。

移行前後で同じ判断になることを、**定数側を実際に読んで**突き合わせる。
定数を手で書き写すと、写し間違いごと green で固定される。

■ 期待値の出どころ

規則側の値は 2026-08-06 に primary canonical の `tagger_tag_application` /
`tagger_producer_threshold` / `tagger_tag_exclusion` から読んだ実測。
自作 fixture ではない。
"""
import ast
import re
from pathlib import Path

import pytest

import sys
REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
from tagger import tag_rules as TR  # noqa: E402

INFER = (REPO / "tagger" / "infer.py").read_text()


def _assigned_in(src: str, name: str):
    """`src` の top-level 代入から定数を読む。無ければ None。"""
    for node in ast.parse(src).body:
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = ([node.target] if isinstance(node, ast.AnnAssign)
                       else node.targets)
            for t in targets:
                if isinstance(t, ast.Name) and t.id == name and node.value is not None:
                    return ast.literal_eval(node.value)
    return None


def _imported_from(src: str, name: str) -> str | None:
    """`from X import name` の X を返す (相対 import は tagger パッケージ内とみなす)。"""
    for node in ast.parse(src).body:
        if isinstance(node, ast.ImportFrom) and node.module:
            if any(a.name == name for a in node.names):
                return node.module.split(".")[-1]
    return None


def _const(name: str):
    """定数を **import せずに** 読む (torch 等を引きたくない)。

    以前は infer.py の代入だけを見ていたが、実装が別モジュールへ移り infer.py が
    import するだけになると「infer.py に無い」で落ちる (2026-09-04、
    PIXAI_THRESH_CHAR が tagger/pixai_chars.py へ移動して実際に main が赤くなった)。
    このテストの意図は「torch を引かずに値を読む」であって「infer.py に代入されて
    いること」ではないので、import を辿って定義元まで読む。
    """
    val = _assigned_in(INFER, name)
    if val is not None:
        return val
    mod = _imported_from(INFER, name)
    if mod:
        src = (REPO / "tagger" / f"{mod}.py")
        if src.is_file():
            val = _assigned_in(src.read_text(), name)
            if val is not None:
                return val
    raise AssertionError(
        f"{name} が infer.py にも、infer.py が import している tagger モジュールにも無い")


# ── 2026-08-06 に primary canonical から読んだ規則 (実測) ────────────────────
SEEDED = {
    "rules": [
        {"producer": "pixai", "tag": "smegma", "threshold": 0.25,
         "remove_below": 0.25, "may_add": True, "may_remove": False},
        {"producer": "mldanbooru", "tag": "smegma", "threshold": 0.45,
         "remove_below": 0.45, "may_add": True, "may_remove": False},
        *[{"producer": p, "tag": t, "threshold": 0.88, "remove_below": 0.88,
           "may_add": True, "may_remove": False}
          for p in ("pixai", "mldanbooru")
          for t in ("vaginal", "vaginal_object_insertion", "cum_in_pussy",
                    "pee", "peeing")],
        *[{"producer": "wd14_fixed", "tag": t, "threshold": 0.5,
           "remove_below": 0.5, "may_add": True, "may_remove": True,
           "all_tags": ["pussy"]}
          for t in ("wrinkled_labia", "dark_labia_majora", "dark_labia_minora")],
        *[{"producer": f"{t}_head", "tag": t, "threshold": 0.50,
           "remove_below": 0.05, "may_add": False, "may_remove": True}
          for t in ("chastity_cage", "chastity_belt")],
    ],
    "producer_defaults": [
        {"producer": "pixai", "category": 0, "threshold": 0.4},
        {"producer": "pixai", "category": 4, "threshold": 0.80},
        {"producer": "mldanbooru", "category": 0, "threshold": 0.7},
        {"producer": "wd14", "category": 4, "threshold": 0.85},
        {"producer": "wd14_fixed", "category": 0, "threshold": 0.5},
    ],
    "exclusions": [
        {"tag_a": "1girl", "tag_b": "multiple_girls"},
        {"tag_a": "1boy", "tag_b": "multiple_boys"},
        {"tag_a": "solo", "tag_b": "multiple_girls"},
        {"tag_a": "solo", "tag_b": "multiple_boys"},
        {"tag_a": "uncensored", "tag_b": "censored"},
        {"tag_a": "uncensored", "tag_b": "mosaic_censoring"},
        {"tag_a": "uncensored", "tag_b": "bar_censor"},
        *[{"tag_a": a, "tag_b": b,
           "any_tags": ["solo", "1girl", "1boy"],
           "none_tags": ["multiple_girls", "multiple_boys", "2girls", "2boys",
                         "3girls", "3boys"]}
          for a, b in (("open_mouth", "closed_mouth"), ("smile", "expressionless"),
                       ("long_hair", "short_hair"), ("medium_hair", "short_hair"),
                       ("medium_hair", "long_hair"))],
    ],
}


@pytest.fixture()
def rules():
    return TR.parse(SEEDED)


# ── 閾値 ────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("producer,low,high,general", [
    ("pixai", "PIXAI_LOW_THRESH", "PIXAI_HIGH_THRESH", "PIXAI_THRESH_GENERAL"),
    ("mldanbooru", "MLDANBOORU_LOW_THRESH", "MLDANBOORU_HIGH_THRESH",
     "MLDANBOORU_THRESH"),
])
def test_per_tag_thresholds_match(rules, producer, low, high, general):
    """定数に載っているタグは、規則からも同じ閾値が出ること。

    定数側にあって規則に無いタグは **dead config** — producer の出力語彙にも
    kura にも無く一度も効いていなかったものだけが該当する (0138 の注記)。
    その場合は producer 既定に落ちるので、ここでは「規則があるものは一致」を見る。
    """
    for name, value in {**_const(low), **_const(high)}.items():
        got = rules.threshold(producer, name, 0)
        if rules.rule(producer, name) is None:
            continue  # dead config — 0138 で移送していない
        assert got == value, f"{producer}/{name}: 規則 {got} vs 定数 {value}"


def test_the_general_default_comes_from_a_row(rules):
    """既定値もコードではなく行から来ること。"""
    assert rules.threshold("pixai", "no_such_tag", 0) == _const("PIXAI_THRESH_GENERAL")
    assert rules.threshold("pixai", "no_such_tag", 4) == _const("PIXAI_THRESH_CHAR")
    assert rules.threshold("mldanbooru", "no_such_tag", 0) == _const("MLDANBOORU_THRESH")


def test_an_unknown_producer_yields_no_threshold(rules):
    """**行が無い = 採らない。** 呼ぶ側が既定値をでっち上げないよう None を返す。"""
    assert rules.threshold("nope", "smegma", 0) is None


# ── gate ────────────────────────────────────────────────────────────────

def test_wd14_fixed_gates_match(rules):
    """`WD14_FIXED_GATES` と同じタグが同じ gate を持つこと。

    移送時に **worker は all 意味 / infer は any 意味で逆だった** ことが判明した。
    要素 1 個の間は同値なので露見しない。all 意味に統一している。
    """
    for tag, gates in _const("WD14_FIXED_GATES").items():
        r = rules.rule("wd14_fixed", tag)
        assert r is not None, f"{tag} の規則が無い"
        assert r.condition.all_tags == tuple(gates)


def test_a_gated_rule_does_not_fire_outside_its_domain(rules):
    r = rules.rule("wd14_fixed", "wrinkled_labia")
    assert r.condition.holds({"pussy", "1girl"})
    assert not r.condition.holds({"1girl"})


# ── 排他 ────────────────────────────────────────────────────────────────

def _old_is_solo(tags):
    """`infer.py::_is_solo` の写し。ここだけは写すしかないので、下の
    test_the_solo_condition_matches_the_old_predicate で全組合せを突き合わせる。"""
    return ((("solo" in tags) or ("1girl" in tags) or ("1boy" in tags))
            and not any(t in tags for t in ("multiple_girls", "multiple_boys",
                                            "2girls", "2boys", "3girls", "3boys")))


def test_the_solo_condition_matches_the_old_predicate(rules):
    """**本題**: `solo` 条件が `_is_solo()` と全組合せで一致すること。

    `when_tags=['solo']` のような 1 本の配列では表現できない — 1girl だけの
    画像でも真になるし、複数系があれば偽になる。all/any/none の 3 節で書ける。
    """
    cond = next(c for a, b, c in rules.exclusions if a == "open_mouth")
    universe = ["solo", "1girl", "1boy", "multiple_girls", "multiple_boys",
                "2girls", "2boys", "3girls", "3boys"]
    for mask in range(1 << len(universe)):
        tags = {t for i, t in enumerate(universe) if mask >> i & 1}
        assert cond.holds(tags) == _old_is_solo(tags), f"食い違い: {sorted(tags)}"


def test_unconditional_pairs_match_the_old_list(rules):
    got = {(a, b) for a, b, c in rules.exclusions if c is TR.ALWAYS or not (
        c.all_tags or c.any_tags or c.none_tags)}
    want = {tuple(p) for p in _const("MUTEX_PAIRS")}
    assert got == want


def test_solo_only_pairs_match_the_old_list(rules):
    got = {(a, b) for a, b, c in rules.exclusions
           if c.any_tags or c.none_tags}
    want = {tuple(p) for p in _const("MUTEX_PAIRS_SOLO_ONLY")}
    assert got == want


def test_resolution_keeps_the_higher_score(rules):
    """排他は高い方を残す (`_resolve_mutex` と同じ)。"""
    out = rules.resolve_exclusions({"1girl": 0.9, "multiple_girls": 0.4})
    assert out == {"1girl": 0.9}
    out = rules.resolve_exclusions({"1girl": 0.3, "multiple_girls": 0.8})
    assert out == {"multiple_girls": 0.8}


def test_solo_only_pairs_do_not_fire_when_multiple_people(rules):
    scores = {"multiple_girls": 0.9, "long_hair": 0.8, "short_hair": 0.7}
    assert rules.resolve_exclusions(scores) == scores


# ── chastity Apply Gate (付与しない / 削除する) ─────────────────────────

def test_the_apply_gate_never_adds(rules):
    """`apply_mode` の 2 値では書けなかった形。付与せず、確信 FP のみ削除する。"""
    for tag in ("chastity_cage", "chastity_belt"):
        r = rules.rule(f"{tag}_head", tag)
        assert r.may_add is False and r.may_remove is True
        assert r.remove_below == _const(f"{tag.upper()}_REMOVE_BELOW")
        assert r.threshold == _const(f"{tag.upper()}_KEEP_ABOVE")


def test_the_hold_zone_is_expressible(rules):
    """[remove_below, threshold) は触らない域。閾値 1 本では表せない。"""
    r = rules.rule("chastity_cage_head", "chastity_cage")
    assert r.remove_below < r.threshold


def test_a_plain_threshold_rule_has_an_empty_hold_zone(rules):
    """既存の「閾値 1 本」は保留域の幅 0 として**特別扱い無しに**含まれる。"""
    r = rules.rule("pixai", "smegma")
    assert r.remove_below == r.threshold
