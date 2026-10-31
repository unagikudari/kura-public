#!/usr/bin/env python3
"""Build manifest.json for a GT export dir already populated by export_censor_gt.py
(labels.jsonl + images/ + export_summary.json). Content-addressed: lists every
image sha256 shipped, and the sha256 of labels.jsonl itself, so a transfer
corruption or partial copy is caught by validate_gt_export.py before training.

Usage: build_gt_manifest.py --dir /path/to/export --schema-version censor-gt-v1
"""
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
from pathlib import Path


def sha256_file(p: Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def git_commit(repo_root: Path) -> str | None:
    try:
        out = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo_root,
                              capture_output=True, text=True, check=True)
        return out.stdout.strip()
    except Exception:
        return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", required=True)
    ap.add_argument("--schema-version", required=True)
    args = ap.parse_args()

    root = Path(args.dir)
    labels_path = root / "labels.jsonl"
    images_dir = root / "images"
    summary_path = root / "export_summary.json"

    records = [json.loads(l) for l in labels_path.read_text().splitlines() if l.strip()]
    image_shas = sorted(p.stem for p in images_dir.iterdir())

    summary = json.loads(summary_path.read_text()) if summary_path.exists() else {}

    manifest = {
        "schema_version": args.schema_version,
        "kura_commit": git_commit(Path(__file__).resolve().parent.parent),
        "total_records": len(records),
        "image_count": len(image_shas),
        "labels_jsonl_sha256": sha256_file(labels_path),
        "image_sha256_list": image_shas,
        "split_seed": summary.get("seed"),
        "split_counts": summary.get("split_counts"),
        "final_label_counts": summary.get("final_label_counts"),
        "censor_type_counts": summary.get("censor_type_counts"),
        "dropped": summary.get("dropped"),
        "label_conflicts": summary.get("label_conflicts"),
        "pairs": summary.get("pairs"),
        "annotated": summary.get("annotated"),
        "pool_only": summary.get("pool_only"),
        "vocab": {
            "censor_type": ["mosaic", "black_bar", "white_bar", "slash", "other"],
            "split": ["train", "val", "test"],
        },
    }
    (root / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2))
    print(json.dumps({k: v for k, v in manifest.items() if k != "image_sha256_list"},
                      ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
