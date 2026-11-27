"""Freeze a supplemental catalog population for an already measured candidate."""
import argparse
import json
from pathlib import Path
import re

from tagger import dedicated_protocol as protocol


def plan(parent, measurement, scores, additional_size):
    if type(additional_size) is not int or not 1 <= additional_size <= 20000:
        raise protocol.ProtocolError("supplemental catalog sample size outside budget")
    if "catalog_sample" not in parent:
        raise protocol.ProtocolError("supplement requires an initial catalog sample")
    if (measurement["snapshot_sha256"] != parent["sha256"]
            or scores["snapshot_sha256"] != parent["sha256"]
            or scores["measurement_sha256"] != measurement["sha256"]
            or scores["model_sha256"] != measurement["bundle_sha256"]
            or scores["threshold"] != measurement["threshold"]
            or scores["tag"] != measurement["tag"] or measurement["tag"] != parent["tag"]
            or scores["scope"] != protocol.production_score_scope(parent)):
        raise protocol.ProtocolError("supplemental catalog parent binding mismatch")
    initial_ids, exclusions = protocol.independent_production_frame(parent)
    initial_scores = dict(scores["scores"])
    if len(initial_scores) != len(scores["scores"]) or scores["exclusions"] != exclusions:
        raise protocol.ProtocolError("supplemental catalog parent population mismatch")
    protocol.validate_scores(initial_ids, initial_scores)

    _, groups = protocol.validate_snapshot(parent)
    known = {row["id"] for row in parent["gt"]}
    known.update(parent["split"]["train"])
    known.update(parent["split"]["heldout"])
    known_groups = {groups[post_id] for post_id in known}
    sampled = set(parent["catalog_sample"]["ids"])
    remaining = [post_id for post_id in parent["graph_nodes"]
                 if post_id not in sampled and groups[post_id] not in known_groups]
    seed = parent["seed"] + 22019
    return {
        "schema": "tagger-catalog-extension-plan/1",
        "tag": parent["tag"], "snapshot_sha256": parent["sha256"],
        "measurement_sha256": measurement["sha256"], "previous_scores_sha256": scores["sha256"],
        "model_sha256": scores["model_sha256"], "threshold": scores["threshold"],
        "seed": seed, "requested_n": additional_size, "remaining_population_n": len(remaining),
        "post_ids": protocol.sample_catalog_ids(remaining, size=additional_size, seed=seed),
    }


def eligible_now(conn, ids, tag):
    from api.tagger_core.post_exclusion import not_excluded
    from tagger.vnext_arms import q

    return {post_id for post_id, in q(conn, f"""
        SELECT p.id FROM post p
        WHERE p.id=ANY(%s) AND p.status='ok' AND p.type='image' AND {not_excluded('p.id')}
          AND NOT EXISTS (SELECT FROM tagger_gt_label gt WHERE gt.tag=%s AND gt.post_id=p.id)
    """, (ids, tag))}


def require_current_media(conn, ids, tag):
    if eligible_now(conn, ids, tag) != set(ids):
        raise protocol.ProtocolError("extension media is no longer eligible; preserve the existing manifest")


def freeze_media(conn, parent, selection):
    """Bind the selected posts to canonical media without replacing exclusions."""
    ids = selection["post_ids"]
    eligible = eligible_now(conn, ids, selection["tag"])
    current = {row["id"]: row for row in protocol._media_rows(conn, [i for i in ids if i in eligible])}
    previous_ids = {row["id"] for row in parent["gt"]} | set(parent["catalog_sample"]["ids"])
    previous_ids.update(parent["split"]["train"])
    previous_ids.update(parent["split"]["heldout"])
    previous_hashes = {row["blob_sha256"] for row in parent["media"] if row["id"] in previous_ids}
    rows = []
    for post_id in ids:
        row = current.get(post_id)
        if row is None:
            rows.append({"id": post_id, "blob_sha256": None, "exclusion": "no_longer_eligible"})
        elif not row["exclusion"] and row["blob_sha256"] in previous_hashes:
            rows.append(dict(row, exclusion="previous_sample_or_training_bytes"))
        else:
            rows.append(row)
    return {"schema": "tagger-catalog-extension-media/1", "plan_sha256": protocol.digest(selection),
            "media": rows}


def media_hashes(selection, media):
    if (media["plan_sha256"] != protocol.digest(selection)
            or [row["id"] for row in media["media"]] != selection["post_ids"]):
        raise protocol.ProtocolError("extension media does not match selected posts")
    hashes = {}
    for row in media["media"]:
        if row["exclusion"] is not None:
            continue
        sha = row["blob_sha256"]
        if not isinstance(sha, str) or re.fullmatch(r"[0-9a-f]{64}", sha) is None:
            raise protocol.ProtocolError("invalid extension media hash")
        hashes[row["id"]] = sha
    if not hashes:
        raise protocol.ProtocolError("extension has no eligible media")
    return hashes


def score_extension(selection, media, dataset_dir, bundle, output, *, device="cpu"):
    from functools import partial
    from tagger.dedicated_dataset import read_frozen_images
    from tagger.dedicated_qualification import score_images

    hashes = media_hashes(selection, media)
    expected = {str(post_id): sha for post_id, sha in hashes.items()}
    dataset = protocol.read_verified(dataset_dir / "dataset.json", "tagger-catalog-extension-dataset/1")
    plan_sha = protocol.digest(selection)
    if (dataset["plan_sha256"] != plan_sha or dataset["media_sha256"] != media["sha256"]
            or dataset["images"] != expected):
        raise protocol.ProtocolError("extension dataset binding mismatch")
    reader = partial(read_frozen_images, dataset_dir, expected)
    scores = score_images(bundle, selection["model_sha256"], reader, sorted(hashes), device=device)
    return protocol.write_once(output, {
        "schema": "tagger-catalog-extension-scores/1",
        "tag": selection["tag"], "plan_sha256": plan_sha, "media_sha256": media["sha256"],
        "dataset_sha256": dataset["sha256"], "model_sha256": selection["model_sha256"],
        "measurement_sha256": selection["measurement_sha256"],
        "previous_scores_sha256": selection["previous_scores_sha256"],
        "threshold": selection["threshold"], "scores": sorted(scores.items()),
    })


def review_frame(selection, media, dataset, scores, per_stratum, *, excluded=()):
    """Sample the new cohort alone, since the first cohort triggered expansion."""
    from tagger.canonical_evaluation import freeze_scored_frame

    eligible = media_hashes(selection, media)
    bindings = {
        "tag": selection["tag"], "plan_sha256": protocol.digest(selection),
        "media_sha256": media["sha256"], "dataset_sha256": dataset["sha256"],
        "model_sha256": selection["model_sha256"], "threshold": selection["threshold"],
        "measurement_sha256": selection["measurement_sha256"],
        "previous_scores_sha256": selection["previous_scores_sha256"],
    }
    if (any(scores.get(key) != value for key, value in bindings.items())
            or dataset["plan_sha256"] != bindings["plan_sha256"]
            or dataset["media_sha256"] != bindings["media_sha256"]
            or dataset["images"] != {str(post_id): sha for post_id, sha in eligible.items()}):
        raise protocol.ProtocolError("extension scores do not match their evaluation inputs")
    values = dict(scores["scores"])
    if len(values) != len(scores["scores"]):
        raise protocol.ProtocolError("duplicate supplemental scores")
    protocol.validate_scores(eligible, values)
    filtered = {pid: score for pid, score in values.items() if pid not in excluded}
    return freeze_scored_frame(selection["model_sha256"], filtered, threshold=selection["threshold"],
                               per_stratum=per_stratum, seed=selection["seed"] + 9001)


def prepare_review(selection, media, dataset, scores, *, per_stratum, minimum_predicted_positive, excluded=()):
    """Produce a bound review proposal, without making a registry write."""
    from tagger.canonical_evaluation import candidate_review_document
    from tagger.dedicated_qualification import worker_trial_inputs

    if type(minimum_predicted_positive) is not int or minimum_predicted_positive < 1:
        raise protocol.ProtocolError("supplemental review policy missing")
    frame = review_frame(selection, media, dataset, scores, per_stratum, excluded=excluded)
    predicted_positive = len(frame["sample"]["predicted_present"])
    result = {
        "schema": "tagger-catalog-extension-review/1", "scope": "catalog_extension_outside_frozen_gt_groups",
        "plan_sha256": protocol.digest(selection), "scores_sha256": scores["sha256"],
        "frame_sha256": protocol.digest(frame), "predicted_positive": predicted_positive,
        "required_predicted_positive": minimum_predicted_positive,
    }
    if predicted_positive < minimum_predicted_positive or not frame["sample"]["predicted_absent"]:
        return {**result, "state": "production_evaluation_insufficient_support"}
    request = candidate_review_document(
        tag=selection["tag"], frame=frame, display_hashes=media_hashes(selection, media),
        snapshot_sha256=selection["snapshot_sha256"], measurement_sha256=selection["measurement_sha256"],
    )
    trials = {
        "schema": "tagger-worker-trial-inputs/1",
        "snapshot_sha256": selection["snapshot_sha256"],
        "artifact_sha256": selection["model_sha256"], "threshold": selection["threshold"],
        "examples": worker_trial_inputs(media, dict(scores["scores"]), threshold=selection["threshold"]),
    }
    return {**result, "state": "review_available", "frame": frame, "request": request,
            "worker_trial_inputs": trials, "worker_trial_inputs_sha256": protocol.digest(trials)}


def registration_review(proposal):
    if proposal["state"] != "review_available":
        raise protocol.ProtocolError("supplemental review is unavailable")
    request = proposal["request"]
    return {
        "frame_sha256": proposal["frame_sha256"], "frame": proposal["frame"],
        "measurement_sha256": request["source_provenance"]["measurement_sha256"],
        "production_scores_sha256": proposal["scores_sha256"], "scope": proposal["scope"],
        "catalog_extension_plan_sha256": proposal["plan_sha256"],
        "worker_trial_inputs": proposal["worker_trial_inputs"],
        "worker_trial_inputs_sha256": proposal["worker_trial_inputs_sha256"],
        "request": request,
    }


def stage_registration_review(root, received, parent, measurement, previous_scores, floor):
    """Rebuild optional pre-staged evidence for this job before registration."""
    from functools import partial
    from tagger.dedicated_dataset import read_frozen_images
    from tagger.dedicated_qualification import stage_worker_trial_images
    from tagger.vnext_arms import connect_primary

    stored = protocol.read_verified(root / "plan.json", "tagger-catalog-extension-plan/1")
    selection = plan(parent, measurement, previous_scores, stored["requested_n"])
    if protocol.digest(selection) != stored["sha256"]:
        raise protocol.ProtocolError("staged supplemental plan disagrees with this job")
    media = protocol.read_verified(root / "media.json", "tagger-catalog-extension-media/1")
    dataset_dir = root / "dataset"
    dataset = protocol.read_verified(dataset_dir / "dataset.json", "tagger-catalog-extension-dataset/1")
    scores = protocol.read_verified(root / "extension-scores.json", "tagger-catalog-extension-scores/1")
    from tagger.review_censor_variants import excluded_for_tag

    excluded = excluded_for_tag(selection["tag"], media_hashes(selection, media))
    proposal = prepare_review(
        selection, media, dataset, scores, excluded=excluded, per_stratum=min(250, parent["parameters"]["precision_n"]),
        minimum_predicted_positive=floor,
    )
    protocol.write_once(received / "catalog-extension-review.json", proposal)
    if proposal["state"] != "review_available":
        return None
    conn = connect_primary()
    try:
        conn.autocommit = False
        conn.set_session(isolation_level="REPEATABLE READ", readonly=True)
        with conn.cursor() as cursor:
            cursor.execute("SET LOCAL statement_timeout='30s'")
        require_current_media(conn, sorted(media_hashes(selection, media)), selection["tag"])
    finally:
        conn.close()
    trial = protocol.write_once(received / "worker-trial-inputs.json", proposal["worker_trial_inputs"])
    reader = partial(read_frozen_images, dataset_dir, dataset["images"])
    stage_worker_trial_images(reader, trial, received / "worker-trial-images")
    return registration_review(proposal)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot", type=Path, required=True)
    parser.add_argument("--measurement", type=Path, required=True)
    parser.add_argument("--scores", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--additional-size", type=int, required=True)
    parser.add_argument("--media-out", type=Path)
    parser.add_argument("--export-dir", type=Path)
    parser.add_argument("--score-dataset", type=Path)
    parser.add_argument("--check-media", action="store_true")
    parser.add_argument("--bundle", type=Path)
    parser.add_argument("--scores-out", type=Path)
    parser.add_argument("--review-out", type=Path)
    parser.add_argument("--review-dataset", type=Path)
    parser.add_argument("--extension-scores", type=Path)
    parser.add_argument("--minimum-predicted-positive", type=int)
    parser.add_argument("--device", default="cpu")
    args = parser.parse_args()
    if (args.export_dir or args.check_media) and not args.media_out:
        parser.error("--export-dir and --check-media require --media-out")
    if args.score_dataset and not all((args.media_out, args.bundle, args.scores_out)):
        parser.error("--score-dataset requires --media-out, --bundle and --scores-out")
    if args.score_dataset and not args.media_out.is_file():
        parser.error("scoring requires an existing media manifest")
    if args.review_out and not all((args.media_out, args.review_dataset, args.extension_scores,
                                    args.minimum_predicted_positive)):
        parser.error("--review-out requires media, dataset, extension scores and the current policy minimum")
    parent = protocol.read_verified(args.snapshot, protocol.SCHEMA)
    document = plan(
        parent,
        protocol.read_verified(args.measurement, "vnext-dedicated-measurement/1"),
        protocol.read_verified(args.scores, "dedicated-production-scores/1"),
        args.additional_size,
    )
    receipt = protocol.write_once(args.out, document)
    if args.media_out and not args.media_out.exists():
        from tagger.vnext_arms import connect_primary

        conn = connect_primary()
        try:
            conn.autocommit = False
            conn.set_session(isolation_level="REPEATABLE READ", readonly=True)
            with conn.cursor() as cursor:
                cursor.execute("SET LOCAL statement_timeout='30s'")
            media = protocol.write_once(args.media_out, freeze_media(conn, parent, document))
            print(json.dumps({"media_sha256": media["sha256"],
                              "eligible": sum(not row["exclusion"] for row in media["media"])}))
        finally:
            conn.close()
    if args.export_dir:
        from tagger.dedicated_dataset import export_frozen_media

        media = protocol.read_verified(args.media_out, "tagger-catalog-extension-media/1")
        exported = export_frozen_media(args.export_dir, media_hashes(document, media), {
            "schema": "tagger-catalog-extension-dataset/1",
            "plan_sha256": receipt["sha256"], "media_sha256": media["sha256"],
        }, resume=True, require_current=lambda conn, ids: require_current_media(conn, ids, document["tag"]))
        print(json.dumps({"dataset_sha256": exported["sha256"], "bytes": exported["bytes"]}))
    if args.check_media:
        from tagger.vnext_arms import connect_primary

        media = protocol.read_verified(args.media_out, "tagger-catalog-extension-media/1")
        ids = sorted(media_hashes(document, media))
        conn = connect_primary()
        try:
            conn.autocommit = False
            conn.set_session(isolation_level="REPEATABLE READ", readonly=True)
            with conn.cursor() as cursor:
                cursor.execute("SET LOCAL statement_timeout='30s'")
            require_current_media(conn, ids, document["tag"])
            print(json.dumps({"currently_eligible": len(ids)}))
        finally:
            conn.close()
    if args.score_dataset:
        media = protocol.read_verified(args.media_out, "tagger-catalog-extension-media/1")
        result = score_extension(document, media, args.score_dataset, args.bundle, args.scores_out,
                                 device=args.device)
        print(json.dumps({"scores_sha256": result["sha256"], "scored": len(result["scores"]),
                          "predicted_positive": sum(v >= result["threshold"] for _, v in result["scores"])}))
    if args.review_out:
        media = protocol.read_verified(args.media_out, "tagger-catalog-extension-media/1")
        dataset = protocol.read_verified(args.review_dataset / "dataset.json", "tagger-catalog-extension-dataset/1")
        scores = protocol.read_verified(args.extension_scores, "tagger-catalog-extension-scores/1")
        evaluation = prepare_review(
            document, media, dataset, scores, per_stratum=min(250, parent["parameters"]["precision_n"]),
            minimum_predicted_positive=args.minimum_predicted_positive,
        )
        recorded = protocol.write_once(args.review_out, evaluation)
        print(json.dumps({key: recorded[key] for key in ("sha256", "state", "predicted_positive")}))
    print(json.dumps({"sha256": receipt["sha256"], "selected": len(receipt["post_ids"]),
                      "remaining_population_n": receipt["remaining_population_n"]}, sort_keys=True))
