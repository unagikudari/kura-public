"""昇格が作った記録が **canonical に届く**ことを固定する。

■ 2026-08-05 の経緯

`auto_review` は patch ファイルを書いて git commit するが、**push しなかった**。
それでも伝わっていたのは、nodec / desk の origin が **primary の作業ツリーへの
直接 SSH** だったから — GitHub を迂回した配布経路が常設されていた。

その経路を GitHub に一本化した結果、**push しないと canonical にも他ノードにも
届かない**。`build_release.py` は `tagger/patches/<tag>/v{version}.json` を読むので、
届かなければ release が組めなくなる。

**片方を直すと、それに依存していたもう片方が壊れる。** 迂回路を塞ぐなら、
正規の経路を通す必要がある。
"""
import ast
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SRC = (REPO / "tagger" / "auto_review.py").read_text()
BUILD = (REPO / "tagger" / "build_release.py").read_text()


def _promote_body() -> str:
    tree = ast.parse(SRC)
    lines = SRC.splitlines()
    for n in ast.walk(tree):
        if isinstance(n, ast.AsyncFunctionDef) and n.name == "promote_validated":
            return "\n".join(lines[n.lineno - 1:n.end_lineno])
    raise AssertionError("promote_validated が無い")


def test_the_files_are_actually_read_by_the_release_builder():
    """ファイルが誰にも読まれないなら push も要らない。読まれるから要る。"""
    assert 'json_path = patches_dir / tag / f"v{version}.json"' in BUILD


def test_the_commit_is_pushed():
    b = _promote_body()
    assert '"git", "push", "origin", "HEAD"' in b, "commit しただけで push していない"


def test_a_failed_push_is_reported_with_the_backlog():
    """溜まった未 push は「動いているのに伝わっていない」状態。
    件数まで出さないと、気づいても規模が分からない。"""
    b = _promote_body()
    assert "origin/main..HEAD" in b, "未 push 件数を数えていない"
    assert "他ノードには届いて" in b


def test_a_failed_push_does_not_undo_the_promotion():
    """patch 行は既に DB にあり、そちらが権威。push 失敗で昇格を巻き戻さない。"""
    b = _promote_body()
    i = b.index('"git", "push"')
    tail = b[i:]
    assert "except Exception" in tail
    assert "raise" not in tail.split("except Exception")[1][:400]
