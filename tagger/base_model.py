"""Pinned upstream base model descriptors (docs/notes/tagger-base-switch-plan.md step 1).

Two roles must not be confused:

* the **release base** -- the model whose head rows produce wd14-profile general
  tags, which logit patches and veto heads are built on. It comes from the
  ``active`` descriptor in ``tagger/base_models.json`` and is what a base switch
  replaces.
* the **content-head feature backbone** -- every content-head bundle carries its
  own frozen backbone (tagger/content_head_model.py, feature_contract.py), so it
  does not follow the release base. ``CONTENT_HEAD_BACKBONE_REPO`` names it.
"""
import hashlib
import json
import re
from pathlib import Path

DESCRIPTORS = Path(__file__).resolve().parent / "base_models.json"
CONTENT_HEAD_BACKBONE_REPO = "SmilingWolf/wd-swinv2-tagger-v3"
RUNTIMES = {"timm"}
PREPROCESSING = {"resize-totensor-unnormalized", "wd-v3"}


class BaseModelError(ValueError):
    pass


def _validate(name, model):
    if not isinstance(model, dict):
        raise BaseModelError(f"base model {name}: descriptor must be an object")
    for key in ("repo", "revision", "runtime", "files"):
        if key not in model:
            raise BaseModelError(f"base model {name}: missing {key}")
    if not re.fullmatch(r"[0-9a-f]{40}", str(model["revision"])):
        raise BaseModelError(f"base model {name}: revision must be a full commit hash")
    if model.get("preprocessing") not in PREPROCESSING:
        raise BaseModelError(f"base model {name}: preprocessing must be one of {sorted(PREPROCESSING)}")
    if model["runtime"] not in RUNTIMES:
        raise BaseModelError(f"base model {name}: unsupported runtime {model['runtime']!r}")
    files = model["files"]
    if not isinstance(files, dict) or "selected_tags.csv" not in files or not files:
        raise BaseModelError(f"base model {name}: files must pin selected_tags.csv")
    for filename, digest in files.items():
        if not re.fullmatch(r"[0-9a-f]{64}", str(digest)):
            raise BaseModelError(f"base model {name}: {filename} needs a sha256")
    return model


def load(path=DESCRIPTORS):
    data = json.loads(Path(path).read_text())
    models = data.get("models")
    if not isinstance(models, dict) or data.get("active") not in models:
        raise BaseModelError("base model descriptors: active must name a declared model")
    return {"active": data["active"],
            "models": {name: _validate(name, model) for name, model in models.items()}}


def active(path=DESCRIPTORS):
    data = load(path)
    return dict(data["models"][data["active"]], name=data["active"])


def for_repo(repo, path=DESCRIPTORS):
    """The descriptor pinning ``repo``, or None when it is not declared."""
    for name, model in load(path)["models"].items():
        if model["repo"] == repo:
            return dict(model, name=name)
    return None


def verify_file(model, filename, local_path):
    """Fail closed unless the downloaded file is byte-identical to the pin."""
    expected = model["files"].get(filename)
    if expected is None:
        raise BaseModelError(f"{model['repo']}: {filename} is not pinned")
    hasher = hashlib.sha256()
    with open(local_path, "rb") as source:
        for chunk in iter(lambda: source.read(1 << 20), b""):
            hasher.update(chunk)
    digest = hasher.hexdigest()
    if digest != expected:
        raise BaseModelError(f"{model['repo']}@{model['revision']}: {filename} sha256 {digest} "
                             f"does not match the pinned {expected}")
    return local_path
