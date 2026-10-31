#!/usr/bin/env python3
# Description: measure GT-positive recall per reap-domain tag and update the
#              evidence-gated reap authority (data/tagger/reap_authorized.json).
# Kind: evaluator
# Effects: mutating (writes data/tagger/reap_authorized.json — untracked runtime 状態) — READ-ONLY on kura DB
# Runs-on: primary (full pipeline) or any node with the tagger venv + models
"""reap_authorization_eval.py — the loop that keeps reap authority honest.

The veto-authority reap deletes a gate tag the improved tagger no longer
produces. That is only safe where the tagger reliably REPRODUCES true
positives — otherwise its own recall gap (false negatives) gets deleted as if
they were false positives. The 2026-07-22 dry-run proved this concretely:
smegma GT-positive recall was 17%, so a blanket reap would have deleted ~50k
true smegma tags.

So reap authority is EARNED, not hand-picked: for each tag in the reap domain
(patched ∪ veto), run the real tagger over that tag's human-verified
GT-POSITIVE posts and measure recall. A tag is authorized iff recall >=
min_recall over >= min_n labels. This file is the ONLY input to the server-side
reap gate (api/services/tagging.py::_reap_authorized), so a tag cannot be reaped
until it has earned it by measurement.

This closes the loop: as the base improves (fine-tune or logit-patch) and recall
rises, re-running this evaluator automatically GROWS the authorized set — the
reap domain expands by evidence, never by editing a hardcoded list. Run it after
every release that changes the corrected tags.
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import re
import subprocess
import sys
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
# kura#44: この doc は 6h ごとに **この script 自身が** 書き換える runtime 状態。
# tracked な tagger/ に置いていたので production tree が恒常 dirty になり、
# /api/build の dirty:true が常時点灯して「commit hash が唯一の ground truth」を
# 濁していた。runtime 状態は git ではなく data/ (gitignore 済) に置く。
if str(REPO_ROOT) not in sys.path:   # run by path from kura-reap-authorization.service
    sys.path.insert(0, str(REPO_ROOT))
from api.state_paths import reap_authority_path  # noqa: E402

AUTHORIZED_PATH = reap_authority_path()
API = os.environ.get("KURA_API_LOCAL", "http://127.0.0.1:8001")


def _creds():
    env = (REPO_ROOT / "api" / "env").read_text()
    user = re.search(r"^KURA_USER=(.*)$", env, re.M).group(1).strip()
    token = re.search(r"^KURA_TOKEN=(.*)$", env, re.M).group(1).strip()
    # The database is the one this node's api/env names (on nodeb 18/kura on 5433), not a
    # fixed localhost:5432 (kura-on-nodeb plan 5.2). The password goes to PGPASSWORD only.
    from scripts.kura_dsn import without_password
    url = re.search(r"^DATABASE_URL=(.*)$", env, re.M).group(1).strip()
    dsn, pw = without_password(url)
    auth = "Token " + base64.b64encode(f"{user}:{token}".encode()).decode()
    return auth, dsn, pw


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--min-recall", type=float, default=None,
                    help="override min_recall (default: from the authority doc)")
    ap.add_argument("--min-n", type=int, default=10,
                    help="minimum GT-positive labels to make an authorize decision")
    ap.add_argument("--sample", type=int, default=80, help="cap GT-positive posts sampled per tag")
    ap.add_argument("--dry-run", action="store_true", help="print the decision, do not write")
    args = ap.parse_args()

    auth, dsn, pw = _creds()
    # 無ければ空から始める (min_recall は既定値)。fail-closed 側 — 権限は
    # このあとの測定で 1 タグずつ earn し直される。
    doc = json.loads(AUTHORIZED_PATH.read_text()) if AUTHORIZED_PATH.is_file() else {}
    min_recall = args.min_recall if args.min_recall is not None else float(doc.get("min_recall", 0.85))

    def sql(q):
        return subprocess.check_output(
            ["psql", dsn, "-tAX", "-c", q],
            env=dict(os.environ, PGPASSWORD=pw)).decode().strip()

    def fetch(pid):
        req = urllib.request.Request(f"{API}/api/post/{pid}/content", headers={"Authorization": auth})
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.read(), r.headers.get("Content-Type", "")

    sys.path.insert(0, str(REPO_ROOT))
    os.environ.setdefault("KURA_MODELS_DIR", str(Path.home() / ".cache" / "kura-tagger" / "models"))
    import torch  # noqa: PLC0415
    from tagger.infer import _run_wd14_fixed, load_all_models, reap_domain, tag_image  # noqa: PLC0415

    print("[reap-auth] loading models...", flush=True)
    models = load_all_models()
    fixed = models.get("wd14_fixed")
    domain = reap_domain(fixed, models.get("veto_heads"))
    print(f"[reap-auth] reap domain ({len(domain)}): {domain}", flush=True)

    def _pooled(pids):
        out = []
        for pid in pids:
            try:
                img, _ = fetch(pid)
                _, p = _run_wd14_fixed(img, fixed, want_pooled=True)
                if p is not None:
                    out.append(p.float().flatten())
            except Exception:  # noqa: BLE001
                pass
        return torch.stack(out) if out else torch.empty(0)

    def _auc(scores, labels):
        order = torch.argsort(scores)
        ranks = torch.empty_like(order, dtype=torch.float)
        ranks[order] = torch.arange(1, len(scores) + 1, dtype=torch.float)
        npos = labels.sum().item(); nneg = len(labels) - npos
        if npos == 0 or nneg == 0:
            return None
        return (ranks[labels == 1].sum().item() - npos * (npos + 1) / 2) / (npos * nneg)

    def _promote_signal(t, pos_pids):
        """Is this denied tag HEAD-FIXABLE? Fit a FRESH linear probe on the
        frozen wd14-fixed features and measure DISCRIMINATION — ROC-AUC and the
        precision it pays at recall=min_recall. High AUC + usable precision =>
        a retrained logit-patch could lift recall over the bar (昇格候補). NOTE:
        a recall-FORCED threshold always yields ~min_recall by construction —
        that is why the earlier ceiling metric marked everything promotable;
        AUC/precision is the real head-fixable signal. Returns (auc, prec)."""
        Xp = _pooled(pos_pids)
        neg_pids = [int(x) for x in sql(
            f"SELECT post_id FROM tagger_gt_label WHERE tag='{t}' AND verdict='negative' "
            "ORDER BY md5(post_id::text) LIMIT 150").split("\n") if x.strip()]
        Xn = _pooled(neg_pids)
        if len(Xp) < args.min_n or len(Xn) < 10:
            return (None, None)
        X = torch.cat([Xp, Xn]); y = torch.cat([torch.ones(len(Xp)), torch.zeros(len(Xn))])
        mu, sd = X.mean(0), X.std(0) + 1e-6
        Xs = (X - mu) / sd
        m = len(X); perm = torch.randperm(m); oof = torch.zeros(m)
        for k in range(5):
            te = perm[k::5]; tr = torch.tensor([i for i in perm.tolist() if i not in set(te.tolist())])
            w = torch.zeros(X.shape[1], requires_grad=True); b = torch.zeros(1, requires_grad=True)
            opt = torch.optim.Adam([w, b], lr=0.05, weight_decay=1e-2)
            lf = torch.nn.BCEWithLogitsLoss(pos_weight=torch.tensor([len(Xn) / max(1, len(Xp))]))
            for _ in range(300):
                opt.zero_grad(); lf(Xs[tr] @ w + b, y[tr]).backward(); opt.step()
            oof[te] = (Xs[te] @ w.detach() + b.detach())
        auc = _auc(oof, y)
        ps = torch.sigmoid(oof)
        thr = torch.sort(ps[y == 1], descending=True).values
        tcut = thr[min(len(thr) - 1, int(min_recall * len(thr)))]
        pred = ps >= tcut
        tp = ((pred == 1) & (y == 1)).sum().item(); fp = ((pred == 1) & (y == 0)).sum().item()
        prec = tp / max(1, tp + fp)
        return (round(auc, 3) if auc is not None else None, round(prec, 3))

    measured: dict[str, dict] = {}
    authorized: list[str] = []
    prev_auth = set(doc.get("authorized") or [])  # fail-safe: never demote on a failed measurement
    for tag in domain:
        rows = sql("SELECT post_id FROM tagger_gt_label "
                   f"WHERE tag='{tag}' AND verdict='positive' ORDER BY md5(post_id::text) "
                   f"LIMIT {args.sample}")
        pids = [int(x) for x in rows.split("\n") if x.strip()]
        if len(pids) < args.min_n:
            measured[tag] = {"gt_pos_recall": None, "n": len(pids), "verdict": "insufficient_gt"}
            print(f"[reap-auth] {tag}: only {len(pids)} GT-pos (< {args.min_n}) — cannot authorize", flush=True)
            continue
        repro = 0
        n = 0
        for pid in pids:
            try:
                img, mime = fetch(pid)
                if tag in {t["name"] for t in tag_image(img, mime, models)}:
                    repro += 1
                n += 1
            except Exception as e:  # noqa: BLE001
                print(f"[reap-auth] {tag} post {pid} err: {str(e)[:60]}", flush=True)
        if n < args.min_n:
            # measurement invalid — fetches errored (e.g. kura-api restarting),
            # NOT insufficient GT (pids had enough). Never demote on bad data:
            # preserve prior authority so a transient outage can't wipe the reap
            # gate (2026-07-22 incident: a run during a kura-api restart got
            # n=0 for erection and dropped it from authorized).
            kept = tag in prev_auth
            if kept:
                authorized.append(tag)
            measured[tag] = {"gt_pos_recall": None, "n": n, "verdict": "kept_prior" if kept else "measurement_failed"}
            print(f"[reap-auth] {tag}: only {n} valid measurements of {len(pids)} sampled "
                  f"(fetch failures?) — {'kept prior authority' if kept else 'skipped'}", flush=True)
            continue
        rec = repro / n if n else 0.0
        ok = rec >= min_recall
        margin = 0.05  # hysteresis band — small-sample recall noise must not flip the gate on/off
        measured[tag] = {"gt_pos_recall": round(rec, 3), "n": n, "verdict": "safe" if ok else "unsafe"}
        if ok:
            authorized.append(tag)
            print(f"[reap-auth] {tag}: recall {repro}/{n} = {rec:.0%} → AUTHORIZED (min={min_recall:.0%})", flush=True)
        elif tag in prev_auth and rec >= (min_recall - margin):
            # borderline (e.g. 80-85%) AND was authorized → keep. A tag whose
            # true recall sits near the bar would otherwise flip authority every
            # run on sampling noise (2026-07-22: erection measured 95% then 80%).
            authorized.append(tag)
            measured[tag]["verdict"] = "kept_borderline"
            print(f"[reap-auth] {tag}: recall {rec:.0%} borderline (≥{min_recall - margin:.0%}) + was authorized → kept", flush=True)
        else:
            # denied → head-fixable? fit a FRESH linear probe on frozen features
            # and measure DISCRIMINATION (AUC + precision at recall=min_recall).
            # promotable = a retrained logit-patch could reach the bar at usable
            # precision (昇格候補). NOT the recall-forced ceiling, which always
            # ~min_recall and wrongly marked everything promotable.
            auc, prec = _promote_signal(tag, pids)
            measured[tag]["fresh_auc"] = auc
            measured[tag]["prec_at_recall"] = prec
            measured[tag]["promotable"] = bool(prec is not None and prec >= 0.40)
            _p = (f" · ⭐昇格候補(AUC {auc} / P@R {prec})" if measured[tag]["promotable"]
                  else (f" · backbone-bound(AUC {auc} / P@R {prec})" if auc is not None else " · 測定不能"))
            print(f"[reap-auth] {tag}: recall {repro}/{n} = {rec:.0%} → denied{_p}", flush=True)

    authorized.sort()
    new_doc = dict(doc)
    new_doc["min_recall"] = min_recall
    new_doc["authorized"] = authorized
    new_doc["measured"] = measured
    print(f"\n[reap-auth] authorized set: {authorized}", flush=True)

    if args.dry_run:
        print("[reap-auth] dry-run — not writing", flush=True)
        return 0
    AUTHORIZED_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = AUTHORIZED_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(new_doc, ensure_ascii=False, indent=2) + "\n")
    os.replace(tmp, AUTHORIZED_PATH)
    print(f"[reap-auth] wrote {AUTHORIZED_PATH}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
