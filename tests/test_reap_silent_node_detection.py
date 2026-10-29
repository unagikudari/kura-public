"""fleet 内で reap の挙動が割れていることを検出する。

■ 2026-08-05 実測

reap は「サーバが認可する」と「worker が提案する」の積で動く。提案は worker の
コードが出すので、**古いコードのノードは黙って掃除に参加しない**。DB からは
「タグ付けは進んでいる」ようにしか見えない。

    primary    84 件タグ付け /  5 件削除   ← 現行コード
    desk 699 件         /  0 件
    nodec  691 件         /  0 件
    nodeb  230 件         /  0 件

**コーパスの 95% で reap が走っていなかった。** 3 ノードは
`tagger_node_state.detail` も空で、現行 worker とは別のコードベースだった —
migration 0068 が 2026-07-20 に「3 つのコピーがあり同期を強制するものが無い」と
記録し、cut-over されないまま残っていたもの。

**気づく手段が無かったのが問題。** 静かに 95% が参加しない状態は、誰かが
post_tag_audit を host 別に数えるまで見えない。
"""
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SRC = (REPO / "tagger" / "health.py").read_text()


def _fn(name: str) -> str:
    b = SRC[SRC.index(f"async def {name}("):]
    i = b.find("\nasync def ", 10)
    return b[:i] if i != -1 else b


def test_the_check_exists_and_runs():
    assert "async def check_reap_silent_nodes(" in SRC
    body = _fn("run_health_check")
    assert "check_reap_silent_nodes(db)" in body, "定義しただけで呼んでいない"


def test_it_reports_so_the_fleet_sees_it():
    """signal に出さなければ「気づく手段が無い」ままで、直す動機が生まれない。"""
    assert '"reap-silent-node"' in SRC


def test_zero_removals_alone_is_not_an_alarm():
    """消すべきものが無ければ削除 0 は正常。**他ノードが同期間に削除している**
    ことを条件に入れて、fleet 内で割れているときだけ鳴らす。"""
    b = _fn("check_reap_silent_nodes")
    assert "EXISTS (SELECT 1 FROM act o WHERE o.removed > 0)" in b


def test_a_quiet_node_is_not_flagged():
    """数件しか処理していないノードで鳴らさない (統計的に意味が無い)。"""
    b = _fn("check_reap_silent_nodes")
    assert "tagged >= 100" in b


def test_it_looks_at_a_bounded_window():
    b = _fn("check_reap_silent_nodes")
    assert "interval '2 hours'" in b
