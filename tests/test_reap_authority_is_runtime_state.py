"""reap authority を「git が持つソース」に戻さないための固定 (kura#44)。

■ 何が起きていたか

`tagger/reap_authorized.json` は 6h ごとに `kura-reap-authorization.timer` が
再測定して**丸ごと書き換える** runtime 状態なのに、tracked ファイルとして
tagger/ に置かれていた。timer は commit しないので production tree は常に
`M tagger/reap_authorized.json` の dirty で、`/api/build` が `dirty:true` を
返し続けた。**「commit hash が唯一の ground truth」という運用の前提**が、
毎回「ただし working tree はいつも汚れている」で濁っていた。

生成物はソースではない。`data/` (gitignore 済) に移し、書き手 (evaluator) と
読み手 (api/services/tagging.py, scripts/backfill_head_policy.py) を同じ場所に
向けた。evaluator の `--commit` (git add+commit する経路) は、状態が git に
無い以上つじつまが合わないので削除。

■ ここで見るもの

置き場所が 3 箇所で一致していること、tracked に戻っていないこと、evaluator が
自分の出力を commit しないこと。読み手の fail-closed (ファイル欠損 → 何も
reap しない) は既存の `_reap_authorized` の except が担保する。
"""
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
RUNTIME_REL = 'data" / "tagger" / "reap_authorized.json'

SITES = (
    "api/services/tagging.py",       # 読み手 (reap gate)
    "tagger/reap_authorization_eval.py",  # 書き手 (6h timer)
    "scripts/backfill_head_policy.py",    # 読み手 (legacy measured recall)
)


def test_every_site_points_at_the_runtime_path():
    """2026-10-25: the sites resolve the path through api/state_paths.py (and the pools
    dashboard, which still read the pre-kura#44 path, joined them). The resolver's default
    is data/tagger/reap_authorized.json; tests/test_state_paths.py pins the order."""
    resolver = (REPO / "api" / "state_paths.py").read_text()
    assert '/ "tagger" / "reap_authorized.json"' in resolver and 'REPO / "data"' in resolver
    bad = [rel for rel in SITES + ("api/routers/pools.py",)
           if "reap_authority_path" not in (REPO / rel).read_text()]
    assert not bad, f"reap authority の置き場所を自分で綴っている: {bad}"


def test_every_site_allows_the_env_override():
    """host ごとに置き場所を変えられること (KURA_REAP_AUTHORITY_PATH / KURA_STATE_DIR)。"""
    resolver = (REPO / "api" / "state_paths.py").read_text()
    assert "KURA_REAP_AUTHORITY_PATH" in resolver and "KURA_STATE_DIR" in resolver


def test_the_authority_is_not_tracked_again():
    """tracked に戻ると 6h ごとに tree が dirty になる (元の症状)。"""
    assert not (REPO / "tagger" / "reap_authorized.json").exists(), (
        "tagger/reap_authorized.json が復活している — runtime 状態は "
        "data/tagger/reap_authorized.json へ")
    ignored = (REPO / ".gitignore").read_text()
    assert "tagger/reap_authorized.json" in ignored, (
        "うっかり git add されないよう .gitignore で fence すること")


def test_the_evaluator_does_not_commit_its_own_output():
    src = (REPO / "tagger" / "reap_authorization_eval.py").read_text()
    assert '"commit"' not in src and "args.commit" not in src, (
        "git に無い状態を commit しようとしている")
