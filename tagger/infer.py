"""Model loading + inference for the kura auto-tagger pipeline.

PixAI primary -> ml-danbooru extras -> WD14 character fallback.

Ported from the legacy broker/scripts/auto_tagger.py, trimmed of
the szurubooru HTTP helpers (those are replaced by the claim/complete
calls in kura/tagger/loop.py). All model downloads live under
``KURA_MODELS_DIR`` (default ``~/.cache/kura-tagger/models``).
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import os
from pathlib import Path

# PyTorch バンドルの CUDA DLL を PATH に追加（onnxruntime-gpu が参照できるように）
try:
    import torch  # type: ignore
    _torch_lib = str(Path(torch.__file__).parent / "lib")
    os.environ["PATH"] = _torch_lib + os.pathsep + os.environ.get("PATH", "")
except ImportError:
    pass

import av
import numpy as np
import onnxruntime as rt
from huggingface_hub import hf_hub_download
from PIL import Image


# ── model locations ────────────────────────────────────────────────────

MODELS_DIR = Path(
    os.environ.get("KURA_MODELS_DIR")
    or (Path.home() / ".cache" / "kura-tagger" / "models")
)

# pixai の repo/前処理/char 閾値の本籍は tagger/pixai_chars.py (fleet 共有の
# 唯一の実装 — WD14 worker と drift させない、2026-09-04)。ここは再輸出。
from .pixai_chars import (  # noqa: E402
    PIXAI_REPO, PIXAI_THRESH_CHAR, preprocess_bytes as _pixai_preprocess_bytes)
PIXAI_DIR = MODELS_DIR / "pixai"
PIXAI_THRESH_GENERAL = 0.4

MLDANBOORU_REPO = "deepghs/ml-danbooru-onnx"
MLDANBOORU_FILE = "ml_caformer_m36_dec-5-97527.onnx"
MLDANBOORU_DIR = MODELS_DIR / "ml-danbooru"
MLDANBOORU_THRESH = 0.7

WD14_REPO = "SmilingWolf/wd-eva02-large-tagger-v3"
WD14_DIR = MODELS_DIR / "wd14"
WD14_THRESH_CHAR = 0.85

INPUT_SIZE = 448

# smegma/foreskin/phimosis 専用タガー v1 (2026-07-11、898枚の v3 教師選別データで
# fine-tune、WD14/ml-danbooru/PixAI の smegma 誤検出・foreskin under-tagging を
# 補正する目的)。HF hub には無い自前モデル — checkpoint は手動配置が必要
# (~/lora-data/tagger-smegma-foreskin-phimosis/output/ から scp)。
SMEGMA_TAGGER_DIR = MODELS_DIR / "smegma-foreskin-phimosis"
SMEGMA_TAGGER_CKPT = SMEGMA_TAGGER_DIR / "tagger-smegma-foreskin-phimosis-v1-best.pt"
SMEGMA_THRESH = 0.5
PRECUM_THRESH = 0.5

# chastity_cage content-head v2 (plan kura-chastity-cage-overdetection-hotfix)。
# PixAI (csv index 8981) が chastity_cage を過検知 (GT136 で FP ~73–75%、
# post_count 1464)。閾値 bump 無効 (真陽性 median 0.857 と偽陽性 median 0.605 が
# 重なる)、かつ chastity_cage は PixAI 専用出力 (WD14 語彙外) で logit patch 不能
# → content-head が唯一の経路。v2 (2026-07-19): 教師 744 (400 real / 344 FP、全
# 人手判定) で 3 候補比較 (convnext probe / convnext full FT / wd-swinv2 probe) →
# wd-swinv2 penultimate linear probe が勝利 (LOO AUC 0.988 vs 0.970 / 0.985)。
# checkpoint は backbone+head バンドルの自前モデル (手動配置、HF 接続不要)。
# v1 (convnext) は rollback 用に同 dir に温存 — 戻すのはファイル名 1 行。
CHASTITY_CAGE_TAGGER_DIR = MODELS_DIR / "chastity-cage"
CHASTITY_CAGE_CKPT = CHASTITY_CAGE_TAGGER_DIR / "chastity_cage-content-head-v2.pt"
# Apply Gate (A4): content-head P(本物の cage)。LO 未満=確信 FP→自動削除、
# [LO,HI)=不確実→タグ保持+人手プール route、HI 以上=確信 real→保持。
# LOO 完全 held-out 再校正 (2026-07-19, n=744, v2_report.py): LO=0.05 で自動削除域
# removal-P=0.984 / Wilson95%下限=0.959 / real 自動喪失 4/400 (survival 0.990)。
# 旧 LO=0.35 は v2 で real 14/400 を自動喪失するため 0.05 へ引き下げ (基準:
# removal Wilson-lo>=0.95 AND real 生存>=0.98 を満たす唯一の運用点)。境界
# [0.05,0.50) は 106/744 — タグ保持のまま人手 review へ (real 13 がここで救済)。
CHASTITY_CAGE_REMOVE_BELOW = 0.05   # LO — これ未満のみ自動削除
CHASTITY_CAGE_KEEP_ABOVE = 0.50     # HI — これ以上は確信 real で保持

# chastity_belt content-head v1 (2026-07-19)。ml-danbooru 語彙タグ (mldanbooru
# merge 後の all_scores に乗る) の過検知を cage gate と同型の Apply Gate で検証。
# 教師 377 (real 63 / FP 314、全人手判定)。3 候補 held-out 比較で wd-swinv2
# penultimate linear probe 勝利 (LOO AUC 0.998 vs convnext probe 0.971 / full FT
# 0.969)。checkpoint は backbone+head バンドル (cage v2 と同 backbone、別 head)。
CHASTITY_BELT_TAGGER_DIR = MODELS_DIR / "chastity-belt"
CHASTITY_BELT_CKPT = CHASTITY_BELT_TAGGER_DIR / "chastity_belt-content-head-v1.pt"
# LOO 校正 (2026-07-19, n=376, v2_report.py): LO=0.05 で removal-P=1.000 /
# Wilson95%下限=0.987 / real 自動喪失 0/62 (低スコア real 2 件は band [0.05,0.50)
# に入り人手 route で救済)。removal-R=0.936。HI=0.50 で FP 混入 6/314。
CHASTITY_BELT_REMOVE_BELOW = 0.05   # LO — これ未満のみ自動削除
CHASTITY_BELT_KEEP_ABOVE = 0.50     # HI — これ以上は確信 real で保持

# grey_owl content-heads v1 (2026-07-20, trainer-cc-primary 学習、pool 7578
# grey_owl verified GT 212枚 + round pools 7539-7573 由来)。cage/belt と同型
# (frozen wd-swinv2 penultimate + linear probe) だが、cage/belt の LO/HI Apply
# Gate(LOO再校正済み・削除も行う)とは違い、ここでは**削除安全性の校正データが
# 無い**(教師サイズが最小46/92と小さいものもある) — よって ADD-ONLY で運用する
# (score>=閾値でタグ追加のみ、既存タグの自動削除は一切しない。tagger/infer.py
# の既存方針「foreskin/phimosis: 専用モデルがpositiveのときのみ追加」と同型)。
# 閾値は trainer が報告した検証時のもの(80/20 split, th=0.5)をそのまま使う —
# 独自の再校正はしていない(根拠のない閾値を作らない)。
GREYOWL_HEADS_DIR = MODELS_DIR  # 各 head は "<tag雅称>/<tag>-content-head-v1.pt"
GREYOWL_SMALL_PENIS_CKPT = MODELS_DIR / "small-penis" / "small_penis-content-head-v2.pt"
GREYOWL_DARK_PENIS_CKPT = MODELS_DIR / "dark-penis" / "dark_penis-content-head-v2.pt"
GREYOWL_DARK_PERINEUM_CKPT = MODELS_DIR / "dark-perineum" / "dark_perineum-content-head-v2.pt"
# half-erect は v1 のまま据え置き (2026-08-04): 再学習版は等価性 gate を
# 通ったが、GT が 17 枚しかなく**現行版より悪くないことを確かめられなかった**。
# 判定不能を緑にしない — GT が育ってから差し替える。
GREYOWL_HALF_ERECT_CKPT = MODELS_DIR / "half-erect" / "half-erect-content-head-v1.pt"
GREYOWL_ADD_THRESH = 0.5
# val 評価 (2026-07-11): skin bare recall=0.97 / foreskin recall=0.63 / phimosis
# recall=0.77。smegma precision=1.00 recall=0.82 (val), hard-case 14/14 一致。
#
# 適用ポリシー (foreskin recall 0.63 の取りこぼしを踏まえた非対称マージ):
# - smegma/precum: 専用モデルの判定で完全上書き (既存タグを削除して置き換え) —
#   WD14/PixAI/ml-danbooru の smegma 誤検出 (色素沈着等) を正すのが目的なので、
#   専用モデルが negative と言えば既存の positive も消える。val precision=1.00
#   なので「smegma 無し」判定への信頼度が高い。
# - foreskin/phimosis: 専用モデルが positive のときのみ追加 (既存タグへの
#   加算)。専用モデルが bare (negative) と言っても既存タグは消さない —
#   recall 不足 (0.63) で既存モデルが正しく拾えていた検出まで消すのを防ぐ。


# ── thresholds / mutex rules ──────────────────────────────────────────

PIXAI_HIGH_THRESH: dict[str, float] = {
    "vaginal": 0.88,
    "vaginal_object_insertion": 0.88,
    "vaginal_fluid": 0.88,
    "cum_in_pussy": 0.88,
    "squirting": 0.88,
    "pee": 0.88,
    "peeing": 0.88,
    "urine": 0.88,
}
PIXAI_LOW_THRESH: dict[str, float] = {
    "smegma": 0.25,
}
MLDANBOORU_HIGH_THRESH: dict[str, float] = {
    "pee": 0.88,
    "peeing": 0.88,
    "urine": 0.88,
    "vaginal": 0.88,
    "vaginal_object_insertion": 0.88,
    "vaginal_fluid": 0.88,
    "squirting": 0.88,
    "cum_in_pussy": 0.88,
}
MLDANBOORU_LOW_THRESH: dict[str, float] = {
    "smegma": 0.45,
}

# 2026-07-15 quarantine: 人手判定で壊れていると実測されたが修復行が wd14-fixed
# に未搭載のタグ。condom_on_penis は pool 6348-6355 で 173 枚中正解 4、
# QUARANTINE_TAGS の canonical 定義は api/tagger_core/config.py に単一化(旧: ここ +
# nodec_autotag_worker.py の二重定義。理由コメントも config.py へ移設)。
from api.tagger_core.config import QUARANTINE_TAGS, WD14_FIXED_THRESH  # noqa: E402

MUTEX_PAIRS = [
    ("1girl", "multiple_girls"),
    ("1boy", "multiple_boys"),
    ("solo", "multiple_girls"),
    ("solo", "multiple_boys"),
    ("uncensored", "censored"),
    ("uncensored", "mosaic_censoring"),
    ("uncensored", "bar_censor"),
]
MUTEX_PAIRS_SOLO_ONLY = [
    ("open_mouth", "closed_mouth"),
    ("smile", "expressionless"),
    ("long_hair", "short_hair"),
    ("medium_hair", "short_hair"),
    ("medium_hair", "long_hair"),
]


def _is_solo(tag_scores: dict) -> bool:
    has_solo = "solo" in tag_scores
    has_1girl = "1girl" in tag_scores
    has_1boy = "1boy" in tag_scores
    has_multi = any(
        t in tag_scores
        for t in ("multiple_girls", "multiple_boys", "2girls", "2boys", "3girls", "3boys")
    )
    return (has_solo or has_1girl or has_1boy) and not has_multi


# ── model download + load ─────────────────────────────────────────────


def ensure_pixai() -> tuple[Path, Path]:
    PIXAI_DIR.mkdir(parents=True, exist_ok=True)
    for fname in ("model.onnx", "selected_tags.csv"):
        if not (PIXAI_DIR / fname).exists():
            hf_hub_download(PIXAI_REPO, fname, local_dir=str(PIXAI_DIR))
    return PIXAI_DIR / "model.onnx", PIXAI_DIR / "selected_tags.csv"


def ensure_mldanbooru() -> tuple[Path, Path]:
    MLDANBOORU_DIR.mkdir(parents=True, exist_ok=True)
    for fname in (MLDANBOORU_FILE, "classes.json"):
        if not (MLDANBOORU_DIR / fname).exists():
            hf_hub_download(MLDANBOORU_REPO, fname, local_dir=str(MLDANBOORU_DIR))
    return MLDANBOORU_DIR / MLDANBOORU_FILE, MLDANBOORU_DIR / "classes.json"


def ensure_wd14() -> tuple[Path, Path]:
    WD14_DIR.mkdir(parents=True, exist_ok=True)
    for fname in ("model.onnx", "selected_tags.csv"):
        if not (WD14_DIR / fname).exists():
            hf_hub_download(WD14_REPO, fname, local_dir=str(WD14_DIR))
    return WD14_DIR / "model.onnx", WD14_DIR / "selected_tags.csv"


def load_smegma_tagger():
    """HF hub には無い自前モデル。無ければ None を返し、呼び出し側でスキップする
    (kura-tagger 全体を落とさない — 段階ロールアウトを許容する設計)。"""
    if not SMEGMA_TAGGER_CKPT.exists():
        print(f"[kura-tagger] smegma tagger checkpoint not found at {SMEGMA_TAGGER_CKPT} — skipping", flush=True)
        return None
    from . import smegma_model
    return smegma_model.load(SMEGMA_TAGGER_CKPT)


def load_chastity_cage_head():
    """chastity_cage content-head (自前モデル)。無ければ None を返し呼び出し側で
    スキップ (段階ロールアウト — checkpoint 未配置ノードでも tagger は落とさない)。"""
    if not CHASTITY_CAGE_CKPT.exists():
        print(f"[kura-tagger] chastity_cage content-head not found at {CHASTITY_CAGE_CKPT} — skipping", flush=True)
        return None
    from . import content_head_model
    return content_head_model.load(CHASTITY_CAGE_CKPT)


def load_chastity_belt_head():
    """chastity_belt content-head (自前モデル)。cage と同じ graceful skip。"""
    if not CHASTITY_BELT_CKPT.exists():
        print(f"[kura-tagger] chastity_belt content-head not found at {CHASTITY_BELT_CKPT} — skipping", flush=True)
        return None
    from . import content_head_model
    return content_head_model.load(CHASTITY_BELT_CKPT)


def _load_greyowl_head(tag: str, ckpt: Path):
    """grey_owl content-head 群共通のロード (小さいが同型なので個別関数を4つ
    書かない)。cage/belt と同じ graceful skip -- checkpoint 未配置ノードでも
    tagger は落とさない。"""
    if not ckpt.exists():
        print(f"[kura-tagger] {tag} content-head not found at {ckpt} — skipping", flush=True)
        return None
    from . import content_head_model
    return content_head_model.load(ckpt)


def load_greyowl_heads() -> dict:
    """4つの grey_owl content-head (small_penis/dark_penis/dark_perineum/
    half-erect, 2026-07-20, trainer-cc-primary 学習・pool 7578 GT) を一括
    ロード。個別に None になり得る (段階配置)。"""
    return {
        "small_penis": _load_greyowl_head("small_penis", GREYOWL_SMALL_PENIS_CKPT),
        "dark_penis": _load_greyowl_head("dark_penis", GREYOWL_DARK_PENIS_CKPT),
        "dark_perineum": _load_greyowl_head("dark_perineum", GREYOWL_DARK_PERINEUM_CKPT),
        "half-erect": _load_greyowl_head("half-erect", GREYOWL_HALF_ERECT_CKPT),
    }


from tagger.api_target import api_origin
KURA_URL = api_origin(required=False)
WD14_FIXED_CACHE_DIR = MODELS_DIR / "wd14-fixed-release"
# Domain gates. A patched row whose GT negatives were all sampled inside one
# visual domain (the labia heads: uncensored∩pussy, patches/*/v1.json "apply")
# is only calibrated there -- on an image outside that domain the head sees
# out-of-distribution features and its measured precision means nothing. Such
# rows only fire when at least one gate tag is already present. The ungated
# rows override first (order matters: "pussy" is itself a patched row, and the
# labia gates must read its corrected verdict, not the stock ensemble's).
# The 9 heads of release id=2 run ungated -- that is exactly the behaviour the
# user validated on the manually-shipped wd14-fixed.pt (nodeb/nodec/desk).
WD14_FIXED_GATES: dict[str, tuple[str, ...]] = {
    "wrinkled_labia": ("pussy",),
    "dark_labia_majora": ("pussy",),
    "dark_labia_minora": ("pussy",),
}


def load_wd14_fixed():
    """Safely pull + verify the tagger registry's active release (plan:kura-tagger-
    registry rev2 Step 4-5, fact 58082f48) via tagger/release_client.py's fail-closed
    pull -- write-.partial -> fsync -> sha256 -> load+selftest -> atomic rename, with
    fallback to the last verified release and refuse-if-none semantics inside pull()
    itself.

    Before this change, the git-tracked infer.py had NO consumption path for the
    corrected (SwinV2, base_repo=SmilingWolf/wd-swinv2-tagger-v3) model at all --
    today's 9 patched tags (pussy/erection/fellatio/precum/phimosis/anilingus/
    newhalf/smegma/licking_testicle) only exist as a hand-built wd14-fixed.pt that
    was manually scp'd to nodeb/nodec/desk outside git (the drift this plan exists to
    close). This function is the safe DISTRIBUTION half of closing that gap.

    The corrected rows ARE consumed by tag_image() (task 439a2276): the ckpt's
    `patched` map ({tag: logit_index}, written by build_release.py) drives which
    rows override the ensemble -- data-driven, so a future release that adds a
    row needs no code change here (a release that needs a new GATE does, see
    WD14_FIXED_GATES). Like load_smegma_tagger(), returns None on any failure so
    a release-pull problem never blocks the PixAI/ml-danbooru/WD14-char pipeline
    this node already runs (staged rollout, same reasoning as the smegma tagger)."""
    token = os.environ.get("KURA_TOKEN")
    if not token or not KURA_URL:
        print("[kura-tagger] wd14-fixed: KURA_TOKEN unset, skipping release pull", flush=True)
        return None

    from . import release_client as rc
    from .credentials import token_headers
    try:
        headers = token_headers(os.environ.get('KURA_USER', ''), token)
        fetch_meta_fn, fetch_data_fn = rc.make_http_callbacks(KURA_URL, headers)
        verified = rc.pull(fetch_meta_fn, fetch_data_fn, WD14_FIXED_CACHE_DIR, rc.default_loader)
    except rc.ReleaseClientError as e:
        print(f"[kura-tagger] wd14-fixed: no usable release ({e}) — skipping", flush=True)
        return None
    except Exception as e:  # noqa: BLE001 -- any pull failure degrades to "unavailable", never crashes the tagger
        print(f"[kura-tagger] wd14-fixed: pull failed unexpectedly "
              f"({type(e).__name__}: {e}) — skipping", flush=True)
        return None

    import torch  # noqa: PLC0415
    import timm  # noqa: PLC0415
    ckpt = torch.load(str(verified.model_path), map_location="cpu", weights_only=False)
    model = timm.create_model(f"hf-hub:{ckpt['repo']}", pretrained=False, num_classes=ckpt["n_out"])
    model.load_state_dict(ckpt["state_dict"])
    model.eval()
    tags = _load_tag_csv(verified.csv_path)
    print(f"[kura-tagger] wd14-fixed: loaded release_id={verified.release_id}"
          f"{' (FALLBACK to previous verified release)' if verified.fallback else ''}, "
          f"n_out={ckpt['n_out']}, patched={sorted(ckpt.get('patched', {}))}", flush=True)

    # Report to tagger_node_state (plan Step 5/8: fleet drift becomes a SELECT,
    # not an ssh audit). Best-effort -- reporting failure must not degrade tagging.
    try:
        import socket  # noqa: PLC0415
        import requests  # noqa: PLC0415
        report = requests.post(f"{KURA_URL}/api/tagger/node-state",
                      headers=headers,
                      json={"host": socket.gethostname(), "release_id": verified.release_id,
                            "verified": True,
                            "detail": f"n_out={ckpt['n_out']} fallback={verified.fallback}"},
                      timeout=10)
        report.raise_for_status()
    except Exception as e:  # noqa: BLE001
        print(f"[kura-tagger] wd14-fixed: node-state report failed ({e})", flush=True)

    return {"model": model, "tags": tags, "img_size": ckpt.get("img_size", 448),
            "patched": dict(ckpt.get("patched", {})),
            # Veto heads bundled INTO the release (build_release.py) — the
            # fleet-wide distribution channel. Falls back to {} for a pre-veto
            # release; the local-file load_veto_heads() remains as a dev/primary
            # override path but the release is the fleet source.
            "veto_heads": dict(ckpt.get("veto_heads", {})),
            "release_id": verified.release_id, "n_out": ckpt["n_out"],
            "fallback": verified.fallback}


VETO_HEADS_DIR = MODELS_DIR / "veto-heads"
VETO_RETIRED_PATH = Path(__file__).resolve().parent / "veto_retired.json"


def _load_retired_gates(path: Path = VETO_RETIRED_PATH) -> set[str]:
    """Gate names retired from the veto post-pass (tagger/veto_retired.json).
    build_release.py already honours this ledger when bundling ckpt veto_heads;
    the local-file path must honour it too, or a retired gate whose
    .safetensors is still on disk keeps vetoing on that node alone (anilingus
    on primary, 2026-08-02: release 8 dropped the veto from the ckpt but the
    full-profile loop kept loading the stale local file, re-suppressing the
    recall its v2 patch had just recovered). Fail-soft to an empty set, but
    loudly -- an unreadable ledger silently resurrects retired vetoes."""
    if not path.exists():
        return set()
    try:
        data = json.loads(path.read_text())
        return set(data.get("retired") or [])
    except Exception as e:  # noqa: BLE001
        print(f"[kura-tagger] WARN: could not read {path.name}: {e} -- "
              f"retired-gate filter inactive", flush=True)
        return set()


def load_veto_heads() -> dict:
    """Load gated-veto heads: a linear probe on the SAME wd14-fixed backbone
    features that cancels (removes) a tag the ensemble/logit-patch produced,
    when the probe disagrees. plan:kura-tagger-self-improvement-loop — the
    "打ち消し続ける" structure. anilingus is the first (McNemar p=5e-05 in-gate
    vs v1). Self-contained safetensors (delivered by trainer via
    POST /api/tagger/veto-head, NOT scp): tensors w/mu/sd/b (1024/1024/1024/1),
    __metadata__ carries gate(tag)/threshold/apply. Apply:
    sigmoid(((pooled-mu)/sd) @ w + b) < threshold  → veto. Fail-soft: any load
    error degrades to 'no veto for this tag', never blocks tagging (same policy
    as load_wd14_fixed / grey_owl)."""
    heads: dict[str, dict] = {}
    if not VETO_HEADS_DIR.is_dir():
        return heads
    try:
        import torch  # noqa: PLC0415
        from safetensors import safe_open  # noqa: PLC0415
    except Exception as e:  # noqa: BLE001
        print(f"[kura-tagger] veto-heads: import failed ({e}) — skipping", flush=True)
        return heads
    retired = _load_retired_gates()
    for f in sorted(VETO_HEADS_DIR.glob("*.safetensors")):
        try:
            with safe_open(str(f), framework="pt") as sf:
                meta = sf.metadata() or {}
                gate = meta.get("gate") or f.stem
                if gate in retired:
                    print(f"[kura-tagger] veto-head skipped (retired): "
                          f"gate={gate} file={f.name}", flush=True)
                    continue
                thr = float(meta.get("threshold", 0.4))
                w = sf.get_tensor("w").float().flatten()
                mu = sf.get_tensor("mu").float().flatten()
                sd = sf.get_tensor("sd").float().flatten()
                b = float(sf.get_tensor("b").float().flatten()[0])
            heads[gate] = {"w": w, "mu": mu, "sd": sd, "b": b, "threshold": thr}
            print(f"[kura-tagger] veto-head loaded: gate={gate} thr={thr} dim={w.numel()}", flush=True)
        except Exception as e:  # noqa: BLE001
            print(f"[kura-tagger] veto-head load failed for {f.name}: {e}", flush=True)
    return heads


def reap_domain(fixed: dict | None, veto_heads: dict | None = None, character_heads: dict | None = None) -> list[str]:
    """The correction-authoritative gate tags this worker is entitled to REAP:
    the release's patched rows (corrected wd14-fixed logits) ∪ its gated-veto
    heads. The improved tagger decides present/absent for EXACTLY these tags, so
    the server may delete a stale one that a run leaves out of its output
    (plan:kura-tagger-self-improvement-loop「打ち消し続ける」; user 2026-07-22
    「anilingus 限定になっていないか。最終的に一般化すること」— not just the one
    veto head but every corrected tag, e.g. phimosis/erection which carry far
    larger human-correction volume than anilingus). Fully data-driven from the
    ckpt: the set grows automatically as a release adds patched rows or veto
    heads — no per-tag code, no hardcoded tag names. Tags NOT in this domain are
    never reap candidates (a per-head worker is not authoritative for tags it
    doesn't correct — this is the bound that makes it safe, unlike a blanket
    'delete anything absent' full-reap)."""
    tags: set[str] = set()
    if fixed:
        tags |= set(fixed.get("patched") or {})
        tags |= set(fixed.get("veto_heads") or {})
    tags |= set(veto_heads or {})
    tags |= {tag for tag in (character_heads or {}) if not tag.startswith("__")
             and character_heads[tag] is not None
             and (character_heads.get(f"__policy__{tag}") or {}).get("apply_mode") == "replace"}
    return sorted(tags)


def _run_veto_head(pooled, vh: dict) -> float:
    """sigmoid(((pooled-mu)/sd) @ w + b) — the feature standardization (mu/sd) is
    REQUIRED (trainer f7e0d98d: dropping it invalidates the probe)."""
    import torch  # noqa: PLC0415
    f = (pooled - vh["mu"]) / vh["sd"]
    return float(torch.sigmoid((f * vh["w"]).sum() + vh["b"]))


def _load_tag_csv(path: Path) -> list:
    with open(path, newline="", encoding="utf-8") as f:
        return [{"name": r["name"], "category": int(r["category"])} for r in csv.DictReader(f)]


def _onnx_providers() -> tuple[list[str], list[dict]]:
    """Return (providers, provider_options) for InferenceSession.

    Prefers GPU in priority order: CUDA -> DML -> ROCM -> OpenVINO GPU -> CPU.
    OpenVINOExecutionProvider defaults to CPU device unless device_type=GPU is
    explicitly passed -- without provider_options the iGPU is silently ignored.

    Override with KURA_TAGGER_FORCE_CPU=1 for diagnostics.
    """
    if os.environ.get("KURA_TAGGER_FORCE_CPU") == "1":
        return ["CPUExecutionProvider"], [{}]
    available = rt.get_available_providers()
    for ep in ("CUDAExecutionProvider", "DmlExecutionProvider", "ROCMExecutionProvider"):
        if ep in available:
            return [ep, "CPUExecutionProvider"], [{}, {}]
    if "OpenVINOExecutionProvider" in available:
        return (
            ["OpenVINOExecutionProvider", "CPUExecutionProvider"],
            [{"device_type": "GPU"}, {}],
        )
    return ["CPUExecutionProvider"], [{}]

def load_character_heads(*, api_url=None, headers=None) -> dict:
    # Same policy/artifact loader and inference implementation as fleet workers.
    from .nodec_autotag_worker import load_head_policies, load_content_heads, _fetch_artifact
    from .pixai_chars import require_character_heads
    api_url = api_origin(api_url)
    # Policy and artifact reads are public; use the caller credentials when provided.
    headers = headers if headers is not None else {}
    policies = [policy for policy in load_head_policies(api_url=api_url, headers=headers)
                if policy.get("category") == "character"]
    heads = load_content_heads(policies, artifact_fetcher=lambda sha: _fetch_artifact(sha, api_url=api_url, headers=headers))
    require_character_heads(heads)
    return heads


def load_all_models(*, api_url=None, headers=None) -> dict:
    """Download (if missing) and load all three ONNX sessions + tag defs."""
    pixai_onnx, pixai_tags = ensure_pixai()
    ml_onnx, ml_classes = ensure_mldanbooru()
    wd14_onnx, wd14_tags = ensure_wd14()
    providers, provider_options = _onnx_providers()
    print(f"[kura-tagger] onnx providers: {providers} options: {provider_options}", flush=True)
    smegma_model = load_smegma_tagger()
    print(f"[kura-tagger] smegma tagger: {'loaded' if smegma_model else 'unavailable (skipped)'}", flush=True)
    chastity_cage_model = load_chastity_cage_head()
    print(f"[kura-tagger] chastity_cage content-head: {'loaded' if chastity_cage_model else 'unavailable (skipped)'}", flush=True)
    chastity_belt_model = load_chastity_belt_head()
    print(f"[kura-tagger] chastity_belt content-head: {'loaded' if chastity_belt_model else 'unavailable (skipped)'}", flush=True)
    greyowl_heads = load_greyowl_heads()
    print(f"[kura-tagger] grey_owl content-heads: "
          f"{[t for t, m in greyowl_heads.items() if m is not None]} loaded, "
          f"{[t for t, m in greyowl_heads.items() if m is None]} unavailable (skipped)", flush=True)
    wd14_fixed = load_wd14_fixed()
    veto_heads = load_veto_heads()
    return {
        "pixai_sess": rt.InferenceSession(str(pixai_onnx), providers=providers, provider_options=provider_options),
        "pixai_tags": _load_tag_csv(pixai_tags),
        "ml_sess": rt.InferenceSession(str(ml_onnx), providers=providers, provider_options=provider_options),
        "ml_classes": json.loads(ml_classes.read_text(encoding="utf-8")),
        "wd14_sess": rt.InferenceSession(str(wd14_onnx), providers=providers, provider_options=provider_options),
        "wd14_tags": _load_tag_csv(wd14_tags),
        "smegma_model": smegma_model,
        "chastity_cage_model": chastity_cage_model,
        "chastity_belt_model": chastity_belt_model,
        "greyowl_heads": greyowl_heads,
        # Not yet consumed by tag_image() -- see load_wd14_fixed() docstring.
        "wd14_fixed": wd14_fixed,
        "veto_heads": veto_heads,
        "character_heads": load_character_heads(api_url=api_url, headers=headers),
    }


def _sha256_short(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()[:12]


def model_source_id(models: dict | None = None) -> str:
    """Provenance identifier for post.tagging_source (task 4328fb0e).

    Hashes the actual on-disk model files this worker loaded: deterministic, no
    extra network calls beyond what load_all_models() already does (files are
    already downloaded/cached by the time this runs), and it catches local
    modifications a floating HF Hub revision string would miss. Includes the
    quarantine tag set so a provenance lookup can tell, without cross-
    referencing a deploy log, whether a given post was tagged before or after a
    quarantine landed. The wd14-fixed release enters the same way (the verified
    active/ cache file is what this process actually runs, and its sha256 maps
    1:1 to a tagger_release row via tagger_release.model_sha256).
    """
    parts = [
        f"pixai={_sha256_short(PIXAI_DIR / 'model.onnx')}",
        f"mldanbooru={_sha256_short(MLDANBOORU_DIR / MLDANBOORU_FILE)}",
        f"wd14={_sha256_short(WD14_DIR / 'model.onnx')}",
    ]
    if SMEGMA_TAGGER_CKPT.exists():
        parts.append(f"smegma={_sha256_short(SMEGMA_TAGGER_CKPT)}")
    if CHASTITY_CAGE_CKPT.exists():
        parts.append(f"chastity_cage={_sha256_short(CHASTITY_CAGE_CKPT)}")
    if CHASTITY_BELT_CKPT.exists():
        parts.append(f"chastity_belt={_sha256_short(CHASTITY_BELT_CKPT)}")
    for tag, ckpt in (("small_penis", GREYOWL_SMALL_PENIS_CKPT), ("dark_penis", GREYOWL_DARK_PENIS_CKPT),
                      ("dark_perineum", GREYOWL_DARK_PERINEUM_CKPT), ("half-erect", GREYOWL_HALF_ERECT_CKPT)):
        if ckpt.exists():
            parts.append(f"{tag}={_sha256_short(ckpt)}")
    wd14_fixed_active = WD14_FIXED_CACHE_DIR / "active" / "wd14-fixed.pt"
    if wd14_fixed_active.exists():
        parts.append(f"wd14fixed={_sha256_short(wd14_fixed_active)}")
    # veto= records which gated-veto tags this worker CODE actually applies
    # (user 2026-07-22: タガーのバージョンをポストに書き込んで). The release ckpt
    # can carry veto weights that an old-code worker ignores; stamping the gates
    # this process will actually veto makes a post's provenance say whether the
    # veto ran, not just whether the weights were present.
    _vh = load_veto_heads()
    if _vh:
        parts.append(f"veto={','.join(sorted(_vh))}")
    if QUARANTINE_TAGS:
        parts.append(f"quarantine={','.join(sorted(QUARANTINE_TAGS))}")
    from .pixai_chars import CHARACTER_POLICY
    parts.append(CHARACTER_POLICY)
    from .pixai_chars import character_head_source
    if policy_source := character_head_source((models or {}).get("character_heads")):
        parts.append(policy_source)
    return ";".join(parts)


# ── image IO / preprocess ────────────────────────────────────────────


def extract_video_frame(video_bytes: bytes, seek_sec: float = 1.0) -> bytes:
    buf = io.BytesIO(video_bytes)
    with av.open(buf) as container:
        stream = next((s for s in container.streams if s.type == "video"), None)
        if stream is None:
            raise ValueError("No video stream found")
        try:
            container.seek(int(seek_sec * av.time_base ** -1), stream=stream)
        except Exception:
            pass
        for frame in container.decode(stream):
            img = frame.to_image()
            out = io.BytesIO()
            img.save(out, format="PNG")
            return out.getvalue()
    raise ValueError("Could not decode any frame from video")


def _preprocess_pixai(image_bytes: bytes, size: int = INPUT_SIZE) -> np.ndarray:
    # 本籍は tagger/pixai_chars.preprocess — 実装を 2 箇所に持たない (drift 防止)。
    return _pixai_preprocess_bytes(image_bytes, size)


def _preprocess_mldanbooru(image_bytes: bytes, size: int = INPUT_SIZE) -> np.ndarray:
    img = Image.open(io.BytesIO(image_bytes)).convert("RGB")
    img = img.resize((size, size), Image.BILINEAR)
    arr = np.array(img, dtype=np.float32) / 255.0
    arr = arr.transpose(2, 0, 1)
    return arr.reshape(1, *arr.shape)


def _preprocess_smegma(image_bytes: bytes):
    import torch
    from torchvision import transforms
    from .smegma_model import IMG_SIZE

    tf = transforms.Compose([
        transforms.Resize((IMG_SIZE, IMG_SIZE)),
        transforms.ToTensor(),
        transforms.Normalize(mean=[0.485, 0.456, 0.406], std=[0.229, 0.224, 0.225]),
    ])
    img = Image.open(io.BytesIO(image_bytes)).convert("RGB")
    return tf(img).unsqueeze(0)


def _run_smegma_tagger(image_bytes: bytes, model) -> tuple[str, float, float]:
    """Returns (skin_class, smegma_prob, precum_prob). skin_class は
    smegma_model.SKIN_CLASSES のいずれか ("bare"/"foreskin"/"phimosis")。"""
    import torch
    from .smegma_model import SKIN_CLASSES

    x = _preprocess_smegma(image_bytes)
    with torch.no_grad():
        skin_logits, smeg_logits, prec_logits = model(x)
    skin_idx = int(skin_logits.argmax(1)[0])
    smeg_prob = torch.sigmoid(smeg_logits)[0].item()
    prec_prob = torch.sigmoid(prec_logits)[0].item()
    return SKIN_CLASSES[skin_idx], smeg_prob, prec_prob


def _run_content_head(image_bytes: bytes, model) -> float:
    """content-head の P(タグが本物) を返す (chastity_cage / chastity_belt 共通)。
    前処理は model 属性 (img_size/mean/std) から導出 — v1 convnext (384+ImageNet
    norm) / v2 wd-swinv2 (448+0.5/0.5) のどちらの checkpoint でも学習時特徴抽出と
    一致する。"""
    import torch
    from torchvision import transforms
    from .content_head_model import IMG_SIZE

    size = getattr(model, "img_size", IMG_SIZE)
    mean = list(getattr(model, "mean", (0.485, 0.456, 0.406)))
    std = list(getattr(model, "std", (0.229, 0.224, 0.225)))
    tf = transforms.Compose([
        transforms.Resize((size, size)),
        transforms.ToTensor(),
        transforms.Normalize(mean=mean, std=std),
    ])
    x = tf(Image.open(io.BytesIO(image_bytes)).convert("RGB")).unsqueeze(0)
    with torch.no_grad():
        return float(torch.sigmoid(model(x))[0].item())


def _run_wd14_fixed(image_bytes: bytes, fixed: dict, want_pooled: bool = False):
    """Return {tag: probability} for the release's patched rows only.

    Preprocessing is Resize+ToTensor with NO mean/std normalization -- not an
    oversight. Every patch row (retrain_wd14_logit.py on desk, and
    tagger/train_logit_patch.py here) was trained through exactly this pipeline,
    and a linear row is only valid in the feature space it was trained in.
    Changing this to timm's "proper" transform would silently invalidate every
    shipped patch; if that migration is ever wanted, all rows must be retrained
    in the same release."""
    import torch  # noqa: PLC0415
    from torchvision import transforms  # noqa: PLC0415

    size = fixed["img_size"]
    img = Image.open(io.BytesIO(image_bytes)).convert("RGB")
    x = transforms.Compose([transforms.Resize((size, size)),
                            transforms.ToTensor()])(img).unsqueeze(0)
    model = fixed["model"]
    pooled = None
    with torch.no_grad():
        if want_pooled and hasattr(model, "forward_features") and hasattr(model, "forward_head"):
            # One backbone pass (the expensive part) → both the 1024-d pooled
            # features the veto heads need AND the patched-row logits. Same
            # feature space (Resize+ToTensor, no normalize) the veto probe was
            # trained in — backbone is byte-identical to the release's (trainer
            # verified 423/425 tensors identical, only head.fc differs).
            feats = model.forward_features(x)
            pooled = model.forward_head(feats, pre_logits=True)[0]
            probs = torch.sigmoid(model.forward_head(feats)[0])
        else:
            probs = torch.sigmoid(model(x)[0])
    scores = {tag: float(probs[idx]) for tag, idx in fixed["patched"].items()}
    return scores, pooled


def _preprocess_wd14(image_bytes: bytes, size: int = INPUT_SIZE) -> np.ndarray:
    img = Image.open(io.BytesIO(image_bytes)).convert("RGBA")
    bg = Image.new("RGBA", img.size, (255, 255, 255, 255))
    bg.paste(img, mask=img.split()[3])
    img = bg.convert("RGB")
    img.thumbnail((size, size), Image.LANCZOS)
    pad = Image.new("RGB", (size, size), (255, 255, 255))
    pad.paste(img, ((size - img.width) // 2, (size - img.height) // 2))
    arr = np.array(pad, dtype=np.float32)
    arr = arr[:, :, ::-1]  # RGB -> BGR
    return np.expand_dims(arr, axis=0)


# ── inference ────────────────────────────────────────────────────────


def _sigmoid(x: np.ndarray) -> np.ndarray:
    return 1.0 / (1.0 + np.exp(-x))


def _run_pixai(image_bytes: bytes, sess: rt.InferenceSession, tag_defs: list) -> dict:
    from .pixai_chars import prediction_vector
    scores = prediction_vector(sess, _preprocess_pixai(image_bytes), len(tag_defs))

    result: dict = {"general": [], "character": []}
    for score, tag in zip(scores, tag_defs):
        cat, name = tag["category"], tag["name"]
        if cat == 0:
            if name in PIXAI_LOW_THRESH:
                thresh = PIXAI_LOW_THRESH[name]
            else:
                thresh = PIXAI_HIGH_THRESH.get(name, PIXAI_THRESH_GENERAL)
            if score >= thresh:
                result["general"].append((name, float(score)))
        elif cat == 4 and score >= PIXAI_THRESH_CHAR:
            result["character"].append((name, float(score)))

    for key in ("general", "character"):
        result[key].sort(key=lambda x: x[1], reverse=True)
    return result


def _resolve_mutex(tag_scores: dict) -> dict:
    result = dict(tag_scores)
    pairs = list(MUTEX_PAIRS)
    if _is_solo(result):
        pairs += MUTEX_PAIRS_SOLO_ONLY
    for a, b in pairs:
        if a in result and b in result:
            if result[a] >= result[b]:
                del result[b]
            else:
                del result[a]
    return result


def _run_mldanbooru(
    image_bytes: bytes, sess: rt.InferenceSession, classes: list, pixai_tags: set
) -> list:
    raw_output, = sess.run(["output"], {"input": _preprocess_mldanbooru(image_bytes)})
    scores = _sigmoid(raw_output[0])
    extras = []
    for i, score in enumerate(scores):
        name = classes[i]
        if name in MLDANBOORU_LOW_THRESH:
            thresh = MLDANBOORU_LOW_THRESH[name]
        else:
            thresh = MLDANBOORU_HIGH_THRESH.get(name, MLDANBOORU_THRESH)
        if score >= thresh and name not in pixai_tags:
            extras.append((name, float(score)))
    extras.sort(key=lambda x: x[1], reverse=True)
    return extras


def _run_wd14_chars(
    image_bytes: bytes, sess: rt.InferenceSession, tag_defs: list
) -> list[tuple[str, float]]:
    from .pixai_chars import prediction_vector
    scores = prediction_vector(sess, _preprocess_wd14(image_bytes), len(tag_defs), output_name=None)
    chars = []
    for score, tag in zip(scores, tag_defs):
        if tag["category"] == 4 and score >= WD14_THRESH_CHAR:
            chars.append((tag["name"], float(score)))
    chars.sort(key=lambda x: x[1], reverse=True)
    return chars


# ── top-level entry ──────────────────────────────────────────────────


def tag_image(
    image_bytes: bytes, mime_type: str, models: dict, notes: dict | None = None,
    return_scores: bool = False,
) -> list[dict] | dict:
    """Run PixAI + ml-danbooru (+WD14 fallback) on the given bytes.

    For video MIME types the first frame is extracted. Returns the final
    ordered tag list (characters first, then general tags by score) as
    ``[{"name": str, "category": "character"|"general"}, ...]``. category は
    モデルが既に区別している情報 (新タグを増やさず genre だけ運ぶ)。

    ``notes`` を渡すと Apply Gate の判定結果を副チャネルで返す (wire 契約は不変):
    chastity_cage が不確実域なら ``notes["chastity_cage_review"] = {"prob": float}``
    を書き、呼び出し側 (loop.py) が人手検証プールへ route できる。
    """
    if (mime_type or "").lower().startswith("video/"):
        image_bytes = extract_video_frame(image_bytes)

    pixai_result = _run_pixai(image_bytes, models["pixai_sess"], models["pixai_tags"])
    pixai_gen_names = {n for n, _ in pixai_result["general"]}
    # Keep the existing base-model selection policy; merge identities before
    # corrections so a negative patch/veto cannot be bypassed by a late append.
    char_tags = pixai_result["character"]
    if not char_tags:
        char_tags = _run_wd14_chars(image_bytes, models["wd14_sess"], models["wd14_tags"])
    character_names = {tag["name"] for key in ("pixai_tags", "wd14_tags")
                       for tag in models.get(key, []) if tag["category"] == 4}
    character_names.update(tag["name"] for tag in (models.get("wd14_fixed") or {}).get("tags", [])
                           if tag["category"] == 4)
    character_names.update(name for name, _ in char_tags)

    ml_extras = _run_mldanbooru(
        image_bytes, models["ml_sess"], models["ml_classes"], pixai_gen_names
    )

    all_scores: dict = {name: score for name, score in pixai_result["general"]}
    all_scores.update(char_tags)
    for name, score in ml_extras:
        if name not in all_scores:
            all_scores[name] = score
    all_scores = _resolve_mutex(all_scores)
    for tag in QUARANTINE_TAGS:
        all_scores.pop(tag, None)

    # The release's repaired rows overwrite whatever the stock stack said about
    # them. Overwrite, not merge: the old value is not a second opinion, it is
    # the error we measured (phimosis recall 0.05, erection wrong on 114/115).
    # Ungated rows first, then gated rows against the corrected scores.
    wd14_fixed = models.get("wd14_fixed")
    if wd14_fixed is not None:
        # Release-bundled veto heads are the fleet source; local-file heads
        # (models["veto_heads"]) override for dev/primary. Merge, local wins.
        veto_heads = {**(wd14_fixed.get("veto_heads") or {}),
                      **(models.get("veto_heads") or {})}
        try:
            fixed_scores, wd14_pooled = _run_wd14_fixed(
                image_bytes, wd14_fixed, want_pooled=bool(veto_heads))
        except Exception as exc:
            if character_names & (set(wd14_fixed.get("patched") or {}) | set(veto_heads)):
                from .pixai_chars import RequiredCharacterComponentError
                raise RequiredCharacterComponentError(f"character correction model unavailable: {exc}") from exc
            raise
        for gated_pass in (False, True):
            for tag, prob in fixed_scores.items():
                if tag in character_names and not 0 <= prob <= 1:
                    from .pixai_chars import RequiredCharacterComponentError
                    raise RequiredCharacterComponentError(f"invalid character patch probability for {tag}")
                gates = WD14_FIXED_GATES.get(tag)
                if (gates is not None) != gated_pass:
                    continue
                if gates and not any(g in all_scores for g in gates):
                    continue  # out of the head's calibrated domain — leave the ensemble alone
                all_scores.pop(tag, None)
                if prob >= WD14_FIXED_THRESH:
                    all_scores[tag] = prob
        all_scores = _resolve_mutex(all_scores)

        # Gated-veto pass (plan:kura-tagger-self-improvement-loop, 「打ち消し続け
        # る」). A veto head fires ONLY where its gate tag is present (v1/ensemble
        # produced it) and cancels it when the correction-trained probe disagrees
        # (< threshold). This is the incumbent-FP suppressor McNemar-validated for
        # anilingus (p=5e-05 in-gate vs v1). Uses the pooled features from the
        # single backbone pass above. Fail-soft per head.
        if veto_heads and wd14_pooled is not None:
            for gate_tag, vh in veto_heads.items():
                if gate_tag not in all_scores:
                    continue  # gate closed: v1 didn't produce it, nothing to veto
                try:
                    from .veto_apply import run_veto  # noqa: PLC0415
                    vscore = run_veto(wd14_pooled, vh)
                    if gate_tag in character_names and not 0 <= vscore <= 1:
                        raise ValueError("invalid character veto probability")
                except Exception as e:  # noqa: BLE001
                    print(f"[kura-tagger] veto-head {gate_tag} run failed: {e}", flush=True)
                    if gate_tag in character_names:
                        from .pixai_chars import RequiredCharacterComponentError
                        raise RequiredCharacterComponentError(f"character veto {gate_tag} failed: {e}") from e
                    continue
                if vscore < vh["threshold"]:
                    all_scores.pop(gate_tag, None)
                    if notes is not None:
                        notes[f"{gate_tag}_veto"] = {"prob": round(vscore, 4), "threshold": vh["threshold"]}

    smegma_model = models.get("smegma_model")
    if smegma_model is not None and "penis" in all_scores:
        skin_class, smeg_prob, prec_prob = _run_smegma_tagger(image_bytes, smegma_model)
        if wd14_fixed is None:
            # release 不在時のみ従来動作: smegma/precum は convnext モデルで完全
            # 上書き、phimosis は additive (recall 不足を踏まえ既存タグは消さない)
            all_scores.pop("smegma", None)
            all_scores.pop("precum", None)
            if smeg_prob >= SMEGMA_THRESH:
                all_scores["smegma"] = smeg_prob
            if prec_prob >= PRECUM_THRESH:
                all_scores["precum"] = prec_prob
            if skin_class == "phimosis":
                all_scores.setdefault("phimosis", 1.0)
        # foreskin は wd14-fixed の行が無い唯一の担当タグ — release の有無に
        # かかわらずこのモデルが additive に供給する (smegma/precum/phimosis は
        # release があればそちらが所有。同じタグに補正器を 2 つ走らせると
        # どちらが決めたのか分からなくなる — rogue worker 版と同じ判断)
        if skin_class == "foreskin":
            all_scores.setdefault("foreskin", 1.0)

    # grey_owl content-heads (2026-07-20, trainer-cc-primary 学習・pool 7578
    # GT) — ADD-ONLY (cage/belt の Apply Gate とは違い削除はしない、GREYOWL_
    # ADD_THRESH の docstring 参照: 削除安全性を校正するデータが無いため)。
    # smegma_model と同じ "penis" ゲート(教師が penis 文脈の画像から取られて
    # いるため、無関係な画像への無駄な推論を避ける)。既存スコアがあれば
    # setdefault で上書きしない(アンサンブルが既により高い確信で検出済みなら
    # それを尊重する)。
    greyowl_heads = models.get("greyowl_heads") or {}
    if "penis" in all_scores:
        for tag, head_model in greyowl_heads.items():
            if head_model is None:
                continue
            prob = _run_content_head(image_bytes, head_model)
            if prob >= GREYOWL_ADD_THRESH:
                all_scores.setdefault(tag, prob)

    # chastity_cage Apply Gate (plan A2/A4): PixAI が付けた chastity_cage を
    # content-head で検証。確信 FP (prob<LO) のみ自動削除、境界 [LO,HI) は保持し
    # 人手プールへ route、確信 real (prob>=HI) は保持。可逆性は pixai 再実行で担保。
    cc_model = models.get("chastity_cage_model")
    if cc_model is not None and "chastity_cage" in all_scores:
        cc_prob = _run_content_head(image_bytes, cc_model)
        if cc_prob < CHASTITY_CAGE_REMOVE_BELOW:
            all_scores.pop("chastity_cage", None)  # 確信 FP → 自動削除
        elif cc_prob < CHASTITY_CAGE_KEEP_ABOVE:
            # 不確実域 → タグは保持したまま人手検証へ route (境界は自動削除しない)
            if notes is not None:
                notes["chastity_cage_review"] = {"prob": round(cc_prob, 4)}
        # else: 確信 real → そのまま保持

    # chastity_belt Apply Gate — cage gate と同型。chastity_belt は ml-danbooru
    # 語彙 (mldanbooru merge 後の all_scores に乗る) なのでこの位置で機能する。
    # 確信 FP (prob<LO) のみ自動削除、境界 [LO,HI) は保持+人手 route、確信 real
    # (prob>=HI) は保持。可逆性はタガー再実行で担保 (provenance ガード A5 と同じ)。
    cb_model = models.get("chastity_belt_model")
    if cb_model is not None and "chastity_belt" in all_scores:
        cb_prob = _run_content_head(image_bytes, cb_model)
        if cb_prob < CHASTITY_BELT_REMOVE_BELOW:
            all_scores.pop("chastity_belt", None)  # 確信 FP → 自動削除
        elif cb_prob < CHASTITY_BELT_KEEP_ABOVE:
            if notes is not None:
                notes["chastity_belt_review"] = {"prob": round(cb_prob, 4)}
        # else: 確信 real → そのまま保持

    character_heads = models.get("character_heads") or {}
    if character_heads:
        from .nodec_autotag_worker import apply_content_heads
        from .pixai_chars import require_character_heads
        require_character_heads(character_heads)
        character_names.update(tag for tag in character_heads if not tag.startswith("__"))
        apply_content_heads(list(all_scores), Image.open(io.BytesIO(image_bytes)), character_heads,
                            scores=all_scores)

    # Raw per-tag scores after the full ensemble (base + release repairs + specialist heads +
    # gates/mutex), BEFORE thresholding — for benchmark scoring (plan kura-event-sourced-rebuild P6).
    # Additive/non-invasive: default path (return_scores=False) is unchanged.
    if return_scores:
        return dict(all_scores)

    tag_list: list[dict] = [
        {"name": name, "category": "character" if name in character_names else "general"}
        for name, score in sorted(all_scores.items(), key=lambda x: (x[0] not in character_names, -x[1]))
    ]
    return tag_list
