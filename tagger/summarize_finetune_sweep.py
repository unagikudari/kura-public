#!/usr/bin/env python3
# Description: tabulate the drift-vs-GT-gain curves from a train_base_finetune.py sweep.
# Kind: tool
# Effects: read-only (reads finetune_result-*.json)
# Runs-on: anywhere
"""Read every finetune_result-*.json in a directory and print, per arm, the
conf_jaccard@0.35 trajectory against the GT micro-F1 it bought.

The decision this feeds is a TRADE, so it has to be shown as one. A preservation table
alone answers "did it break the base"; a GT table alone answers "did it learn the
corrections"; only the pair answers the actual question, which is whether the second was
worth the first. The gate column is conf_jaccard@0.35 -- NOT topk50_jaccard, which on a
10.8k vocab is dominated by rank churn among near-tied tail tags at p~1e-3 and moves for
reasons no user can see.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

GATE = 0.98


def main() -> int:
    d = Path(sys.argv[1] if len(sys.argv) > 1 else ".")
    files = sorted(d.glob("finetune_result-*.json"))
    if not files:
        print(f"no finetune_result-*.json under {d}")
        return 1
    rows = []
    for f in files:
        try:
            r = json.loads(f.read_text())
        except Exception as e:  # noqa: BLE001
            print(f"[skip] {f.name}: {e}")
            continue
        arm = f.stem.replace("finetune_result-", "")
        tr, lo = r.get("train", {}), r.get("lora", {})
        curve = r.get("curve") or []
        rows.append((arm, tr, lo, curve, r))

    print(f"{'arm':<18s} {'r':>2s} {'lr':>7s} {'kd':>5s} {'anc':>4s} {'step':>5s} "
          f"{'conf_jac':>9s} {'topk50':>7s} {'abs_dp':>9s} {'flips':>6s} "
          f"{'GT_F1':>7s} {'dGT_F1':>7s} {'gate':>5s}")
    for arm, tr, lo, curve, r in rows:
        if not curve:
            print(f"{arm:<18s} (no curve -- pre-phase-3 result json)")
            continue
        for c in curve:
            mi = c.get("gt_micro") or {}
            gate_ok = (c.get("conf_jaccard") or 0) >= GATE
            print(f"{arm:<18s} {lo.get('r', '?'):>2} {tr.get('lr', 0):>7.1e} "
                  f"{tr.get('kd_mode', '?'):>5s} {tr.get('anchor_weight', 0):>4g} "
                  f"{c['step']:>5d} {c.get('conf_jaccard') or 0:>9.4f} "
                  f"{c.get('topk50_jaccard') or 0:>7.4f} {c.get('mean_abs_dp') or 0:>9.6f} "
                  f"{c.get('flips_per_image') or 0:>6.2f} "
                  f"{mi.get('F1', float('nan')):>7.4f} "
                  f"{c.get('gt_micro_delta_f1', float('nan')):>+7.4f} "
                  f"{'ok' if gate_ok else 'FAIL':>5s}")
        print()

    print("=== verdict per arm (final gate point) ===")
    print(f"{'arm':<18s} {'conf_jac_final':>14s} {'min_conf_jac':>13s} {'dGT_F1':>8s} "
          f"{'holds>=0.98':>12s}")
    for arm, tr, lo, curve, r in rows:
        if not curve:
            continue
        cj = [c.get("conf_jaccard") or 0 for c in curve]
        last = curve[-1]
        holds = all(v >= GATE for v in cj)
        print(f"{arm:<18s} {cj[-1]:>14.4f} {min(cj):>13.4f} "
              f"{last.get('gt_micro_delta_f1', float('nan')):>+8.4f} "
              f"{'YES' if holds else 'no':>12s}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
