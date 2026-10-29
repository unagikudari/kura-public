#!/usr/bin/env python3
# Description: 再学習した content head を推論ノードへ配れる形へ変換し、実 load 経路で検証する。
# Kind: tool
# Effects: mutating (変換結果を出力先へ書く。--verify-only なら read-only)
# Requires: torch, timm, safetensors, psycopg2 (comfyui venv)
"""再学習した線形プローブを ContentHeadModel の checkpoint へ変換する。

plan `kura-head-distribution-and-sweep-run` rev3 (approved、audit c9d769f5) 相 A。

**なぜ要るか**: `~/tagger-heads` の再学習は generation 66 まで進んでいるのに、
推論ノードが読む head は 7 月の手置き版のまま。`auto_review.py` の docstring が
「content_head model file distribution is not yet automated」と自分で書いており、
登録→promote→patch 行までは自動なのに**モデル本体を配る工程が無い**。

**2 つの形式のギャップ**:
  学習側 (train_heads.py): safetensors `{w(1024), b(1), mu(1024), sd(1024)}`
      p = sigmoid( ((f - mu)/sd) @ w + b )     f = backbone 特徴 (1024)
  推論側 (content_head_model.load): `{'model': backbone+head の state_dict,
      'meta': {'backbone': ...}}`、`ContentHeadModel` = frozen backbone + Linear(1024,1)
      p = sigmoid( head(backbone(image)) )

backbone は両者とも `hf-hub:SmilingWolf/wd-swinv2-tagger-v3` で同一なので、
**標準化を線形層へ畳めば**等価になる:
      w' = w / sd,   b' = b − Σ(w · mu / sd)

**線形層だけの checkpoint を書いてはいけない** (レビュー指摘、実測で確認済み):
`content_head_model.load` は backbone を同梱した state_dict を要求し、
線形層だけだと `RuntimeError` で落ちるか、backbone が `pretrained=False` の
random のまま head だけ載って**ゴミ推論**になる。だからここは backbone ごと
bundle する (1 本あたり約 348 MB)。

**gate は生画像から通す** (レビュー指摘、最重要):
1024 次元の特徴を head に直接入れて比べると、**backbone が random でも一致する**
ので検証にならない。検証は必ず `画像 → load() した model → 確率` の経路で行う。
判定は (i) 確率差の最大 < 1e-4 (ii) **閾値でのラベル反転 0 件**。

**negative control** (レビュー指摘): わざと壊した head が REJECT されることを
確認してから使う。通ることだけ確かめた gate は、通っても何も意味しない。
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import sys
from pathlib import Path

import psycopg2
import timm
import torch
from PIL import Image
from safetensors.torch import load_file

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
from tagger.content_head_model import WD_SWINV2, ContentHeadModel, load  # noqa: E402
from tagger import display_bytes  # noqa: E402

HEADS_DIR = Path.home() / "tagger-heads" / "heads"
OUT_DIR = Path.home() / "tagger-heads" / "dist"

# w' = w/sd は sd_i が 0 近傍で発散する。**黙って inf/nan を書かない**。
SD_FLOOR = 1e-6
# gate の判定 (生画像 end-to-end)
MAX_PROB_DIFF = 1e-4
MIN_SAMPLES = 200
THRESHOLD = 0.5


def _rows(sql, args=None):
    from tagger.kura_db import connect_read   # bulk reads (tagger/kura_db.py)
    conn = connect_read()
    try:
        cur = conn.cursor()
        cur.execute(sql, args)
        return cur.fetchall()
    finally:
        conn.close()


def fold(probe: dict) -> tuple[torch.Tensor, torch.Tensor]:
    """標準化を線形層へ畳む。sd が床を割ったら例外 (配らない)。"""
    w, b, mu, sd = probe["w"], probe["b"], probe["mu"], probe["sd"]
    sd_min = float(sd.abs().min())
    if sd_min < SD_FLOOR:
        raise ValueError(
            f"sd の最小絶対値 {sd_min:.3g} が床 {SD_FLOOR} を下回る — "
            "w/sd が発散するので変換しない")
    return (w / sd).reshape(1, -1), (b - (w * mu / sd).sum()).reshape(1)


def build_bundle(tag: str) -> tuple[Path, ContentHeadModel]:
    """backbone を同梱した checkpoint を書く。"""
    probe = load_file(HEADS_DIR / f"{tag}.safetensors")
    w2, b2 = fold(probe)
    model = bundle_from_folded(w2, b2)
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out = OUT_DIR / f"{tag}-content-head.pt"
    torch.save({"model": model.state_dict(), "meta": {"backbone": WD_SWINV2}}, out)
    return out, model


def bundle_from_folded(w2, b2) -> ContentHeadModel:
    """畳み込み済み w/b から、**推論側が load できる形**のモデルを組む。

    `content_head_model.load` は `torch.load` で
    `{'model': backbone+head の state_dict}` を要求する。2026-08-05 に測定側が
    生の `{w, b}` safetensors を artifact として出し、**3 ノードすべてで
    `invalid load key` になった** — 作る形と読む形が違うと、配布経路が
    どれだけ正しくても load できない。

    ここに括り出すのは、同じ組み立てを測定側にも書くと片方だけ直したときに
    ずれるため。
    """
    model = ContentHeadModel(backbone=WD_SWINV2)
    # **backbone に学習済み重みを入れる。** ContentHeadModel は
    # `pretrained=False` で構築するので、ここで入れないと backbone は random の
    # まま head だけが載る。2026-08-04 に実際にそれをやり、生画像 gate が
    # 確率差 1 / ラベル反転 88 件で弾いた。
    # **その手前でローカル検算は通っていた** — 両側とも同じ random backbone で
    # 比べていたので一致してしまう (vacuous pass)。だから gate は生画像から
    # 通さなければならない。
    pretrained = timm.create_model(WD_SWINV2, pretrained=True)
    pretrained.reset_classifier(0)
    missing, unexpected = model.backbone.load_state_dict(
        pretrained.state_dict(), strict=False)
    if missing:
        raise ValueError(f"backbone の重みが埋まらない: missing={missing[:5]}")
    if model.backbone.num_features != w2.numel():
        raise ValueError(
            f"次元不一致: head {w2.numel()} vs backbone "
            f"{model.backbone.num_features} — 同じ特徴空間ではない")
    with torch.no_grad():
        model.head.weight.copy_(w2)
        model.head.bias.copy_(b2)

    return model


def _sample_images(tag: str, n: int) -> list[bytes]:
    """検証用の画像。**対象タグの陽性・陰性・その他**を混ぜる。

    確率差の検証には任意の画像で足りるが、**閾値近傍の反転**を見るには
    対象タグ周辺の画像が要る (無関係な画像ばかりだと全部 0 付近に張り付き、
    境界を一度も踏まないまま green になる)。
    """
    pos = _rows("select p.id from post p join tagger_gt_label g on g.post_id=p.id "
                "where g.tag=%s and g.verdict='positive' and p.media_location = 'pg' limit %s",
                (tag, n // 2))
    neg = _rows("select p.id from post p join tagger_gt_label g on g.post_id=p.id "
                "where g.tag=%s and g.verdict='negative' and p.media_location = 'pg' limit %s",
                (tag, n // 2))
    ids = [r[0] for r in pos + neg]
    if len(ids) < n:  # GT が足りなければ corpus から埋める (境界を踏む数を確保)
        extra = _rows("select id from post where media_location = 'pg' and type='image' "
                      "order by id desc limit %s", (n - len(ids),))
        ids += [r[0] for r in extra]
    return [data for _, data in display_bytes.load(_rows, ids)]


def _probs_via_probe(imgs, backbone, tf, probe) -> torch.Tensor:
    """学習側の経路: 画像 → backbone → 標準化 → 線形 → sigmoid。"""
    w, b, mu, sd = probe["w"], probe["b"], probe["mu"], probe["sd"]
    out = []
    for i in range(0, len(imgs), 16):
        x = torch.stack([tf(im) for im in imgs[i:i + 16]])
        with torch.no_grad():
            f = backbone(x).float()
        out.append(torch.sigmoid(((f - mu) / sd) @ w + b))
    return torch.cat(out)


def _probs_via_bundle(imgs, model, tf) -> torch.Tensor:
    """推論側の経路: 画像 → load() した model → sigmoid。"""
    out = []
    for i in range(0, len(imgs), 16):
        x = torch.stack([tf(im) for im in imgs[i:i + 16]])
        with torch.no_grad():
            out.append(torch.sigmoid(model(x)))
    return torch.cat(out)


def verify(tag: str, bundle_path: Path, n: int = MIN_SAMPLES) -> dict:
    """**生画像 end-to-end** で等価性を確かめる。ここが gate。"""
    probe = load_file(HEADS_DIR / f"{tag}.safetensors")
    backbone = timm.create_model(WD_SWINV2, pretrained=True)
    backbone.reset_classifier(0)
    backbone.eval()
    cfg = timm.data.resolve_data_config({}, model=backbone)
    tf = timm.data.create_transform(**cfg)

    raw = _sample_images(tag, n)
    imgs = []
    for b in raw:
        try:
            imgs.append(Image.open(io.BytesIO(b)).convert("RGB"))
        except Exception:  # noqa: BLE001
            continue
    if len(imgs) < n:
        return {"ok": False, "why": f"検証画像が {len(imgs)} 枚しか集まらない (< {n})"}

    # **load() を通す** — 保存した checkpoint が実際に読めることも同時に確かめる
    model = load(bundle_path)
    model.eval()

    p_probe = _probs_via_probe(imgs, backbone, tf, probe)
    p_bundle = _probs_via_bundle(imgs, model, tf)
    diff = float((p_probe - p_bundle).abs().max())
    flips = int(((p_probe >= THRESHOLD) != (p_bundle >= THRESHOLD)).sum())
    near = int((((p_probe - THRESHOLD).abs() < 0.05)).sum())

    ok = diff < MAX_PROB_DIFF and flips == 0
    return {"ok": ok, "n": len(imgs), "max_prob_diff": diff, "label_flips": flips,
            "near_threshold_samples": near,
            "why": "" if ok else f"確率差 {diff:.3g} / 反転 {flips} 件"}


def negative_control(tag: str, bundle_path: Path) -> dict:
    """**わざと壊した head が REJECT されること**を確かめる。

    これが無いと、gate が本当に効いているのか vacuous に pass しているのかを
    区別できない。**牙があることを証明してから使う。**
    """
    ck = torch.load(bundle_path, map_location="cpu", weights_only=False)
    sd_ = dict(ck["model"])
    sd_["head.weight"] = sd_["head.weight"] * -1.0  # 符号反転 = 別の判定になる
    bad = OUT_DIR / f"{tag}-NEGATIVE-CONTROL.pt"
    torch.save({"model": sd_, "meta": ck["meta"]}, bad)
    try:
        r = verify(tag, bad, n=MIN_SAMPLES)
        rejected = not r["ok"]
        return {"rejected": rejected,
                "why": "壊した head を gate が通してしまった" if not rejected else "",
                "detail": r}
    finally:
        bad.unlink(missing_ok=True)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("tags", nargs="+", help="変換するタグ")
    ap.add_argument("--samples", type=int, default=MIN_SAMPLES)
    ap.add_argument("--skip-negative-control", action="store_true",
                    help="**推奨しない** — gate に牙があることを確かめずに使う")
    args = ap.parse_args()

    results = {}
    failed = False
    for tag in args.tags:
        print(f"\n=== {tag} ===", flush=True)
        try:
            path, _ = build_bundle(tag)
        except Exception as e:  # noqa: BLE001
            print(f"  変換できない: {e}")
            results[tag] = {"ok": False, "why": str(e)}
            failed = True
            continue
        size_mb = path.stat().st_size / 1e6
        sha = hashlib.sha256(path.read_bytes()).hexdigest()
        print(f"  bundle: {path.name} {size_mb:.1f}MB sha={sha[:16]}", flush=True)

        v = verify(tag, path, n=args.samples)
        print(f"  等価性 (生画像 {v.get('n', 0)} 枚): 確率差 max="
              f"{v.get('max_prob_diff', float('nan')):.3g} "
              f"ラベル反転={v.get('label_flips')} 件 "
              f"閾値近傍={v.get('near_threshold_samples')} 枚 → "
              f"{'OK' if v['ok'] else '**NG: ' + v['why'] + '**'}", flush=True)

        nc = {"rejected": None}
        if v["ok"] and not args.skip_negative_control:
            nc = negative_control(tag, path)
            print(f"  negative control: 壊した head を "
                  f"{'REJECT した (gate は効いている)' if nc['rejected'] else '**通してしまった**'}",
                  flush=True)

        ok = v["ok"] and (nc["rejected"] is not False)
        results[tag] = {"ok": ok, "sha256": sha, "bytes": path.stat().st_size,
                        "path": str(path), "verify": v, "negative_control": nc}
        if not ok:
            failed = True
            path.unlink(missing_ok=True)   # **通らなかったものは残さない**
            print("  → 破棄した (配布しない)")

    out = OUT_DIR / "distribution_report.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(results, ensure_ascii=False, indent=1, default=float))
    print(f"\n報告: {out}")
    print("**gate を通ったものだけが dist/ に残る。**")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
