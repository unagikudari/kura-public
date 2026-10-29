"""再タグが **人間の操作なしで**始まることを固定する。

■ user 2026-08-05

  「誰かが操作しないと reap が走らないのは問題だ。機構化しているとは言えない」
  「人間はタグ付けするだけだ」

■ 何が人間待ちだったか

sweep の候補判定は `release_id` の一致だけだった:

    NOT EXISTS (post_tag_audit WHERE post_id=p.id AND release_id=:rid)

**head policy が増えても release_id は変わらない。** 新しい head を配備しても、
既にその release でタグ済みの post は「済み」のままなので候補に入らず、
**release を publish する人間が居ない限り再タグは 1 件も起きない**。
再タグが起きなければ reap も起きない (reap は完了時にしか動かない)。

実測 (2026-08-05、primary の本番データ):

    旧判定の候補       0 件   ← 永久に何も起きない
    新判定の候補  31,643 件   ← アンサンブルが変わったので対象
"""
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SRC = (REPO / "tagger" / "sweep.py").read_text()
#: 候補の SQL は段 0 (docs/notes/tagging-work-table-plan.md) でキューの読みを集めた先にある。
QUEUE_SRC = (REPO / "api" / "services" / "tagging_queue.py").read_text()


def _fn(name: str, src: str = SRC) -> str:
    b = src[src.index(f"async def {name}("):]
    for end in ("\nasync def ", "\ndef ", "\nif __name__"):
        i = b.find(end, 10)
        if i != -1:
            return b[:i]
    return b


def test_the_ensemble_change_time_is_computed():
    """アンサンブルは release と head policy の**両方**で決まる。"""
    b = _fn("_ensemble_changed_at")
    assert "tagger_head_policy" in b, "policy の更新を見ていない"
    assert "tagger_patch" in b
    assert "GREATEST(" in b, "新しいほうを取っていない"


def test_the_candidate_query_uses_it():
    """**本題**: release_id の一致だけで判定しない。"""
    b = _fn("requeue_candidates", QUEUE_SRC)
    assert "a.created_at >= :changed_at" in b, "時刻を見ていない"
    assert "a.release_id = :rid" in b, "release の一致は残す"
    # sweep 側がその関数に時刻を渡していること (渡さなければ判定は元のまま)。
    assert "requeue_candidates(db, release_id, limit, changed_at)" in _fn("_candidate_ids")


def test_run_sweep_passes_the_change_time():
    """関数を作っただけで渡さなければ、判定は元のまま (今日 7 回踏んだ形)。"""
    b = _fn("run_sweep")
    assert "_ensemble_changed_at(db)" in b
    assert "_candidate_ids(db, release_id, ceiling, changed_at)" in b


def test_the_change_time_is_recorded_in_the_summary():
    """なぜ候補が増えた/増えないかを後から説明できるようにする。"""
    assert '"ensemble_changed_at"' in SRC


def test_the_rate_ceilings_are_still_there():
    """一度に全部戻さない。既存の 2 つの上限を外さないこと。"""
    assert "DEFAULT_MAX_POSTS = 5000" in SRC
    assert "DEFAULT_MAX_FRACTION = 0.25" in SRC
    assert "ceiling = min(max_posts, int(total_done * max_fraction))" in SRC


def test_there_is_no_null_branch():
    """`_ensemble_changed_at` は GREATEST(COALESCE(...,'epoch'), ...) なので
    **NULL にならない**。それでも `:changed_at IS NULL OR` を書いたら、
    asyncpg が型を決められず `AmbiguousParameterError` で落ち、23:19 から
    sweep が 15 分ごとに失敗した (601,069 件が再処理されない直接の原因)。

    **起きない場合のための分岐が、起きる障害を作っていた。**
    """
    # 注記に経緯として旧コードを引用するので、コメント行を除いて見る。
    b = "\n".join(l for l in _fn("_candidate_ids").splitlines()
                  if not l.lstrip().startswith("#"))
    assert "IS NULL OR" not in b
    assert "COALESCE" in _fn("_ensemble_changed_at"), "NULL にならない保証が無い"
