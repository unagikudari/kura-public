"""WD14 worker が宣言済みアンサンブルの character 成分 (pixai) を適用することを固定する。

■ 2026-09-04 の実害 (user 報告「最近のキャラが判定されない」)

pixai-tagger-v0.9 は release アンサンブルの**宣言済み component**
(tagger_release_component role='pixai') で、実測 ellen_joe を score 1.0 で
当てる。しかし適用していたのは primary の full pipeline だけ — スループットの
98.5% を担う WD14 worker は `infer()` で cat==4 を捨てており、**直近 30 日で
キャラタグを付与した host は primary のみ (9,118 件)** だった。content head の
2026-08-04 の教訓 (「99.4% が head を一度も通らない」) と同じ、宣言された
アンサンブルと配備の乖離。

固定するのは:
  - worker が pixai char pass を実行し、character category で送ること。
  - 実装 (前処理・語彙・閾値) の本籍が tagger/pixai_chars.py に 1 つだけ
    あり、infer.py と worker の両方がそこを読むこと (0138 の drift 教訓)。
  - pixai が積めなければ claim せず、推論障害を空の予測にしないこと。
  - provenance (ensemble_source_id) が pixai の有無を刻むこと — 積めなかった
    個体の self-retraction が pixai 個体のキャラを別 actor として触れない側に
    倒すため。
"""
import sys
import pytest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "tagger"))

from tagger import nodec_autotag_worker as w  # noqa: E402
from tagger import pixai_chars  # noqa: E402


def test_worker_sends_characters_with_their_category():
    tags = w.build_tags(["1girl"], None, ["ellen_joe"])
    assert {"name": "ellen_joe", "category": "character"} in tags, (
        "worker が character を category 付きで送っていない — サーバ側で "
        "general として作られるか、落ちる")


def test_process_path_runs_the_char_pass():
    src = (REPO / "tagger" / "nodec_autotag_worker.py").read_text()
    body = src[src.index("def process_item"):src.index("def main")]
    assert "apply_pixai_chars" in body, (
        "process_item が char pass を呼んでいない — 宣言済み component の不適用が再発")


def test_the_implementation_lives_in_one_place():
    infer_src = (REPO / "tagger" / "infer.py").read_text()
    assert "from .pixai_chars import" in infer_src, (
        "infer.py が pixai_chars を読んでいない — 前処理/閾値の二重実装は必ず drift する")
    # 閾値の本籍は pixai_chars (tagger_producer_threshold の鏡)
    assert pixai_chars.PIXAI_THRESH_CHAR == 0.8


def test_missing_required_model_is_not_an_empty_prediction():
    old = w.PIXAI_CHARS
    try:
        w.PIXAI_CHARS = None
        with pytest.raises(pixai_chars.RequiredCharacterComponentError, match="not loaded"):
            w.apply_pixai_chars(object())
        assert "pixai=absent" in w.ensemble_source_id(14, None), (
            "pixai 不在が provenance に刻まれない — self-retraction の actor 分離が壊れる")
    finally:
        w.PIXAI_CHARS = old


def test_quarantine_applies_to_characters_too():
    old = list(w.QUARANTINE_TAGS)
    try:
        tags = w.build_tags([], None, [next(iter(w.QUARANTINE_TAGS))] if w.QUARANTINE_TAGS else [])
        assert all(t["category"] != "character" or t["name"] not in w.QUARANTINE_TAGS
                   for t in tags)
    finally:
        assert list(w.QUARANTINE_TAGS) == old
