import hashlib
import json

import pytest

from tagger import base_model


def test_repository_descriptor_pins_the_current_release_base():
    active = base_model.active()
    assert active["repo"] == "SmilingWolf/wd-swinv2-tagger-v3"
    assert active["revision"] == "627aef95638667ddcaa3ac8ae625e88ea5b02f51"
    assert set(active["files"]) >= {"selected_tags.csv", "model.safetensors"}
    # Content heads keep their own backbone whatever the release base is.
    assert base_model.CONTENT_HEAD_BACKBONE_REPO == "SmilingWolf/wd-swinv2-tagger-v3"
    assert base_model.for_repo(active["repo"])["name"] == active["name"]
    assert base_model.for_repo("someone/else") is None


def test_build_release_names_the_active_base():
    from tagger import build_release

    assert build_release.BASE_REPO == base_model.active()["repo"]


def _write(tmp_path, data):
    path = tmp_path / "base_models.json"
    path.write_text(json.dumps(data))
    return path


def test_descriptors_fail_closed(tmp_path):
    good = {"repo": "a/b", "revision": "0" * 40, "runtime": "timm", "preprocessing": "wd-v3",
            "files": {"selected_tags.csv": "1" * 64}}
    assert base_model.active(_write(tmp_path, {"active": "x", "models": {"x": good}}))["repo"] == "a/b"
    for broken, message in (
        ({"active": "y", "models": {"x": good}}, "active must name"),
        ({"active": "x", "models": {"x": good | {"revision": "main"}}}, "full commit hash"),
        ({"active": "x", "models": {"x": good | {"runtime": "onnx-unknown"}}}, "unsupported runtime"),
        ({"active": "x", "models": {"x": good | {"preprocessing": "timm-default"}}}, "preprocessing"),
        ({"active": "x", "models": {"x": good | {"files": {"model.onnx": "1" * 64}}}}, "selected_tags"),
        ({"active": "x", "models": {"x": good | {"files": {"selected_tags.csv": "short"}}}}, "sha256"),
    ):
        with pytest.raises(base_model.BaseModelError, match=message):
            base_model.load(_write(tmp_path, broken))


def test_verify_file_requires_the_pinned_bytes(tmp_path):
    payload = tmp_path / "selected_tags.csv"
    payload.write_bytes(b"tag_id,name,category,count\n")
    model = {"repo": "a/b", "revision": "0" * 40,
             "files": {"selected_tags.csv": hashlib.sha256(payload.read_bytes()).hexdigest()}}
    assert base_model.verify_file(model, "selected_tags.csv", payload) == payload
    payload.write_bytes(b"tampered\n")
    with pytest.raises(base_model.BaseModelError, match="does not match"):
        base_model.verify_file(model, "selected_tags.csv", payload)
    with pytest.raises(base_model.BaseModelError, match="not pinned"):
        base_model.verify_file(model, "model.onnx", payload)


def test_build_release_refuses_a_base_name_it_cannot_honour(tmp_path):
    pytest.importorskip("torch")
    pytest.importorskip("timm")
    from tagger import build_release

    with pytest.raises(build_release.BuildError, match="unknown base model"):
        build_release.build_and_save(tmp_path, base_only=True, base="not-a-model")
    with pytest.raises(build_release.BuildError, match="--base names the model"):
        build_release.build_and_save(tmp_path, base="wd-eva02-large-tagger-v3")
