"""dismiss batch は「人間の決定が立っているタグ」で全体が落ちてはいけない。

2026-09-04 の敵対的レビュー: P7 の RAISE は仕様どおりだが、その唯一の呼び出し元が
try/except を持たず、conn.autocommit = True だった。保護されたタグが 1 つあると
例外が main() を貫き、**それ以降のタグは dismiss されず**、先行分は commit 済みなのに
`APPLY done` も出ない — 無音の部分適用。守られたタグは結果であって障害ではないので、
飛ばして数え、最後に名乗る。
"""
import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

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


@pytest.mark.parametrize("failure", ["protected", "connection"])
def test_apply_handles_only_governed_refusals(monkeypatch, capsys, failure):
    import psycopg2
    from tagger import kura_db, vnext_triage

    class Cursor:
        closed = False
        attempted = []

        def execute(self, sql, args):
            self.attempted.append(args[0])
            if args[0] == "protected":
                if failure == "protected":
                    raise psycopg2.errors.RaiseException("human decision is protected")
                raise psycopg2.OperationalError("connection lost")

        def fetchone(self):
            return (2,)

        def close(self):
            self.closed = True

    class Connection:
        closed = False

        def cursor(self):
            return cursor

        def close(self):
            self.closed = True

    cursor, conn = Cursor(), Connection()
    monkeypatch.setattr(sys, "argv", ["triage", "--apply"])
    monkeypatch.setattr(vnext_triage, "apply_host", vnext_triage.socket.gethostname)
    monkeypatch.setattr(kura_db, "connect_canonical", lambda: conn)
    monkeypatch.setattr(vnext_triage, "fetch_candidates", lambda c: [
        ("protected", "character", 1), ("next", "character", 2)])
    monkeypatch.setattr(vnext_triage, "is_non_trainable", lambda tag, category: True)
    if failure == "connection":
        with pytest.raises(psycopg2.OperationalError):
            vnext_triage.main()
        assert cursor.attempted == ["protected"]
        assert "APPLY done" not in capsys.readouterr().out
    else:
        assert vnext_triage.main() == 0
        assert cursor.attempted == ["protected", "next"]
        output = capsys.readouterr().out
        assert "SKIPPED 'protected'" in output
        assert "2 candidate row(s) dismissed; 1 skipped" in output
        assert cursor.closed
    assert conn.closed
