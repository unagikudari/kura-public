"""測定 → 昇格 → policy → dispatch → reap の**全段**が繋がっていることを固定する。

■ なぜ全段を 1 ファイルで見るか (2026-08-05)

この日、1 段ずつ直しては報告するのを 6 回繰り返した。毎回「次の段でも止まって
いた」。user の時間を最も浪費したのがこれ。段ごとの test はあっても、
**段と段の継ぎ目**を見る test が無かった。

実際に止まっていた継ぎ目 (すべて実測):

  1. 測定が head 本体を保存しない (`artifact_sha256` は測定 JSON の hash)
  2. `ArtifactStore.ingest_file` はメソッドではなく module 関数 →
     呼び出しが AttributeError で落ち、head は保存されない
  3. 昇格が `tagger_artifact` 行を作らない → policy の FK が満たせない
  4. GC が lifecycle だけ戻し `tagger_head_candidate` は promoted のまま →
     dispatch されない
  5. `head_policy.authorized_tags()` を書いたのに reap 側が手書き JSON を読む
     → 新しいタグは永久に掃除されない
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

ARMS = (REPO / "tagger" / "vnext_arms.py").read_text()
REVIEW = (REPO / "tagger" / "auto_review.py").read_text()
RECON = (REPO / "tagger" / "reconciler.py").read_text()
TAGGING = (REPO / "api" / "services" / "tagging.py").read_text()
STORE = (REPO / "api" / "tagger_core" / "artifact_store.py").read_text()


# ── 継ぎ目 1-2: 測定 → artifact bytes ────────────────────────────────────

def test_the_store_api_is_called_the_way_it_is_defined():
    """`ingest_file` は **module 関数**であってメソッドではない。

    `store.ingest_file(...)` と書くと AttributeError になり、私の except が
    それを飲んで「head を保存できない」と出すだけになる — **測定は成功した
    ように見えて head は 1 つも増えない**。
    """
    assert "def ingest_file(store: ArtifactStore" in STORE, "module 関数のはず"
    assert "    def ingest_file(" not in STORE, "メソッドではない"
    body = ARMS[ARMS.index("def _save_deployable_head("):]
    body = body[:body.index("\ndef ", 10)]
    assert "store.ingest_file(" not in body, "メソッドとして呼んでいる"
    assert "ingest_file(store," in body


def test_the_measurement_publishes_the_hash_of_the_bytes():
    body = ARMS[ARMS.index("def _save_deployable_head("):]
    body = body[:body.index("\ndef ", 10)]
    assert 'out["artifact_sha256"] = sha' in body


# ── 継ぎ目 3: 昇格 → artifact 行 → policy ────────────────────────────────

def test_promotion_registers_the_artifact_before_the_policy():
    """policy.content_sha256 は `tagger_artifact` への FK。行が無ければ
    policy は**絶対に書けない**。"""
    body = REVIEW[REVIEW.index("async def promote_validated("):]
    art = body.index("INSERT INTO tagger_artifact")
    pol = body.index("tagger_governed_upsert_head_policy")
    assert art < pol, "artifact 行を作る前に policy を書こうとしている"


def test_only_a_real_hash_is_registered_as_an_artifact():
    """`(none: insufficient_gt_for_cv)` のような固定文字列を artifact として
    登録すると、**model でないものに FK が「実体がある」と言う**。"""
    assert "_looks_like_sha256" in REVIEW
    assert 'r"[0-9a-f]{64}"' in REVIEW


# ── 継ぎ目 4: GC → dispatch ──────────────────────────────────────────────

def test_the_gc_reopens_both_the_lifecycle_and_the_candidate_row():
    """主ループが読むのは lifecycle ではなく `tagger_head_candidate` の
    open 行。片方だけ戻すと **5 件戻して 0 件 dispatch** になる (実測)。"""
    body = RECON[RECON.index("async def _reap_approved_without_head("):]
    body = body[:body.index("\nasync def ", 10)]
    assert 'to_stage="candidate"' in body
    assert "tagger_governed_reopen_headless_candidate" in body


# ── 継ぎ目 5: policy → reap ──────────────────────────────────────────────

def test_reap_authority_comes_from_the_schema():
    """手書き JSON だけを見ていると、昇格しても列に載らない限り永久に
    掃除されない (monochrome: corpus 601,069 件・in-gate 精度 0.031)。"""
    assert "tagger_head_policy" in TAGGING, "reap 権限が policy を見ていない"
    assert "apply_mode = 'replace'" in TAGGING


def test_reap_authority_is_actually_used_not_just_defined():
    """**宣言して使っていない**を今日 6 回踏んだ。定義だけで終わらせない。"""
    assert "_reap_authorized_tags(db)" in TAGGING, "定義しただけで呼んでいない"
    i = TAGGING.index("reap_gates = ")
    assert "_auth" in TAGGING[i:i + 120]


def test_the_transition_keeps_the_tags_currently_being_cleaned():
    """置き換えだけすると、policy 行をまだ持たない 9 タグの掃除が**止まる**。
    和を取る (JSON は減る一方で、増えるのは policy だけ)。"""
    body = TAGGING[TAGGING.index("async def _reap_authorized_tags("):]
    body = body[:body.index("\n# ", 10)]
    assert "tags = set(_reap_authorized())" in body
    assert "tags |= set(rows)" in body


def test_losing_the_policy_read_does_not_stop_existing_cleanup():
    body = TAGGING[TAGGING.index("async def _reap_authorized_tags("):]
    body = body[:body.index("\n# ", 10)]
    assert "except Exception" in body
    assert "旧 JSON の" in body
