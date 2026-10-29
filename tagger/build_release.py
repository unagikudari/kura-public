#!/usr/bin/env python3
# Description: allowlist-only builder — merges git-tracked wd14 patches into one release.
# Kind: tool
# Effects: mutating (writes tagger/build/ output; network reads from HF hub)
# Requires: torch, timm, safetensors, huggingface_hub (tagger/requirements.txt)
# Runs-on: any
"""build_release.py — merge approved wd14 patches into a release (plan:kura-tagger-registry
rev2 Step 3, fact 58082f48).

Reads ONLY the MANIFEST.json allowlist (never globs tagger/patches/**) and merges each
named {tag, version, patch_sha256} entry's logit row into a freshly downloaded, revision-
pinned copy of the base wd14 model. Before saving anything, every row NOT named by the
allowlist must be byte-identical to the base model (tagger/registry_freeze.py) — this is
what lets "N tags fixed" mean exactly that, instead of "a different, unverified model".

    python3 build_release.py [--output-dir DIR] [--pin-revision SHA] [--venv-python PATH]

A build that fails ANY check prints why to stderr and exits 1 without writing anything —
publish_release.py (Step 4) only ever sees a release.json that already passed this gate.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
PATCHES_DIR = REPO_ROOT / "tagger" / "patches"
MANIFEST_PATH = PATCHES_DIR / "MANIFEST.json"
# Loop-closure retirement ledger (plan:kura-tagger-self-improvement-loop): gates
# whose veto probe the base has absorbed. Skipped when bundling so the release
# stops carrying a redundant post-pass — the tag keeps being cancelled via its
# patched row (reap_domain = patched ∪ veto). This is how the loop closes.
VETO_RETIRED_PATH = REPO_ROOT / "tagger" / "veto_retired.json"


def _load_retired_gates(path: Path = VETO_RETIRED_PATH) -> set[str]:
    """Gate names retired from the veto post-pass (git-tracked decision)."""
    if not path.exists():
        return set()
    try:
        data = json.loads(path.read_text())
        return set(data.get("retired") or [])
    except Exception as e:  # noqa: BLE001
        print(f"[build_release] WARN: could not read {path.name}: {e}", file=sys.stderr)
        return set()

sys.path.insert(0, str(REPO_ROOT))
from tagger import registry_freeze as rf  # noqa: E402


class BuildError(Exception):
    """A build precondition failed; nothing has been written to disk yet."""


def _read_manifest(manifest_path: Path = MANIFEST_PATH) -> list[dict]:
    manifest = json.loads(manifest_path.read_text())
    return manifest.get("approved", [])


def _load_patch(tag: str, version: int, expected_sha256: str, patches_dir: Path = PATCHES_DIR):
    """Read patches/<tag>/v<version>.{json,safetensors} — exactly the file the
    MANIFEST entry names, never a glob over the tag's directory (a later,
    unreviewed, or rejected version must not be picked up by accident)."""
    json_path = patches_dir / tag / f"v{version}.json"
    st_path = patches_dir / tag / f"v{version}.safetensors"
    if not json_path.is_file() or not st_path.is_file():
        raise BuildError(f"MANIFEST entry {tag} v{version} names a missing patch file")
    meta = json.loads(json_path.read_text())
    if meta.get("status") != "approved":
        raise BuildError(
            f"{tag} v{version}: patch metadata status={meta.get('status')!r}, but "
            "MANIFEST allowlisted it -- refusing (status must be 'approved')")
    actual_sha256 = hashlib.sha256(st_path.read_bytes()).hexdigest()
    if actual_sha256 != expected_sha256:
        raise BuildError(
            f"{tag} v{version}: patch_sha256 mismatch (MANIFEST={expected_sha256}, "
            f"file={actual_sha256}) -- patch file was modified after approval")
    if meta.get("sha256") != expected_sha256:
        raise BuildError(f"{tag} v{version}: patch json's own sha256 field disagrees "
                          "with its sibling safetensors file")

    from safetensors.torch import load_file as load_safetensors  # noqa: PLC0415
    tensors = load_safetensors(str(st_path))
    # Two raw upstream patch schemas exist (weight_row/bias vs w/b); in-repo
    # .safetensors are already normalized to w/b, but read both defensively so
    # a future patch committed straight from the older schema doesn't silently
    # fail to load.
    if "w" in tensors:
        weight = tensors["w"]
    elif "weight_row" in tensors:
        weight = tensors["weight_row"]
    else:
        raise BuildError(f"{tag} v{version}: safetensors has neither 'w' nor 'weight_row'")
    if "b" in tensors:
        bias = tensors["b"]
    elif "bias" in tensors:
        bias = tensors["bias"]
    else:
        raise BuildError(f"{tag} v{version}: safetensors has neither 'b' nor 'bias'")
    weight_list = weight.reshape(-1).tolist()
    bias_val = float(bias.reshape(-1)[0])
    return meta, weight_list, bias_val


def _resolve_base(metas: list[dict]) -> tuple[str, str | None]:
    repos = {m["base_repo"] for m in metas}
    if len(repos) != 1:
        raise BuildError(f"MANIFEST entries reference multiple base repos: {sorted(repos)}")
    revisions = {m["base_revision"] for m in metas if m.get("base_revision")}
    if len(revisions) > 1:
        raise BuildError(f"MANIFEST entries pin conflicting base revisions: {sorted(revisions)}")
    return repos.pop(), (revisions.pop() if revisions else None)


def _read_csv_rows(path: Path) -> list[dict]:
    with open(path, newline="") as f:
        return [
            {"tag_id": int(r["tag_id"]), "name": r["name"],
             "category": int(r["category"]), "count": int(r["count"])}
            for r in csv.DictReader(f)
        ]


def _patch_set_digest(entries: list[dict]) -> str:
    parts = sorted(f"{e['tag']}:{e['version']}:{e['patch_sha256']}" for e in entries)
    return hashlib.sha256("\n".join(parts).encode()).hexdigest()


def _git_commit_sha() -> str:
    return subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=REPO_ROOT, text=True).strip()


def merge(base_rows, stock_weight, stock_bias, loaded):
    """Pure(ish) merge core — torch tensors in, torch tensors + bookkeeping out.
    No network/filesystem access, so this half is unit-testable with tiny
    synthetic tensors. `loaded` = [(manifest_entry, patch_meta, weight_list, bias), ...].
    """
    import torch  # noqa: PLC0415

    n_stock = len(base_rows)
    max_idx = max(meta["logit_index"] for _, meta, _, _ in loaded)
    n_out = max(max_idx + 1, n_stock)

    new_weight = torch.zeros(n_out, stock_weight.shape[1], dtype=stock_weight.dtype)
    new_bias = torch.full((n_out,), -6.0, dtype=stock_bias.dtype)  # new rows default "always negative"
    new_weight[:n_stock] = stock_weight
    new_bias[:n_stock] = stock_bias

    merged_rows = [dict(r) for r in base_rows]
    allowed_keys: set[str] = set()
    patched_map: dict[str, int] = {}

    # Sorted by logit_index so multiple appended ("add") rows land contiguously
    # regardless of MANIFEST iteration order.
    for entry, meta, weight, bias in sorted(loaded, key=lambda t: t[1]["logit_index"]):
        idx = meta["logit_index"]
        tag = entry["tag"]
        op = rf.classify_op(idx, n_stock)
        if op == "add":
            if idx != len(merged_rows):
                raise BuildError(
                    f"{tag}: logit_index {idx} is not the next free row "
                    f"({len(merged_rows)}) -- appended rows must be contiguous, no gaps")
            category = meta.get("category")
            if type(category) is not int or category not in (0, 4):
                raise BuildError(f"{tag}: appended row requires explicit approved metadata category 0 (general) or 4 (character)")
            merged_rows.append({"tag_id": -1, "name": tag, "category": category, "count": 0})
            key = rf.identity_key(-1, tag)
        else:
            if idx >= n_stock:
                raise BuildError(f"{tag}: logit_index {idx} out of stock range")
            key = rf.identity_key(merged_rows[idx].get("tag_id"), merged_rows[idx]["name"])

        if key in allowed_keys:
            raise BuildError(
                f"two patches target the same tag identity {key!r} "
                "(logit_index collision) -- refusing to merge")
        allowed_keys.add(key)
        patched_map[tag] = idx
        new_weight[idx] = torch.tensor(weight, dtype=new_weight.dtype)
        new_bias[idx] = bias

    return new_weight, new_bias, merged_rows, allowed_keys, patched_map, n_stock, n_out


def _vectors(rows: list[dict], weight, bias) -> dict:
    return {
        rf.identity_key(r.get("tag_id"), r["name"]):
            (tuple(weight[i].tolist()), float(bias[i].item()))
        for i, r in enumerate(rows)
    }


def build_and_save(output_dir: Path, pin_revision: str | None = None,
                    venv_python: str | None = None) -> dict:
    import timm  # noqa: PLC0415
    import torch  # noqa: PLC0415
    import torch.nn as nn  # noqa: PLC0415
    from huggingface_hub import HfApi, hf_hub_download  # noqa: PLC0415

    entries = _read_manifest()
    if not entries:
        raise BuildError("MANIFEST.json has no approved entries -- nothing to build")
    loaded = [(entry, *_load_patch(entry["tag"], entry["version"], entry["patch_sha256"]))
              for entry in entries]
    metas = [meta for _, meta, _, _ in loaded]
    base_repo, pinned = _resolve_base(metas)
    resolved_revision = pin_revision or pinned or HfApi().model_info(base_repo).sha
    if not resolved_revision:
        raise BuildError(f"could not resolve a pinned revision for {base_repo}")

    # timm's "hf-hub:repo@revision" syntax pins the exact snapshot (hf_split in
    # timm/models/_hub.py) -- passing a local directory path here does not work,
    # timm always re-validates the hub id.
    tags_csv = hf_hub_download(base_repo, "selected_tags.csv", revision=resolved_revision)
    base_rows = _read_csv_rows(Path(tags_csv))
    model = timm.create_model(f"hf-hub:{base_repo}@{resolved_revision}", pretrained=True)
    stock_weight = model.head.fc.weight.detach().clone()
    stock_bias = model.head.fc.bias.detach().clone()
    if stock_weight.shape[0] != len(base_rows):
        raise BuildError(
            f"selected_tags.csv has {len(base_rows)} rows but head.fc has "
            f"{stock_weight.shape[0]} rows -- base model/vocab mismatch, refusing to guess")

    new_weight, new_bias, merged_rows, allowed_keys, patched_map, n_stock, n_out = merge(
        base_rows, stock_weight, stock_bias, loaded)

    base_vectors = _vectors(base_rows, stock_weight, stock_bias)
    merged_vectors = _vectors(merged_rows, new_weight, new_bias)
    # Raises FreezeViolation on any violation; nothing below this line runs
    # unless the merge is provably "base model + only the allowlisted rows".
    rf.validate_merge(base_rows, merged_rows, base_vectors, merged_vectors, allowed_keys)

    fc = nn.Linear(new_weight.shape[1], n_out)
    with torch.no_grad():
        fc.weight.copy_(new_weight)
        fc.bias.copy_(new_bias)
    model.head.fc = fc

    # Bundle gated-veto heads (plan:kura-tagger-self-improvement-loop) so they
    # ride the release to EVERY node (release_client distribution). Without this
    # an primary-only file-based veto is fleet-ineffective: other workers re-tag the
    # tag back. Additive — the freeze check (every non-patched base row byte-
    # identical) is unaffected; veto_heads is a separate dict, not a model row.
    import os as _os  # noqa: PLC0415
    from tagger.veto_apply import load_veto_dir  # noqa: PLC0415
    _veto_dir = Path(_os.environ.get("KURA_MODELS_DIR")
                     or (Path.home() / ".cache" / "kura-tagger" / "models")) / "veto-heads"
    veto_heads = load_veto_dir(_veto_dir)
    _retired = _load_retired_gates()
    if _retired:
        _skipped = sorted(g for g in veto_heads if g in _retired)
        veto_heads = {g: v for g, v in veto_heads.items() if g not in _retired}
        if _skipped:
            print(f"[build_release] skipping RETIRED veto heads (base absorbed): {_skipped} "
                  f"— tag stays cancelled via its patched row", file=sys.stderr)
    if veto_heads:
        print(f"[build_release] bundling veto heads: {sorted(veto_heads)}", file=sys.stderr)

    output_dir.mkdir(parents=True, exist_ok=True)
    model_path = output_dir / "wd14-fixed.pt"
    torch.save({
        "state_dict": model.state_dict(), "repo": base_repo, "revision": resolved_revision,
        "n_out": n_out, "n_stock": n_stock, "patched": patched_map,
        "img_size": model.pretrained_cfg["input_size"][-1],
        "veto_heads": veto_heads,
    }, model_path)

    csv_path = output_dir / "selected_tags.csv"
    with open(csv_path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["tag_id", "name", "category", "count"])
        w.writeheader()
        w.writerows(merged_rows)

    model_sha256 = hashlib.sha256(model_path.read_bytes()).hexdigest()
    release = {
        "commit_sha": _git_commit_sha(),
        "base_model_repo": base_repo,
        "base_model_revision": resolved_revision,
        "patch_set_digest": _patch_set_digest(entries),
        "model_sha256": model_sha256,
        "n_stock": n_stock,
        "n_out": n_out,
        "patched": patched_map,
        "model_path": str(model_path),
        "selected_tags_csv_path": str(csv_path),
        "lockfile": (subprocess.check_output([venv_python, "-m", "pip", "freeze"], text=True)
                     if venv_python else None),
    }
    (output_dir / "release.json").write_text(json.dumps(release, indent=2, sort_keys=True))
    return release


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                  formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--output-dir", default=str(REPO_ROOT / "tagger" / "build"))
    ap.add_argument("--pin-revision", default=None,
                     help="override HF revision (else: the patches' agreed pin, "
                          "else: resolve the repo's current HEAD)")
    ap.add_argument("--venv-python", default=None,
                     help="python of the build venv, captured as the release lockfile")
    args = ap.parse_args()
    try:
        release = build_and_save(Path(args.output_dir), pin_revision=args.pin_revision,
                                  venv_python=args.venv_python)
    except (BuildError, rf.FreezeViolation) as e:
        print(f"[build_release] REFUSED: {e}", file=sys.stderr)
        return 1
    print(json.dumps({k: v for k, v in release.items() if k != "lockfile"},
                      indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
