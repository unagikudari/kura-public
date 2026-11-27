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
import random
from collections import Counter, defaultdict
from pathlib import Path

from api.tagger_core.learning_artifacts import (
    SNAPSHOT_SCHEMA as SCHEMA, ProtocolError, digest, read_verified, write_bytes_once, write_once,
)

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


def unjudged_catalog_groups(snapshot):
    """Group tag-present review candidates outside the frozen GT components."""
    groups = connected_groups(snapshot["graph_nodes"], snapshot["graph_edges"])
    known = {groups.get(row["id"], row["id"]) for row in snapshot["gt"]}
    candidates = defaultdict(list)
    for post_id in reversed(snapshot["production_frame"]):
        group = groups[post_id]
        if group not in known:
            candidates[group].append(post_id)
    return sorted(candidates.values(), key=lambda posts: posts[0], reverse=True)


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


def feasible_holdout(labels, groups, *, positive, negative, seed, tolerance, floor):
    """Reduce an unreachable preferred positive target using label groups only."""
    counts = defaultdict(Counter)
    for post, label in labels.items():
        counts[groups[post]][label] += 1
    eligible_cap = sum(count[1] for count in counts.values()
                       if count[1] <= positive * (1 + tolerance)
                       and count[0] <= negative * (1 + tolerance))
    selected = max(1, eligible_cap) if eligible_cap < positive * (1 - tolerance) else positive
    split = fixed_split(labels, groups, positive=selected, negative=negative,
                        seed=seed, tolerance=tolerance)
    if selected != positive:
        require_holdout_support(labels, split, floor)
    return selected, split


def require_holdout_support(labels, split, floor):
    train = Counter(labels[post] for post in split["train"])
    if (split["heldout_positive"] < math.ceil(float(floor["min_val_pos_support"]))
            or split["heldout_negative"] < math.ceil(float(floor["min_held_out_samples"]))
            or train[1] < math.ceil(float(floor["min_gt_positive"]))
            or train[0] < math.ceil(float(floor["min_gt_negative"]))):
        raise ProtocolError("adjusted holdout cannot meet existing support floors")


def calibration_has_both_classes(labels, groups, train, seed):
    from tagger.vnext_arms import make_group_folds

    ordered = [i for i in train if labels[i]] + [i for i in train if not labels[i]]
    folds = make_group_folds(ordered, groups, seed, 5, y=labels)
    return all({labels[i] for i in fold} == {0, 1} for fold in folds)


def calibratable_holdout(labels, groups, *, positive, negative, seed, tolerance, floor):
    target, candidate = feasible_holdout(labels, groups, positive=positive, negative=negative,
                                         seed=seed, tolerance=tolerance, floor=floor)
    minimum = math.ceil(float(floor["min_val_pos_support"]))
    for _ in range(8):
        require_holdout_support(labels, candidate, floor)
        if calibration_has_both_classes(labels, groups, candidate["train"], seed):
            return target, candidate
        next_target = max(minimum, min(target - 1, math.floor(target * .95)))
        if next_target >= target:
            break
        target = next_target
        candidate = fixed_split(labels, groups, positive=target, negative=negative,
                                seed=seed, tolerance=tolerance)
    raise ProtocolError("whole-group holdout leaves a calibration fold without both classes")


def _media_rows(conn, ids):
    from tagger.vnext_arms import q
    rows = {}
    for start in range(0, len(ids), 1000):
        from tagger.display_bytes import identities
        for pid, kind, status, sha in identities(lambda sql, args: q(conn, sql, args),
                                                 ids[start:start+1000]):
            reason = ("unsupported_media_type:" + kind if kind != "image" else
                      "missing_blob" if sha is None else
                      "status:" + str(status) if status != "ok" else None)
            rows[pid] = {"id": pid, "type": kind, "status": status,
                         "blob_sha256": sha, "exclusion": reason}
    return [rows.get(i, {"id": i, "blob_sha256": None, "exclusion": "missing_post"}) for i in ids]


def prepare(tag, path, *, seed=802081, positive=300, negative=1000,
            precision_n=300, tolerance=0.10, catalog_sample_size=None,
            fit_positive_to_groups=False):
    """Read canonical state once in REPEATABLE READ READ ONLY. No recovered adds.

    All pools deliberately over-group creator/curation collections: a coarse,
    conservative policy, rather than pretending source_pool_id identifies work.
    Store every edge so the closure is auditable. Freeze the full veto deployment
    frame (all current tag-present posts), without an early ID LIMIT.
    """
    from tagger.vnext_arms import connect_primary, q
    from api.tagger_core import training_eligibility as eligibility
    from api.tagger_core.post_exclusion import not_excluded, training_label
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
                     "FROM tagger_gt_label g WHERE tag=%s AND " + training_label("g") + " ORDER BY post_id", (tag,))
        training_eligibility = eligibility.load(lambda sql, args: q(conn, sql, args), tag)
        nodes = [i for (i,) in q(conn, "SELECT id FROM post ORDER BY id")]
        excluded = {i for (i,) in q(conn, "SELECT p.id FROM post p WHERE NOT (" + not_excluded("p.id") + ")")}
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
                    "AND " + not_excluded("pt.post_id") + " ORDER BY pt.post_id", (tag,))]
        gt_ids = {row[0] for row in gt}
        # Missing GT posts are explicitly accounted, never vanish in an inner join.
        for i in gt_ids:
            groups.setdefault(i, i)
        catalog_sample = None
        if catalog_sample_size is not None:
            known_groups = {groups[i] for i in gt_ids}
            catalog_candidates = [i for i in nodes if groups[i] not in known_groups]
            catalog_excluded = [i for i in catalog_candidates if i in excluded]
            eligible_catalog = [i for i in catalog_candidates if i not in excluded]
            catalog_sample = sample_catalog_ids(eligible_catalog, size=catalog_sample_size, seed=seed)
        media_ids = gt_ids | set(corpus) | set(catalog_sample or [])
        media = _media_rows(conn, sorted(media_ids))
        floor = {k: str(v) for k, v in q(conn, "SELECT key,value FROM tagger_config ORDER BY key")}
    finally:
        conn.rollback()
        conn.close()
    media_by = {r["id"]: r for r in media}
    eligible_ids = eligibility.eligible_ids(training_eligibility, ((r[0], r[1]) for r in gt))
    labels = {}
    records = []
    for i, verdict, derived, pool, at in gt:
        if verdict not in ("positive", "negative"):
            raise ProtocolError(f"unknown GT verdict for {i}: {verdict}")
        records.append({"id": i, "verdict": verdict, "derived_from": derived,
                        "source_pool_id": pool, "verified_at": str(at),
                        "media_exclusion": media_by[i]["exclusion"]})
        if (derived == "human_correction" and not media_by[i]["exclusion"]
                and i in eligible_ids):
            labels[i] = int(verdict == "positive")
    body = {"schema": SCHEMA, "tag": tag, "seed": seed,
            "canonical_at": str(stamp[0]), "transaction_snapshot": str(stamp[1]),
            "grouping_policy": POLICY, "work_edge_sources": sorted(extra),
            "graph_nodes": nodes, "graph_edges": edges,
            "gt": records, "media": media, "production_frame": corpus,
            "training_eligibility": training_eligibility,
            "production_frame_definition": "all tag-present posts; candidate veto head, before group/media exclusions",
            "floor_snapshot": floor,
            "parameters": {"positive": positive, "negative": negative,
                           "precision_n": precision_n, "tolerance": tolerance},
            "counts_by_provenance": dict(Counter(r["derived_from"]+":"+r["verdict"] for r in records))}
    if catalog_sample is not None:
        body["catalog_sample"] = {
            "ids": catalog_sample, "population_n": len(eligible_catalog),
            "requested_n": catalog_sample_size, "excluded_ids": catalog_excluded,
        }
    try:
        if fit_positive_to_groups:
            selected, body["split"] = calibratable_holdout(
                labels, groups, positive=positive, negative=negative,
                seed=seed, tolerance=tolerance, floor=floor,
            )
            body["parameters"].update(positive=selected, requested_positive=positive,
                                      calibration_policy="five-grouped-folds/1")
        else:
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
    from api.tagger_core import training_eligibility as eligibility
    if doc.get("status") != "prepared":
        raise ProtocolError("snapshot preparation blocked; inspect its blocker before measurement")
    groups = connected_groups(doc["graph_nodes"], doc["graph_edges"])
    gt = doc["gt"]
    if "training_eligibility" in doc and doc["training_eligibility"].get("tag") != doc.get("tag"):
        raise ProtocolError("training eligibility tag mismatch")
    if len({r["id"] for r in gt}) != len(gt):
        raise ProtocolError("duplicate GT IDs")
    eligible_ids = (eligibility.eligible_ids(doc["training_eligibility"],
                    ((r["id"], r["verdict"]) for r in gt))
                    if "training_eligibility" in doc else {r["id"] for r in gt})
    labels = {r["id"]: int(r["verdict"] == "positive") for r in gt
              if r["derived_from"] == "human_correction" and not r["media_exclusion"]
              and r["id"] in eligible_ids}
    for r in gt:
        groups.setdefault(r["id"], r["id"])
    params = doc["parameters"]
    if "requested_positive" in params:
        calibration = params.get("calibration_policy")
        if calibration not in (None, "five-grouped-folds/1"):
            raise ProtocolError("unknown holdout calibration policy")
        choose = calibratable_holdout if calibration else feasible_holdout
        selected, expected = choose(
            labels, groups, positive=params["requested_positive"], negative=params["negative"],
            seed=doc["seed"], tolerance=params["tolerance"], floor=doc["floor_snapshot"],
        )
        if selected != params["positive"] or selected > params["requested_positive"] or (not calibration and selected == params["requested_positive"]):
            raise ProtocolError("adjusted holdout target mismatch")
    else:
        expected = fixed_split(labels, groups, seed=doc["seed"],
                               **{k: params[k] for k in ("positive", "negative", "tolerance")})
    if doc["split"] != expected or not _known_policy(doc["grouping_policy"]):
        raise ProtocolError("split/policy mismatch; restore the frozen snapshot")
    return labels, groups


def features(conn, ids, doc, cache_dir, *, media_dir=None):
    """Verify current replica bytes against frozen canonical hashes before reuse.

    Do not use post-ID-only shared caches: snapshot-specific cache plus extractor
    source digest prevents stale content/code vectors from entering this protocol.
    Missing/changed/undecodable media aborts with every affected ID; never shrink.
    """
    from tagger import vnext_arms as arms
    expected = {r["id"]: r for r in doc["media"]}
    image_loader = None
    if media_dir is None:
        actual = _media_rows(conn, ids)
    else:
        from tagger.dedicated_dataset import FrozenImages
        image_loader = FrozenImages(media_dir, doc)
        actual = [expected[i] for i in ids]  # bytes are checked on every read below
    failures = [{"id": r["id"], "reason": r["exclusion"] or "blob_changed"}
                for r in actual if r["exclusion"] or
                r["blob_sha256"] != expected[r["id"]]["blob_sha256"]]
    if failures:
        raise ProtocolError("media mismatch; restore snapshot media, do not resample: " + json.dumps(failures))
    source = hashlib.sha256(Path(arms.__file__).read_bytes()).hexdigest()
    cache = Path(cache_dir) / (doc["sha256"] + "-" + source + ".pt")
    cache.parent.mkdir(parents=True, exist_ok=True)
    from tagger.feature_contract import extract
    emb, evidence = extract(conn, ids, cache, classifier_reset=True,
                            label="dedicated frozen media", image_loader=image_loader)
    # Bind the same contract used by serving; a legacy cache/normalization is
    # never silently relabelled. Frame expansion may add input hashes but must
    # keep the backbone and transform unchanged.
    expected_hashes = {str(i): expected[i]["blob_sha256"] for i in ids}
    if evidence["input_sha256"] != expected_hashes:
        raise ProtocolError("feature inputs differ from frozen snapshot")
    write_once(Path(cache_dir) / "backbone-v2.json", {
        "schema": "dedicated-backbone/2", "identity": evidence["identity"],
        "backbone_path": evidence["backbone_path"]})
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


def measure(tag, snapshot_path, out_path, *, media_dir=None):
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
    conn = None if media_dir is not None else arms.connect_local()
    if conn is not None:
        conn.autocommit = False
        conn.set_session(isolation_level="REPEATABLE READ", readonly=True)
    try:
        emb = features(conn, tr+te, doc, Path(out_path).parent / "features", media_dir=media_dir)
    except ProtocolError as exc:
        write_once(str(out_path)+".failure.json", {"schema": "dedicated-failure/1", "snapshot_sha256": doc["sha256"], "reason": str(exc)})
        raise
    finally:
        if conn is not None:
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
    # Bundle the exact frozen backbone+head and its explicit input contract.
    # This is the worker-loadable artifact, not just a weight vector evaluated
    # through a different extraction path. Persist it before reading holdout.
    from tagger.feature_contract import attach_head
    import io
    backbone = read_verified(Path(out_path).parent / "features" / "backbone-v2.json", "dedicated-backbone/2")
    model, metadata = attach_head(tensors["w"], tensors["b"], backbone)
    stream = io.BytesIO()
    torch.save({"model": model.state_dict(), "meta": metadata}, stream)
    bundle = stream.getvalue()
    bundle_path = Path(str(out_path)+".bundle.pt")
    write_bytes_once(bundle_path, bundle)
    bundle_sha = hashlib.sha256(bundle).hexdigest()
    del model, bundle, stream
    # Persist the model/threshold binding BEFORE reading any heldout predictions.
    frozen = write_once(str(out_path)+".candidate.json", {"schema": "dedicated-candidate/1", "tag": tag,
          "snapshot_sha256": doc["sha256"], "model_sha256": model_sha,
          "bundle_sha256": bundle_sha, "bundle_path": str(bundle_path),
          "threshold": threshold, "rule": "training-grouped-OOF:max_f1", "train_ids": tr,
          "backbone_sha256": backbone["sha256"],
          "calibration_manifest": manifest})
    with torch.no_grad():
        values = torch.sigmoid(torch.stack([emb[i] for i in te]) @ tensors["w"] + tensors["b"]).flatten().tolist()
    scores = dict(zip(te, values))
    return write_once(out_path, {"schema": "vnext-dedicated-measurement/1", "tag": tag,
            "snapshot_sha256": doc["sha256"], "candidate_sha256": frozen["sha256"],
            "model_sha256": model_sha, "bundle_sha256": bundle_sha,
            "threshold": threshold, "feature_contract": backbone["identity"],
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


def sample_catalog_ids(population, *, size, seed):
    """Freeze a uniform sample before looking at tags, media or scores."""
    if type(size) is not int or not 1 <= size <= 20000:
        raise ProtocolError("catalog sample size outside budget")
    ids = sorted(population)
    return sorted(random.Random(seed + 9137).sample(ids, min(size, len(ids))))


def production_score_scope(doc):
    if "catalog_sample" in doc:
        return "catalog_sample_outside_frozen_gt_groups"
    return "tag_present_outside_frozen_gt_groups"


def independent_production_frame(doc):
    """Score tag-present posts outside all frozen GT components.

    This population measures performance on unseen groups in the frozen
    tag-present domain. It cannot establish catalog-wide serving precision.
    """
    _, groups = validate_snapshot(doc)
    known_ids = {row["id"] for row in doc["gt"]}
    known_ids.update(doc["split"]["train"])
    known_ids.update(doc["split"]["heldout"])
    known_groups = {groups[post_id] for post_id in known_ids}
    if "catalog_sample" in doc:
        sample = doc["catalog_sample"]
        population = [i for i in doc["graph_nodes"] if groups[i] not in known_groups]
        excluded_ids = sample.get("excluded_ids", [])
        if (not isinstance(excluded_ids, list)
                or any(type(i) is not int for i in excluded_ids)
                or excluded_ids != sorted(set(excluded_ids))
                or not set(excluded_ids).issubset(population)):
            raise ProtocolError("catalog sample exclusion inventory mismatch")
        excluded_set = set(excluded_ids)
        if excluded_set:
            population = [i for i in population if i not in excluded_set]
        if (sample["population_n"] != len(population)
                or sample["ids"] != sample_catalog_ids(
                    population, size=sample["requested_n"], seed=doc["seed"])):
            raise ProtocolError("catalog sample membership mismatch")
        eligible, excluded = precision_frame(dict(doc, production_frame=sample["ids"]))
    else:
        eligible, excluded = precision_frame(doc)
    independent = []
    for post_id in eligible:
        if groups[post_id] in known_groups:
            excluded.append({"id": post_id, "reason": "known_label_group"})
        else:
            independent.append(post_id)
    return independent, sorted(excluded, key=lambda row: row["id"])


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
    backbone = read_verified(Path(measurement_path).parent / "features" / "backbone-v2.json", "dedicated-backbone/2")
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
        if row.get("label") not in ("positive", "negative", "uncertain") or not str(row.get("reviewer") or "").strip():
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
    if not rows:
        raise ProtocolError("human precision needs a nonempty fixed sample")
    uncertain = sum(r["label"] == "uncertain" for r in rows)
    strata = {}
    for name in ("train", "heldout", "other_GT", "unseen"):
        subset = [r for r in rows if overlap[str(r["id"])] == name]
        true = sum(r["label"] == "positive" for r in subset)
        unknown = sum(r["label"] == "uncertain" for r in subset)
        strata[name] = {"n": len(subset), "true_positive": true,
                        "precision": true / len(subset) if subset and not unknown else None}
        if uncertain:
            strata[name].update(uncertain=unknown, false_positive=len(subset)-true-unknown)
    n = len(rows)
    tp = sum(r["label"] == "positive" for r in rows)
    if uncertain:
        # Keep every sampled ID in the denominator. Human uncertainty is not a
        # negative, and dropping it would inflate precision on easy examples.
        # These are partial-identification bounds, NOT confidence intervals.
        return write_once(out_path, {
            "schema": "dedicated-human-precision/2", "sample_sha256": sample["sha256"],
            "reviews_sha256": digest(reviews), "n": n, "true_positive": tp,
            "false_positive": n-tp-uncertain, "uncertain": uncertain,
            "precision": None, "wilson_95": None,
            "precision_on_decidable": tp/(n-uncertain) if n > uncertain else None,
            "decidable_fraction": (n-uncertain)/n,
            "unresolved_precision_bounds": [tp/n, (tp+uncertain)/n],
            "bounds_are_confidence_interval": False,
            "estimand": sample["estimand"], "scope_limit": sample["scope_limit"],
            "sample_group_overlap_counts": dict(Counter(overlap.values())),
            "precision_by_overlap_diagnostic": strata,
            "full_frame_n": sample.get("full_frame_n"), "eligible_frame_n": sample.get("eligible_frame_n"),
            "excluded_media_n": len(sample.get("excluded", [])),
            "promotion": "not_requested", "registration_eligible": False,
            "status": "human_uncertainty_recorded_not_a_precision_claim"})
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
