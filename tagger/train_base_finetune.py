#!/usr/bin/env python3
# Description: LoRA fine-tune of the wd14 SwinV2 BASE backbone on accumulated GT, with stock-teacher distillation.
# Kind: tool
# Effects: mutating (writes a checkpoint + result json; reads kura DB and HF hub over the network)
# Runs-on: trainer (only CUDA node; kura DB is read read-only from primary)
# Requires: torch, timm, psycopg2, safetensors, pillow, torchvision
"""train_base_finetune.py — close the tagger self-improvement loop by improving the
BASE model, instead of stacking another correction layer on a frozen one.

WHY THIS EXISTS
---------------
Today every correction is a layer bolted onto a frozen stock backbone: a patched
head.fc row (tagger/patches/<tag>/vN, merged by build_release.py) or a gated veto
head (tagger/infer.py::load_veto_heads). Both are linear functions of features the
stock backbone produces, and neither ever retires -- a veto head cancels the same
false positive forever because nothing teaches the backbone not to make it. Layers
accumulate; the base never learns. This script is the other half: periodically fold
the accumulated human corrections (tagger_gt_label) back INTO the backbone.

RELEASE LINEAGE -- READ THIS BEFORE WIRING IT ANYWHERE
------------------------------------------------------
tagger/build_release.py is built on registry_freeze.validate_merge, which PROVES
every non-allowlisted head.fc row is byte-identical to the stock model. That proof
is what lets "N tags fixed" mean exactly that. A backbone fine-tune changes the
features under EVERY row, so it breaks that invariant by construction -- not by
accident, and not fixably by a flag. Therefore:

  * this script writes ``wd14-finetuned.pt``, never ``wd14-fixed.pt``;
  * the ckpt carries ``lineage="base-finetune"`` and ``freeze_validated=False``;
  * it does NOT call build_release / publish_release, and must not be smuggled
    through that path. Accepting a fine-tuned base is a separate, explicit change
    to the release pipeline with its own (different) acceptance gate -- the gate
    below (per-tag floors AND stock preservation) is what that gate would need.

METHOD
------
1. Base/init: the ACTIVE release ckpt pulled read-only from primary (default), so the
   fine-tune starts from what the fleet actually runs (patched rows and appended
   rows included) rather than from stock. ``--base stock`` starts from the pinned
   hub model instead.
2. LoRA (rank r) on the backbone's attention/MLP projections. Base weights frozen;
   only the adapters train. Mergeable (W += BA*scale) and low-capacity, which is
   the first of two anti-forgetting mechanisms.
3. head.fc: GT-covered rows train; every other row is frozen BYTE-IDENTICAL via a
   gradient mask (asserted after training, see _assert_head_rows_frozen). The
   second anti-forgetting mechanism -- but note it is only structural. Features
   move under the frozen rows, so their OUTPUTS still move. Hence (4).
4. Distillation: the frozen pre-fine-tune model is the teacher. On every batch the
   ~10.8k non-GT columns are pulled toward the teacher's own probabilities
   (Bernoulli cross-entropy == KL up to the teacher's constant entropy). KD-only
   batches drawn from random posts widen this beyond the GT domain, which is a
   narrow and biased slice of the archive. This is what makes "fine-tune a dozen
   tags" not mean "silently rewrite ten thousand".

FEATURE SPACE -- DO NOT "FIX" THE TRANSFORM
-------------------------------------------
Preprocessing is Resize((448,448)) + ToTensor with NO mean/std normalization,
byte-for-byte what kura runs at inference (tagger/infer.py::_run_wd14_fixed). timm's
"proper" transform is a DIFFERENT space (measured cos=0.850 on trainer, and the
gap is not absorbable by feature standardization because it sits before a nonlinear
backbone). Every shipped patch row and veto head was fitted in the kura space; a
fine-tune done in timm space would be evaluated in a space the fleet never sees.

USAGE
-----
    # smoke: a few hundred labels, 2 steps, all metrics computed end-to-end
    python3 train_base_finetune.py --smoke
    # real run (report before launching -- this is hours)
    python3 train_base_finetune.py --epochs 3 --batch-size 4 --kd-posts 4000
"""
from __future__ import annotations

import argparse
import copy
import csv
import hashlib
import io
import json
import math
import os
import random
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tagger import display_bytes  # noqa: E402

# kura's inference-time preprocessing. Keep in sync with tagger/infer.py::_run_wd14_fixed.
IMG_SIZE = 448
# The threshold the fleet applies to patched rows (tagger/infer.py::WD14_FIXED_THRESH).
FLEET_THRESH = 0.5
# Backbone Linear suffixes that get LoRA adapters. cpb_mlp (the continuous relative
# position-bias MLP) and the patch-merge `reduction` layers are deliberately excluded:
# they encode positional/structural priors shared by all 10.8k tags, and adapting them
# is a large blast radius for a dozen-tag correction.
LORA_TARGET_SUFFIXES = ("attn.qkv", "attn.proj", "mlp.fc1", "mlp.fc2")

from tagger.kura_db import canonical_host as _canonical_host, canonical_pg_port as _canonical_pg_port  # noqa: E402

def default_db() -> dict:
    """Connection settings, resolved when a connection is made (not at import)."""
    return dict(
        host=os.environ.get("KURA_PGHOST") or _canonical_host(),
        port=int(os.environ.get("KURA_PGPORT") or _canonical_pg_port()),
        dbname=os.environ.get("KURA_PGDATABASE", "kura"),
        user=os.environ.get("KURA_PGUSER", "kura"),
        password=os.environ.get("KURA_PGPASSWORD", "kura"),
    )
# Near-duplicate cluster key, as a SQL expression on `post p`. GT comes from verify
# pools whose images are consecutive pages of one doujin / one source post; a random
# split puts near-duplicates of a val image into train and inflates every metric
# (measured on anilingus: pooled F1 0.698 -> 0.757). Mirrors tagger-heads/gt_split.py.
GROUP_SQL = "coalesce('pp:'||p.primary_pool_id, 'src:'||nullif(p.source,''), 'post:'||p.id)"


# ── DB ────────────────────────────────────────────────────────────────


class DB:
    """Read-only kura access. trainer is a replica whose logical replication is
    stalled, so this points at primary canonical and never writes (canonical writes go
    through primary:8443, never a direct DB connection)."""

    def __init__(self, **kw):
        import psycopg2  # noqa: PLC0415

        self.conn = psycopg2.connect(**{**default_db(), **kw})
        self.conn.set_session(readonly=True, autocommit=True)

    def rows(self, sql, args=None):
        cur = self.conn.cursor()
        cur.execute(sql, args)
        out = cur.fetchall()
        cur.close()
        return out


def live_config(db: DB) -> dict:
    """Registration floors are DB-governed (tagger_config, migration 0098) and editable
    from the dashboard. Read them live -- never hardcode a floor that policy moved."""
    return {k: float(v) for k, v in db.rows("select key, value from tagger_config")}


def active_release(db: DB):
    """(release_id, model_bytes, selected_tags_csv_text) for the release the fleet runs."""
    r = db.rows(
        "select r.id, r.model_data, r.selected_tags_csv, r.base_model_repo, r.base_model_revision "
        "from tagger_release_active a join tagger_release r on r.id = a.release_id")
    if not r:
        raise SystemExit("no active release in tagger_release_active -- refusing to guess a base")
    rid, model_data, csv_data, repo, rev = r[0]
    return rid, bytes(model_data), bytes(csv_data).decode(), repo, rev


def gt_labels(db: DB, min_positive: int, tags: list[str] | None = None,
              extra: set[str] | None = None):
    """-> (labels, groups). labels = [(post_id, tag, y)], groups = {post_id: group_key}.

    Only images with a display rendition (media_location='pg') -- the on-disk fallback used by the anilingus
    round is a one-tag rescue, not a general path, and silently dropping to a smaller
    denominator is worse than reporting the real one.
    """
    where_tag = "and g.tag = any(%s)" if tags else ""
    args = ([tags] if tags else [])
    rows = db.rows(
        f"select g.post_id, g.tag, g.verdict, {GROUP_SQL} "
        "from tagger_gt_label g join post p on p.id = g.post_id "
        f"where p.type='image' and p.media_location = 'pg' {where_tag} "
        "order by g.tag, g.post_id", args or None)
    by_tag: dict[str, list] = {}
    groups: dict[int, str] = {}
    for pid, tag, verdict, grp in rows:
        by_tag.setdefault(tag, []).append((pid, 1.0 if verdict == "positive" else 0.0))
        groups[pid] = grp
    extra = extra or set()
    kept = {t: v for t, v in by_tag.items()
            if sum(y for _, y in v) >= min_positive or t in extra}
    labels = [(pid, t, y) for t, v in kept.items() for pid, y in v]
    return labels, groups, {t: (int(sum(y for _, y in v)), len(v)) for t, v in kept.items()}


def kd_post_ids(db: DB, n: int, exclude: set[int], seed: int = 0) -> list[int]:
    """Random image posts for KD-only batches. TABLESAMPLE (not ORDER BY random()) --
    the table is ~735k rows and a full sort to draw a few thousand ids is wasteful.

    REPEATABLE(seed) is load-bearing, not tidiness: a kd_weight sweep compares
    preservation across runs, and an unseeded TABLESAMPLE would hand each arm a
    different set of preservation images. The drift differences would then be partly
    a difference in which pictures were measured, which is not a measurement.
    """
    if n <= 0:
        return []
    pct = min(100.0, max(0.2, 100.0 * (n * 4) / 735000))
    rows = db.rows(
        f"select id from post tablesample system ({pct:.3f}) repeatable ({seed}) "
        "where type='image' and media_location = 'pg' order by id limit %s", (n * 3,))
    ids = [r[0] for r in rows if r[0] not in exclude]
    return ids[:n]


def fetch_images(db: DB, post_ids: list[int], cache: Path | None = None,
                 chunk: int = 2000) -> dict[int, bytes]:
    """Blobs by post id, optionally through a local disk cache.

    The cache exists because a calibration sweep runs the same corpus 4+ times and the
    GT set is ~3 GB across the wire from primary; blobs are immutable content for a given
    post id, so re-reading them is pure waste. Chunked because `id = any(ARRAY[...])`
    with 25k ids materialises the whole result set in one round trip.
    """
    out: dict[int, bytes] = {}
    want = list(post_ids)
    if cache is not None:
        cache.mkdir(parents=True, exist_ok=True)
        missing = []
        for pid in want:
            f = cache / f"{pid}.bin"
            if f.exists():
                out[pid] = f.read_bytes()
            else:
                missing.append(pid)
        want = missing
    for i in range(0, len(want), chunk):
        for pid, data in display_bytes.load(db.rows, want[i:i + chunk]):
            if data is not None:
                out[pid] = data
                if cache is not None:
                    (cache / f"{pid}.bin").write_bytes(out[pid])
    return out


# ── grouping / split (mirrors tagger-heads/gt_split.py, vendored so this script
#    is self-contained and repo-tracked -- the off-repo drift this closes) ──────


def group_folds(ids, groups, k=5, y=None):
    """Groups -> k folds, no group split across folds. With per-id labels `y` this is
    StratifiedGroupKFold (largest group first into whichever fold minimises per-class
    spread), so a tag with few groups doesn't produce single-class folds."""
    by_g: dict[str, list] = {}
    for i in ids:
        by_g.setdefault(groups[i], []).append(i)
    folds = [[] for _ in range(k)]
    order = sorted(by_g.items(), key=lambda kv: (-len(kv[1]), kv[0]))
    if y is None:
        for _, members in order:
            folds[min(range(k), key=lambda t: len(folds[t]))].extend(members)
        return folds
    cnt = [[0, 0] for _ in range(k)]
    for _, members in order:
        gp = sum(1 for i in members if y.get(i)); gn = len(members) - gp

        def cost(t):
            cnt[t][0] += gn; cnt[t][1] += gp
            c = sum(max(x[c_] for x in cnt) - min(x[c_] for x in cnt) for c_ in (0, 1))
            cnt[t][0] -= gn; cnt[t][1] -= gp
            return c

        j = min(range(k), key=lambda t: (cost(t), len(folds[t])))
        cnt[j][0] += gn; cnt[j][1] += gp
        folds[j].extend(members)
    return folds


def split_holdout(post_ids: list[int], groups: dict, any_pos: dict, k: int = 5):
    """20% group-held-out val (fold 0 of a stratified group 5-fold). Returns (train, val)
    post-id lists. Held out by GROUP, so no near-duplicate of a val image is in train."""
    folds = group_folds(sorted(post_ids), groups, k, any_pos)
    val = set(folds[0])
    return [p for p in post_ids if p not in val], [p for p in post_ids if p in val]


def balance_train_cells(label_map: dict, train_set: set, neg_per_pos: float, seed: int):
    """Per-tag negative capping on the TRAIN side -> (cells, counts).

    WHY. Raw GT is not merely imbalanced, it is imbalanced *unevenly across tags*:
    anal_hair alone is 24 pos / 15933 neg, 56% of every label in the corpus, and
    phimosis + erection carry another 35%. Trained as-is, one epoch is three tags'
    negatives; the gradient is dominated by "say no to anal_hair" and the other eleven
    tags are rounding error. pos_weight cannot rescue this -- it was capped at 20 vs a
    true 664:1, i.e. it was silently lying about the imbalance it claimed to correct.

    Capping negatives at neg_per_pos x positives fixes the cause instead: after it, the
    true per-tag ratio is <= neg_per_pos, so the pos_weight cap stops binding and the
    weight becomes the real ratio rather than a clip. Negatives are re-drawn every epoch
    (seed varies with the epoch), so a multi-epoch run still sees the full negative pool
    over time -- this is per-epoch subsampling, not a permanent discard.

    Applied to TRAIN ONLY. The val split keeps every negative: capping there would drop
    the false positives out of the denominator and inflate precision, which is exactly
    the number this run has to be honest about.
    """
    rng = random.Random(seed)
    cells: dict[int, dict[str, float]] = {}
    counts: dict[str, dict] = {}
    for tag, pairs in sorted(label_map.items()):
        pos = [p for p, y in pairs if p in train_set and y > 0]
        neg = [p for p, y in pairs if p in train_set and y == 0]
        cap = int(round(neg_per_pos * len(pos))) if neg_per_pos > 0 else len(neg)
        keep_neg = neg if len(neg) <= cap else rng.sample(neg, cap)
        for p in pos:
            cells.setdefault(p, {})[tag] = 1.0
        for p in keep_neg:
            cells.setdefault(p, {})[tag] = 0.0
        counts[tag] = dict(pos=len(pos), neg_avail=len(neg), neg_kept=len(keep_neg),
                           ratio=round(len(keep_neg) / max(1, len(pos)), 1))
    return cells, counts


# ── model: base + LoRA ────────────────────────────────────────────────


def load_base(db: DB, base: str, pin_revision: str | None):
    """-> (model, tag_names, meta). `base='release'` starts from the active release
    (patched + appended rows intact -- the fleet's actual current state);
    `base='stock'` starts from the pinned hub model."""
    import timm  # noqa: PLC0415
    import torch  # noqa: PLC0415

    if base == "release":
        rid, model_bytes, csv_text, repo, rev = active_release(db)
        ckpt = torch.load(io.BytesIO(model_bytes), map_location="cpu", weights_only=False)
        model = timm.create_model(f"hf-hub:{ckpt['repo']}", pretrained=False,
                                  num_classes=ckpt["n_out"])
        model.load_state_dict(ckpt["state_dict"])
        names = [r["name"] for r in csv.DictReader(io.StringIO(csv_text))]
        meta = {"base": "release", "release_id": rid, "repo": ckpt["repo"],
                "revision": ckpt.get("revision", rev), "n_stock": ckpt.get("n_stock"),
                "n_out": ckpt["n_out"], "patched": dict(ckpt.get("patched", {})),
                "veto_heads": sorted(ckpt.get("veto_heads", {}) or {}),
                "img_size": ckpt.get("img_size", IMG_SIZE)}
    else:
        from huggingface_hub import HfApi, hf_hub_download  # noqa: PLC0415
        repo = "SmilingWolf/wd-swinv2-tagger-v3"
        rev = pin_revision or HfApi().model_info(repo).sha
        model = timm.create_model(f"hf-hub:{repo}@{rev}", pretrained=True)
        names = [r["name"] for r in csv.DictReader(
            open(hf_hub_download(repo, "selected_tags.csv", revision=rev)))]
        meta = {"base": "stock", "release_id": None, "repo": repo, "revision": rev,
                "n_stock": len(names), "n_out": len(names), "patched": {},
                "veto_heads": [], "img_size": model.pretrained_cfg["input_size"][-1]}
    if len(names) != model.head.fc.weight.shape[0]:
        raise SystemExit(f"vocab/head mismatch: csv={len(names)} head={model.head.fc.weight.shape[0]}")
    return model, names, meta


def apply_lora(model, r: int, alpha: float, dropout: float, seed: int):
    """Wrap the targeted Linears with W' = W + (alpha/r) * B @ A; base weights frozen.

    Written out rather than pulled from peft: the venv this runs in on trainer is
    shared with ComfyUI (adding a dependency there is a production risk for an
    unrelated service), and merging back into a PLAIN timm state_dict -- the shape
    tagger/infer.py::load_wd14_fixed loads -- is the whole point of the output.
    """
    import torch  # noqa: PLC0415
    import torch.nn as nn  # noqa: PLC0415

    gen = torch.Generator().manual_seed(seed)

    class _LoRA(nn.Module):
        def __init__(self, base: nn.Linear):
            super().__init__()
            self.base = base
            self.r, self.scale = r, alpha / r
            self.A = nn.Parameter(torch.empty(r, base.in_features))
            self.B = nn.Parameter(torch.zeros(base.out_features, r))
            # Kaiming on A, zeros on B => the adapter is an exact no-op at step 0, so
            # the student starts byte-equal to the teacher (asserted in the smoke).
            with torch.no_grad():
                self.A.copy_(torch.empty(r, base.in_features).normal_(
                    0, 1.0 / math.sqrt(base.in_features), generator=gen))
            self.drop = nn.Dropout(dropout) if dropout > 0 else nn.Identity()

        def forward(self, x):
            return self.base(x) + self.drop(x) @ self.A.t() @ self.B.t() * self.scale

        def merged_weight(self):
            return self.base.weight + (self.B @ self.A) * self.scale

    adapters = []
    for name, mod in list(model.named_modules()):
        for child_name, child in list(mod.named_children()):
            full = f"{name}.{child_name}" if name else child_name
            if isinstance(child, nn.Linear) and full.endswith(LORA_TARGET_SUFFIXES):
                child.weight.requires_grad_(False)
                if child.bias is not None:
                    child.bias.requires_grad_(False)
                wrapper = _LoRA(child)
                setattr(mod, child_name, wrapper)
                adapters.append((full, wrapper))
                # timm's WindowAttention normally reaches PAST the qkv module
                # (F.linear(x, weight=self.qkv.weight, bias=qkv_bias)) to splice in its
                # split q/k/v bias, which would silently skip the adapter. timm ships
                # `qkv_bias_separate` for exactly this case: `self.qkv(x)` then `+= bias`,
                # numerically identical (the qkv Linear itself carries bias=False here).
                if child_name == "qkv" and hasattr(mod, "qkv_bias_separate"):
                    mod.qkv_bias_separate = True
    return adapters


def set_drop_path(model, rate: float) -> list[float]:
    """Set stochastic depth on every DropPath. Returns the ORIGINAL rates.

    The wd14 checkpoint ships a drop_path schedule rising to 0.10, and
    timm.create_model hands the model back in TRAIN mode, so a naive student forward
    is stochastic while the teacher's is not. That silently poisons this design in
    two ways: the KD term spends its gradient fighting layer-drop noise instead of
    measuring real divergence, and every "student vs teacher" number becomes a
    sample of that noise rather than a measurement of drift. Default 0.0 here: this
    is a small-delta correction under a 1.5%-parameter LoRA, which is already the
    capacity constraint that stochastic depth would otherwise be providing.
    """
    orig = []
    for mod in model.modules():
        if type(mod).__name__ == "DropPath":
            orig.append(float(mod.drop_prob))
            mod.drop_prob = rate
    return orig


def merge_lora(model):
    """Fold every adapter back into its base Linear and restore the plain module tree,
    so the saved state_dict has exactly the stock key names timm expects."""
    import torch  # noqa: PLC0415
    import torch.nn as nn  # noqa: PLC0415

    merged = 0
    for name, mod in list(model.named_modules()):
        for child_name, child in list(mod.named_children()):
            if hasattr(child, "merged_weight") and isinstance(getattr(child, "base", None), nn.Linear):
                with torch.no_grad():
                    child.base.weight.copy_(child.merged_weight())
                setattr(mod, child_name, child.base)
                merged += 1
                if child_name == "qkv" and hasattr(mod, "qkv_bias_separate"):
                    mod.qkv_bias_separate = False  # back to the stock fused path
    return merged


def freeze_head_rows(model, trainable_rows: list[int]):
    """head.fc trains ONLY the GT-covered rows. Every other row keeps its exact stock /
    release value: the gradient is masked to zero, and _assert_head_rows_frozen checks
    the result bitwise afterwards rather than trusting the mask."""
    import torch  # noqa: PLC0415

    fc = model.head.fc
    mask = torch.zeros(fc.weight.shape[0], 1)
    mask[trainable_rows] = 1.0
    fc.weight.requires_grad_(True)
    fc.bias.requires_grad_(True)
    fc.weight.register_hook(lambda g, m=mask: g * m.to(g.device, g.dtype))
    fc.bias.register_hook(lambda g, m=mask: g * m.to(g.device, g.dtype).squeeze(1))
    return mask


def _assert_head_rows_frozen(model, ref_weight, ref_bias, trainable_rows: list[int]):
    """The structural half of the anti-forgetting claim, verified not assumed."""
    import torch  # noqa: PLC0415

    keep = torch.ones(ref_weight.shape[0], dtype=torch.bool)
    keep[trainable_rows] = False
    w = model.head.fc.weight.detach().cpu()
    b = model.head.fc.bias.detach().cpu()
    if not torch.equal(w[keep], ref_weight[keep]) or not torch.equal(b[keep], ref_bias[keep]):
        drift = (w[keep] != ref_weight[keep]).sum().item()
        raise SystemExit(f"FROZEN HEAD ROWS DRIFTED ({drift} weight elements) -- refusing to save")
    return int(keep.sum())


# ── data ──────────────────────────────────────────────────────────────


def make_transform():
    from torchvision import transforms  # noqa: PLC0415

    return transforms.Compose([transforms.Resize((IMG_SIZE, IMG_SIZE)), transforms.ToTensor()])


def decode_batch(images: dict[int, bytes], post_ids: list[int], tf):
    import torch  # noqa: PLC0415
    from PIL import Image  # noqa: PLC0415

    xs, kept = [], []
    for pid in post_ids:
        raw = images.get(pid)
        if raw is None:
            continue
        try:
            xs.append(tf(Image.open(io.BytesIO(raw)).convert("RGB")))
        except Exception as e:  # noqa: BLE001 -- one unreadable blob must not kill a run
            print(f"  [warn] decode failed post={pid}: {type(e).__name__}: {e}", flush=True)
            continue
        kept.append(pid)
    if not xs:
        return None, []
    return torch.stack(xs), kept


# ── loss ──────────────────────────────────────────────────────────────


def gt_loss(student_logits, target, mask, pos_weight):
    """BCE on the (image, tag) cells that actually have a human verdict. Cells without
    GT contribute nothing -- an unlabelled tag is not a negative."""
    import torch  # noqa: PLC0415
    import torch.nn.functional as F  # noqa: PLC0415

    if mask.sum() == 0:
        return torch.zeros((), device=student_logits.device)
    w = torch.where(target > 0.5, pos_weight, torch.ones_like(pos_weight)) * mask
    return (F.binary_cross_entropy_with_logits(student_logits, target, reduction="none")
            * w).sum() / w.sum().clamp(min=1.0)


def distill_loss(student_logits, teacher_logits, col_mask, temperature: float):
    """Bernoulli KD on the non-GT columns: cross-entropy H(p_teacher, p_student).
    Its minimum is exactly p_student == p_teacher, so this is KL up to the teacher's
    (constant, gradient-free) entropy. `kl` is returned separately as the honest,
    reportable divergence."""
    import torch  # noqa: PLC0415
    import torch.nn.functional as F  # noqa: PLC0415

    t = temperature
    pt = torch.sigmoid(teacher_logits / t)
    ce = F.binary_cross_entropy_with_logits(student_logits / t, pt, reduction="none")
    ent = -(pt * torch.log(pt.clamp_min(1e-8)) + (1 - pt) * torch.log((1 - pt).clamp_min(1e-8)))
    ce = ce * col_mask
    kl = ((ce - ent * col_mask).clamp_min(0)).sum() / col_mask.sum().clamp(min=1.0)
    return ce.sum() / col_mask.sum().clamp(min=1.0) * (t * t), kl.detach()


# ── evaluation ────────────────────────────────────────────────────────


def eval_gt(student, val_ids, images, label_map, tag_index, tf, device, batch_size, thresh):
    """Per-tag P/R/F1 on the group-held-out split at the fleet threshold."""
    import torch  # noqa: PLC0415

    student.eval()
    scores: dict[int, dict] = {}
    with torch.no_grad():
        for i in range(0, len(val_ids), batch_size):
            x, kept = decode_batch(images, val_ids[i:i + batch_size], tf)
            if x is None:
                continue
            p = torch.sigmoid(student(x.to(device)).float().cpu())
            for j, pid in enumerate(kept):
                scores[pid] = p[j]
    out = {}
    for tag, col in tag_index.items():
        tp = fp = fn = tn = 0
        for pid, y in label_map.get(tag, []):
            if pid not in scores:
                continue
            pred = float(scores[pid][col]) >= thresh
            if y and pred: tp += 1
            elif y and not pred: fn += 1
            elif not y and pred: fp += 1
            else: tn += 1
        P = tp / max(1, tp + fp); R = tp / max(1, tp + fn)
        out[tag] = dict(P=round(P, 4), R=round(R, 4),
                        F1=round(2 * P * R / max(1e-9, P + R), 4),
                        tp=tp, fp=fp, fn=fn, tn=tn, val_pos=tp + fn, pred_pos=tp + fp)
    return out, scores


def eval_preservation(student, teacher, val_ids, images, gt_cols, tf, device,
                      batch_size, topk=50, thresholds=(0.35, 0.5), conf_thresh=0.35):
    """Did fine-tuning a dozen tags quietly rewrite the other ~10.8k?

    Reported on held-out images over the NON-GT columns only:
      mean_abs_dp   -- mean |p_student - p_teacher|
      mean_kl       -- mean Bernoulli KL(teacher || student), in nats
      topk_jaccard  -- overlap of the teacher's vs student's top-k tag sets.
      conf_jaccard  -- overlap of the CONFIDENT tag sets ({p >= conf_thresh}), teacher
                       vs student. Read this one first. topk50 is dominated by rank
                       churn in the tail: on a 10.8k vocab the 50th tag sits at p~1e-3
                       among hundreds of near-ties, so an utterly meaningless Δp of
                       1e-4 reorders it and costs 49/51 = 0.961 on that image. Both are
                       reported (topk50 is the conservative canary, and it is what the
                       plan asked for), but conf_jaccard is the one that means "the tags
                       a user actually sees did not change".
      flip_rate@t   -- fraction of (image, tag) cells whose >=t decision changed, and
                       flips_per_image@t, which is the operationally legible number.
    """
    import torch  # noqa: PLC0415

    student.eval(); teacher.eval()
    n_cells = 0; abs_dp = 0.0; kl_sum = 0.0; jac = []; conf_jac = []; max_dp = 0.0
    flips = {t: 0 for t in thresholds}
    n_img = 0
    with torch.no_grad():
        for i in range(0, len(val_ids), batch_size):
            x, kept = decode_batch(images, val_ids[i:i + batch_size], tf)
            if x is None:
                continue
            xb = x.to(device)
            ps = torch.sigmoid(student(xb).float())[:, ~gt_cols]
            pt = torch.sigmoid(teacher(xb).float())[:, ~gt_cols]
            d = (ps - pt).abs()
            abs_dp += d.sum().item(); max_dp = max(max_dp, float(d.max()))
            kl_sum += (pt * torch.log((pt / ps.clamp_min(1e-8)).clamp_min(1e-8))
                       + (1 - pt) * torch.log(((1 - pt) / (1 - ps).clamp_min(1e-8)).clamp_min(1e-8))
                       ).clamp_min(0).sum().item()
            n_cells += ps.numel(); n_img += len(kept)
            for t in thresholds:
                flips[t] += ((ps >= t) != (pt >= t)).sum().item()
            k = min(topk, ps.shape[1])
            for j in range(ps.shape[0]):
                a = set(torch.topk(ps[j], k).indices.tolist())
                b = set(torch.topk(pt[j], k).indices.tolist())
                jac.append(len(a & b) / len(a | b))
                ca = set((ps[j] >= conf_thresh).nonzero().flatten().tolist())
                cb = set((pt[j] >= conf_thresh).nonzero().flatten().tolist())
                # both empty => nothing confident to disagree about => perfect agreement
                conf_jac.append(len(ca & cb) / len(ca | cb) if (ca or cb) else 1.0)
    if not n_cells:
        return {"error": "no val images decoded"}
    return {
        "n_images": n_img, "n_nongt_cols": int((~gt_cols).sum()),
        "mean_abs_dp": round(abs_dp / n_cells, 6),
        "max_abs_dp": round(max_dp, 6),
        "mean_kl_nats": round(kl_sum / n_cells, 6),
        f"topk{topk}_jaccard": round(sum(jac) / len(jac), 4),
        f"conf_jaccard@{conf_thresh}": round(sum(conf_jac) / len(conf_jac), 4),
        **{f"flip_rate@{t}": round(flips[t] / n_cells, 8) for t in thresholds},
        **{f"flips_per_image@{t}": round(flips[t] / max(1, n_img), 3) for t in thresholds},
    }


# ── preservation gate ─────────────────────────────────────────────────


GATE_KEYS = ("topk50_jaccard", "conf_jaccard@0.35", "mean_abs_dp", "flips_per_image@0.5")


def check_gate(pres: dict, args) -> dict:
    """Fail-closed evaluation of the preservation bounds.

    The whole point of this gate: a run that quietly degrades the base must abort
    itself rather than hand back a checkpoint whose damage is only visible to whoever
    thinks to look. Missing metric => FAIL (a gate that can't measure has not passed).
    """
    bounds = [
        ("topk50_jaccard", ">=", args.gate_topk_jaccard),
        ("conf_jaccard@0.35", ">=", args.gate_conf_jaccard),
        ("mean_abs_dp", "<=", args.gate_abs_dp),
        ("flips_per_image@0.5", "<=", args.gate_flips_per_image),
    ]
    checks = []
    for key, op, bound in bounds:
        v = pres.get(key)
        ok = (v is not None) and ((v >= bound) if op == ">=" else (v <= bound))
        checks.append(dict(metric=key, value=v, op=op, bound=bound, pass_=bool(ok)))
    return dict(passed=all(c["pass_"] for c in checks), checks=checks)


def gate_report(label: str, pres: dict, gate: dict) -> str:
    parts = [f"{c['metric']}={c['value']} {c['op']} {c['bound']} "
             f"{'ok' if c['pass_'] else 'FAIL'}" for c in gate["checks"]]
    return f"[gate:{label}] {'PASS' if gate['passed'] else 'ABORT'} | " + " | ".join(parts)


# ── main ──────────────────────────────────────────────────────────────


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--smoke", action="store_true",
                    help="tiny end-to-end run: few labels, --steps steps, all metrics computed")
    ap.add_argument("--base", choices=("release", "stock"), default="release")
    ap.add_argument("--pin-revision", default=None)
    ap.add_argument("--tags", nargs="*", default=None, help="restrict to these GT tags")
    ap.add_argument("--min-positive", type=int, default=None,
                    help="min GT positives for a tag to be trained (default: tagger_config "
                         "min_gt_positive, read live)")
    ap.add_argument("--extra-tags", nargs="*", default=None,
                    help="train these tags even below --min-positive. Their val metrics are "
                         "reported with low_confidence=true rather than being silently mixed "
                         "in with tags that cleared the floor.")
    ap.add_argument("--max-labels", type=int, default=0, help="0 = all")
    ap.add_argument("--neg-per-pos", type=float, default=20.0,
                    help="per-tag negative cap on the TRAIN side, as a multiple of that tag's "
                         "positives; re-drawn each epoch. 0 = uncapped (the old behaviour, in "
                         "which anal_hair is 56%% of the corpus). See balance_train_cells.")
    ap.add_argument("--max-val-posts", type=int, default=0,
                    help="0 = full val (required for an honest precision). >0 subsamples the "
                         "val NEGATIVES, which makes P optimistic -- for sweeps only, and the "
                         "result json records val_subsampled=true when it is used.")
    ap.add_argument("--blob-cache", default=os.environ.get("KURA_FINETUNE_BLOB_CACHE"),
                    help="local dir to cache post blobs across runs (a sweep re-reads ~3GB)")
    ap.add_argument("--kd-posts", type=int, default=0,
                    help="random non-GT posts sampled for KD-only batches (anti-forgetting "
                         "beyond the narrow GT domain)")
    ap.add_argument("--pres-val-posts", type=int, default=200,
                    help="random posts held out from ALL training, used only to measure stock "
                         "preservation on the general archive (see the two-domain note below)")
    ap.add_argument("--lora-r", type=int, default=8)
    ap.add_argument("--lora-alpha", type=float, default=16.0)
    ap.add_argument("--lora-dropout", type=float, default=0.0)
    ap.add_argument("--lr", type=float, default=1e-4)
    ap.add_argument("--head-lr", type=float, default=1e-3)
    ap.add_argument("--kd-weight", type=float, default=1.0)
    ap.add_argument("--kd-temperature", type=float, default=1.0)
    ap.add_argument("--epochs", type=int, default=1)
    ap.add_argument("--steps", type=int, default=0, help="0 = full epochs; else stop after N steps")
    ap.add_argument("--batch-size", type=int, default=4)
    ap.add_argument("--amp", action="store_true", default=True)
    ap.add_argument("--no-amp", dest="amp", action="store_false")
    ap.add_argument("--drop-path", type=float, default=0.0,
                    help="stochastic depth during fine-tuning (checkpoint ships up to 0.10; "
                         "see set_drop_path for why 0.0 is the default here)")
    ap.add_argument("--overfit-batch", type=int, default=0,
                    help="repeat ONE batch for N steps -- the standard sanity check that the "
                         "optimizer actually reduces this loss (used by --smoke)")
    # ── preservation gate. Fail-closed: any bound breached aborts the run and NOTHING
    #    promotable is written. Bounds are measured against the pre-fine-tune teacher on
    #    the random-archive held-out draw (never trained on, not the GT domain).
    ap.add_argument("--gate-topk-jaccard", type=float, default=0.95)
    ap.add_argument("--gate-conf-jaccard", type=float, default=0.98)
    ap.add_argument("--gate-abs-dp", type=float, default=0.002)
    ap.add_argument("--gate-flips-per-image", type=float, default=3.0)
    ap.add_argument("--gate-every", type=int, default=500,
                    help="also evaluate the gate every N steps, not just at epoch end, so a "
                         "run that diverges mid-epoch dies in minutes rather than hours")
    ap.add_argument("--no-gate", action="store_true",
                    help="calibration only: measure and report the gate but never abort. "
                         "Recorded as gate_enforced=false in the result json.")
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--outdir", default=os.environ.get(
        "KURA_FINETUNE_OUTDIR", str(Path.home() / "tagger-heads" / "finetune")))
    ap.add_argument("--no-save", action="store_true", help="compute everything, write no ckpt")
    ap.add_argument("--run-name", default=None,
                    help="suffix for the result json, so a sweep's runs do not overwrite "
                         "each other (finetune_result[-<run-name>].json)")
    args = ap.parse_args()

    if args.smoke:
        args.max_labels = args.max_labels or 200
        args.overfit_batch = args.overfit_batch or 8
        args.kd_posts = args.kd_posts or 8
        args.pres_val_posts = args.pres_val_posts if args.pres_val_posts != 200 else 40
        args.no_save = True

    import torch  # noqa: PLC0415

    random.seed(args.seed); torch.manual_seed(args.seed)
    device = "cuda" if torch.cuda.is_available() else "cpu"
    t_start = time.time()
    outdir = Path(args.outdir); outdir.mkdir(parents=True, exist_ok=True)

    db = DB()
    conf = live_config(db)
    min_pos = args.min_positive if args.min_positive is not None else int(conf.get("min_gt_positive", 20))
    print(f"[cfg] live tagger_config: min_gt_positive={conf.get('min_gt_positive')} "
          f"min_precision={conf.get('min_precision')} min_recall={conf.get('min_recall')} "
          f"min_val_pos_support={conf.get('min_val_pos_support')} "
          f"min_pred_pos_support={conf.get('min_pred_pos_support')}", flush=True)

    extra_tags = set(args.extra_tags or [])
    labels, groups, tag_counts = gt_labels(db, min_pos, args.tags, extra_tags)
    if not labels:
        raise SystemExit(f"no GT tag reaches min_positive={min_pos} -- nothing to train")
    # Below-floor tags admitted by --extra-tags. Policy call for `erection`: it has 19 GT
    # positives against a floor of 20, but 4,370 negatives -- one of the largest correction
    # domains in the corpus, and dropping it for a one-positive shortfall would discard the
    # reap signal that motivated this loop. Trained; its val metric is flagged low-confidence
    # rather than being read as if it had support.
    low_conf = sorted(t for t in tag_counts
                      if t in extra_tags and int(tag_counts[t][0]) < min_pos)
    if low_conf:
        print(f"[gt] below-floor tags trained by --extra-tags (metrics LOW-CONFIDENCE): "
              + ", ".join(f"{t}(pos={tag_counts[t][0]}<{min_pos})" for t in low_conf), flush=True)
    print(f"[gt] tags={len(tag_counts)} labels={len(labels)} "
          f"posts={len({p for p, _, _ in labels})}", flush=True)

    model, tag_names, base_meta = load_base(db, args.base, args.pin_revision)
    name_to_col = {n: i for i, n in enumerate(tag_names)}
    trainable_tags = sorted(t for t in tag_counts if t in name_to_col)
    missing = sorted(t for t in tag_counts if t not in name_to_col)
    if missing:
        # No row exists to train. Appending rows is build_release's "add" op and needs a
        # logit_index allocation + MANIFEST entry -- out of scope here, reported not hidden.
        print(f"[gt] {len(missing)} GT tags have NO row in the base vocab, skipped: {missing}",
              flush=True)
    labels = [(p, t, y) for p, t, y in labels if t in name_to_col]
    if args.max_labels and len(labels) > args.max_labels:
        # Positives first: GT is ~10:1 negative overall (anal_hair alone is 15937:28), so a
        # uniform sample of a few hundred labels would contain almost no positive and the
        # per-tag metrics below would be vacuous.
        rng = random.Random(args.seed)
        pos = [l for l in labels if l[2] > 0]; neg = [l for l in labels if l[2] == 0]
        rng.shuffle(pos); rng.shuffle(neg)
        half = args.max_labels // 2
        labels = pos[:max(half, args.max_labels - len(neg))] + neg[:args.max_labels - min(len(pos), half)]
        labels = labels[:args.max_labels]
        trainable_tags = sorted({t for _, t, _ in labels})
        print(f"[gt] sampled down to {len(labels)} labels over {len(trainable_tags)} tags "
              f"(--max-labels)", flush=True)

    tag_index = {t: name_to_col[t] for t in trainable_tags}
    gt_post_ids = sorted({p for p, _, _ in labels})
    label_map: dict[str, list] = {}
    per_post: dict[int, dict[str, float]] = {}
    for p, t, y in labels:
        label_map.setdefault(t, []).append((p, y))
        per_post.setdefault(p, {})[t] = y
    any_pos = {p: any(v > 0 for v in d.values()) for p, d in per_post.items()}
    train_ids, val_ids = split_holdout(gt_post_ids, groups, any_pos)
    print(f"[split] group-held-out 20%: train_posts={len(train_ids)} val_posts={len(val_ids)} "
          f"groups={len({groups[p] for p in gt_post_ids})}", flush=True)

    val_subsampled = False
    if args.max_val_posts and len(val_ids) > args.max_val_posts:
        # Sweep-only. Keeps every val POSITIVE and subsamples the negative-only posts, so
        # recall stays exact while precision becomes optimistic (false positives are being
        # removed from the denominator). Never use for an acceptance number.
        vp = [p for p in val_ids if any_pos.get(p)]
        vn = [p for p in val_ids if not any_pos.get(p)]
        room = max(0, args.max_val_posts - len(vp))
        keep = set(vp) | set(random.Random(args.seed + 7).sample(vn, min(room, len(vn))))
        val_ids = [p for p in val_ids if p in keep]
        val_subsampled = True
        print(f"[split] val SUBSAMPLED to {len(val_ids)} posts (--max-val-posts): all "
              f"{len(vp)} positive-bearing kept, negatives capped -- PRECISION IS OPTIMISTIC",
              flush=True)

    # Two disjoint random draws. `kd_ids` is TRAINED on (KD-only batches); `pres_ids` is
    # trained on by nothing and exists solely to answer "did the general archive change?".
    # Measuring preservation only on held-out GT images would understate forgetting: GT is
    # a narrow slice (penis/genital close-ups from a handful of verify pools), and the
    # tags at risk of being silently rewritten are the ~10.8k that describe everything
    # else. Both domains are reported separately below; the random one is the honest one.
    drawn = kd_post_ids(db, args.kd_posts + args.pres_val_posts, set(gt_post_ids), args.seed)
    kd_ids = drawn[:args.kd_posts]
    pres_ids = drawn[args.kd_posts:args.kd_posts + args.pres_val_posts]
    print(f"[kd] distillation-only train posts={len(kd_ids)} | "
          f"random preservation-val posts (never trained)={len(pres_ids)}", flush=True)

    # Balance BEFORE fetching: capping negatives drops most of anal_hair/phimosis/erection
    # from the train side, and there is no reason to pull 3 GB of blobs for images no step
    # will ever see. Epoch 0's draw; later epochs re-draw and fetch their own additions.
    n_pre_cap_posts = len(train_ids)
    n_pre_cap_cells = sum(len(per_post[p]) for p in train_ids)
    train_cells, train_counts = balance_train_cells(
        label_map, set(train_ids), args.neg_per_pos, args.seed)
    train_ids = [p for p in train_ids if p in train_cells]
    need = sorted(set(train_ids) | set(val_ids) | set(kd_ids) | set(pres_ids))
    cache = Path(args.blob_cache) if args.blob_cache else None
    print(f"[data] fetching {len(need)} image blobs from primary"
          + (f" (cache {cache})" if cache else "") + "...", flush=True)
    images = fetch_images(db, need, cache)
    print(f"[data] blobs fetched={len(images)}", flush=True)

    n_cells = sum(len(d) for d in train_cells.values())
    print(f"[balance] neg-per-pos cap={args.neg_per_pos}: train cells {n_pre_cap_cells} -> "
          f"{n_cells}, train posts {n_pre_cap_posts} -> {len(train_ids)}", flush=True)
    print(f"{'  tag':<24s} {'pos':>5s} {'neg_avail':>10s} {'neg_kept':>9s} {'ratio':>7s}", flush=True)
    for t in sorted(train_counts, key=lambda x: -train_counts[x]["neg_avail"]):
        c = train_counts[t]
        print(f"  {t:<22s} {c['pos']:>5d} {c['neg_avail']:>10d} {c['neg_kept']:>9d} "
              f"{c['ratio']:>7.1f}", flush=True)

    orig_dp = set_drop_path(model, args.drop_path)
    print(f"[model] drop_path {max(orig_dp) if orig_dp else 0:.3f}(max, from ckpt) "
          f"-> {args.drop_path}", flush=True)

    # teacher = the pre-fine-tune model, frozen. Every preservation number below is
    # measured against exactly what the fleet runs today, not against an abstraction.
    teacher = copy.deepcopy(model).eval().to(device)
    for p in teacher.parameters():
        p.requires_grad_(False)

    ref_w = model.head.fc.weight.detach().clone()
    ref_b = model.head.fc.bias.detach().clone()
    for p in model.parameters():
        p.requires_grad_(False)
    adapters = apply_lora(model, args.lora_r, args.lora_alpha, args.lora_dropout, args.seed)
    trainable_rows = sorted(tag_index.values())
    freeze_head_rows(model, trainable_rows)
    student = model.to(device)
    lora_params = [p for _, a in adapters for p in (a.A, a.B)]
    n_lora = sum(p.numel() for p in lora_params)
    print(f"[lora] r={args.lora_r} alpha={args.lora_alpha} modules={len(adapters)} "
          f"params={n_lora/1e6:.2f}M ({100*n_lora/sum(p.numel() for p in teacher.parameters()):.2f}% "
          f"of base); head rows trainable={len(trainable_rows)}/{ref_w.shape[0]}", flush=True)

    gt_cols = torch.zeros(ref_w.shape[0], dtype=torch.bool)
    gt_cols[trainable_rows] = True
    gt_cols_d = gt_cols.to(device)
    opt = torch.optim.AdamW([
        {"params": lora_params, "lr": args.lr, "weight_decay": 0.0},
        {"params": [student.head.fc.weight, student.head.fc.bias],
         "lr": args.head_lr, "weight_decay": 0.0},  # weight_decay MUST stay 0: it would
    ])                                              # shrink the frozen rows via the optimizer.
    scaler = torch.amp.GradScaler("cuda", enabled=(args.amp and device == "cuda"))
    tf = make_transform()

    # Class balance per trained tag, from the CAPPED train side. Because
    # balance_train_cells already holds the ratio at --neg-per-pos, the old min(20, ...)
    # clip is no longer doing the work: the weight below is the true post-cap ratio, not
    # a number that quietly stopped tracking a 664:1 imbalance at 20.
    def make_pos_weight(counts):
        pw = torch.ones(ref_w.shape[0])
        for t, col in tag_index.items():
            c = counts.get(t)
            if c and c["pos"]:
                pw[col] = c["neg_kept"] / c["pos"]
        return pw.to(device)

    pos_weight_d = make_pos_weight(train_counts)

    # Step-0 identity: LoRA initialises B=0, so before any update the student MUST be
    # numerically identical to the teacher. Fail-closed -- this is the check that catches
    # a wrapper the model routes around (timm's WindowAttention reaching past .qkv) or a
    # train/eval mismatch, both of which would otherwise show up only as bad metrics.
    x0, _ = decode_batch(images, train_ids[:min(2, len(train_ids))], tf)
    if x0 is not None:
        student.eval()
        with torch.no_grad():
            d = (student(x0.to(device)) - teacher(x0.to(device))).abs().max().item()
        print(f"[check] step-0 student-vs-teacher max|Δlogit| = {d:.3e}", flush=True)
        if d > 1e-3:
            raise SystemExit(
                f"step-0 identity check FAILED (max|Δlogit|={d:.3e}, expected ~0). The LoRA "
                "wrapper is not on the path the model actually executes, or student/teacher "
                "differ in mode. Refusing to train on a base that isn't the teacher.")

    def build_batches(ids, seed):
        out = []
        tr = list(ids); random.Random(seed).shuffle(tr)
        kd = list(kd_ids); random.Random(seed + 1).shuffle(kd)
        for i in range(0, len(tr), args.batch_size):
            out.append(("gt", tr[i:i + args.batch_size]))
        for i in range(0, len(kd), args.batch_size):
            out.append(("kd", kd[i:i + args.batch_size]))
        random.Random(seed + 2).shuffle(out)
        return out

    batches = build_batches(train_ids, args.seed)
    if args.overfit_batch:
        # One batch, repeated. Consecutive steps over DIFFERENT batches say nothing about
        # whether the optimizer works (batch-to-batch variance dominates); repeating one
        # batch isolates it. Pick a batch that actually carries positive GT cells.
        pick = next((b for b in batches if b[0] == "gt"
                     and any(any(v > 0 for v in train_cells.get(p, {}).values()) for p in b[1])),
                    batches[0])
        batches = [pick] * args.overfit_batch
        args.epochs, args.steps = 1, args.overfit_batch
        print(f"[overfit] repeating one {pick[0]} batch (n={len(pick[1])}) for "
              f"{args.overfit_batch} steps", flush=True)

    # ── the gate, as a closure over the frozen teacher and the never-trained random draw.
    #    Returns (pres, gate). Called every --gate-every steps and at every epoch end;
    #    a breach aborts unless --no-gate (calibration) is set.
    gate_log = []

    def run_gate(label: str):
        if not pres_ids:
            return None, dict(passed=False, checks=[dict(metric="pres_ids", value=0,
                              op=">=", bound=1, pass_=False)])
        p = eval_preservation(student, teacher, pres_ids, images, gt_cols.to(device),
                              tf, device, args.batch_size)
        g = check_gate(p, args)
        print(gate_report(label, p, g), flush=True)
        gate_log.append(dict(at=label, step=step, preservation=p, gate=g))
        return p, g

    def save_ckpt(tag: str) -> dict:
        """Per-epoch snapshot, so a good epoch survives a later one going bad. Written
        only after the gate passed for that epoch -- an aborted run leaves nothing that
        could be mistaken for promotable."""
        import copy as _copy  # noqa: PLC0415

        snap = _copy.deepcopy(student).cpu().eval()
        n_m = merge_lora(snap)
        _assert_head_rows_frozen(snap, ref_w, ref_b, trainable_rows)
        path = outdir / f"wd14-finetuned-{tag}.pt"
        torch.save({
            "state_dict": snap.state_dict(),
            "repo": base_meta["repo"], "revision": base_meta["revision"],
            "n_out": base_meta["n_out"], "n_stock": base_meta["n_stock"],
            "patched": base_meta["patched"], "img_size": base_meta["img_size"],
            "lineage": "base-finetune", "freeze_validated": False,
            "base_release_id": base_meta.get("release_id"),
            "finetuned_tags": trainable_tags, "low_confidence_tags": low_conf,
            "merged_modules": n_m, "epoch_tag": tag,
        }, path)
        sha = hashlib.sha256(path.read_bytes()).hexdigest()
        print(f"[save] {path} sha256={sha[:16]}", flush=True)
        return dict(path=str(path), sha256=sha, merged_modules=n_m)

    hist = []
    epoch_ckpts = []
    step = 0
    stop = False
    aborted = None
    for epoch in range(args.epochs):
        if stop:
            break
        if epoch > 0 and args.neg_per_pos > 0 and not args.overfit_batch:
            # Re-draw the per-tag negative sample so a multi-epoch run sees the full
            # negative pool over time instead of the same capped slice repeatedly.
            train_cells, train_counts = balance_train_cells(
                label_map, set(train_ids) | set(train_cells), args.neg_per_pos,
                args.seed + 100 * epoch)
            have = [p for p in train_cells if p in images]
            pos_weight_d = make_pos_weight(train_counts)
            batches = build_batches(have, args.seed + 100 * epoch)
            print(f"[balance] epoch {epoch}: re-drew negatives, train posts={len(have)}",
                  flush=True)
        for kind, ids in batches:
            x, kept = decode_batch(images, ids, tf)
            if x is None:
                continue
            xb = x.to(device, non_blocking=True)
            target = torch.zeros(len(kept), ref_w.shape[0])
            mask = torch.zeros(len(kept), ref_w.shape[0])
            if kind == "gt":
                for j, pid in enumerate(kept):
                    for t, y in train_cells.get(pid, {}).items():
                        target[j, tag_index[t]] = y
                        mask[j, tag_index[t]] = 1.0
            target, mask = target.to(device), mask.to(device)
            student.train()
            with torch.no_grad(), torch.amp.autocast("cuda", enabled=scaler.is_enabled()):
                tl = teacher(xb).float()
            with torch.amp.autocast("cuda", enabled=scaler.is_enabled()):
                sl = student(xb)
            sl = sl.float()
            l_gt = gt_loss(sl, target, mask, pos_weight_d)
            col_mask = (~gt_cols_d).float().expand_as(sl)
            l_kd, kl = distill_loss(sl, tl, col_mask, args.kd_temperature)
            loss = l_gt + args.kd_weight * l_kd
            opt.zero_grad(set_to_none=True)
            scaler.scale(loss).backward()
            scaler.step(opt); scaler.update()
            step += 1
            fl, fgt, fkd = float(loss.detach()), float(l_gt.detach()), float(l_kd.detach())
            hist.append(dict(step=step, kind=kind, n=len(kept), loss=round(fl, 6),
                             gt=round(fgt, 6), kd=round(fkd, 6), kl=round(float(kl), 8),
                             t=round(time.time() - t_start, 2)))
            print(f"  step {step:<4d} [{kind}] n={len(kept)} loss={fl:.5f} "
                  f"gt={fgt:.5f} kd={fkd:.5f} kl={float(kl):.3e} "
                  f"({time.time() - t_start:.1f}s)", flush=True)
            if args.gate_every and step % args.gate_every == 0:
                _, g = run_gate(f"step{step}")
                if not g["passed"] and not args.no_gate:
                    aborted = f"step{step}"; stop = True; break
            if args.steps and step >= args.steps:
                stop = True
                break
        if aborted:
            break
        _, g = run_gate(f"epoch{epoch + 1}")
        if not g["passed"] and not args.no_gate:
            aborted = f"epoch{epoch + 1}"
            break
        if not args.no_save and not args.overfit_batch:
            epoch_ckpts.append(dict(epoch=epoch + 1, step=step, **save_ckpt(f"e{epoch + 1}")))

    if aborted:
        # HARD ABORT. Everything measured so far is still reported -- the numbers are the
        # point -- but no checkpoint from this run is offered as promotable.
        print(f"\n[gate] *** RUN ABORTED at {aborted}: preservation bound breached. "
              "No promotable checkpoint written. ***", flush=True)
        args.no_save = True

    peak_vram = (torch.cuda.max_memory_allocated() / 2**30) if device == "cuda" else None
    n_frozen = _assert_head_rows_frozen(student, ref_w, ref_b, trainable_rows)
    print(f"[check] head.fc frozen rows verified byte-identical: {n_frozen}", flush=True)

    print(f"[eval] per-tag GT metrics on {len(val_ids)} held-out posts...", flush=True)
    gt_metrics, _ = eval_gt(student, val_ids, images, label_map, tag_index, tf, device,
                            args.batch_size, FLEET_THRESH)
    base_metrics, _ = eval_gt(teacher, val_ids, images, label_map, tag_index, tf, device,
                              args.batch_size, FLEET_THRESH)
    print("[eval] stock preservation (GT-domain held-out + random archive held-out)...", flush=True)
    pres_gt = eval_preservation(student, teacher, val_ids, images, gt_cols.to(device),
                                tf, device, args.batch_size)
    pres_rand = (eval_preservation(student, teacher, pres_ids, images, gt_cols.to(device),
                                   tf, device, args.batch_size)
                 if pres_ids else {"skipped": "no --pres-val-posts drawn"})
    pres = {"gt_domain_heldout": pres_gt, "random_archive_heldout": pres_rand}

    floor_p = float(conf.get("min_precision", 0.85)); floor_r = float(conf.get("min_recall", 0.7))
    floor_vp = int(conf.get("min_val_pos_support", 20)); floor_pp = int(conf.get("min_pred_pos_support", 20))
    print("\n=== per-tag (held-out, thr=%.2f) — student vs teacher ===" % FLEET_THRESH, flush=True)
    if val_subsampled:
        print("  !! val negatives were subsampled (--max-val-posts): P is OPTIMISTIC, "
              "relative comparison only", flush=True)
    for t in sorted(gt_metrics, key=lambda x: -gt_metrics[x]["val_pos"]):
        m, b = gt_metrics[t], base_metrics[t]
        ok = (m["P"] >= floor_p and m["R"] >= floor_r
              and m["val_pos"] >= floor_vp and m["pred_pos"] >= floor_pp)
        flag = "LOW-CONF" if t in low_conf else ("PASS" if ok else "below-floor")
        print(f"  {t:22s} val_pos={m['val_pos']:<4d} P={m['P']:.3f}({b['P']:.3f}) "
              f"R={m['R']:.3f}({b['R']:.3f}) F1={m['F1']:.3f}({b['F1']:.3f}) "
              f"pred_pos={m['pred_pos']:<4d} {flag}", flush=True)
    print("\n=== stock preservation (non-GT columns) ===", flush=True)
    for domain, d in pres.items():
        print(f"  [{domain}]", flush=True)
        for k, v in d.items():
            print(f"    {k:24s} {v}", flush=True)
    if peak_vram:
        print(f"\n[gpu] {torch.cuda.get_device_name(0)} peak_alloc={peak_vram:.2f} GiB "
              f"steps={step} wall={time.time() - t_start:.1f}s "
              f"per_step={(hist[-1]['t'] - hist[0]['t']) / max(1, len(hist) - 1):.2f}s"
              if hist else "", flush=True)

    result = dict(
        script="tagger/train_base_finetune.py", smoke=bool(args.smoke),
        lineage="base-finetune", freeze_validated=False,
        base=base_meta,
        feature_space=dict(preprocess="Resize((448,448)) + ToTensor, no Normalize",
                           matches="tagger/infer.py::_run_wd14_fixed"),
        lora=dict(r=args.lora_r, alpha=args.lora_alpha, dropout=args.lora_dropout,
                  targets=list(LORA_TARGET_SUFFIXES), modules=len(adapters), params=n_lora),
        train=dict(lr=args.lr, head_lr=args.head_lr, kd_weight=args.kd_weight,
                   kd_temperature=args.kd_temperature, batch_size=args.batch_size,
                   epochs=args.epochs, steps_run=step, amp=scaler.is_enabled(), seed=args.seed,
                   neg_per_pos=args.neg_per_pos),
        gate=dict(enforced=(not args.no_gate), aborted_at=aborted,
                  bounds=dict(topk50_jaccard=args.gate_topk_jaccard,
                              conf_jaccard=args.gate_conf_jaccard,
                              mean_abs_dp=args.gate_abs_dp,
                              flips_per_image=args.gate_flips_per_image),
                  every_steps=args.gate_every, evaluations=gate_log),
        epoch_checkpoints=epoch_ckpts,
        data=dict(tags=trainable_tags, tags_missing_from_vocab=missing,
                  low_confidence_tags=low_conf, val_subsampled=val_subsampled,
                  labels=len(labels), gt_posts=len(gt_post_ids), train_posts=len(train_ids),
                  val_posts=len(val_ids), kd_posts=len(kd_ids), blobs=len(images),
                  per_tag_counts={t: tag_counts[t] for t in trainable_tags},
                  per_tag_train_counts=train_counts),
        config_live={k: conf.get(k) for k in
                     ("min_gt_positive", "min_precision", "min_recall", "min_val_pos_support",
                      "min_pred_pos_support", "eval_split_mode")},
        loss_history=hist,
        per_tag_student=gt_metrics, per_tag_teacher=base_metrics,
        preservation=pres,
        head_rows_frozen_verified=n_frozen,
        gpu=dict(device=device, name=(torch.cuda.get_device_name(0) if device == "cuda" else None),
                 peak_alloc_gib=(round(peak_vram, 3) if peak_vram else None)),
        wall_seconds=round(time.time() - t_start, 1),
    )

    if not args.no_save:
        # Merge correctness: folding W += (alpha/r)BA must not change what the model
        # computes. Checked on real images, because "the merge is obviously right" is
        # exactly the assumption that ships a checkpoint nobody can reproduce.
        probe_x, _ = decode_batch(images, val_ids[:min(4, len(val_ids))], tf)
        student.eval()
        pre = None
        if probe_x is not None:
            with torch.no_grad():
                pre = student(probe_x.to(device)).float().cpu()
        n_merged = merge_lora(student)
        merge_delta = None
        if pre is not None:
            with torch.no_grad():
                merge_delta = float((student(probe_x.to(device)).float().cpu() - pre).abs().max())
            print(f"[check] LoRA merge max|Δlogit| = {merge_delta:.3e} over {n_merged} modules",
                  flush=True)
            if merge_delta > 1e-2:
                raise SystemExit(f"LoRA merge changed the model (max|Δlogit|={merge_delta:.3e}) "
                                 "-- refusing to save a checkpoint that isn't what was trained")
        result["merge_max_abs_dlogit"] = merge_delta
        student = student.cpu().eval()
        _assert_head_rows_frozen(student, ref_w, ref_b, trainable_rows)
        ckpt_path = outdir / "wd14-finetuned.pt"
        torch.save({
            "state_dict": student.state_dict(),
            "repo": base_meta["repo"], "revision": base_meta["revision"],
            "n_out": base_meta["n_out"], "n_stock": base_meta["n_stock"],
            "patched": base_meta["patched"], "img_size": base_meta["img_size"],
            # Lineage markers. build_release.py CANNOT validate this ckpt (a backbone
            # fine-tune breaks registry_freeze by construction) -- anything that loads
            # it must read these, not assume the wd14-fixed contract.
            "lineage": "base-finetune", "freeze_validated": False,
            "base_release_id": base_meta.get("release_id"),
            "finetuned_tags": trainable_tags,
            "lora": result["lora"], "merged_modules": n_merged,
        }, ckpt_path)
        sha = hashlib.sha256(ckpt_path.read_bytes()).hexdigest()
        result["checkpoint"] = dict(path=str(ckpt_path), sha256=sha, merged_modules=n_merged)
        print(f"\n[save] {ckpt_path} sha256={sha[:16]} merged_lora_modules={n_merged}", flush=True)
    else:
        print("\n[save] skipped (--no-save / --smoke)", flush=True)

    stem = "smoke_result" if args.smoke else "finetune_result"
    rpath = outdir / (f"{stem}-{args.run_name}.json" if args.run_name else f"{stem}.json")
    rpath.write_text(json.dumps(result, indent=1))
    print(f"[result] {rpath}", flush=True)
    print("FINETUNE_DONE " + json.dumps({
        "steps": step, "loss_first": hist[0]["loss"] if hist else None,
        "loss_last": hist[-1]["loss"] if hist else None,
        "gate_aborted_at": aborted, "gate_enforced": (not args.no_gate),
        "preservation": pres,
    }), flush=True)
    return 2 if aborted else 0


if __name__ == "__main__":
    sys.exit(main())
