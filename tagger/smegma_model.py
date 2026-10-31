"""smegma/foreskin/phimosis タガー v1 のモデル定義 (推論専用)。

学習は kura リポジトリ外 (~/lora-data/tagger-smegma-foreskin-phimosis/train_tagger.py、
nodec 上、898枚の v3 教師選別データ、2026-07-11)。ここでは推論に必要な
アーキテクチャ定義のみを持つ — 学習コードとは意図的に分離 (kura はタグ付け
パイプラインの本籍、学習パイプラインの本籍ではない)。
"""
from __future__ import annotations

import torch
import torch.nn as nn
import timm

SKIN_CLASSES = ["bare", "foreskin", "phimosis"]
BACKBONE = "convnext_tiny"
IMG_SIZE = 384


class TaggerModel(nn.Module):
    def __init__(self):
        super().__init__()
        self.backbone = timm.create_model(BACKBONE, pretrained=False, num_classes=0)
        feat_dim = self.backbone.num_features
        self.skin_head = nn.Linear(feat_dim, len(SKIN_CLASSES))
        self.smegma_head = nn.Linear(feat_dim, 1)
        self.precum_head = nn.Linear(feat_dim, 1)

    def forward(self, x):
        feat = self.backbone(x)
        return self.skin_head(feat), self.smegma_head(feat).squeeze(-1), self.precum_head(feat).squeeze(-1)


def load(checkpoint_path) -> TaggerModel:
    ckpt = torch.load(checkpoint_path, map_location="cpu", weights_only=False)
    model = TaggerModel()
    model.load_state_dict(ckpt["model"])
    model.eval()
    return model
