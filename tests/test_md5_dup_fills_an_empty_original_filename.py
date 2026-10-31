"""md5-dup の再 upload は、空の original_filename だけを埋め、既存の名前は上書きしない。

■ 何が起きていたか (2026-09-27 実測)

ある作家の 11 post は 2026-04-25 にファイル名を持たない取り込み元から入り、original_filename が無い。
後に creator の archive (ファイル名つき) が同じ md5 で届いたが、dup 経路は
source / source_archive だけを NULL->値 で埋め、ファイル名は捨てていた。per-set
split はファイル名で set を決めるので、この 11 post を含む pool 9879 / 9182 を
walk のたびに REFUSED にしていた。

■ ここで固定すること

dup 経路に「空なら埋める、あれば触らない」の文があること。live API を叩かずに
source を読んで確かめる (source / source_archive の同じ規則と並べて置く)。
"""
from __future__ import annotations

import ast
from pathlib import Path

UPLOAD = Path(__file__).resolve().parent.parent / "api" / "routers" / "upload.py"


def _fill_if_empty_ifs(tree: ast.AST, attr: str) -> list[ast.If]:
    """`if <x> and not existing.<attr>: existing.<attr> = ...` の形の if を集める。"""
    found = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.If):
            continue
        t = node.test
        if not (isinstance(t, ast.BoolOp) and isinstance(t.op, ast.And)):
            continue
        guards = [v for v in t.values if isinstance(v, ast.UnaryOp)
                  and isinstance(v.op, ast.Not)
                  and isinstance(v.operand, ast.Attribute)
                  and v.operand.attr == attr
                  and isinstance(v.operand.value, ast.Name)
                  and v.operand.value.id == "existing"]
        if not guards:
            continue
        assigns = [s for s in node.body if isinstance(s, ast.Assign)
                   and any(isinstance(tg, ast.Attribute) and tg.attr == attr
                           and isinstance(tg.value, ast.Name) and tg.value.id == "existing"
                           for tg in s.targets)]
        if assigns:
            found.append(node)
    return found


def _all_assignments(tree: ast.AST, attr: str) -> list[ast.Assign]:
    return [n for n in ast.walk(tree) if isinstance(n, ast.Assign)
            and any(isinstance(tg, ast.Attribute) and tg.attr == attr
                    and isinstance(tg.value, ast.Name) and tg.value.id == "existing"
                    for tg in n.targets)]


def test_the_dup_path_fills_an_empty_original_filename():
    tree = ast.parse(UPLOAD.read_text(encoding="utf-8"))
    assert _fill_if_empty_ifs(tree, "original_filename"), (
        "md5-dup が空の original_filename を埋める文が無い")


def test_it_never_overwrites_a_name_that_is_there():
    """existing.original_filename への代入は、すべて「空なら」の if の中にある。"""
    tree = ast.parse(UPLOAD.read_text(encoding="utf-8"))
    guarded = {id(s) for i in _fill_if_empty_ifs(tree, "original_filename") for s in i.body}
    loose = [a.lineno for a in _all_assignments(tree, "original_filename") if id(a) not in guarded]
    assert not loose, f"空チェックの外で original_filename を書いている: lines {loose}"


def test_it_sits_beside_the_same_rule_for_source():
    """同じ規則の兄弟 (source) が健在であること — 並びが崩れたら気づく。"""
    tree = ast.parse(UPLOAD.read_text(encoding="utf-8"))
    assert _fill_if_empty_ifs(tree, "source")
