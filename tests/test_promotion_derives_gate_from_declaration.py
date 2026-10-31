"""昇格が curated な domain 宣言 (tagger_tag_rule) から gate を導出することを固定する。

■ 2026-09-03 の実害 (user 報告「dark_labia_minora が過検知しているようだ」)

dark_labia_minora は DB に `in_pussy_domain` と**宣言済み**だった
(tagger_tag_rule, migration 0138 で infer.py WD14_FIXED_GATES から移送)。
なのに content-head の昇格 (auto_review → head_policy.derive) は宣言を読まず、
`derive(metrics, config=config)` — gate_tags 引数を渡さない — で
**gate_tags=[] の policy を書いた**。校正域 (uncensored∩pussy、域内 FPR 31%)
の外を含む全画像に無条件発火し、アーカイブの 45% (447,770 post) に付いた。
うち 205,222 post は pussy タグすら無かった。

固定するのは:
  - 宣言があれば昇格はそれを policy の gate_tags にする。
  - 宣言が**無い**ことは「無条件でよい」ではない — fail-closed で昇格を止める
    (registration は validated のまま残り、宣言を足せば次サイクルで通る)。
  - gate_tags で表現できない条件 (any/none) を黙って緩めない。
  - 複数 producer の宣言が食い違えば厳しい方 (全条件の和) に倒す。
"""
from __future__ import annotations

import ast
import asyncio
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from tagger import head_policy as H  # noqa: E402


class _Result:
    def __init__(self, rows):
        self._rows = rows

    def mappings(self):
        return self

    def all(self):
        return self._rows


class _Db:
    """domain_gate_tags が発行する 1 クエリだけに答える最小の非同期 DB。"""

    def __init__(self, rows):
        self._rows = rows

    async def execute(self, *_a, **_k):
        return _Result(self._rows)


def _gates(rows):
    return asyncio.run(H.domain_gate_tags(_Db(rows), "dark_labia_minora"))


def test_undeclared_is_fail_closed_not_unconditional():
    """宣言が無い ⇒ None (昇格を止める)。「無い=無条件」の読み替えが実害の核。"""
    assert _gates([]) is None


def test_declared_domain_becomes_the_gate():
    rows = [{"name": "in_pussy_domain", "all_tags": ["pussy"],
             "any_tags": [], "none_tags": []}]
    assert _gates(rows) == ("pussy",)


def test_always_means_unconditional_by_explicit_declaration():
    rows = [{"name": "always", "all_tags": [], "any_tags": [], "none_tags": []}]
    assert _gates(rows) == ()


def test_disagreeing_producers_fall_to_the_stricter_side():
    """片方の producer が always、他方が domain 条件 ⇒ 和集合 = domain 側。"""
    rows = [
        {"name": "always", "all_tags": [], "any_tags": [], "none_tags": []},
        {"name": "in_pussy_domain", "all_tags": ["pussy"],
         "any_tags": [], "none_tags": []},
    ]
    assert _gates(rows) == ("pussy",)


def test_inexpressible_condition_is_not_silently_weakened():
    """any/none を含む条件は gate_tags (all-of) で書けない。黙って落とすと
    宣言より緩い配備になる — None で止める。"""
    rows = [{"name": "solo", "all_tags": [], "any_tags": ["solo", "1girl"],
             "none_tags": ["multiple_girls"]}]
    assert _gates(rows) is None


# ── 昇格側が実際に宣言を消費していることの走査 ────────────────────────────

def _promote_validated_src() -> str:
    src = (REPO / "tagger" / "auto_review.py").read_text()
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "promote_validated":
            return ast.get_source_segment(src, node)
    raise AssertionError("promote_validated が auto_review.py に無い — 名前が変わった?")


def test_promotion_consults_the_declaration_before_writing_the_patch():
    """宣言の確認は **patch を書く前** — policy 側で止めると promoted_patch_id が
    埋まって再訪されず、「patch はあるのに policy が永久に無い」になる。"""
    body = _promote_validated_src()
    assert "domain_gate_tags" in body, \
        "promote_validated が domain 宣言を読んでいない — 実害の再導入"
    assert body.index("domain_gate_tags") < body.index("tagger_governed_upsert_patch"), \
        "宣言の確認が patch 書き込みより後にある — validated へ戻れない位置"


def test_promotion_passes_the_declared_gates_to_derive():
    body = _promote_validated_src()
    assert "gate_tags=domain_gates" in body, \
        "derive() に宣言由来の gate が渡っていない (gate_tags=[] の policy が書かれる)"
