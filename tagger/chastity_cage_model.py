"""chastity_cage content-head — 後方互換 shim。

実装は content_head_model.py に一般化済み (chastity_belt と共用、2026-07-19)。
このモジュールは既存 import (infer.py 旧版 / scripts/kura_chastity_sweep.py /
学習側 v2_build_checkpoint.py) を壊さないための re-export のみ。

背景・評価の経緯は content_head_model.py と
~/.cache/kura-tagger/models/chastity-cage/chastity_cage-content-head-v2.manifest.json
を参照: PixAI (csv index 8981) の chastity_cage 過検知 (GT136 で FP ~73–75%) →
content-head が唯一の補正経路。v2 (2026-07-19): 教師 744 全人手判定、wd-swinv2
penultimate linear probe が 3 候補比較で勝利 (LOO AUC 0.988)、gate LO=0.05 で
removal-P=0.984 / Wilson95%下限=0.959 / real 自動喪失 4/400。
"""
from __future__ import annotations

from .content_head_model import (  # noqa: F401
    BACKBONE,
    IMG_SIZE,
    WD_SWINV2,
    ContentHeadModel,
    ContentHeadModel as ChastityCageModel,
    load,
)
