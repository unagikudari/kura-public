"""dismiss batch は「人間の決定が立っているタグ」で全体が落ちてはいけない。

2026-09-04 の敵対的レビュー: P7 の RAISE は仕様どおりだが、その唯一の呼び出し元が
try/except を持たず、conn.autocommit = True だった。保護されたタグが 1 つあると
例外が main() を貫き、**それ以降のタグは dismiss されず**、先行分は commit 済みなのに
`APPLY done` も出ない — 無音の部分適用。守られたタグは結果であって障害ではないので、
飛ばして数え、最後に名乗る。
"""
import re
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SRC = (REPO / "tagger" / "vnext_triage.py").read_text(encoding="utf-8")


def test_the_dismiss_loop_catches_the_governed_refusal():
    i = SRC.index("for tag, category, _n in triage")
    loop = SRC[i:SRC.index("cur.close()", i)]
    assert "except psycopg2.errors.RaiseException" in loop, (
        "P7 の RAISE を受けていない — 保護タグ 1 つで残り全部が未処理のまま落ちる")
    assert "continue" in loop, "受けたあと次のタグへ進んでいない"


def test_skipped_tags_are_reported_not_swallowed():
    """飛ばしたことが出力に出ない握り潰しは、落ちるより悪い。"""
    assert "SKIPPED" in SRC
    tail = SRC[SRC.index("APPLY done"):]
    assert "skipped" in tail and "protected" in tail, (
        "APPLY done が守られたタグ数を名乗っていない")


def test_the_summary_line_is_still_printed_when_some_were_protected():
    """先行分は commit 済みなので、要約が出ないと何が起きたか誰にも分からない。"""
    assert re.search(r'print\(f"\[vnext_triage\] APPLY done', SRC)
