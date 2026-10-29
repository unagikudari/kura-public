"""`log()` の第 1 引数名と衝突する kwarg を渡さないことを固定する。

■ 2026-08-05 20:38 実測

    [vnext-runner] registration.failed tag=dark_labia_minora
        error=TypeError detail=log() got multiple values for argument 'event'

`def log(event: str, **kv)` に対して `log("registration.submitted", event=...)`
と呼んでいた。**登録が例外で落ち**、gt-unchanged の backfill 経路が
1 件も登録できていなかった (monochrome / greyscale が通る経路そのもの)。

except が TypeError を飲んで "registration.failed" と出すだけなので、
**ログを読まない限り気づけない**。呼び出し規約は静的に確かめられる。
"""
import ast
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
TARGETS = sorted((REPO / "tagger").glob("*.py")) + sorted((REPO / "scripts").glob("*.py"))


def _log_first_param(tree: ast.AST) -> str | None:
    for n in ast.walk(tree):
        if isinstance(n, ast.FunctionDef) and n.name == "log" and n.args.args:
            return n.args.args[0].arg
    return None


@pytest.mark.parametrize("path", TARGETS, ids=lambda p: p.name)
def test_no_kwarg_shadows_logs_first_parameter(path: Path):
    """**本題**: `log(...)` の kwarg が第 1 引数名と被っていないこと。

    被ると必ず TypeError。呼び出し時にしか出ないので本番のログでしか
    分からないが、**呼び出し規約なので静的に分かる**。
    """
    try:
        tree = ast.parse(path.read_text())
    except SyntaxError:
        pytest.skip("parse できない")
    first = _log_first_param(tree)
    if first is None:
        pytest.skip("この module に log() の定義が無い")
    bad = []
    for n in ast.walk(tree):
        if (isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
                and n.func.id == "log" and n.args):
            for kw in n.keywords:
                if kw.arg == first:
                    bad.append(f"{path.name}:{n.lineno} log(..., {first}=...) は "
                               f"第 1 引数 '{first}' と衝突する")
    assert not bad, "\n".join(bad)


def test_the_checker_would_catch_the_real_bug():
    """実際に踏んだ形を検出できること (通るだけの検査は無意味)。"""
    tree = ast.parse('def log(event, **kv): pass\n'
                     'log("registration.submitted", tag=1, event=2)\n')
    first = _log_first_param(tree)
    assert first == "event"
    hits = [kw.arg for n in ast.walk(tree)
            if isinstance(n, ast.Call) and getattr(n.func, "id", None) == "log" and n.args
            for kw in n.keywords if kw.arg == first]
    assert hits == ["event"]
