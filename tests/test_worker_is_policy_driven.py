"""worker が **タグ名の表も既定閾値も持たない**ことを固定する。

■ user 2026-08-05

  「すべてのタグが平等に昇格できるようにしろ。その際にマジックナンバーや
   特殊変数で管理するな。スキーマを整えろ。一本化しろ」

旧 worker は:

    _HEAD_SPECS = (("small_penis", ...), ("dark_penis", ...), ("dark_perineum", ...))
    GREYOWL_ADD_THRESH = 0.5
    if not heads or "penis" not in general: return general

3 タグを手で列挙し、全 head 共通の既定閾値を持ち、`"penis"` というタグ名で
書かれた前提で発火を絞っていた。**monochrome / greyscale が patch まで到達
しても永久に効かなかった**のはこれ。
"""
import ast
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SRC = (REPO / "tagger" / "nodec_autotag_worker.py").read_text()


def _code_only() -> str:
    tree = ast.parse(SRC)
    lines = SRC.splitlines()
    if (tree.body and isinstance(tree.body[0], ast.Expr)
            and isinstance(tree.body[0].value, ast.Constant)):
        lines = lines[tree.body[0].end_lineno:]
    out = []
    for l in lines:
        st = l.lstrip()
        if st.startswith("#"):
            continue
        out.append(l)
    src = "\n".join(out)
    # docstring 内の説明を落とす (経緯として旧名が出る)
    return re.sub(r'"""(?:.|\n)*?"""', "", src)


import re  # noqa: E402


def test_no_hardcoded_head_table():
    assert "_HEAD_SPECS" not in _code_only(), "タグ名の表が実装に残っている"


def test_no_shared_default_threshold():
    """根拠の無い閾値は、測定した閾値と見分けがつかないまま配備に混ざる。"""
    assert "GREYOWL_ADD_THRESH" not in _code_only()


def _fn_code(name: str) -> str:
    """関数本体から docstring を除いたコード。注記に旧コードが引用されるので、
    そこを拾うと「まだ残っている」と誤検出する。"""
    body = SRC[SRC.index(f"def {name}("):]
    body = body[:body.index("\ndef ", 10)]
    return re.sub(r'"""(?:.|\n)*?"""', "", body)


def test_no_tag_name_gates_the_whole_head_stage():
    """`"penis" not in general` のようなタグ名の前提を置かない —
    無関係なタグの head が永久に発火しなくなる。"""
    assert '"penis" not in general' not in _fn_code("apply_content_heads")


def test_heads_come_from_the_policy_endpoint():
    assert "/api/tagger/head-policies" in SRC
    assert "def load_head_policies(" in SRC


def test_bytes_are_verified_after_download():
    """名前は約束にならない。受け取った bytes を hash し直す
    (release_client.pull と同じ規律)。"""
    body = _fn_code("_fetch_artifact")
    assert "hashlib.sha256(r.content).hexdigest()" in body
    assert "actual != sha" in body


def test_a_head_without_a_threshold_is_not_applied():
    """既定値で埋めない (fail-closed)。"""
    body = _fn_code("apply_content_heads")
    assert "if not isinstance(thr, (int, float)):" in body
    assert "continue" in body


def test_the_gate_requires_every_tag():
    """domain gate は「この視覚領域でだけ校正されている」の意味。
    一部一致は校正の外側。"""
    body = _fn_code("apply_content_heads")
    assert "all(g in present for g in gates)" in body


def test_failing_to_read_policies_refuses_incomplete_tagging():
    """「取れなかったから既定で全部当てる」にしない — 何を当てたか説明
    できないまま書き込むことになる。"""
    body = _fn_code("load_head_policies")
    assert "raise RequiredCharacterComponentError" in body
    assert "retry before claiming" in body


# ── 再起動を要らなくする (user 2026-08-05:「一切手を加えずに動作させろ」) ──
#
# `CONTENT_HEADS` は起動時に 1 度だけ読んでいた。つまり **新しいタグが昇格しても、
# 誰かが worker を再起動するまで適用されない**。release は `refresh_model()` が
# 毎サイクル見てホットスワップしているのに、head だけ手動だった。

def test_content_heads_are_refreshed_every_cycle():
    """**本題**: 常駐ループが毎サイクル policy を見ること。"""
    body = SRC[SRC.index("    while True:"):]
    assert "refresh_content_heads()" in body[:1600], "ループで積み直していない"


def test_heads_are_refreshed_before_the_release():
    """`reap_gates` は `refresh_model` が CONTENT_HEADS から組み立てるので、
    順序が逆だと新しい head が 1 サイクル遅れて提案される。"""
    body = SRC[SRC.index("    while True:"):]
    assert body.index("refresh_content_heads()") < body.index("refresh_model(state)")


def test_an_unchanged_policy_set_does_not_reload():
    """変わっていなければ HTTP 1 回で返る (release の content-addressed skip と
    同じ契約)。毎サイクル 348MB を読み直さない。"""
    b = SRC[SRC.index("def refresh_content_heads("):]
    b = b[:b.index("\ndef ", 10)]
    assert "if fp == _POLICY_FP" in b
    assert "return False" in b


def test_the_fingerprint_covers_what_matters():
    """sha だけでなく閾値・モード・gate も見る — policy が変わったのに
    積み直さないと、古い閾値で当て続ける。"""
    b = SRC[SRC.index("def policy_fingerprint("):]
    b = b[:b.index("\ndef ", 10)]
    for k in ("content_sha256", "threshold", "apply_mode", "gate_tags"):
        assert k in b, f"{k} が指紋に入っていない"


# ── apply_mode を「適用」にも使う (2026-08-06 実測) ──────────────────────
#
# `replace` を reap 権限と gate 提案には配線したのに、**適用に配線していなかった**。
# `apply_content_heads` は ADD-ONLY で、既に付いているタグは `tag in present` で
# 早期に抜けていた。base の wd14 が monochrome を出し続ける限り出力に残り、
# サーバの reap は「出力にある」ので消さない。
#
#     再タグした post の 8 割が monochrome を保持 (desk 111 件中 97 件)
#
# 消えるには 3 つ揃う必要がある: 権限 (policy の apply_mode) / 提案 (veto_gates) /
# **worker が自分の出力から外すこと**。

def test_a_replace_head_retracts_the_tag_when_it_disagrees():
    """**本題**: 閾値未満なら出力から外す。外さなければ reap は起きない。"""
    b = _fn_code("apply_content_heads")
    assert "out = [t for t in out if t != tag]" in b, "出力から外していない"
    assert "present.discard(tag)" in b


def test_an_add_only_head_never_retracts():
    """削除安全性の校正を経ていない head は足すだけ。"""
    b = _fn_code("apply_content_heads")
    assert 'pol.get("apply_mode") == REPLACE' in b


def test_an_existing_tag_is_not_skipped_for_replace_heads():
    """`tag in present` で早期に抜けると、**付いているが違う**を言えない。"""
    b = _fn_code("apply_content_heads")
    assert 'if tag in present and pol.get("apply_mode") != REPLACE:' in b


def test_the_mode_constant_is_shared_with_the_policy_module():
    """文字列を worker 側で書き直さない (綴りがずれたら黙って add_only になる)。"""
    assert "from tagger.head_policy import REPLACE" in SRC
