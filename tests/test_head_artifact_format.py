"""測定が作る artifact を、推論側が**そのまま**読めることを固定する。

2026-08-05 実測: 測定側は生の `{w, b}` safetensors (4KB) を出し、推論側の
`content_head_model.load` は `torch.load` で
`{'model': backbone+head の state_dict}` を要求していた。

    content-head horse_penis の load に失敗: invalid load key, '\\xf8'

**nodec / desk / nodeb の 3 ノードすべてで失敗。** 配布経路 (upload / policy /
hash 検証) はすべて正しく動いていたのに、**作る形と読む形が違うだけ**で
1 件も適用できなかった。

形式は 1 つだけにする。2 つ持てば、どちらで書かれたかを読む側が判別することに
なり、判別が要るということは 1 つに決めていないということ。
"""
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
ARMS = (REPO / "tagger" / "vnext_arms.py").read_text()
DIST = (REPO / "tagger" / "distribute_heads.py").read_text()
LOADER = (REPO / "tagger" / "content_head_model.py").read_text()


def _save_block() -> str:
    b = ARMS[ARMS.index("def _save_deployable_head("):]
    return b[:b.index("\ndef ", 10)]


def test_the_loader_has_one_format():
    """読む側が形式を判別していないこと。判別が要る = 1 つに決めていない。"""
    assert "torch.load(checkpoint_path" in LOADER
    assert "safetensors" not in LOADER


def test_the_measurement_emits_that_format():
    b = _save_block()
    assert "bundle_from_folded(" in b
    assert "torch.save(" in b
    assert "save_file(" not in b, "生の safetensors を artifact にしている"


def test_the_bundle_builder_is_not_duplicated():
    """同じ組み立てを 2 箇所に書くと、片方だけ直したときにずれる。"""
    assert "def bundle_from_folded(" in DIST
    body = DIST[DIST.index("def build_bundle("):]
    body = body[:body.index("\ndef ", 10)]
    assert "bundle_from_folded(" in body
    assert "timm.create_model" not in body


def test_the_backbone_weights_are_loaded():
    """`pretrained=False` のまま head だけ載せるとゴミ推論になる
    (2026-08-04 実測: 生画像 gate がラベル反転 88 件で弾いた)。"""
    b = DIST[DIST.index("def bundle_from_folded("):]
    b = b[:b.index("\ndef ", 10)]
    assert "pretrained=True" in b
    assert "if missing:" in b


def test_the_feature_dimension_is_checked():
    b = DIST[DIST.index("def bundle_from_folded("):]
    b = b[:b.index("\ndef ", 10)]
    assert "num_features != w2.numel()" in b
