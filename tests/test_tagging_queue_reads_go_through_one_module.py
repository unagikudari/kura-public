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

#: (path, 使われ方, 何か) -> 理由。使われ方は "subscript" (鍵: 何か = "関数名:式[鍵]")、
#: "str" (SQL などの文字列そのもの)、"attr" (属性・キーワード名)。ファイル単位でも鍵の名前
#: だけでも免除しない (#155): どの関数の中で、何を subscript しているかまで一致したものだけ。
#: 同じファイルに足した SQL、別の式への subscript (Post.table.c["tagging_attempts"] 等) は落ちる。
ALLOWED = {
    ("api/services/tagging.py", "subscript", "claim_auto_tag:row['tagging_attempts']"):
        "不変: 統制関数 claim の戻り値 (rows.mappings() の row) の列名を鍵として読む",
    ("api/services/tagging.py", "subscript", "claim_auto_tag:row['tagging_locked_until']"):
        "不変: 統制関数 claim の戻り値 (rows.mappings() の row) の列名を鍵として読む",
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


def _references(path: Path) -> set[tuple[str, str]]:
    """(how it is used, the name or the whole string) for every reference: a string used only
    as a subscript key, any other string (normalized whitespace), an attribute or keyword."""
    tree = ast.parse(path.read_text(), filename=str(path))
    docstrings = _docstring_nodes(tree)
    # a subscript key is named with its function and the expression it subscripts
    keyed: dict[int, str] = {}
    for fn in ast.walk(tree):
        if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for n in ast.walk(fn):
                if isinstance(n, ast.Subscript):
                    keyed[id(n.slice)] = f"{fn.name}:{ast.unparse(n)}"
    for n in ast.walk(tree):
        if isinstance(n, ast.Subscript) and id(n.slice) not in keyed:
            keyed[id(n.slice)] = f"<module>:{ast.unparse(n)}"
    refs: set[tuple[str, str]] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docstrings:
            if COLUMN.search(node.value):
                if id(node) in keyed and COLUMN.fullmatch(node.value):
                    refs.add(("subscript", keyed[id(node)]))
                else:
                    refs.add(("str", " ".join(node.value.split())))
        elif isinstance(node, ast.Attribute) and (COLUMN.fullmatch(node.attr) or node.attr == "initial_tagging_status"):
            refs.add(("attr", node.attr))
        elif isinstance(node, ast.keyword) and node.arg and COLUMN.fullmatch(node.arg):
            refs.add(("attr", node.arg))
    return refs


def _found() -> dict[tuple[str, str, str], int]:
    found: dict[tuple[str, str, str], int] = {}
    for top in SCANNED:
        for path in sorted((ROOT / top).rglob("*.py")):
            rel = path.relative_to(ROOT).as_posix()
            if rel.startswith(SKIPPED) or "/overlay/" in rel:
                continue
            for kind, what in _references(path):
                found[(rel, kind, what)] = found.get((rel, kind, what), 0) + 1
    return found


def test_no_new_reader_of_the_tagging_queue_outside_the_module():
    unexpected = sorted(k for k in _found() if k not in ALLOWED)
    assert not unexpected, (
        "post.tagging_* を tagging_queue.py の外で参照しています。読み手なら "
        "api/services/tagging_queue.py に関数を足して、そこを呼んでください "
        "(docs/notes/tagging-work-table-plan.md の段 2 が 1 ファイルで切り替えられるように):\n  "
        + "\n  ".join(f"{p} ({k}: {w[:120]})" for p, k, w in unexpected))


def test_every_allowance_is_still_needed():
    # 片付いた参照の許可が残ると、次に同じ場所へ足された読み手を見逃す。
    stale = sorted(k for k in ALLOWED if k not in _found())
    assert not stale, "もう参照が無いので ALLOWED から外してください:\n  " + "\n  ".join(
        f"{p} ({k}: {w}) — {ALLOWED[(p, k, w)]}" for p, k, w in stale)


def test_an_allowed_file_gets_no_new_reader_alongside_its_allowance(tmp_path, monkeypatch):
    """#155: the allowance is for those two return-value keys, not for the file. A SQL reader
    added next to them, or one that replaces a key, is reported."""
    src = (ROOT / "api/services/tagging.py").read_text()
    added = src + '\n\ndef _sneak(db):\n    return db.execute(text("SELECT tagging_status FROM post WHERE id=:id"))\n'
    replaced = src.replace('row["tagging_attempts"]', 'db.execute(text("SELECT tagging_attempts FROM post")).scalar()', 1)
    column = src + '\n\ndef _sneak2(db):\n    return db.execute(select(Post.__table__.c["tagging_attempts"]))\n'
    elsewhere = src + '\n\ndef _sneak3(row):\n    return row["tagging_attempts"]\n'
    assert replaced != src
    for variant in (added, replaced, column, elsewhere):
        f = tmp_path / "tagging.py"
        f.write_text(variant)
        refs = {("api/services/tagging.py",) + r for r in _references(f)}
        assert refs - set(ALLOWED), variant[-200:]
