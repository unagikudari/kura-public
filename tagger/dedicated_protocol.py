"""Dedicated human-only protocol for vnext_arms (no DB writes or promotion).

Snapshots and outputs are content-addressed, create-only JSON. Whole connected
components are held out; the final fit never consumes them. Production precision
is a separate uniform sample of all scoreable candidate-head positives, awaiting
explicit human review. The caller is responsible for GPU admission on trainer.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import tempfile
import random
from collections import Counter, defaultdict
from pathlib import Path

SCHEMA = "vnext-dedicated-snapshot/1"
POLICY = "all-pools-primary-duplicate-sha256-confirmed-pair+work-edges/v2"
# The v1 policy (sha256 of its name) grouped one more kind of same-work pair by
# name; it now comes from tagger/work_edges. A snapshot stores its own edges, so
# v1 snapshots stay valid.
_EARLIER_POLICIES = {"cb7a26f0ab04c9eaeda1577db90071dc87faff720f5e4a25dd816f16b364de75"}
# Same-work pairs an installed extension knows (e.g. the files of one item it
# fetched): each tagger/work_edges/*.sql is one SELECT of (post id, post id).
WORK_EDGES = Path(__file__).with_name("work_edges")


def _known_policy(policy) -> bool:
    return policy == POLICY or hashlib.sha256(str(policy).encode()).hexdigest() in _EARLIER_POLICIES


def work_edge_sql() -> dict[str, str]:
    """The installed extensions' same-work SELECTs, exactly the files compose recorded.

    A snapshot made on a node composed with other extension commits than this release pins
    (pulled, not yet recomposed), or with a file here that no installed extension declares,
    would silently miss or invent edges, so both are refused. No extension and no pin file
    (a core-only install) is zero sources.
    """
    from api import extension_hooks

    extension_hooks.assert_composed_as_pinned()  # also checks every recorded file's hash
    declared = sorted(Path(f).name for ext in extension_hooks.installed().values()
                      for f in ext["files"] if Path(f).parent == Path("tagger/work_edges"))
    present = sorted(p.name for p in WORK_EDGES.glob("*.sql"))
    if present != declared:
        raise ProtocolError(f"tagger/work_edges holds {present} but the installed extensions "
                            f"declare {declared}; compose the pinned extensions")
    return {n: (WORK_EDGES / n).read_text().strip().rstrip(";") for n in declared}


class ProtocolError(ValueError):
    pass


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     allow_nan=False).encode()).hexdigest()


def write_bytes_once(path, payload):
    """Publish complete bytes atomically with a no-overwrite link, even on races."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".dedicated-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(payload)
            f.flush()
            os.fsync(f.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError:
            if path.read_bytes() != payload:
                raise ProtocolError(f"immutable output exists: {path}; use a new output path")
    finally:
        os.unlink(temporary)


def write_once(path, body):
    """Never overwrite evidence, including on rerun. Identical writes are OK."""
    doc = dict(body, sha256=digest(body))
    payload = json.dumps(doc, sort_keys=True, indent=1, allow_nan=False) + "\n"
    write_bytes_once(path, payload.encode())
    return doc


def read_verified(path, schema=None):
    doc = json.loads(Path(path).read_text())
    body = {k: v for k, v in doc.items() if k != "sha256"}
    if doc.get("sha256") != digest(body) or (schema and doc.get("schema") != schema):
        raise ProtocolError(f"snapshot digest/schema mismatch: {path}; restore original evidence")
    return doc


def connected_groups(nodes, edges):
    """Full-universe closure BEFORE projecting to GT; unlabeled bridges matter."""
    parent = {i: i for i in nodes}

    def root(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for a, b in edges:
        if a not in parent or b not in parent:
            raise ProtocolError(f"graph edge has absent endpoint: {a},{b}")
        a, b = root(a), root(b)
        if a != b:
            parent[max(a, b)] = min(a, b)
    return {i: root(i) for i in parent}


def fixed_split(labels, groups, *, positive=300, negative=1000, seed=802081,
                tolerance=0.10):
    """Bounded deterministic search of whole components; labels, never scores.

    Targets and tolerance are frozen in the snapshot. Failure stays explicit;
    do not split a connected component to obtain pretty sample counts.
    """
    if positive < 1 or negative < 1 or not 0 <= tolerance <= 0.10:
        raise ProtocolError("invalid holdout targets/tolerance; use positive counts and <=10%")
    members = defaultdict(list)
    for i in sorted(labels):
        members[groups[i]].append(i)
    counts = {g: (sum(labels[i] == 1 for i in ids), sum(labels[i] == 0 for i in ids))
              for g, ids in members.items()}
    best = None
    for trial in range(256):
        order = sorted(members)
        random.Random(seed + trial).shuffle(order)
        picked, p, n = [], 0, 0
        for g in order:
            gp, gn = counts[g]
            before = abs(p - positive) / positive + abs(n - negative) / negative
            after = abs(p + gp - positive) / positive + abs(n + gn - negative) / negative
            if after < before and p + gp <= positive * (1 + tolerance) and n + gn <= negative * (1 + tolerance):
                picked.append(g)
                p, n = p + gp, n + gn
        loss = abs(p - positive) / positive + abs(n - negative) / negative
        if best is None or loss < best[0]:
            best = (loss, picked, p, n)
        if loss == 0:
            break
    _, picked, p, n = best
    held = sorted(i for g in picked for i in members[g])
    train = sorted(set(labels) - set(held))
    if (abs(p - positive) > positive * tolerance or abs(n - negative) > negative * tolerance
            or {labels[i] for i in train} != {0, 1}):
        raise ProtocolError(f"whole-component holdout infeasible: best +{p}/-{n}, "
                            f"target +{positive}/-{negative}; review grouping/plan, never split groups")
    return {"train": train, "heldout": held, "heldout_positive": p, "heldout_negative": n}


def _media_rows(conn, ids):
    from tagger.vnext_arms import q
    rows = {}
    for start in range(0, len(ids), 1000):
        for pid, kind, status, sha in q(conn, "SELECT id,type,status,encode(sha256(data),'hex') "
                                      "FROM post WHERE id=ANY(%s)", (ids[start:start+1000],)):
            reason = ("unsupported_media_type:" + kind if kind != "image" else
                      "missing_blob" if sha is None else
                      "status:" + str(status) if status != "ok" else None)
            rows[pid] = {"id": pid, "type": kind, "status": status,
                         "blob_sha256": sha, "exclusion": reason}
    return [rows.get(i, {"id": i, "blob_sha256": None, "exclusion": "missing_post"}) for i in ids]


def prepare(tag, path, *, seed=802081, positive=300, negative=1000,
            precision_n=300, tolerance=0.10):
    """Read canonical state once in REPEATABLE READ READ ONLY. No recovered adds.

    All pools deliberately over-group creator/curation collections: a coarse,
    conservative policy, rather than pretending source_pool_id identifies work.
    Store every edge so the closure is auditable. Freeze the full veto deployment
    frame (all current tag-present posts), without an early ID LIMIT.
    """
    from tagger.vnext_arms import connect_primary, q
    if Path(path).exists():
        raise ProtocolError("snapshot already exists; reuse it or choose a new explicit snapshot path")
    if precision_n < 1:
        raise ProtocolError("precision sample size must be positive")
    conn = connect_primary()
    conn.autocommit = False
    conn.set_session(isolation_level="REPEATABLE READ", readonly=True)
    try:
        stamp = q(conn, "SELECT now(),txid_current_snapshot()")[0]
        gt = q(conn, "SELECT post_id,verdict,derived_from,source_pool_id,verified_at "
                     "FROM tagger_gt_label WHERE tag=%s ORDER BY post_id", (tag,))
        nodes = [i for (i,) in q(conn, "SELECT id FROM post ORDER BY id")]
        # Window star edges preserve all memberships, with linear edge count.
        extra = work_edge_sql()
        extra_links = "".join(f"\n              UNION ALL SELECT * FROM ({sql}) AS work_edge_{n}"
                              for n, sql in enumerate(extra.values()))
        edges = q(conn, f"""WITH memberships AS (
              SELECT post_id,pool_id FROM pool_post UNION
              SELECT id,primary_pool_id FROM post WHERE primary_pool_id IS NOT NULL),
            links AS (
              SELECT post_id a,min(post_id) OVER (PARTITION BY pool_id) b FROM memberships
              UNION ALL SELECT id,duplicate_of FROM post WHERE duplicate_of IS NOT NULL
              UNION ALL SELECT id,min(id) OVER (PARTITION BY sha256) FROM post
                WHERE sha256 IS NOT NULL AND octet_length(sha256)>0
              UNION ALL SELECT censored_post_id,uncensored_post_id FROM censor_pair WHERE confirmed{extra_links})
            SELECT DISTINCT least(a,b),greatest(a,b) FROM links WHERE a<>b ORDER BY 1,2""")
        groups = connected_groups(nodes, edges)
        corpus = [i for (i,) in q(conn, "SELECT DISTINCT pt.post_id FROM post_tag pt "
                    "JOIN tag_name tn ON tn.tag_id=pt.tag_id WHERE tn.ord=0 AND tn.name=%s "
                    "ORDER BY pt.post_id", (tag,))]
        gt_ids = {row[0] for row in gt}
        # Missing GT posts are explicitly accounted, never vanish in an inner join.
        for i in gt_ids:
            groups.setdefault(i, i)
        media = _media_rows(conn, sorted(gt_ids | set(corpus)))
        floor = {k: str(v) for k, v in q(conn, "SELECT key,value FROM tagger_config ORDER BY key")}
    finally:
        conn.rollback()
        conn.close()
    media_by = {r["id"]: r for r in media}
    labels = {}
    records = []
    for i, verdict, derived, pool, at in gt:
        if verdict not in ("positive", "negative"):
            raise ProtocolError(f"unknown GT verdict for {i}: {verdict}")
        records.append({"id": i, "verdict": verdict, "derived_from": derived,
                        "source_pool_id": pool, "verified_at": str(at),
                        "media_exclusion": media_by[i]["exclusion"]})
        if derived == "human_correction" and not media_by[i]["exclusion"]:
            labels[i] = int(verdict == "positive")
    body = {"schema": SCHEMA, "tag": tag, "seed": seed,
            "canonical_at": str(stamp[0]), "transaction_snapshot": str(stamp[1]),
            "grouping_policy": POLICY, "work_edge_sources": sorted(extra),
            "graph_nodes": nodes, "graph_edges": edges,
            "gt": records, "media": media, "production_frame": corpus,
            "production_frame_definition": "all tag-present posts; candidate veto head, before group/media exclusions",
            "floor_snapshot": floor,
            "parameters": {"positive": positive, "negative": negative,
                           "precision_n": precision_n, "tolerance": tolerance},
            "counts_by_provenance": dict(Counter(r["derived_from"]+":"+r["verdict"] for r in records))}
    try:
        body["split"] = fixed_split(labels, groups, positive=positive, negative=negative,
                                     seed=seed, tolerance=tolerance)
        body["status"] = "prepared"
    except ProtocolError as exc:
        body.update(status="blocked", blocker=str(exc))
    result = write_once(path, body)
    if result["status"] != "prepared":
        raise ProtocolError(result["blocker"] + f"; evidence saved to {path}")
    return result


def validate_snapshot(doc):
    if doc.get("status") != "prepared":
        raise ProtocolError("snapshot preparation blocked; inspect its blocker before measurement")
    groups = connected_groups(doc["graph_nodes"], doc["graph_edges"])
    gt = doc["gt"]
    if len({r["id"] for r in gt}) != len(gt):
        raise ProtocolError("duplicate GT IDs")
    labels = {r["id"]: int(r["verdict"] == "positive") for r in gt
              if r["derived_from"] == "human_correction" and not r["media_exclusion"]}
    for r in gt:
        groups.setdefault(r["id"], r["id"])
    expected = fixed_split(labels, groups, seed=doc["seed"],
                           **{k: doc["parameters"][k] for k in ("positive", "negative", "tolerance")})
    if doc["split"] != expected or not _known_policy(doc["grouping_policy"]):
        raise ProtocolError("split/policy mismatch; restore the frozen snapshot")
    return labels, groups


def features(conn, ids, doc, cache_dir):
    """Verify current replica bytes against frozen canonical hashes before reuse.

    Do not use post-ID-only shared caches: snapshot-specific cache plus extractor
    source digest prevents stale content/code vectors from entering this protocol.
    Missing/changed/undecodable media aborts with every affected ID; never shrink.
    """
    from tagger import vnext_arms as arms
    expected = {r["id"]: r for r in doc["media"]}
    actual = _media_rows(conn, ids)
    failures = [{"id": r["id"], "reason": r["exclusion"] or "blob_changed"}
                for r in actual if r["exclusion"] or
                r["blob_sha256"] != expected[r["id"]]["blob_sha256"]]
    if failures:
        raise ProtocolError("media mismatch; restore snapshot media, do not resample: " + json.dumps(failures))
    source = hashlib.sha256(Path(arms.__file__).read_bytes()).hexdigest()
    cache = Path(cache_dir) / (doc["sha256"] + "-" + source + ".pt")
    cache.parent.mkdir(parents=True, exist_ok=True)
    emb = arms._extract_features(conn, ids, cache, classifier_reset=True, label="dedicated frozen media",
                                  model_identity_path=Path(cache_dir) / "backbone.json")
    missing = sorted(set(ids) - set(emb))
    if missing:
        raise ProtocolError("feature/decode failures; restore media and retry, do not resample: " + json.dumps(missing))
    return emb


def validate_scores(ids, scores):
    if set(ids) != set(scores) or any(not math.isfinite(v) or not 0 <= v <= 1 for v in scores.values()):
        raise ProtocolError("incomplete/nonfinite prediction scores; do not sample a truncated frame")


def sample_predictions(frame, scores, *, threshold, n, seed):
    validate_scores(frame, scores)
    positives = sorted(i for i, v in scores.items() if v >= threshold)
    return positives, sorted(random.Random(seed).sample(positives, min(n, len(positives))))


def point_metrics(scores, labels, threshold):
    validate_scores(labels, scores)
    tp = sum(labels[i] == 1 and scores[i] >= threshold for i in labels)
    fp = sum(labels[i] == 0 and scores[i] >= threshold for i in labels)
    pos, neg = sum(labels.values()), len(labels) - sum(labels.values())
    from tagger.vnext_arms import rankauc
    return {"tp": tp, "fp": fp, "fn": pos-tp, "tn": neg-fp,
            "precision": tp/(tp+fp) if tp+fp else None, "recall": tp/pos if pos else None,
            "auc": rankauc([scores[i] for i in labels if labels[i]],
                           [scores[i] for i in labels if not labels[i]]) if pos and neg else None}


def fit_candidate(getv, labels, groups, split, *, tag, seed):
    """Only the training partition enters calibration or the final fit."""
    from tagger import vnext_arms as arms
    tr, te = split["train"], split["heldout"]
    if set(tr) & set(te) or {groups[i] for i in tr} & {groups[i] for i in te}:
        raise ProtocolError("train/heldout group overlap")
    pos, neg = [i for i in tr if labels[i]], [i for i in tr if not labels[i]]
    sc, y, _, manifest = arms.grouped_oof(getv, pos, neg, groups,
                                          tag=tag, seed=seed, arm="dedicated_feat1024_calibration")
    if set(sc) != set(tr):
        raise ProtocolError("incomplete training OOF calibration; inspect group stratification")
    validate_scores(tr, sc)
    threshold = arms.thr_rules(sc, y)["max_f1"]
    tensors = arms.fit_deployable(getv, pos, neg, seed=seed)
    return tensors, threshold, manifest


def measure(tag, snapshot_path, out_path):
    """Reuse existing grouped OOF calibration and logistic fit, human-only.

    Single predeclared arm feat1024 and max-F1 threshold, chosen on training OOF
    only. No oracle holdout threshold, arm selection, augmentation or final GT refit.
    """
    from tagger import vnext_arms as arms
    from safetensors.torch import save
    import torch
    if not out_path or Path(out_path).exists():
        raise ProtocolError("dedicated measurement needs a new --out path")
    doc = read_verified(snapshot_path, SCHEMA)
    if doc["tag"] != tag:
        raise ProtocolError("snapshot tag mismatch")
    labels, groups = validate_snapshot(doc)
    tr, te = doc["split"]["train"], doc["split"]["heldout"]
    conn = arms.connect_local()
    conn.autocommit = False
    conn.set_session(isolation_level="REPEATABLE READ", readonly=True)
    try:
        emb = features(conn, tr+te, doc, Path(out_path).parent / "features")
    except ProtocolError as exc:
        write_once(str(out_path)+".failure.json", {"schema": "dedicated-failure/1", "snapshot_sha256": doc["sha256"], "reason": str(exc)})
        raise
    finally:
        conn.close()
    tensors, threshold, manifest = fit_candidate(emb.__getitem__, labels, groups, doc["split"],
                                                 tag=tag, seed=doc["seed"])
    pos, neg = [i for i in tr if labels[i]], [i for i in tr if not labels[i]]
    model_path = Path(str(out_path)+".safetensors")
    if model_path.exists():
        raise ProtocolError("model already exists; preserve frozen candidate and use a new run path")
    model_path.parent.mkdir(parents=True, exist_ok=True)
    write_bytes_once(model_path, save(tensors))
    model_sha = hashlib.sha256(model_path.read_bytes()).hexdigest()
    backbone = read_verified(Path(out_path).parent / "features" / "backbone.json", "dedicated-backbone/1")
    # Persist the model/threshold binding BEFORE reading any heldout predictions.
    frozen = write_once(str(out_path)+".candidate.json", {"schema": "dedicated-candidate/1", "tag": tag,
          "snapshot_sha256": doc["sha256"], "model_sha256": model_sha,
          "threshold": threshold, "rule": "training-grouped-OOF:max_f1", "train_ids": tr,
          "backbone_sha256": backbone["sha256"],
          "calibration_manifest": manifest})
    with torch.no_grad():
        values = torch.sigmoid(torch.stack([emb[i] for i in te]) @ tensors["w"] + tensors["b"]).flatten().tolist()
    scores = dict(zip(te, values))
    return write_once(out_path, {"schema": "vnext-dedicated-measurement/1", "tag": tag,
            "snapshot_sha256": doc["sha256"], "candidate_sha256": frozen["sha256"],
            "model_sha256": model_sha, "threshold": threshold,
            "heldout_scores": {str(i): scores[i] for i in te},
            "heldout": point_metrics(scores, {i: labels[i] for i in te}, threshold),
            "train_counts": {"positive": len(pos), "negative": len(neg)},
            "human_media_exclusions": [r for r in doc["gt"] if r["derived_from"] == "human_correction" and r["media_exclusion"]],
            "production_precision": {"status": "pending_sampling_and_human_review"},
            "promotion": "not_requested", "registration_eligible": False})


def precision_frame(doc):
    """Full production frame; GT overlap is disclosed, never a sampling filter."""
    media = {r["id"]: r for r in doc["media"]}
    excluded = [{"id": i, "reason": media[i]["exclusion"]}
                for i in doc["production_frame"] if media[i]["exclusion"]]
    excluded_ids = {r["id"] for r in excluded}
    return sorted(set(doc["production_frame"]) - excluded_ids), excluded


def precision_sample(tag, snapshot_path, measurement_path, out_path):
    """Uniform sampling without replacement AFTER scoring the complete frame.

    Full scoreable production is sampled, including GT-connected groups. Group
    overlap is disclosed so this operational precision is not confused with the
    separate unseen-group heldout generalization estimate. Every sample receives
    a fresh human verdict; existing GT and model scores never become its labels.
    """
    from tagger import vnext_arms as arms
    from safetensors.torch import load_file
    import torch
    if not out_path or Path(out_path).exists():
        raise ProtocolError("precision sampling needs a new --out path")
    doc = read_verified(snapshot_path, SCHEMA)
    result = read_verified(measurement_path, "vnext-dedicated-measurement/1")
    candidate = read_verified(str(measurement_path)+".candidate.json", "dedicated-candidate/1")
    if (doc["tag"] != tag or result["snapshot_sha256"] != doc["sha256"]
            or result["candidate_sha256"] != candidate["sha256"]
            or candidate["snapshot_sha256"] != doc["sha256"]
            or result["threshold"] != candidate["threshold"]):
        raise ProtocolError("candidate/snapshot binding mismatch")
    _, groups = validate_snapshot(doc)
    gt_groups = {groups[r["id"]] for r in doc["gt"]}
    train_groups = {groups[i] for i in doc["split"]["train"]}
    heldout_groups = {groups[i] for i in doc["split"]["heldout"]}
    frame, excluded = precision_frame(doc)
    model_path = Path(str(measurement_path)+".safetensors")
    if hashlib.sha256(model_path.read_bytes()).hexdigest() != result["model_sha256"]:
        raise ProtocolError("candidate model digest mismatch")
    backbone = read_verified(Path(measurement_path).parent / "features" / "backbone.json", "dedicated-backbone/1")
    if backbone["sha256"] != candidate["backbone_sha256"]:
        raise ProtocolError("backbone identity mismatch")
    tensors = load_file(str(model_path))
    conn = arms.connect_local()
    conn.autocommit = False
    conn.set_session(isolation_level="REPEATABLE READ", readonly=True)
    try:
        emb = features(conn, frame, doc, Path(measurement_path).parent / "features")
    except ProtocolError as exc:
        write_once(str(out_path)+".failure.json", {"schema": "dedicated-failure/1", "snapshot_sha256": doc["sha256"], "reason": str(exc)})
        raise
    finally:
        conn.close()
    scores = {}
    with torch.no_grad():
        for start in range(0, len(frame), 1000):
            ids = frame[start:start+1000]
            values = torch.sigmoid(torch.stack([emb[i] for i in ids]) @ tensors["w"] + tensors["b"]).flatten().tolist()
            scores.update(zip(ids, values))
    n = doc["parameters"]["precision_n"]
    positives, sample = sample_predictions(frame, scores, threshold=result["threshold"],
                                           n=n, seed=doc["seed"]+9001)
    body = {"schema": "dedicated-precision-sample/1", "tag": tag,
            "snapshot_sha256": doc["sha256"], "measurement_sha256": result["sha256"],
            "model_sha256": result["model_sha256"], "threshold": result["threshold"],
            "estimand": "candidate-head precision on ALL frozen tag-present, scoreable predicted-positive posts",
            "scope_limit": "excludes enumerated unavailable media; includes training/heldout group overlap, not an independent generalization estimate",
            "gt_connected_frame_n": sum(groups[i] in gt_groups for i in frame),
            "sample_group_overlap": {str(i): ("train" if groups[i] in train_groups else
                 "heldout" if groups[i] in heldout_groups else "other_GT" if groups[i] in gt_groups else "unseen") for i in sample},
            "full_frame_n": len(doc["production_frame"]), "eligible_frame_n": len(frame),
            "excluded": excluded, "scores": {str(i): v for i, v in scores.items()},
            "predicted_positive_n": len(positives), "requested_n": n, "sample_ids": sample,
            "status": "awaiting_human_review" if len(sample) == n else "blocked_insufficient_predictions",
            "human_reviews": [{"id": i, "label": None, "reviewer": None, "reviewed_at": None} for i in sample]}
    return write_once(out_path, body)


def precision_evaluate(sample_path, reviews_path, out_path):
    sample = read_verified(sample_path, "dedicated-precision-sample/1")
    reviews = json.loads(Path(reviews_path).read_text())
    if (sample["status"] != "awaiting_human_review" or
            reviews.get("sample_sha256") != sample["sha256"] or
            reviews.get("label_source") != "human"):
        raise ProtocolError("complete sample and explicit human review bound to sample_sha256 required")
    rows = reviews.get("human_reviews", [])
    ids = [r["id"] for r in rows]
    if len(ids) != len(set(ids)) or set(ids) != set(sample["sample_ids"]):
        raise ProtocolError("human review IDs must match the fixed sample exactly; do not replace missing media")
    from datetime import datetime
    for row in rows:
        if row.get("label") not in ("positive", "negative") or not str(row.get("reviewer") or "").strip():
            raise ProtocolError("all sample labels require explicit human verdict/reviewer")
        try:
            at = datetime.fromisoformat(row["reviewed_at"].replace("Z", "+00:00"))
            if at.tzinfo is None:
                raise ValueError("timezone missing")
        except (KeyError, TypeError, ValueError, AttributeError) as exc:
            raise ProtocolError("human reviewed_at must be an ISO timestamp with timezone") from exc
    overlap = sample["sample_group_overlap"]
    if set(overlap) != {str(i) for i in ids} or not set(overlap.values()) <= {"train", "heldout", "other_GT", "unseen"}:
        raise ProtocolError("sample group-overlap accounting must cover every sample ID")
    strata = {}
    for name in ("train", "heldout", "other_GT", "unseen"):
        subset = [r for r in rows if overlap[str(r["id"])] == name]
        true = sum(r["label"] == "positive" for r in subset)
        strata[name] = {"n": len(subset), "true_positive": true,
                        "precision": true / len(subset) if subset else None}
    n = len(rows)
    tp = sum(r["label"] == "positive" for r in rows)
    phat, z = tp / n, 1.959963984540054
    center = (phat + z*z/(2*n))/(1+z*z/n)
    radius = z*math.sqrt(phat*(1-phat)/n+z*z/(4*n*n))/(1+z*z/n)
    return write_once(out_path, {"schema": "dedicated-human-precision/1", "sample_sha256": sample["sha256"],
           "reviews_sha256": digest(reviews), "n": n, "true_positive": tp, "false_positive": n-tp,
           "precision": phat, "wilson_95": [center-radius, center+radius],
           "estimand": sample["estimand"], "scope_limit": sample["scope_limit"],
           "sample_group_overlap_counts": dict(Counter(overlap.values())),
           "precision_by_overlap_diagnostic": strata,
           "full_frame_n": sample.get("full_frame_n"), "eligible_frame_n": sample.get("eligible_frame_n"),
           "excluded_media_n": len(sample.get("excluded", [])),
           "promotion": "not_requested", "registration_eligible": False})
