#!/usr/bin/env python3
# Description: per-tag regression gate for a base-finetune candidate checkpoint vs the live release, true OOF, noise-safe.
# Kind: tool
# Effects: read-only on the kura DB; writes a result json under $OUTDIR
# Runs-on: trainer (only CUDA node) -- CPU works but is slow for the full vocab
"""regression_check.py -- did this base-finetune checkpoint quietly break another tag?

Replaces an ad-hoc, uncommitted script written for the 2026-07-22/25 anilingus Phase 0
experiment. That script's "no-go" verdict (base fine-tuning rejected fleet-wide) turned
out to rest on two bugs, found 2026-08-09 while re-reading its own recovered output
(phase0_regression_ft-l3-full.json, never actually lost -- just never copied off
trainer):

  1. IN-SAMPLE EVAL. The target tag's before/after P/R were measured against the same
     GT the candidate trained on (the file's own note says so), not a held-out split.
     A checkpoint can look regressed on its OWN target tag purely from the eval being
     rigged to look worse than deployment would.
  2. THIN-N NOISE. flaccid (n_pos=15) and foreskin (n_pos=62) were flagged FAIL on
     swings of a couple of tp/fp counts -- exactly the failure shape tagger/health.py
     already had to fix for the *serving*-regression check (regression_min_gt_labels
     floor, migration 0098) after half-erect false-paged for 315h+ off 17 GT labels.
     This script had never been given that fix because it never existed as reusable
     code to fix.

Both fixes are general (any tag, any candidate checkpoint), not specific to
licking_testicle or anilingus:

  - Every tag, INCLUDING the candidate's own fine-tune targets, is scored on val_ids
    from split_holdout() -- the exact function train_base_finetune.py itself uses to
    carve off the held-out fold, called here with the CURRENT live GT (gt_labels()),
    not reconstructed from the checkpoint (checkpoints don't record their split; the
    live GT set is the honest current source of truth for what "held out" means today).
  - A tag's P/R delta only counts toward the PASS/FAIL verdict when its val-fold label
    count clears tagger_config.regression_min_gt_labels (the SAME floor health.py
    already uses -- not a second, slightly-different magic number) AND the drop is a
    two-proportion z-test significant regression at tagger_config.regression_confidence_z
    (migration 0144), not just a negative point delta. Below the floor, or not
    significant, the tag is reported with reliable=false / significant=false and can
    never block the run on its own -- visible, never silent, never blocking on noise.

Usage:
  python3 regression_check.py --candidate /path/to/wd14-finetuned-*.pt [--tags t1 t2 ...]

With no --tags, every tag carrying ANY GT label is scored (matches the old script's
"tier1 live-head / tier2 gt-bearing" coverage in one pass -- the tier split was a
reporting convenience, not a different evaluation).
"""
from __future__ import annotations

import argparse
import io
import json
import math
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from train_base_finetune import (  # noqa: E402
    DB,
    FLEET_THRESH,
    eval_gt,
    eval_preservation,
    fetch_images,
    gt_labels,
    live_config,
    load_base,
    make_transform,
    split_holdout,
)


def wilson_lower_bound(k: int, n: int, z: float) -> float:
    """Wilson score interval lower bound for a binomial proportion k/n.

    Used only to decide "is this reliable at all", never for the significance
    call itself (that's the two-proportion z-test below) -- kept here as the
    building block for --min-reliable-half-width reporting.
    """
    if n <= 0:
        return 0.0
    p = k / n
    denom = 1 + z * z / n
    centre = p + z * z / (2 * n)
    adj = z * math.sqrt((p * (1 - p) + z * z / (4 * n)) / n)
    return max(0.0, (centre - adj) / denom)


def two_proportion_z(k1: int, n1: int, k2: int, n2: int) -> float | None:
    """z-statistic for (p2 - p1), pooled-variance two-proportion test.

    None when either sample is empty (n1==0 or n2==0 -- no comparison possible)
    or the pooled variance is exactly zero (both proportions identical AND at
    an extreme, e.g. both 0/0-safe 0.0 -- no evidence of a difference either way).
    Negative z with p2 < p1 means "candidate significantly lower than teacher".
    """
    if n1 <= 0 or n2 <= 0:
        return None
    p1, p2 = k1 / n1, k2 / n2
    p_pool = (k1 + k2) / (n1 + n2)
    var = p_pool * (1 - p_pool) * (1 / n1 + 1 / n2)
    if var <= 0:
        return 0.0 if p1 == p2 else None
    return (p2 - p1) / math.sqrt(var)


def _safe_ratio(k: int, n: int) -> float | None:
    return None if n <= 0 else round(k / n, 4)


def pick_teacher_release_id(explicit_override: int | None, ckpt_base_release_id) -> int | None:
    """Explicit --teacher-release-id always wins; otherwise the checkpoint's own
    recorded base_release_id; otherwise None (caller falls back to 'active release',
    the only option for checkpoints old enough to predate that field)."""
    if explicit_override is not None:
        return explicit_override
    return ckpt_base_release_id


def load_release(db, release_id: int):
    """Same model construction as train_base_finetune.load_base(base='release'), but
    for an EXPLICIT (possibly archived) release_id instead of always the currently
    active one.

    Needed because a candidate checkpoint's honest teacher is the release it was
    actually trained FROM (ckpt['base_release_id']), not whatever is active today --
    the active release's vocab evolves (rows appended/removed) after a checkpoint is
    built. Found live 2026-08-09 on trainer: a checkpoint built on release 6
    (n_out=10863) hard-failed the vocab-mismatch guard against release 14
    (n_out=10862, one column narrower after licking_testicle's own row was removed
    from the vocab following its round7 rejection) -- release>=8 teachers don't even
    HAVE a licking_testicle column to compare against any more, so no amount of
    column-name alignment would fix it; the archived release is the only honest base.
    """
    import csv  # noqa: PLC0415
    import io  # noqa: PLC0415

    import timm  # noqa: PLC0415
    import torch  # noqa: PLC0415

    r = db.rows(
        "select r.id, r.model_data, r.selected_tags_csv, r.base_model_repo, r.base_model_revision "
        "from tagger_release r where r.id = %s", (release_id,))
    if not r:
        raise SystemExit(f"tagger_release id={release_id} not found -- cannot load teacher")
    rid, model_data, csv_data, repo, rev = r[0]
    ckpt = torch.load(io.BytesIO(bytes(model_data)), map_location="cpu", weights_only=False)
    model = timm.create_model(f"hf-hub:{ckpt['repo']}", pretrained=False, num_classes=ckpt["n_out"])
    model.load_state_dict(ckpt["state_dict"])
    names = [row["name"] for row in csv.DictReader(io.StringIO(bytes(csv_data).decode()))]
    meta = {"base": "release", "release_id": rid, "repo": ckpt["repo"],
            "revision": ckpt.get("revision", rev), "n_stock": ckpt.get("n_stock"),
            "n_out": ckpt["n_out"], "patched": dict(ckpt.get("patched", {})),
            "veto_heads": sorted(ckpt.get("veto_heads", {}) or {}),
            "img_size": ckpt.get("img_size", 448)}
    return model, names, meta


def compare_tag(tag: str, teacher_c: dict, cand_c: dict, min_labels: int, z_crit: float) -> dict:
    """teacher_c / cand_c are eval_gt()'s per-tag dicts (tp/fp/fn/tn) for the SAME
    val_ids -- tp+fn (val_pos) and fp+tn (val_neg) are therefore identical GT counts
    on both sides; only the predictions (tp/fp split) differ between models."""
    val_pos = teacher_c["tp"] + teacher_c["fn"]
    val_neg = teacher_c["fp"] + teacher_c["tn"]
    n_val = val_pos + val_neg
    reliable = n_val >= min_labels

    t_pred_pos = teacher_c["tp"] + teacher_c["fp"]
    c_pred_pos = cand_c["tp"] + cand_c["fp"]
    P_t, P_c = _safe_ratio(teacher_c["tp"], t_pred_pos), _safe_ratio(cand_c["tp"], c_pred_pos)
    R_t, R_c = _safe_ratio(teacher_c["tp"], val_pos), _safe_ratio(cand_c["tp"], val_pos)

    zP = two_proportion_z(teacher_c["tp"], t_pred_pos, cand_c["tp"], c_pred_pos) if (t_pred_pos and c_pred_pos) else None
    zR = two_proportion_z(teacher_c["tp"], val_pos, cand_c["tp"], val_pos) if val_pos else None
    # "significant regression" = candidate PROPORTION is lower AND the drop clears
    # the confidence bar. A significant *rise* is not a regression, whichever metric.
    sig_drop_P = reliable and zP is not None and zP <= -z_crit and (P_c or 0) < (P_t or 0)
    sig_drop_R = reliable and zR is not None and zR <= -z_crit and (R_c or 0) < (R_t or 0)

    verdict = "FAIL" if (sig_drop_P or sig_drop_R) else ("PASS" if reliable else "insufficient_n")
    return {
        "tag": tag, "n_val_labels": n_val, "val_pos": val_pos, "val_neg": val_neg,
        "reliable": reliable,
        "before": {"P": P_t, "R": R_t, **{k: teacher_c[k] for k in ("tp", "fp", "fn", "tn")}},
        "after": {"P": P_c, "R": R_c, **{k: cand_c[k] for k in ("tp", "fp", "fn", "tn")}},
        "dP_pt": None if (P_t is None or P_c is None) else round((P_c - P_t) * 100, 2),
        "dR_pt": None if (R_t is None or R_c is None) else round((R_c - R_t) * 100, 2),
        "z_precision": None if zP is None else round(zP, 3),
        "z_recall": None if zR is None else round(zR, 3),
        "significant_precision_drop": bool(sig_drop_P),
        "significant_recall_drop": bool(sig_drop_R),
        "verdict": verdict,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--candidate", required=True, help="path to a train_base_finetune.py checkpoint (.pt)")
    ap.add_argument("--base", choices=("release", "stock"), default="release", help="teacher baseline")
    ap.add_argument("--pin-revision", default=None)
    ap.add_argument("--teacher-release-id", type=int, default=None,
                    help="load the teacher from this specific (possibly archived) "
                         "tagger_release id instead of the currently active one. "
                         "Default: the candidate checkpoint's own recorded "
                         "base_release_id (an honest retrospective 'did the run that "
                         "made this checkpoint actually regress anything'). Pass the "
                         "CURRENT active release's id explicitly to instead ask 'would "
                         "deploying this today regress the live fleet' -- only "
                         "meaningful if the vocab hasn't diverged since. Ignored when "
                         "--base=stock.")
    ap.add_argument("--tags", nargs="*", default=None,
                    help="restrict scoring to these tags (default: every tag with any GT)")
    ap.add_argument("--blob-cache", default=None)
    ap.add_argument("--pres-val-posts", type=int, default=200,
                    help="held-out-from-everything posts for the whole-vocab drift check "
                         "(tier3 -- unaffected by the in-sample/thin-N bugs, kept as-is)")
    ap.add_argument("--batch-size", type=int, default=8)
    ap.add_argument("--outdir", default=None)
    ap.add_argument("--out", default=None, help="explicit output path (default: <outdir>/regression_result-<candidate stem>.json)")
    args = ap.parse_args()

    import torch  # noqa: PLC0415
    import timm  # noqa: PLC0415

    device = "cuda" if torch.cuda.is_available() else "cpu"
    t0 = time.time()
    db = DB()
    conf = live_config(db)
    min_labels = int(conf.get("regression_min_gt_labels", 10))
    z_crit = float(conf.get("regression_confidence_z", 1.96))
    print(f"[cfg] regression_min_gt_labels={min_labels} regression_confidence_z={z_crit} "
          f"(live tagger_config -- migration 0098/0144, not hardcoded)", flush=True)

    # Load the candidate FIRST -- its own base_release_id decides which teacher is
    # honest, before we go fetch one (see load_release()'s docstring for why "just
    # always use the active release" silently produces either a hard vocab-mismatch
    # SystemExit or, worse, a column that no longer means the same tag).
    ckpt = torch.load(args.candidate, map_location="cpu", weights_only=False)
    teacher_release_id = pick_teacher_release_id(args.teacher_release_id, ckpt.get("base_release_id"))

    if args.base == "stock":
        teacher, tag_names, base_meta = load_base(db, "stock", args.pin_revision)
    elif teacher_release_id is not None:
        teacher, tag_names, base_meta = load_release(db, teacher_release_id)
        print(f"[teacher] release id={teacher_release_id} "
              f"({'explicit --teacher-release-id' if args.teacher_release_id is not None else 'from candidate base_release_id'})",
              flush=True)
    else:
        # No override, no base_release_id on the checkpoint (older checkpoints
        # predate that field) -- fall back to the previous behaviour.
        teacher, tag_names, base_meta = load_base(db, "release", args.pin_revision)
        print("[teacher] active release (candidate has no base_release_id, no override given)",
              flush=True)
    teacher.to(device).eval()
    name_to_col = {n: i for i, n in enumerate(tag_names)}

    if ckpt.get("n_out") != base_meta["n_out"]:
        raise SystemExit(f"vocab mismatch: candidate n_out={ckpt.get('n_out')} "
                         f"teacher n_out={base_meta['n_out']} (release {base_meta.get('release_id')}) "
                         f"-- not comparable. The candidate's base_release_id is "
                         f"{ckpt.get('base_release_id')!r}; pass --teacher-release-id "
                         f"explicitly if that's not what you want compared against.")
    candidate = timm.create_model(f"hf-hub:{ckpt.get('repo', base_meta['repo'])}",
                                  pretrained=False, num_classes=ckpt["n_out"])
    candidate.load_state_dict(ckpt["state_dict"])
    candidate.to(device).eval()
    print(f"[ckpt] {args.candidate} finetuned_tags={ckpt.get('finetuned_tags')}", flush=True)

    # min_positive=0: include EVERY tag with any GT (even 0-positive / thin tags) for
    # visibility -- the reliability floor below decides what can block, not this filter.
    labels, groups, tag_counts = gt_labels(db, 0, args.tags, extra=None)
    if not labels:
        raise SystemExit("no GT labels found for the requested tags")
    trainable_tags = sorted(t for t in tag_counts if t in name_to_col)
    tag_index = {t: name_to_col[t] for t in trainable_tags}
    gt_post_ids = sorted({p for p, _, _ in labels})
    label_map: dict[str, list] = {}
    per_post: dict[int, dict[str, float]] = {}
    for p, t, y in labels:
        if t not in tag_index:
            continue
        label_map.setdefault(t, []).append((p, y))
        per_post.setdefault(p, {})[t] = y
    any_pos = {p: any(v > 0 for v in d.values()) for p, d in per_post.items()}
    # SAME split function the trainer uses, called fresh against CURRENT GT -- true OOF
    # for every tag, including whatever the candidate itself was fine-tuned on.
    _train_ids, val_ids = split_holdout(gt_post_ids, groups, any_pos)
    print(f"[split] {len(tag_index)} tags, val_posts={len(val_ids)} (group-held-out, "
          f"same split_holdout() the trainer uses)", flush=True)

    cache = Path(args.blob_cache) if args.blob_cache else None
    images = fetch_images(db, val_ids, cache=cache)
    tf = make_transform()

    teacher_scores, _ = eval_gt(teacher, val_ids, images, label_map, tag_index, tf, device,
                                args.batch_size, FLEET_THRESH)
    candidate_scores, _ = eval_gt(candidate, val_ids, images, label_map, tag_index, tf, device,
                                  args.batch_size, FLEET_THRESH)

    table = [compare_tag(t, teacher_scores[t], candidate_scores[t], min_labels, z_crit)
             for t in sorted(tag_index)]
    reliable_fails = [r for r in table if r["verdict"] == "FAIL"]
    insufficient = [r["tag"] for r in table if r["verdict"] == "insufficient_n"]
    if insufficient:
        print(f"[reliability] {len(insufficient)} tag(s) below regression_min_gt_labels="
              f"{min_labels}, reported but excluded from the verdict: {insufficient}", flush=True)

    # tier3: whole-vocab drift on posts held out from ALL training (unaffected by the
    # two bugs above -- large N by construction, never the candidate's own GT domain).
    gt_cols_mask = torch.zeros(base_meta["n_out"], dtype=torch.bool)
    for col in tag_index.values():
        gt_cols_mask[col] = True
    from train_base_finetune import kd_post_ids  # noqa: E402
    pres_ids = kd_post_ids(db, args.pres_val_posts, exclude=set(gt_post_ids), seed=0)
    pres_images = fetch_images(db, pres_ids, cache=cache)
    drift = eval_preservation(candidate, teacher, pres_ids, pres_images, gt_cols_mask, tf,
                              device, args.batch_size) if pres_ids else {"error": "no pres posts"}

    result = {
        "candidate": str(args.candidate),
        "teacher_release_id": base_meta.get("release_id"),
        "min_gt_labels_floor": min_labels,
        "confidence_z": z_crit,
        "n_tags_scored": len(table),
        "n_reliable": sum(1 for r in table if r["reliable"]),
        "n_insufficient_n": len(insufficient),
        "n_significant_fails": len(reliable_fails),
        "PASS": len(reliable_fails) == 0,
        "fails": sorted(r["tag"] for r in reliable_fails),
        "insufficient_n_tags": insufficient,
        "table": table,
        "whole_vocab_drift": drift,
        "seconds": round(time.time() - t0, 1),
    }
    outdir = Path(args.outdir) if args.outdir else Path(args.candidate).resolve().parent
    out_path = Path(args.out) if args.out else outdir / f"regression_result-{Path(args.candidate).stem}.json"
    out_path.write_text(json.dumps(result, indent=1))
    verdict_str = "PASS" if result["PASS"] else f"FAIL ({', '.join(result['fails'])})"
    print(f"[verdict] {verdict_str} -- {len(reliable_fails)}/{len(table)} tags significantly "
          f"regressed (of {sum(1 for r in table if r['reliable'])} reliably measurable) "
          f"-> {out_path}", flush=True)
    return 0 if result["PASS"] else 1


if __name__ == "__main__":
    sys.exit(main())
