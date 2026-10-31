"""reap が「認可」と「提案」の**両方**そろって初めて動くことを固定する。

■ 2026-08-05 の切れ目

reap には 2 つの独立した条件がある:

    サーバ: このタグを消してよいか (tagger_head_policy.apply_mode='replace'
            ∪ 旧 reap_authorized.json)
    worker: 私はこのタグについて権威ある出力を出したか (veto_gates の提案)

サーバ側を policy 由来にしたが、**worker の提案は release の ckpt しか
見ていなかった**:

    state.reap_gates = sorted(set(ckpt.get("patched")) | set(state.veto_heads))

policy で content head を積んでも提案に入らないので、サーバが monochrome を
認可しても `reap_gates` は空のまま = **掃除が永久に始まらない**。

■ なぜ「積んだ head だけ」なのか

load に失敗した head を提案すると、**補正されていない出力を根拠に**サーバが
既存タグを消す。head が無いのに「私の出力は権威ある」と言うことになる。
"""
import ast
import re
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
WORKER = (REPO / "tagger" / "nodec_autotag_worker.py").read_text()
TAGGING = (REPO / "api" / "services" / "tagging.py").read_text()


# ソース文字列ではなく **関数の挙動** を固定する。2026-08-06 に reap 集合の
# 組み立てを `_policy_reap_gates()` / `recompute_reap_gates()` に切り出したとき、
# 文字列 grep 型のこれらのテストは「壊れていないのに」4 件落ちた。
# 固定すべきは「policy 由来の replace head だけが、実際に load できたときだけ、
# 提案に入る」であって、その式がどの行に書かれているかではない。
import sys
sys.path.insert(0, str(REPO))
from tagger import nodec_autotag_worker as W  # noqa: E402


class _State:
    def __init__(self):
        self.veto_heads = {}
        self.release_reap = set()
        self.reap_gates = []


def _with_heads(heads: dict):
    """CONTENT_HEADS を差し替えて policy 由来の提案集合を得る。"""
    prev = W.CONTENT_HEADS
    W.CONTENT_HEADS = heads
    try:
        return W._policy_reap_gates()
    finally:
        W.CONTENT_HEADS = prev


def test_the_worker_proposes_gates_for_policy_heads():
    """**本題**: policy で積んだ head も提案に入ること。入らないと、サーバが
    認可しても掃除は 1 件も始まらない。"""
    gates = _with_heads({
        "monochrome": object(),
        "__policy__monochrome": {"apply_mode": "replace"},
    })
    assert gates == {"monochrome"}


def test_only_replace_mode_heads_are_proposed():
    """add_only の head は削除に使わない (削除安全性の校正を経ていない)。"""
    gates = _with_heads({
        "dark_penis": object(),
        "__policy__dark_penis": {"apply_mode": "add_only"},
    })
    assert gates == set()


def test_only_heads_that_actually_loaded_are_proposed():
    """load に失敗した head を提案すると、補正されていない出力を根拠に
    サーバが既存タグを消す。"""
    gates = _with_heads({
        "greyscale": None,  # load 失敗
        "__policy__greyscale": {"apply_mode": "replace"},
    })
    assert gates == set()


def test_hot_swapped_heads_reach_the_proposal():
    """**2026-08-06 の切れ目**: reap 集合の計算が `refresh_model()` の release
    読み込みブロックの中にしかなく、release が変わらなければ early-return する。
    head を hot-swap しても提案は据え置きで、掃除が始まらなかった。

    release 由来が空のままでも、policy 由来だけで提案が立つことを固定する。"""
    st = _State()
    prev = W.CONTENT_HEADS
    W.CONTENT_HEADS = {"greyscale": object(),
                       "__policy__greyscale": {"apply_mode": "replace"}}
    try:
        W.recompute_reap_gates(st)
    finally:
        W.CONTENT_HEADS = prev
    assert st.reap_gates == ["greyscale"]


def test_release_and_policy_gates_both_survive():
    """片方だけになってはいけない — release の patched 行も policy の head も
    どちらも権威ある補正なので、両方が提案に入る。"""
    st = _State()
    st.release_reap = {"anilingus"}
    st.veto_heads = {"fellatio": object()}
    prev = W.CONTENT_HEADS
    W.CONTENT_HEADS = {"monochrome": object(),
                       "__policy__monochrome": {"apply_mode": "replace"}}
    try:
        W.recompute_reap_gates(st)
    finally:
        W.CONTENT_HEADS = prev
    assert st.reap_gates == ["anilingus", "fellatio", "monochrome"]


def test_the_proposal_is_logged():
    """何を提案したかが出ないと、掃除が始まらない理由を追えない。"""
    assert "reap_gates={state.reap_gates}" in WORKER


def test_the_server_still_holds_the_authority():
    """提案だけでは消えない。サーバ側の認可と積で決まる。"""
    assert "reap_gates = [g for g in (veto_gates or []) if g in _auth]" in TAGGING


def test_human_ground_truth_stays_out_of_reach():
    """reap の SQL が構造的に人手 GT へ届かないこと — recall を veto に
    しない根拠そのもの (user 2026-08-05)。"""
    i = TAGGING.index("DELETE FROM post_tag pt")
    sql = TAGGING[i:i + 900]
    assert "NOT pt.locked" in sql, "upload 由来 (locked) を除外していない"
    assert "pin.state = 'present'" in sql, "人が確認したタグを除外していない"
    assert "pt.post_id = :pid" in sql, "再タグした post 以外に及んでいる"
