"""post のタグ付けキュー (`post.tagging_*`) を読むコードが、`api/services/tagging_queue.py` の外に増えないこと。

**DB を必要としない。** source を AST で読むだけです。

docs/notes/tagging-work-table-plan.md の段 2 は、キューの読み先を post から
`post_tagging_work` に切り替える。切り替えるのは `tagging_queue.py` 1 ファイルだけ、
というのが段 0 で作った前提で、ほかの場所に読み手が 1 つでも残ると、段 3 で post の列が
止まったあと、その読み手は**古い値を黙って返し続ける** (列はまだ在るので、エラーにならない)。
grep で読み手を数えた人が見落としたものは、どこも赤くならない。だからここで数える。

数えるもの: 文字列リテラル (SQL) の中の列名と、`Post.tagging_*` の属性参照。
docstring とコメントは数えない (説明は読み手ではない)。

`ALLOWED` は、計画の別の段で片付けることが決まっている参照だけを載せる。理由と段を書く。
ここに足すときは、計画にその段があることを先に確かめること。
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCANNED = ("api", "tagger", "scripts")
SKIPPED = (
    "api/alembic/",                    # migration は当時の形を記録するもの
    "api/services/tagging_queue.py",   # ここが唯一の読み手
)
COLUMN = re.compile(r"(?<!meta_)\btagging_(status|locked_until|attempts|last_error)\b")

#: (path, 何であるか) -> 片付ける段と理由
ALLOWED = {
    ("api/services/tagging.py", "str"): "不変: 統制関数 claim の戻り値の列名 (シグネチャは変えない)",
}


def _docstring_nodes(tree: ast.AST) -> set[int]:
    ids: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = getattr(node, "body", [])
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) \
                    and isinstance(body[0].value.value, str):
                ids.add(id(body[0].value))
    return ids


def _references(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(), filename=str(path))
    docstrings = _docstring_nodes(tree)
    kinds: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docstrings:
            if COLUMN.search(node.value):
                kinds.add("str")
        elif isinstance(node, ast.Attribute) and COLUMN.fullmatch(node.attr) or (
                isinstance(node, ast.Attribute) and node.attr == "initial_tagging_status"):
            kinds.add("attr")
        elif isinstance(node, ast.keyword) and node.arg and COLUMN.fullmatch(node.arg):
            kinds.add("attr")
    return kinds


def _found() -> dict[tuple[str, str], int]:
    found: dict[tuple[str, str], int] = {}
    for top in SCANNED:
        for path in sorted((ROOT / top).rglob("*.py")):
            rel = path.relative_to(ROOT).as_posix()
            if rel.startswith(SKIPPED) or "/overlay/" in rel:
                continue
            for kind in _references(path):
                found[(rel, kind)] = found.get((rel, kind), 0) + 1
    return found


def test_no_new_reader_of_the_tagging_queue_outside_the_module():
    unexpected = sorted(k for k in _found() if k not in ALLOWED)
    assert not unexpected, (
        "post.tagging_* を tagging_queue.py の外で参照しています。読み手なら "
        "api/services/tagging_queue.py に関数を足して、そこを呼んでください "
        "(docs/notes/tagging-work-table-plan.md の段 2 が 1 ファイルで切り替えられるように):\n  "
        + "\n  ".join(f"{p} ({k})" for p, k in unexpected))


def test_every_allowance_is_still_needed():
    # 片付いた参照の許可が残ると、次に同じ場所へ足された読み手を見逃す。
    stale = sorted(k for k in ALLOWED if k not in _found())
    assert not stale, "もう参照が無いので ALLOWED から外してください:\n  " + "\n  ".join(
        f"{p} ({k}) — {ALLOWED[(p, k)]}" for p, k in stale)
