#!/usr/bin/env python3
# Description: is the reap-domain recall gap in the FEATURES or in the decision boundary?
# Kind: tool
# Effects: read-only on the kura DB; writes one result json under --outdir
# Runs-on: trainer (only CUDA node)
"""Answer one question the fine-tune sweep cannot: for a tag the fleet under-detects
(smegma R=0.03, phimosis R=0.00 at the fleet threshold), is the signal ABSENT from the
frozen backbone features -- so only a base change could ever recover it -- or is it
present and merely mis-thresholded by the release's linear row, which a logit patch can
fix without touching the backbone at all?

The reap-safety gate authorises a tag for reap only at GT-positive recall >= 0.85. Today
that admits {anilingus, erection} and nothing else. Whether the domain can widen is
therefore not a question about training knobs -- it is a question about which layer the
missing recall lives in. This script measures that directly, per tag, three ways:

  1. RELEASE ROW, as shipped. The row the fleet runs, scored on the group-held-out GT
     split. Its ROC-AUC is threshold-FREE: it says how well this exact linear functional
     of the frozen features ranks positives above negatives, independent of where the
     0.5 cut falls. High AUC + low R@0.5 == a calibration failure, not a capability one.
  2. RELEASE ROW, re-thresholded. Sweep the cut and report the precision the tag would
     pay at recall 0.85. This is exactly what a logit-patch bias edit buys -- the
     cheapest possible intervention, one float per tag.
  3. FRESH LINEAR PROBE on the same frozen pooled features, fit on the train side of the
     identical group split, evaluated on the same val posts. This is the CEILING of the
     logit-patch mechanism: the best any linear row can do on these features. If (3)
     cannot reach 0.85 recall at usable precision, the gap is genuinely in the backbone
     and no patch head -- however retrained -- will widen the reap domain.

(1) vs (2) separates "wrong threshold" from "wrong row". (2) vs (3) separates "wrong row"
from "wrong features". Only the third case is an argument for a base fine-tune.

The split, the group key and the seed mirror train_base_finetune.py exactly, so these
numbers are directly comparable to that script's per-tag table rather than being a
differently-drawn measurement that happens to be about the same tags.

NOTE ON PRECISION: val is NOT subsampled here (unlike the sweep's --max-val-posts, which
keeps every positive but caps negative-only posts and therefore reports optimistic
precision). Recall is unaffected either way; precision here is the honest one, because
the whole point is what a tag would cost at the recall the gate demands.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tagger.train_base_finetune import (  # noqa: E402
    DB,
    FLEET_THRESH,
    decode_batch,
    fetch_images,
    gt_labels,
    live_config,
    load_base,
    make_transform,
    split_holdout,
)

TARGET_RECALL = 0.85  # the reap-safety gate


def roc_auc(y: list[float], s: list[float]) -> float:
    """Rank-based AUC (ties averaged). No sklearn: the venv is shared with ComfyUI and
    this is twenty lines."""
    pairs = sorted(zip(s, y), key=lambda t: t[0])
    n = len(pairs)
    ranks = [0.0] * n
    i = 0
    while i < n:
        j = i
        while j + 1 < n and pairs[j + 1][0] == pairs[i][0]:
            j += 1
        r = (i + j) / 2.0 + 1.0
        for k in range(i, j + 1):
            ranks[k] = r
        i = j + 1
    npos = sum(1 for _, yy in pairs if yy > 0)
    nneg = n - npos
    if not npos or not nneg:
        return float("nan")
    sum_pos = sum(r for r, (_, yy) in zip(ranks, pairs) if yy > 0)
    return (sum_pos - npos * (npos + 1) / 2.0) / (npos * nneg)


def avg_precision(y: list[float], s: list[float]) -> float:
    order = sorted(range(len(s)), key=lambda i: -s[i])
    npos = sum(1 for v in y if v > 0)
    if not npos:
        return float("nan")
    tp = 0
    total = 0.0
    for rank, i in enumerate(order, start=1):
        if y[i] > 0:
            tp += 1
            total += tp / rank
    return total / npos


def sweep(y: list[float], s: list[float], target_recall: float = TARGET_RECALL) -> dict:
    """-> {at_fleet, best_f1, at_target_recall}. Thresholds are the observed scores, so
    the sweep is exact rather than a grid that can straddle the operating point."""
    npos = sum(1 for v in y if v > 0)
    out: dict = {"n_pos": npos, "n_neg": len(y) - npos}
    if not npos:
        return out

    def at(thresh: float) -> dict:
        tp = sum(1 for yy, ss in zip(y, s) if yy > 0 and ss >= thresh)
        fp = sum(1 for yy, ss in zip(y, s) if yy == 0 and ss >= thresh)
        fn = npos - tp
        P = tp / max(1, tp + fp)
        R = tp / max(1, tp + fn)
        return {"thresh": round(thresh, 6), "P": round(P, 4), "R": round(R, 4),
                "F1": round(2 * P * R / max(1e-9, P + R), 4), "tp": tp, "fp": fp, "fn": fn}

    out["at_fleet"] = at(FLEET_THRESH)
    cands = sorted(set(s), reverse=True)
    scored = [at(t) for t in cands]
    out["best_f1"] = max(scored, key=lambda d: d["F1"]) if scored else None
    # Highest threshold that still reaches the gate's recall -> the best precision
    # obtainable AT that recall. None means the tag cannot reach it at any cut, which is
    # the finding, not a missing value.
    ok = [d for d in scored if d["R"] >= target_recall]
    out["at_target_recall"] = max(ok, key=lambda d: d["thresh"]) if ok else None
    out["max_recall"] = max((d["R"] for d in scored), default=0.0)
    return out


def fit_probe(xtr, ytr, xva, epochs: int, lr: float, seed: int, device: str):
    """Logistic probe on frozen pooled features, pos_weight-balanced. Standardises with
    train-side mu/sd -- the same requirement the deployed veto heads have (trainer
    f7e0d98d: dropping it invalidates the probe)."""
    import torch  # noqa: PLC0415

    torch.manual_seed(seed)
    mu, sd = xtr.mean(0, keepdim=True), xtr.std(0, keepdim=True).clamp_min(1e-6)
    xtr_n, xva_n = ((xtr - mu) / sd).to(device), ((xva - mu) / sd).to(device)
    ytr_d = ytr.to(device)
    npos = float(ytr.sum().item())
    nneg = float(len(ytr) - npos)
    pw = torch.tensor([nneg / max(1.0, npos)], device=device)
    lin = torch.nn.Linear(xtr.shape[1], 1).to(device)
    opt = torch.optim.AdamW(lin.parameters(), lr=lr, weight_decay=1e-4)
    lossf = torch.nn.BCEWithLogitsLoss(pos_weight=pw)
    for _ in range(epochs):
        opt.zero_grad()
        loss = lossf(lin(xtr_n).squeeze(1), ytr_d)
        loss.backward()
        opt.step()
    with torch.no_grad():
        return torch.sigmoid(lin(xva_n).squeeze(1)).float().cpu(), float(loss.item())


def main() -> int:
    import torch  # noqa: PLC0415

    ap = argparse.ArgumentParser()
    ap.add_argument("--base", choices=("release", "stock"), default="release")
    ap.add_argument("--extra-tags", nargs="*", default=None)
    ap.add_argument("--tags", nargs="*", default=None)
    ap.add_argument("--min-positive", type=int, default=None)
    ap.add_argument("--blob-cache", default=None)
    ap.add_argument("--batch-size", type=int, default=8)
    ap.add_argument("--probe-epochs", type=int, default=400)
    ap.add_argument("--probe-lr", type=float, default=0.05)
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--max-val-posts", type=int, default=0,
                    help="0 = full val split (honest precision). Non-zero subsamples "
                         "negative-only posts and makes precision optimistic.")
    ap.add_argument("--outdir",
                    default=str(Path.home() / "tagger-heads" / "finetune"))
    ap.add_argument("--run-name", default="reap-recall-probe")
    args = ap.parse_args()

    device = "cuda" if torch.cuda.is_available() else "cpu"
    db = DB()
    conf = live_config(db)
    min_pos = args.min_positive if args.min_positive is not None else int(
        conf.get("min_gt_positive", 20))
    extra = set(args.extra_tags or [])
    labels, groups, tag_counts = gt_labels(db, min_pos, args.tags, extra)
    print(f"[gt] tags={len(tag_counts)} labels={len(labels)}", flush=True)

    model, tag_names, base_meta = load_base(db, args.base, None)
    name_to_col = {n: i for i, n in enumerate(tag_names)}
    labels = [(p, t, y) for p, t, y in labels if t in name_to_col]
    tag_index = {t: name_to_col[t] for t in sorted({t for _, t, _ in labels})}

    label_map: dict[str, list] = {}
    per_post: dict[int, dict[str, float]] = {}
    for p, t, y in labels:
        label_map.setdefault(t, []).append((p, y))
        per_post.setdefault(p, {})[t] = y
    any_pos = {p: any(v > 0 for v in d.values()) for p, d in per_post.items()}
    gt_post_ids = sorted(per_post)
    train_ids, val_ids = split_holdout(gt_post_ids, groups, any_pos)
    if args.max_val_posts and len(val_ids) > args.max_val_posts:
        import random  # noqa: PLC0415
        vp = [p for p in val_ids if any_pos.get(p)]
        vn = [p for p in val_ids if not any_pos.get(p)]
        room = max(0, args.max_val_posts - len(vp))
        keep = set(vp) | set(random.Random(args.seed + 7).sample(vn, min(room, len(vn))))
        val_ids = [p for p in val_ids if p in keep]
        print(f"[split] val SUBSAMPLED to {len(val_ids)} -- PRECISION IS OPTIMISTIC", flush=True)
    print(f"[split] train_posts={len(train_ids)} val_posts={len(val_ids)}", flush=True)

    need = sorted(set(train_ids) | set(val_ids))
    cache = Path(args.blob_cache) if args.blob_cache else None
    print(f"[data] fetching {len(need)} blobs" + (f" (cache {cache})" if cache else ""),
          flush=True)
    images = fetch_images(db, need, cache)
    print(f"[data] blobs={len(images)}", flush=True)

    tf = make_transform()
    model = model.eval().to(device)

    def encode(ids: list[int]):
        """One backbone pass -> (pooled features, release probabilities). Both come off
        the same forward, so the release row's scores and the probe's inputs are
        guaranteed to be the same feature space -- the mismatch that cost the last
        round 0.2 recall (tagger-train-infer-featspace-mismatch)."""
        feats, probs, kept_ids = [], [], []
        with torch.no_grad():
            for i in range(0, len(ids), args.batch_size):
                x, kept = decode_batch(images, ids[i:i + args.batch_size], tf)
                if x is None:
                    continue
                x = x.to(device)
                f = model.forward_features(x)
                pooled = model.forward_head(f, pre_logits=True)
                logits = model.head.fc(pooled)
                feats.append(pooled.float().cpu())
                probs.append(torch.sigmoid(logits.float()).cpu())
                kept_ids.extend(kept)
                if len(kept_ids) % 500 < args.batch_size:
                    print(f"  encoded {len(kept_ids)}/{len(ids)}", flush=True)
        return torch.cat(feats), torch.cat(probs), kept_ids

    print("[encode] val...", flush=True)
    fva, pva, va_ids = encode(val_ids)
    print("[encode] train...", flush=True)
    ftr, _ptr, tr_ids = encode(train_ids)
    va_pos = {pid: i for i, pid in enumerate(va_ids)}
    tr_pos = {pid: i for i, pid in enumerate(tr_ids)}

    results = {}
    for tag, col in sorted(tag_index.items()):
        va_lab = [(pid, y) for pid, y in label_map[tag] if pid in va_pos]
        tr_lab = [(pid, y) for pid, y in label_map[tag] if pid in tr_pos]
        y_va = [y for _, y in va_lab]
        if not any(y > 0 for y in y_va):
            results[tag] = {"skipped": "no val positives"}
            continue
        s_rel = [float(pva[va_pos[pid]][col]) for pid, _ in va_lab]
        rel = {"auc": round(roc_auc(y_va, s_rel), 4),
               "ap": round(avg_precision(y_va, s_rel), 4), **sweep(y_va, s_rel)}

        probe = None
        if any(y > 0 for _, y in tr_lab) and len(tr_lab) >= 20:
            xtr = ftr[[tr_pos[pid] for pid, _ in tr_lab]]
            ytr = torch.tensor([y for _, y in tr_lab], dtype=torch.float32)
            xva = fva[[va_pos[pid] for pid, _ in va_lab]]
            s_pr_t, final_loss = fit_probe(xtr, ytr, xva, args.probe_epochs,
                                           args.probe_lr, args.seed, device)
            s_pr = [float(v) for v in s_pr_t]
            probe = {"auc": round(roc_auc(y_va, s_pr), 4),
                     "ap": round(avg_precision(y_va, s_pr), 4),
                     "n_train_pos": int(ytr.sum().item()), "n_train": len(tr_lab),
                     "final_loss": round(final_loss, 5), **sweep(y_va, s_pr)}
        results[tag] = {"col": col, "patched": tag in (base_meta.get("patched") or {}),
                        "release_row": rel, "fresh_probe": probe}

    # ── the table the decision is actually read off
    print(f"\n=== reap-recall probe (val_posts={len(va_ids)}, thr_fleet={FLEET_THRESH}, "
          f"gate R>={TARGET_RECALL}) ===", flush=True)
    print(f"{'tag':<18}{'pch':>4}{'npos':>6} | {'relAUC':>7}{'relR@.5':>8}{'relP@.85R':>10}"
          f" | {'prbAUC':>7}{'prbR_max':>9}{'prbP@.85R':>10}  verdict", flush=True)
    for tag, r in results.items():
        if "skipped" in r:
            print(f"{tag:<18}{'':>4}{'':>6} | (skipped: {r['skipped']})", flush=True)
            continue
        rel, pr = r["release_row"], r["fresh_probe"]
        rel_t = rel.get("at_target_recall")
        pr_t = (pr or {}).get("at_target_recall")
        # features-limited: even a freshly fit linear row on these features cannot reach
        # the gate. calibration-limited: the release row CAN, at a different cut.
        if pr is None:
            verdict = "no train positives"
        elif rel_t:
            verdict = "CALIBRATION (release row reaches 0.85 by re-thresholding)"
        elif pr_t:
            verdict = "ROW (refit linear row reaches 0.85 -- logit-patch can)"
        else:
            verdict = "FEATURES (no linear row on frozen feats reaches 0.85)"
        print(f"{tag:<18}{'Y' if r['patched'] else '-':>4}{rel['n_pos']:>6} | "
              f"{rel['auc']:>7.3f}{rel['at_fleet']['R']:>8.3f}"
              f"{(rel_t['P'] if rel_t else float('nan')):>10.3f} | "
              f"{(pr['auc'] if pr else float('nan')):>7.3f}"
              f"{(pr['max_recall'] if pr else float('nan')):>9.3f}"
              f"{(pr_t['P'] if pr_t else float('nan')):>10.3f}  {verdict}", flush=True)

    out = {"script": "probe_reap_recall.py", "base": base_meta, "target_recall": TARGET_RECALL,
           "fleet_thresh": FLEET_THRESH, "val_posts": len(va_ids), "train_posts": len(tr_ids),
           "val_subsampled": bool(args.max_val_posts), "config_live": conf,
           "probe": {"epochs": args.probe_epochs, "lr": args.probe_lr, "seed": args.seed},
           "per_tag": results}
    p = Path(args.outdir) / f"reap_recall_probe-{args.run_name}.json"
    p.write_text(json.dumps(out, indent=2))
    print(f"\n[save] {p}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
