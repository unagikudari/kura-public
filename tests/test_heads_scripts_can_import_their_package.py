"""`tagger/heads/` の実行スクリプトは、パス起動でも自分のパッケージを import できる。

**なぜ機構にするか (2026-09-05 実測)。** `train_heads.py` に
`from tagger.heads import verified_ledger` / `... import planner` が入ったが、
兄弟 (`promote_verified.py`, `backfill_verified_provenance.py`) が持っている
`sys.path.insert(0, ...parents[2])` は付かなかった。このスクリプトは
`update_cycle.sh` から `$PY "$HERE/train_heads.py"` と**パスで**起動され、
`WorkingDirectory` は repo 外 (`~/tagger-heads`) なので `sys.path[0]` は
`tagger/heads/` になり `tagger` パッケージが見えない。結果、6 時間ごとの head
更新サイクルが約 24 時間 (signal `tagger-head-update/trainer` count=125) 死んだ。

失敗は起動時に確定的で、負荷にも DB にも依存しない。**にもかかわらず、import 行
だけを足して bootstrap を忘れる**のは誰でもやる型なので、作法ではなく test で持つ。

DB も GPU も要らない: ソースを ast で読むだけ。実際に import すると
`train_heads.py` は import 時に psycopg2 で接続し backbone を落としに行くため、
「確かめられない設計」になってしまう。
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

HEADS = Path(__file__).resolve().parents[1] / "tagger" / "heads"


def _module_level_package_imports(tree: ast.Module) -> list[ast.stmt]:
    """モジュール直下で `tagger...` を絶対 import している文 (関数内は対象外 —
    起動時に確定で落ちるかどうかが論点なので)。"""
    out = []
    for node in tree.body:
        if isinstance(node, ast.ImportFrom) and node.level == 0 and (
                node.module or "").split(".")[0] == "tagger":
            out.append(node)
        elif isinstance(node, ast.Import):
            if any(a.name.split(".")[0] == "tagger" for a in node.names):
                out.append(node)
    return out


def _bootstrap_lines(tree: ast.Module) -> list[int]:
    """repo root を sys.path に載せる文の行番号。書式は問わず、
    `sys.path.insert(...)` / `sys.path.append(...)` を bootstrap とみなす。"""
    lines = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        f = node.func
        if not isinstance(f, ast.Attribute) or f.attr not in ("insert", "append"):
            continue
        p = f.value
        if isinstance(p, ast.Attribute) and p.attr == "path" and \
                isinstance(p.value, ast.Name) and p.value.id == "sys":
            lines.append(node.lineno)
    return lines


def _scripts():
    return sorted(p for p in HEADS.glob("*.py") if p.name != "__init__.py")


@pytest.mark.parametrize("path", _scripts(), ids=lambda p: p.name)
def test_absolute_package_import_is_preceded_by_a_path_bootstrap(path: Path):
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    imports = _module_level_package_imports(tree)
    if not imports:
        return                      # 相対 import も自己完結もこの test の対象外
    boots = _bootstrap_lines(tree)
    first_import = min(n.lineno for n in imports)
    assert boots, (
        f"{path.name} は `tagger.*` を絶対 import しているのに sys.path へ repo "
        f"root を載せていない。パス起動 (update_cycle.sh) で ModuleNotFoundError "
        f"になる。兄弟に倣い import の前へ "
        f"`sys.path.insert(0, str(Path(__file__).resolve().parents[2]))` を置く。")
    assert min(boots) < first_import, (
        f"{path.name}: sys.path bootstrap (行 {min(boots)}) が最初の `tagger.*` "
        f"import (行 {first_import}) より後ろにある。import は上から実行されるので "
        f"前に置かないと効かない。")


@pytest.mark.parametrize("path", _scripts(), ids=lambda p: p.name)
def test_the_bootstrap_points_at_the_repo_root(path: Path):
    """`parents[2]` — tagger/heads/x.py から見た repo root。深さを間違えた
    bootstrap は「置いてあるのに効かない」形で、上の test を素通りする。"""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    if not _module_level_package_imports(tree):
        return
    src = path.read_text(encoding="utf-8")
    assert "parents[2]" in src, (
        f"{path.name}: bootstrap が repo root (parents[2]) を指していない。"
        f"tagger/heads/ から repo root は 2 つ上。")
