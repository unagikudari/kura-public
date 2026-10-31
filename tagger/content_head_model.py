"""タグ検証用 content-head の共通推論モデル定義 (chastity_cage v1/v2, chastity_belt v1)。

PixAI / ml-danbooru が過検知するタグを、画像 content から P(本物) を出す二値 head で
検証する枠組み (plan kura-chastity-cage-overdetection-hotfix A2/A4 の一般化)。
checkpoint は backbone+head をバンドル — 推論時に timm pretrained / HF hub download
へ依存しない。meta.backbone で backbone を判別し、前処理 (img_size/mean/std) は
model 属性から導出する — 学習時特徴抽出と同一であることを型で保証する。

対応 backbone:
- convnext_tiny (384 + ImageNet norm) — cage v1 (legacy)
- wd-swinv2-tagger-v3 penultimate (448 + 0.5/0.5) — cage v2 / belt v1。hub config
  (architecture/model_args) をここに固定で再現し、runtime は hub に接続しない。
"""
from __future__ import annotations

import torch
import torch.nn as nn
import timm

BACKBONE = "convnext_tiny"  # legacy default (meta.backbone 無しの v1 checkpoint)
IMG_SIZE = 384              # legacy convnext 入力サイズ

WD_SWINV2 = "hf-hub:SmilingWolf/wd-swinv2-tagger-v3"
_WD_SWINV2_ARGS = dict(img_size=448, window_size=14, act_layer="gelu_tanh")


class ContentHeadModel(nn.Module):
    """frozen backbone 特徴 + 線形 head -> P(タグが本物)。"""

    def __init__(self, backbone: str = BACKBONE):
        super().__init__()
        self.backbone_name = backbone
        if backbone == BACKBONE:
            self.backbone = timm.create_model(BACKBONE, pretrained=False, num_classes=0)
            self.img_size = IMG_SIZE
            self.mean = (0.485, 0.456, 0.406)
            self.std = (0.229, 0.224, 0.225)
        elif backbone == WD_SWINV2:
            self.backbone = timm.create_model(
                "swinv2_base_window8_256", pretrained=False, num_classes=0,
                **_WD_SWINV2_ARGS)
            self.img_size = _WD_SWINV2_ARGS["img_size"]
            self.mean = (0.5, 0.5, 0.5)
            self.std = (0.5, 0.5, 0.5)
        else:
            raise ValueError(f"unknown content-head backbone: {backbone!r}")
        self.head = nn.Linear(self.backbone.num_features, 1)

    def forward(self, x):
        return self.head(self.backbone(x)).squeeze(-1)


# **同じ backbone を何度も持たない。**
#
# 実測 2026-08-06: 配備中の head bundle 9 件は backbone が byte 単位で同一
# (指紋 52a4792c5efc)。1 件 348 MB のうち **役に立つのは head.weight (1x1024) と
# head.bias の 4.1 KB だけ**で、残りは全部同じ凍結 backbone の複製。
# `load()` を 9 回呼ぶと同一の backbone が 9 コピーメモリに載る (~3.1 GB)。
#
# desk (WSL の作業機) で tagger worker が **5.2 GB / WSL 全体 12 GB** を占め、
# Windows ホストの空きが 2.8 GB まで落ちて `friction/host-ram-pressure` が
# 48 時間鳴り続けていた。その主因がこれ。
#
# backbone は frozen — 学習で変わらないのだから、プロセスに 1 つあれば足りる。
# **形式は変えない。** bundle の中身はそのままで、読む側が共有するだけで
# 9 コピーが 1 コピーになる (disk はまだ重複しているが、それは別の話)。
_SHARED: dict[str, "ContentHeadModel"] = {}


def _shared_backbone(name: str) -> nn.Module | None:
    m = _SHARED.get(name)
    return None if m is None else m.backbone


def load(checkpoint_path) -> ContentHeadModel:
    ckpt = torch.load(checkpoint_path, map_location="cpu", weights_only=False)
    meta = ckpt.get("meta") or {}
    name = meta.get("backbone", BACKBONE)
    sd = ckpt["model"]

    shared = _shared_backbone(name)
    if shared is not None:
        # 2 件目以降: backbone は共有し、この head の probe だけ載せる。
        # **backbone 重みが本当に同じ前提に乗るので、違っていたら黙って
        # 共有しない** — 静かに別モデルの特徴で判定するのが一番危ない。
        first = _SHARED[name]
        if _backbone_fingerprint(sd) == first._backbone_fp:  # noqa: SLF001
            model = ContentHeadModel.__new__(ContentHeadModel)
            nn.Module.__init__(model)
            model.backbone_name = name
            model.backbone = shared
            model.img_size, model.mean, model.std = (
                first.img_size, first.mean, first.std)
            model.head = nn.Linear(shared.num_features, 1)
            model.head.load_state_dict(
                {k.split(".", 1)[1]: v for k, v in sd.items() if k.startswith("head.")})
            model._backbone_fp = first._backbone_fp  # noqa: SLF001
            model.eval()
            return model

    model = ContentHeadModel(backbone=name)
    model.load_state_dict(sd)
    model._backbone_fp = _backbone_fingerprint(sd)  # noqa: SLF001
    model.eval()
    _SHARED.setdefault(name, model)
    return model


def _backbone_fingerprint(state_dict) -> str:
    """backbone 重みの指紋。共有してよいかの判定に使う。

    形だけ (鍵の集合や shape) では足りない — 別の重みでも一致してしまう。
    値そのものを見る。
    """
    import hashlib
    h = hashlib.sha256()
    for k, v in sorted(state_dict.items()):
        if k.startswith("backbone."):
            h.update(k.encode())
            h.update(v.detach().contiguous().numpy().tobytes())
    return h.hexdigest()
