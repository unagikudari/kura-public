"""Score a frozen image population with the incumbent release and a candidate base
(docs/notes/tagger-base-switch-plan.md step 3; offline, GPU node, read-only).

Fairness rules:

* The incumbent is scored exactly as wd14-profile production does
  (tagger/nodec_autotag_worker.infer): the release checkpoint, its own tag list,
  Resize to img_size + ToTensor with no normalisation, sigmoid, and the
  release's gated vetoes (a vetoed tag scores 0).
* The candidate is scored with the preprocessing contract pinned in its
  descriptor (tagger/base_models.json), from weights loaded from the verified
  safetensors file -- i.e. as it would be deployed.
* Both vocabularies are checked before any scoring; an image either model's
  pipeline cannot decode is skipped for both and recorded.

Only the requested tags are kept; tagger/base_comparison.py builds frames.

GPU memory (nodeb, 2026-11-24): the card is shared with the production worker
and other services, so a model is on the device only during its own pass and
the batch is small; holding both models thrashed evictions to ~2 images/min.
Each finished pass is saved next to ``--out`` (``<out>.<role>.part``) and
reused on a rerun only when its identity, tags and population all match.
"""
import argparse
import csv
import hashlib
import io
import json
import sys
import time
from pathlib import Path

SCHEMA = "tagger-base-scores/2"


def population(dataset):
    """Frozen display bytes, one file per post id (``<id>.bin``); other names are ignored."""
    files = sorted((p for p in Path(dataset).glob("*.bin") if p.stem.isdigit()), key=lambda p: int(p.stem))
    if not files:
        raise SystemExit(f"no images in {dataset}")
    return [(int(p.stem), p) for p in files]


def _indices(names, tags):
    index = {}
    for i, name in enumerate(names):
        index.setdefault(name.lower(), i)
    missing = sorted(set(tags) - set(index))
    if missing:
        raise SystemExit(f"tags not in the model vocabulary: {missing[:10]}")
    return {tag: index[tag] for tag in tags}


def _sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as source:
        for chunk in iter(lambda: source.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


from tagger.base_preprocess import unnormalized_transform, wd_v3_transform  # noqa: E402,F401


def load_incumbent(checkpoint, device):
    import timm
    import torch

    checkpoint = Path(checkpoint)
    ckpt = torch.load(str(checkpoint), map_location="cpu", weights_only=False)
    model = timm.create_model(f"hf-hub:{ckpt['repo']}", pretrained=False, num_classes=ckpt["n_out"])
    model.load_state_dict(ckpt["state_dict"])
    tags_csv = checkpoint.with_name("selected_tags.csv")
    names = [row["name"] for row in csv.DictReader(open(tags_csv))]
    if len(names) != ckpt["n_out"]:
        raise SystemExit(f"release tag list has {len(names)} rows, checkpoint n_out {ckpt['n_out']}")
    meta_path = checkpoint.with_name("meta.json")
    meta = json.loads(meta_path.read_text()) if meta_path.exists() else {}
    size = ckpt.get("img_size", 448)
    return {
        "model": model.eval(), "names": names,
        "transform": unnormalized_transform(size), "veto_heads": ckpt.get("veto_heads") or {},
        "identity": {"model_sha256": _sha256(checkpoint), "release_id": meta.get("release_id"),
                     "selected_tags_sha256": _sha256(tags_csv), "repo": ckpt["repo"],
                     "revision": ckpt.get("revision"), "veto_heads": sorted(ckpt.get("veto_heads") or {}),
                     "preprocessing": "resize-totensor-unnormalized (wd14 production)"},
    }


def load_candidate(name, device):
    import timm
    from huggingface_hub import hf_hub_download
    from safetensors.torch import load_file
    from tagger import base_model

    descriptor = dict(base_model.load()["models"][name], name=name)
    paths = {filename: base_model.verify_file(descriptor, filename, hf_hub_download(
        descriptor["repo"], filename, revision=descriptor["revision"])) for filename in descriptor["files"]}
    model = timm.create_model(f"hf-hub:{descriptor['repo']}@{descriptor['revision']}", pretrained=False)
    model.load_state_dict(load_file(str(paths["model.safetensors"])))
    names = [row["name"] for row in csv.DictReader(open(paths["selected_tags.csv"]))]
    config = model.pretrained_cfg
    size = config["input_size"][-1]
    from tagger.base_preprocess import transform_for
    transform = transform_for(descriptor["preprocessing"], size, config.get("mean"), config.get("std"))
    return {
        "model": model.eval(), "names": names, "transform": transform, "veto_heads": {},
        "identity": {"model_sha256": descriptor["files"]["model.safetensors"], "repo": descriptor["repo"],
                     "revision": descriptor["revision"], "preprocessing": descriptor["preprocessing"],
                     "input_size": size},
    }


def decodable(images, transforms):
    """Post ids every pipeline can decode; the rest are skipped for both models."""
    from PIL import Image

    keep, skipped = [], []
    for post_id, path in images:
        try:
            with Image.open(io.BytesIO(path.read_bytes())) as image:
                image.load()
                for transform in transforms:
                    transform(image)
            keep.append((post_id, path))
        except Exception as exc:  # noqa: BLE001 - recorded, never silently dropped
            skipped.append({"post_id": post_id, "error": type(exc).__name__})
    return keep, skipped


def score(loaded, indices, images, device, batch, *, label="", log_every=50):
    """Scores with the model on ``device`` only for this pass; it goes back to the CPU after."""
    import torch

    model, transform, vetoes = loaded["model"].to(device), loaded["transform"], loaded["veto_heads"]
    try:
        return _score(model, transform, vetoes, indices, images, device, batch, label, log_every)
    finally:
        loaded["model"] = model.to("cpu")
        if str(device).startswith("cuda"):
            torch.cuda.empty_cache()


def _score(model, transform, vetoes, indices, images, device, batch, label, log_every):
    import torch
    from PIL import Image

    out = {tag: {} for tag in indices}
    columns = list(indices.values())
    started = time.monotonic()
    for number, start in enumerate(range(0, len(images), batch), 1):
        if number % log_every == 0:
            done = start
            rate = done / max(time.monotonic() - started, 1e-9)
            print(f"{label}: {done}/{len(images)} images, {rate:.2f}/s", file=sys.stderr, flush=True)
        chunk = images[start:start + batch]
        x = torch.stack([transform(Image.open(io.BytesIO(path.read_bytes()))) for _, path in chunk]).to(device)
        with torch.no_grad():
            if vetoes:
                features = model.forward_features(x)
                pooled = model.forward_head(features, pre_logits=True)
                logits = model.forward_head(features)
            else:
                pooled, logits = None, model(x)
            probs = torch.sigmoid(logits.float())
        for row, (post_id, _) in enumerate(chunk):
            values = probs[row, columns].cpu().tolist()
            for tag, value in zip(indices, values):
                if tag in vetoes:
                    from tagger.veto_apply import run_veto
                    if run_veto(pooled[row].cpu(), vetoes[tag]) < vetoes[tag]["threshold"]:
                        value = 0.0  # production cancels the tag
                out[tag][post_id] = round(float(value), 6)
    return out


def _part_path(out, role):
    return Path(f"{out}.{role}.part")


def _part_key(identity, tags, population_sha256):
    return {"identity": identity, "tags": tags, "population_sha256": population_sha256}


def _json_keys(scores):
    """Post ids as strings, as JSON stores them: the evidence digest sorts keys,
    and int and str ids sort differently (9 < 10 but "10" < "9")."""
    return {tag: {str(post_id): value for post_id, value in values.items()} for tag, values in scores.items()}


def resumed_part(out, role, key):
    """Scores of a finished pass, only when it was made for exactly this run."""
    from tagger.dedicated_protocol import ProtocolError, read_verified

    path = _part_path(out, role)
    if not path.exists():
        return None
    try:
        doc = read_verified(path, "tagger-base-scores-part/1")
    except (ProtocolError, ValueError):
        return None
    if doc.get("key") != key:
        return None
    return {tag: {int(post_id): value for post_id, value in values.items()}
            for tag, values in doc["scores"].items()}


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dataset", required=True)
    parser.add_argument("--incumbent-checkpoint", required=True, help="active release ckpt (wd14-fixed.pt)")
    parser.add_argument("--candidate", required=True, help="descriptor name in tagger/base_models.json")
    parser.add_argument("--tags", required=True, help="JSON list of tags to keep")
    parser.add_argument("--device", default="cuda")
    parser.add_argument("--batch", type=int, default=2)
    parser.add_argument("--out", required=True)
    args = parser.parse_args()
    if Path(args.out).exists():
        raise SystemExit(f"{args.out} exists; scores are written once")
    tags = sorted({t.lower() for t in json.loads(Path(args.tags).read_text())})
    incumbent = load_incumbent(args.incumbent_checkpoint, args.device)
    candidate = load_candidate(args.candidate, args.device)
    indices = {role: _indices(loaded["names"], tags)
               for role, loaded in (("incumbent", incumbent), ("candidate", candidate))}
    images, skipped = decodable(population(args.dataset), [incumbent["transform"], candidate["transform"]])
    result = {"schema": SCHEMA, "tags": tags, "skipped": skipped,
              "population_sha256": hashlib.sha256(json.dumps([i for i, _ in images]).encode()).hexdigest(),
              "models": {}}
    from tagger.dedicated_protocol import write_once
    for role, loaded in (("incumbent", incumbent), ("candidate", candidate)):
        key = _part_key(loaded["identity"], tags, result["population_sha256"])
        scores = resumed_part(args.out, role, key)
        if scores is None:
            scores = score(loaded, indices[role], images, args.device, args.batch, label=role)
            part = _part_path(args.out, role)
            part.unlink(missing_ok=True)  # a stale part for another run
            write_once(part, {"schema": "tagger-base-scores-part/1", "key": key, "scores": _json_keys(scores)})
        else:
            print(f"{role}: reusing {_part_path(args.out, role)}", file=sys.stderr, flush=True)
        result["models"][role] = dict(loaded["identity"], scores=_json_keys(scores))
    write_once(args.out, result)
    print(json.dumps({"out": args.out, "images": len(images), "skipped": len(skipped), "tags": len(tags)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
