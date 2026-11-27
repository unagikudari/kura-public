"""Qualify the saved bundle through the serving loader on the frozen holdout.

This never promotes a model. Balanced/grouped holdout precision is not production
precision; a separate reviewed production sample is still required. Baseline
training overlap is unknown, so its score is a diagnostic, not an unbiased trial.
"""
from __future__ import annotations
import argparse
import hashlib
import io
import math
from pathlib import Path
from . import dedicated_protocol as p
from .dedicated_dataset import FrozenImages
from api.tagger_core.qualification_metrics import quality_floor


def compare_predictions(reference, actual, threshold, *, atol=1e-4):
    if not math.isfinite(threshold) or not 0 < threshold < 1:
        raise p.ProtocolError('invalid frozen threshold')
    p.validate_scores(reference, reference)
    p.validate_scores(reference, actual)
    error=max((abs(reference[i]-actual[i]) for i in reference),default=0.)
    crossed=sum((reference[i]>=threshold)!=(actual[i]>=threshold) for i in reference)
    return {'passed':bool(reference) and error<=atol and crossed==0,
            'max_absolute_error':error,'decision_differences':crossed,'tolerance':atol,'n':len(reference)}


def score_production(snapshot, measurement, media_dir, out, *, device="cpu"):
    """Score the frozen production population through the actual serving loader."""
    doc = p.read_verified(snapshot, p.SCHEMA)
    result = p.read_verified(measurement, "vnext-dedicated-measurement/1")
    if result["tag"] != doc["tag"] or result["snapshot_sha256"] != doc["sha256"]:
        raise p.ProtocolError("production measurement binding mismatch")
    reader = FrozenImages(media_dir, doc)
    if not reader.includes_production:
        raise p.ProtocolError("production dataset unavailable")
    ids, exclusions = p.independent_production_frame(doc)
    if not ids:
        raise p.ProtocolError("production frame empty")
    bundle = Path(str(measurement) + ".bundle.pt")
    scores = score_images(bundle, result["bundle_sha256"], reader, ids, device=device)
    return p.write_once(out, {
        "schema": "dedicated-production-scores/1", "tag": doc["tag"],
        "snapshot_sha256": doc["sha256"], "dataset_sha256": reader.doc["sha256"],
        "measurement_sha256": result["sha256"], "model_sha256": result["bundle_sha256"],
        "threshold": result["threshold"], "scores": sorted(scores.items()),
        "exclusions": exclusions, "scope": p.production_score_scope(doc),
    })


def score_images(bundle, expected_sha256, reader, ids, *, device="cpu"):
    import torch
    from PIL import Image
    from .content_head_model import load
    from .content_preprocessing import for_model, transform

    with bundle.open("rb") as source:
        sha = hashlib.sha256()
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            sha.update(chunk)
        actual = sha.hexdigest()
    if actual != expected_sha256:
        raise p.ProtocolError("production bundle changed")
    model = load(bundle).eval().to(device)
    preprocess = transform(for_model(model))
    scores = {}
    with torch.inference_mode():
        for start in range(0, len(ids), 8):
            batch = reader(ids[start:start + 8])
            tensors = []
            for _, data in batch:
                with Image.open(io.BytesIO(data)) as image:
                    tensors.append(preprocess(image))
            values = torch.sigmoid(model(torch.stack(tensors).to(device))).reshape(-1).tolist()
            if len(values) != len(batch):
                raise p.ProtocolError("production score count mismatch")
            scores.update((pid, value) for (pid, _), value in zip(batch, values))
            completed = len(scores)
            if start == 0 or completed % 256 == 0 or completed == len(ids):
                print(f"production scoring {completed}/{len(ids)}", flush=True)
    p.validate_scores(ids, scores)
    return scores


def qualify(snapshot, measurement, media_dir, out, *, baseline=None, baseline_sha256=None,
            baseline_threshold=None, device='cpu'):
    import torch
    from PIL import Image
    from .content_head_model import load
    from .content_preprocessing import for_model,transform
    if Path(out).exists():raise p.ProtocolError('qualification output exists')
    doc=p.read_verified(snapshot,p.SCHEMA)
    result=p.read_verified(measurement,'vnext-dedicated-measurement/1')
    candidate=p.read_verified(str(measurement)+'.candidate.json','dedicated-candidate/1')
    labels,_=p.validate_snapshot(doc);ids=doc['split']['heldout']
    if (result['tag']!=doc['tag'] or candidate['tag']!=doc['tag']
        or result['snapshot_sha256']!=doc['sha256'] or candidate['snapshot_sha256']!=doc['sha256']
        or result['candidate_sha256']!=candidate['sha256']
        or result['threshold']!=candidate['threshold']
        or result['bundle_sha256']!=candidate['bundle_sha256']):
        raise p.ProtocolError('qualification binding mismatch')
    bundle=Path(str(measurement)+'.bundle.pt')
    if hashlib.sha256(bundle.read_bytes()).hexdigest()!=result['bundle_sha256']:
        raise p.ProtocolError('candidate bundle changed')
    if baseline is None:
        if baseline_sha256 is not None or baseline_threshold is not None:
            raise p.ProtocolError('baseline binding incomplete')
    else:
        if hashlib.sha256(Path(baseline).read_bytes()).hexdigest()!=baseline_sha256:
            raise p.ProtocolError('baseline bundle changed')
        if baseline_threshold is None or not math.isfinite(baseline_threshold) or not 0<baseline_threshold<1:
            raise p.ProtocolError('baseline threshold invalid')
    reader=FrozenImages(media_dir,doc)
    models={'candidate':load(bundle).eval().to(device)}
    if baseline is not None:
        models['baseline']=load(baseline).eval().to(device)
    transforms={key:transform(for_model(model)) for key,model in models.items()}
    scores={key:{} for key in models}
    for at,pid in enumerate(ids):
        data=reader([pid])[0][1]
        with Image.open(io.BytesIO(data)) as image,torch.inference_mode():
            for key,model in models.items():
                x=transforms[key](image).unsqueeze(0).to(device)
                scores[key][pid]=float(torch.sigmoid(model(x)).reshape(-1)[0].item())
        if at%50==0:print(f'qualification {at+1}/{len(ids)}',flush=True)
    reference={int(k):v for k,v in result['heldout_scores'].items()}
    if set(reference)!=set(ids) or len(reference)!=len(result['heldout_scores']):raise p.ProtocolError('heldout score membership mismatch')
    parity=compare_predictions(reference,scores['candidate'],result['threshold'])
    held_labels={i:labels[i] for i in ids}
    metrics=p.point_metrics(scores['candidate'],held_labels,result['threshold'])
    old=p.point_metrics(scores['baseline'],held_labels,baseline_threshold) if baseline is not None else None
    floor=quality_floor(metrics,doc['floor_snapshot'])
    return p.write_once(out,{'schema':'dedicated-serving-qualification/1','tag':doc['tag'],
        'snapshot_sha256':doc['sha256'],'measurement_sha256':result['sha256'],
        'bundle_sha256':result['bundle_sha256'],'baseline_sha256':baseline_sha256,
        'threshold':result['threshold'],'baseline_threshold':baseline_threshold,
        'parity':parity,'heldout':metrics,'baseline_diagnostic':old,
        'baseline_training_overlap':'unknown' if baseline is not None else 'unavailable','quality_floor':floor,
        'state':'awaiting_production_precision' if parity['passed'] and floor['passed'] else 'rejected',
        'promotion_eligible':False,'device':device,
        'scores':{key:{str(i):v for i,v in values.items()} for key,values in scores.items()}})


def main():
    import json
    ap=argparse.ArgumentParser(description=__doc__)
    for name in ('snapshot','measurement','media-dir','out'):
        ap.add_argument('--'+name,required=True)
    ap.add_argument('--baseline')
    ap.add_argument('--baseline-sha256')
    ap.add_argument('--baseline-threshold',type=float)
    ap.add_argument('--device',choices=['cpu','cuda'],default='cpu')
    a=ap.parse_args();r=qualify(**vars(a))
    print(json.dumps({'outcome':'succeeded','state':r['state'],'reason':'serving_evidence_saved',
                      'next':'production_precision_or_more_corrections','sha256':r['sha256']}))

if __name__=='__main__':main()


def worker_trial_inputs(doc, scores, *, threshold, per_side=8):
    """Freeze the closest scored examples on either side of the decision."""
    if type(per_side) is not int or not 1 <= per_side <= 32:
        raise p.ProtocolError("worker trial sample budget invalid")
    if not math.isfinite(threshold) or not 0 < threshold < 1:
        raise p.ProtocolError("worker trial threshold invalid")
    p.validate_scores(scores, scores)
    below = sorted((i for i in scores if scores[i] < threshold),
                   key=lambda i: (-scores[i], i))[:per_side]
    above = sorted((i for i in scores if scores[i] >= threshold),
                   key=lambda i: (scores[i], i))[:per_side]
    if not below or not above:
        raise p.ProtocolError("worker trial requires both prediction decisions")
    media = {row["id"]: row for row in doc["media"]}
    return [{"post_id": i, "display_sha256": media[i]["blob_sha256"],
             "reference_score": scores[i]} for i in sorted(below + above)]


def probe_full_worker(models, examples, *, tag, artifact_sha256, threshold, apply_mode):
    """Measure candidate parity through full serving inference on frozen examples.

    The caller supplies the already loaded production inventory and a bounded
    trial process. No jobs are claimed and no archive changes are made.
    """
    from .infer import tag_image

    def predict(image, mime, notes):
        tag_image(image, mime, models, notes=notes)

    return _probe_worker(predict, examples, tag=tag, artifact_sha256=artifact_sha256,
                         threshold=threshold, apply_mode=apply_mode)


def probe_wd14_worker(models, examples, *, tag, artifact_sha256, threshold, apply_mode):
    """Measure parity through the WD14 worker proposal path."""
    from .nodec_autotag_worker import predict_image
    from PIL import Image

    def predict(image, mime, notes):
        with Image.open(io.BytesIO(image)) as decoded:
            # As the worker does (fetch_image): the release contract converts the image.
            decoded.load()
            predict_image(**models, img=decoded, notes=notes)

    return _probe_worker(predict, examples, tag=tag, artifact_sha256=artifact_sha256,
                         threshold=threshold, apply_mode=apply_mode)


def _probe_worker(predict, examples, *, tag, artifact_sha256, threshold, apply_mode):
    reference, observed = {}, {}
    expected = dict(tag=tag, artifact_sha256=artifact_sha256,
                    threshold=threshold, apply_mode=apply_mode)
    for post_id, image, mime, score in examples:
        if post_id in reference:
            raise p.ProtocolError("duplicate worker trial input")
        reference[post_id] = score
        notes = {}
        predict(image, mime, notes)
        if expected in notes.get("executed_content_heads", []):
            observed[post_id] = notes["executed_content_head_scores"][tag]
    if not reference:
        raise p.ProtocolError("worker trial inputs empty")
    complete = len(observed) == len(reference)
    parity = compare_predictions(reference, observed, threshold) if complete else None
    return {"total": len(reference), "completed": len(observed),
            "parity_passed": bool(parity and parity["passed"]), "parity": parity}


def stage_worker_trial_images(reader, manifest, directory):
    """Copy only trial images from a verified dataset, with a total byte limit."""
    root = Path(directory)
    if root.is_symlink():
        raise p.ProtocolError("worker trial directory symlink")
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    total = 0
    for row in manifest["examples"]:
        post_id = row["post_id"]
        if type(post_id) is not int or post_id < 1:
            raise p.ProtocolError("worker trial post identity invalid")
        (actual_id, data), = reader([post_id])
        total += len(data)
        if len(data) > 16 * 1024**2 or total > 128 * 1024**2:
            raise p.ProtocolError("worker trial image byte budget exceeded")
        if actual_id != post_id or hashlib.sha256(data).hexdigest() != row["display_sha256"]:
            raise p.ProtocolError("worker trial image digest mismatch")
        destination = root / f"{post_id}.bin"
        if destination.is_symlink():
            raise p.ProtocolError("worker trial image symlink")
        p.write_bytes_once(destination, data)
    return total


def run_frozen_worker_trial(profile, models, manifest_path, image_dir, *,
                            inputs_sha256, artifact_sha256, threshold, tag, apply_mode):
    """Verify trial inputs and run the selected production inference path."""
    if profile not in ("full", "wd14"):
        raise p.ProtocolError("worker trial profile invalid")
    manifest = p.read_verified(manifest_path, "tagger-worker-trial-inputs/1")
    if (manifest["sha256"] != inputs_sha256 or manifest["artifact_sha256"] != artifact_sha256
            or manifest["threshold"] != threshold):
        raise p.ProtocolError("worker trial manifest binding mismatch")
    records = manifest["examples"]
    if not isinstance(records, list) or not 2 <= len(records) <= 64:
        raise p.ProtocolError("worker trial example budget invalid")
    root = Path(image_dir)
    ids = set()
    examples = []
    total_bytes = 0
    for item in records:
        post_id = item["post_id"]
        if type(post_id) is not int or post_id < 1 or post_id in ids:
            raise p.ProtocolError("worker trial post identity invalid")
        ids.add(post_id)
        path = root / f"{post_id}.bin"
        if path.is_symlink():
            raise p.ProtocolError("worker trial image symlink")
        with path.open("rb") as stream:
            data = stream.read(16 * 1024**2 + 1)
        total_bytes += len(data)
        if len(data) > 16 * 1024**2 or total_bytes > 128 * 1024**2:
            raise p.ProtocolError("worker trial image byte budget exceeded")
        if hashlib.sha256(data).hexdigest() != item["display_sha256"]:
            raise p.ProtocolError("worker trial image digest mismatch")
        examples.append((post_id, data, "image/unknown", item["reference_score"]))
    reference = {i: score for i, _, _, score in examples}
    p.validate_scores(reference, reference)
    if len({score >= threshold for score in reference.values()}) != 2:
        raise p.ProtocolError("worker trial requires both prediction decisions")
    probe = probe_full_worker if profile == "full" else probe_wd14_worker
    result = probe(models, examples, tag=tag, artifact_sha256=artifact_sha256,
                   threshold=threshold, apply_mode=apply_mode)
    return {**result, "inputs_sha256": inputs_sha256}


def load_full_trial_inventory(*, release_id, policies, api_url, headers):
    """Load a frozen candidate inventory without reporting it as deployed."""
    from . import character_policy, infer

    current = character_policy.fetch(api_url, headers)
    if current["release_id"] != release_id:
        raise p.ProtocolError("worker trial release changed")
    models = infer.load_all_models(
        api_url=api_url, headers=headers, character_threshold=current["threshold"],
        trial_head_policies=policies,
    )
    fixed = models.get("wd14_fixed")
    if not fixed or fixed.get("fallback") is not False or fixed.get("release_id") != release_id:
        raise p.ProtocolError("worker trial release unavailable")
    character_policy.require_release(current, fixed["release_id"])
    return models


def load_wd14_trial_inventory(*, release_id, policies, api_url, headers):
    """Load the WD14 proposal inventory in a dedicated trial process."""
    from . import character_policy, nodec_autotag_worker as worker
    from .content_head_policy import require_loaded

    if not worker.API or worker.API.rstrip("/") != api_url.rstrip("/"):
        raise p.ProtocolError("worker trial API mismatch")
    current = character_policy.fetch(api_url, headers)
    if current["release_id"] != release_id:
        raise p.ProtocolError("worker trial release changed")
    worker._resolve_device()
    heads = worker.load_content_heads(
        policies, artifact_fetcher=lambda sha: worker._fetch_artifact(
            sha, api_url=api_url, headers=headers),
    )
    require_loaded(heads, policies)
    worker._CHARACTER_RELEASE_POLICY = current
    if current["threshold"] is None:
        worker.refresh_pixai_chars()
    else:
        worker.refresh_pixai_chars(character_threshold=current["threshold"])
    worker.SKIN_MODEL, worker.SKIN_MODEL_SHA = worker.load_skin_model()
    state = worker.ModelState()
    worker.refresh_model(state, trial_release_id=release_id)
    character_policy.require_release(current, state.release_id)
    return dict(model=state.model, names=state.names, cats=state.cats,
                size=state.img_size, veto_heads=state.veto_heads,
                content_heads=heads, patched_tags=state.release_reap,
                transform=getattr(state, "transform", None))
