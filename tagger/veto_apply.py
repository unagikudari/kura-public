# Description: shared gated-veto application, bundled into the release ckpt so every worker runs identical logic
# Kind: library
# Effects: read-only (pure inference)
# Runs-on: any (primary full pipeline + WD14-only workers)
"""Gated-veto head application (plan:kura-tagger-self-improvement-loop, 「打ち消し
続ける」構造). A veto head is a linear probe on the wd14-fixed backbone's 1024-d
pooled features that CANCELS a tag the ensemble/logit-patch produced when the
correction-trained probe disagrees (< threshold). Suppress-only, gated by the
tag's own presence.

This lives in its own tiny module (no heavy imports at module load) so BOTH
inference paths use byte-identical logic:
  - tagger/infer.py::tag_image (primary full pipeline)
  - tagger/nodec_autotag_worker.py::infer (WD14-only GPU/CPU workers)
and so tagger/build_release.py can read the delivered safetensors to bundle the
weights INTO the release ckpt — which is what makes the veto ride the existing
release_client distribution to every node (the fleet-effectiveness gap an
primary-only file-based veto had: other workers re-added the tag).
"""
from __future__ import annotations

from pathlib import Path


def run_veto(pooled, vh: dict) -> float:
    """sigmoid(((pooled-mu)/sd) · w + b). The mu/sd feature standardization is
    REQUIRED (trainer f7e0d98d: dropping it invalidates the probe)."""
    import torch  # noqa: PLC0415
    f = (pooled - vh["mu"]) / vh["sd"]
    return float(torch.sigmoid((f * vh["w"]).sum() + vh["b"]))


def load_veto_dir(veto_dir) -> dict:
    """Read *.safetensors from veto_dir → {gate: {w,mu,sd,b,threshold}} (torch
    tensors + floats). Used by build_release.py to bundle the delivered veto
    heads into the release ckpt. Tensors serialize cleanly through torch.save."""
    heads: dict[str, dict] = {}
    d = Path(veto_dir)
    if not d.is_dir():
        return heads
    from safetensors import safe_open  # noqa: PLC0415
    for f in sorted(d.glob("*.safetensors")):
        with safe_open(str(f), framework="pt") as sf:
            meta = sf.metadata() or {}
            gate = meta.get("gate") or f.stem
            heads[gate] = {
                "w": sf.get_tensor("w").float().flatten(),
                "mu": sf.get_tensor("mu").float().flatten(),
                "sd": sf.get_tensor("sd").float().flatten(),
                "b": float(sf.get_tensor("b").float().flatten()[0]),
                "threshold": float(meta.get("threshold", 0.4)),
            }
    return heads
