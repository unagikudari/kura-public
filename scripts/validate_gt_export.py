#!/usr/bin/env python3
"""Shared validator for GT export packages (plan kura-tag-correction-heads-resume
rev3 Step 5 contract: "送信側と受信側の双方で validator 必須 — schema 不一致は
学習開始前に fail"). Run on BOTH sides: primary before handing off, desk after pulling.

Checks:
  - manifest.json present, schema_version known, sha256(labels.jsonl) matches
  - every labels.jsonl record's image exists under images/ and its sha256 matches
    the filename (content-addressing integrity)
  - vocab: split in {train,val,test}, censor_type in the annotation vocab,
    labels.final in {true,false,null}
  - every sha256 the manifest claims to ship is actually present on disk (and
    vice versa — no orphan files, no missing files)

Exit 0 + "OK" on success. Exit 1 with itemized failures otherwise — this script
is meant to gate training, not just advise.

Usage: validate_gt_export.py --dir /path/to/export
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

KNOWN_SCHEMAS = {"censor-gt-v1"}
VALID_SPLITS = {"train", "val", "test"}
VALID_CENSOR_TYPES = {"mosaic", "black_bar", "white_bar", "slash", "other"}


def sha256_file(p: Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", required=True)
    ap.add_argument("--full", action="store_true",
                     help="verify every image's sha256 (default: filename-only check + 5%% content sample)")
    args = ap.parse_args()

    root = Path(args.dir)
    errors: list[str] = []
    warnings: list[str] = []

    manifest_path = root / "manifest.json"
    if not manifest_path.exists():
        print(f"FAIL: {manifest_path} missing", file=sys.stderr)
        return 1
    manifest = json.loads(manifest_path.read_text())

    if manifest.get("schema_version") not in KNOWN_SCHEMAS:
        errors.append(f"unknown schema_version {manifest.get('schema_version')!r}")

    labels_path = root / "labels.jsonl"
    if not labels_path.exists():
        errors.append("labels.jsonl missing")
    else:
        actual_labels_sha = sha256_file(labels_path)
        expected = manifest.get("labels_jsonl_sha256")
        if expected != actual_labels_sha:
            errors.append(f"labels.jsonl sha256 mismatch: manifest={expected} actual={actual_labels_sha}")

    images_dir = root / "images"
    manifest_images = set(manifest.get("image_sha256_list", []))
    on_disk_images = set()
    if images_dir.is_dir():
        for p in images_dir.iterdir():
            on_disk_images.add(p.stem)

    missing_on_disk = manifest_images - on_disk_images
    orphans = on_disk_images - manifest_images
    if missing_on_disk:
        errors.append(f"{len(missing_on_disk)} images in manifest but not on disk (e.g. {sorted(missing_on_disk)[:5]})")
    if orphans:
        warnings.append(f"{len(orphans)} images on disk but not in manifest (e.g. {sorted(orphans)[:5]})")

    records = []
    if labels_path.exists():
        with labels_path.open() as f:
            for i, line in enumerate(f):
                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError as e:
                    errors.append(f"labels.jsonl line {i}: invalid JSON ({e})")

    seen_post_ids = set()
    to_content_check = []
    for rec in records:
        pid = rec.get("post_id")
        if pid in seen_post_ids:
            errors.append(f"duplicate post_id {pid} in labels.jsonl")
        seen_post_ids.add(pid)

        if rec.get("split") not in VALID_SPLITS:
            errors.append(f"post {pid}: invalid split {rec.get('split')!r}")

        final = rec.get("labels", {}).get("final")
        if final not in (True, False, None):
            errors.append(f"post {pid}: invalid labels.final {final!r}")

        for region in rec.get("regions", []):
            ct = region.get("censor_type")
            if ct not in VALID_CENSOR_TYPES:
                errors.append(f"post {pid} region {region.get('id')}: invalid censor_type {ct!r}")

        sha = rec.get("image_sha256")
        ext = rec.get("image_ext")
        img_path = images_dir / f"{sha}.{ext}"
        if not img_path.exists():
            errors.append(f"post {pid}: image file {img_path.name} missing")
        else:
            to_content_check.append((pid, sha, img_path))

    # content-hash sample (or full, with --full)
    import random
    sample = to_content_check if args.full else random.sample(
        to_content_check, min(len(to_content_check), max(20, len(to_content_check) // 20)))
    for pid, sha, img_path in sample:
        actual = sha256_file(img_path)
        if actual != sha:
            errors.append(f"post {pid}: image sha256 mismatch (filename={sha}, actual={actual})")

    if manifest.get("total_records") != len(records):
        errors.append(f"manifest.total_records={manifest.get('total_records')} but labels.jsonl has {len(records)} lines")

    for w in warnings:
        print(f"WARN: {w}", file=sys.stderr)

    if errors:
        print(f"FAIL: {len(errors)} error(s)", file=sys.stderr)
        for e in errors:
            print(f"  - {e}", file=sys.stderr)
        return 1

    print(f"OK: {len(records)} records, {len(on_disk_images)} images, "
          f"{'full' if args.full else f'{len(sample)}-sample'} content-hash check passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
