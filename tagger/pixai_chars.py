# Description: PixAI tagger の character 判定 — fleet 全 worker が共有する唯一の実装
# Kind: library
# Effects: read-only (HF hub からのモデル取得 + 推論のみ)
# Runs-on: any (CPUExecutionProvider 固定 — GPU 状態に触れない)
"""PixAI character component — fleet 共有の唯一の実装。

■ なぜこのモジュールか (2026-09-04, user 報告「最近のキャラが判定されない」)

pixai-tagger-v0.9 は release アンサンブルの**宣言済み component**
(tagger_release_component role='pixai', sha a8d479098b5e…) で、全タグ語彙
13,461 (うち character 3,720) (ZZZ/NIKKE 世代を含む) を持ち、実測で ellen_joe を score 1.0 で当てる。
しかし適用していたのは primary の full pipeline (tagger/infer.py) だけ —
スループットの 98.5% を担う WD14-only worker (nodec_autotag_worker.py) は
character を一切付けておらず、**直近 30 日でキャラタグを付与したのは primary の
9,118 件のみ**だった。content head のとき (2026-08-04、「99.4% が head を
一度も通らない」) と同じ形の、宣言されたアンサンブルと配備の乖離。

前処理・語彙・閾値を infer.py と worker の 2 箇所に書くと必ず drift する
(0138 の教訓: 同名の gate が worker で all、infer で any と逆だった)。
だから実装はここに 1 つだけ置き、両方がこれを import する。

■ 閾値

PIXAI_THRESH_CHAR = 0.8 は tagger_producer_threshold (producer='pixai',
category=4) の鏡。行の値を変えるときはここも変えること (worker は primary の
DB を読めないため、値そのものはコードが運ぶ — infer.py が従来からそうして
いる形を維持)。
"""
from __future__ import annotations

import hashlib
import io
import os
from pathlib import Path

from PIL import Image

# numpy / onnxruntime / huggingface_hub は関数内で import する — この module は
# api venv (numpy 無し) からも import される (テスト・将来のサーバ側利用)。
# 重い依存は実際に推論する worker 側でだけ要求する。


class RequiredCharacterComponentError(RuntimeError):
    """Infrastructure could not evaluate characters; this is not an empty prediction."""

CHARACTER_POLICY = "characters=corrected-v1"

PIXAI_REPO = "deepghs/pixai-tagger-v0.9-onnx"
PIXAI_FILES = ("model.onnx", "selected_tags.csv")
#: tagger_producer_threshold (pixai, category=4) の鏡 — 変更は両方同時に。
PIXAI_THRESH_CHAR = 0.8
INPUT_SIZE = 448

MODELS_DIR = Path(os.environ.get("KURA_MODELS_DIR")
                  or (Path.home() / ".cache" / "kura-tagger" / "models"))
PIXAI_DIR = MODELS_DIR / "pixai"


def ensure_files() -> tuple[Path, Path]:
    """model.onnx + selected_tags.csv をローカルへ (無ければ HF hub から取得)。"""
    from huggingface_hub import hf_hub_download
    for fname in PIXAI_FILES:
        if not (PIXAI_DIR / fname).exists():
            hf_hub_download(PIXAI_REPO, fname, local_dir=str(PIXAI_DIR))
    return PIXAI_DIR / "model.onnx", PIXAI_DIR / "selected_tags.csv"


def preprocess(img: Image.Image, size: int = INPUT_SIZE):
    """infer.py の _preprocess_pixai と同一 (白背景合成 → 448 BILINEAR → ±1 正規化)。
    実装はここが本籍 — infer.py はこれを呼ぶ。"""
    import numpy as np
    img = img.convert("RGBA")
    bg = Image.new("RGBA", img.size, (255, 255, 255, 255))
    bg.paste(img, mask=img.split()[3])
    img = bg.convert("RGB").resize((size, size), Image.BILINEAR)
    arr = np.array(img, dtype=np.float32) / 255.0
    arr = (arr - 0.5) / 0.5
    arr = arr.transpose(2, 0, 1)
    return np.expand_dims(arr, axis=0)


def preprocess_bytes(image_bytes: bytes, size: int = INPUT_SIZE):
    return preprocess(Image.open(io.BytesIO(image_bytes)), size)


def load(intra_op_threads: int = 4) -> dict:
    """character 判定用の軽量 state を作る。**CPUExecutionProvider 固定** —
    この component は WD14 worker の GPU/torch 状態 (nodeb の ROCm 等) に一切
    触れない。失敗時は例外を上げる (呼び手が fail-soft を選ぶ)。

    返り値: {"sess", "tags" (category==4 の index→name), "sha12"}。
    sha12 は ensemble_source_id / 非否認用 — release component 宣言
    (tagger_release_component role='pixai') と同じ content hash。
    """
    import csv

    import onnxruntime as rt
    onnx_path, csv_path = ensure_files()
    so = rt.SessionOptions()
    so.intra_op_num_threads = intra_op_threads
    sess = rt.InferenceSession(str(onnx_path), so, providers=["CPUExecutionProvider"])
    char_idx: dict[int, str] = {}
    with open(csv_path, newline="", encoding="utf-8") as f:
        for i, row in enumerate(csv.DictReader(f)):
            if int(row.get("category", 0)) == 4:
                char_idx[i] = row["name"]
    h = hashlib.sha256()
    with open(onnx_path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    if not char_idx:
        raise RequiredCharacterComponentError("PixAI character vocabulary is empty")
    return {"sess": sess, "chars": char_idx, "tag_count": i + 1, "sha12": h.hexdigest()[:12]}


def char_tags(state: dict, img: Image.Image,
              thresh: float = PIXAI_THRESH_CHAR) -> list[tuple[str, float]]:
    """1 画像の character タグ [(name, score), …] (score 降順、thresh 以上のみ)。"""
    scores = prediction_vector(state["sess"], preprocess(img), state.get("tag_count"))
    if len(scores) <= max(state["chars"], default=-1):
        raise RequiredCharacterComponentError("character vocabulary exceeds prediction length")
    hits = [(name, float(scores[i])) for i, name in state["chars"].items()
            if scores[i] >= thresh]
    hits.sort(key=lambda x: x[1], reverse=True)
    return hits


def require_character_heads(heads: dict | None) -> None:
    for tag in heads or {}:
        if tag.startswith('__'):
            continue
        policy = heads.get(f'__policy__{tag}') or {}
        if policy.get('category') != 'character':
            continue
        if heads[tag] is None:
            raise RequiredCharacterComponentError(f'required character head {tag} is not loaded')
        threshold = policy.get('threshold')
        if (isinstance(threshold, bool) or not isinstance(threshold, (int, float))
                or not 0 <= threshold <= 1):
            raise RequiredCharacterComponentError(f'character head {tag} requires a calibrated threshold in [0, 1]')
        if policy.get('apply_mode') not in ('replace', 'add_only'):
            raise RequiredCharacterComponentError(f'character head {tag} requires a valid apply mode')


def character_head_source(heads: dict | None) -> str:
    import json
    policies = {tag: {'sha': heads.get(f'__sha__{tag}'), **heads[f'__policy__{tag}']}
                for tag in (heads or {}) if not tag.startswith('__')
                and (heads.get(f'__policy__{tag}') or {}).get('category') == 'character'}
    if not policies:
        return ''
    digest = hashlib.sha256(json.dumps(policies, sort_keys=True).encode()).hexdigest()[:12]
    return f'character-head-policy={digest}'


def prediction_vector(session, inputs, expected_size=None, *, output_name="prediction"):
    """A failed/invalid model output must never be interpreted as all-negative."""
    import numpy as np
    try:
        input_name = session.get_inputs()[0].name
        output_names = [output.name for output in session.get_outputs()]
        outputs = session.run(None, {input_name: inputs})
        scores = np.asarray(outputs[output_names.index(output_name) if output_name is not None else 0][0])
        if (scores.ndim != 1 or not len(scores)
                or (expected_size is not None and len(scores) != expected_size)
                or not np.isfinite(scores).all() or (scores < 0).any() or (scores > 1).any()):
            raise ValueError("invalid prediction shape or probabilities")
        return scores
    except Exception as exc:
        raise RequiredCharacterComponentError(f"character prediction unavailable: {exc}") from exc
