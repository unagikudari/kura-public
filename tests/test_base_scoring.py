import json

import pytest

from tagger import base_scoring


def test_population_is_sorted_by_post_id(tmp_path):
    for name in ("10.bin", "2.bin", "100.bin", "notes.txt"):
        (tmp_path / name).write_bytes(b"x")
    assert [i for i, _ in base_scoring.population(tmp_path)] == [2, 10, 100]
    with pytest.raises(SystemExit):
        base_scoring.population(tmp_path / "empty")


def test_indices_require_every_tag_in_the_vocabulary():
    names = ["1girl", "Long_Hair", "solo"]
    assert base_scoring._indices(names, ["long_hair", "1girl"]) == {"long_hair": 1, "1girl": 0}
    with pytest.raises(SystemExit, match="not in the model vocabulary"):
        base_scoring._indices(names, ["missing_tag"])


def test_cli_refuses_to_overwrite_scores(tmp_path, monkeypatch):
    out = tmp_path / "scores.json"
    out.write_text("{}")
    tags = tmp_path / "tags.json"
    tags.write_text(json.dumps(["1girl"]))
    monkeypatch.setattr("sys.argv", ["base_scoring", "--dataset", str(tmp_path), "--incumbent-checkpoint",
                                     "x", "--candidate", "y", "--tags", str(tags), "--out", str(out)])
    with pytest.raises(SystemExit, match="exists"):
        base_scoring.main()


def test_wd_v3_transform_pads_white_square_and_swaps_to_bgr():
    from PIL import Image

    transform = base_scoring.wd_v3_transform(8, (0.5, 0.5, 0.5), (0.5, 0.5, 0.5))
    tall = Image.new("RGBA", (2, 8), (255, 0, 0, 255))  # red, opaque
    x = transform(tall)
    assert tuple(x.shape) == (3, 8, 8)
    # Padding is white (normalised to 1.0 in every channel); the centre is red,
    # which after RGB->BGR sits in channel 2.
    assert float(x[0, 0, 0]) == 1.0 and float(x[2, 4, 4]) == 1.0 and float(x[0, 4, 4]) == -1.0
    clear = Image.new("RGBA", (4, 4), (0, 0, 0, 0))  # fully transparent -> white
    assert float(base_scoring.wd_v3_transform(4, (0.5,) * 3, (0.5,) * 3)(clear).min()) == 1.0


def test_undecodable_images_are_skipped_for_both_models(tmp_path):
    from PIL import Image
    import io as _io

    good = _io.BytesIO()
    Image.new("RGB", (4, 4)).save(good, format="PNG")
    (tmp_path / "1.bin").write_bytes(good.getvalue())
    (tmp_path / "2.bin").write_bytes(b"not an image")
    (tmp_path / "x.bin").write_bytes(b"ignored name")
    keep, skipped = base_scoring.decodable(base_scoring.population(tmp_path),
                                           [base_scoring.unnormalized_transform(4)])
    assert [i for i, _ in keep] == [1]
    assert skipped == [{"post_id": 2, "error": "UnidentifiedImageError"}]


class _Model:
    """Records where it is moved; zero logits, so every score is 0.5."""

    def __init__(self):
        self.moves = []

    def to(self, device):
        self.moves.append(device)
        return self

    def __call__(self, x):
        import torch
        return torch.zeros((x.shape[0], 2))


def _images(tmp_path, n):
    from PIL import Image
    import io as _io

    for i in range(1, n + 1):
        buf = _io.BytesIO()
        Image.new("RGB", (4, 4)).save(buf, format="PNG")
        (tmp_path / f"{i}.bin").write_bytes(buf.getvalue())
    return base_scoring.population(tmp_path)


def test_model_is_on_the_device_only_for_its_own_pass(tmp_path):
    model = _Model()
    loaded = {"model": model, "transform": base_scoring.unnormalized_transform(4), "veto_heads": {}}
    scores = base_scoring.score(loaded, {"t": 1}, _images(tmp_path, 3), "cpu", 2)
    assert scores == {"t": {1: 0.5, 2: 0.5, 3: 0.5}}
    assert model.moves == ["cpu", "cpu"]  # to the device, then back


def test_a_finished_pass_is_reused_only_for_the_same_run(tmp_path):
    from tagger.dedicated_protocol import write_once

    out = tmp_path / "scores.json"
    key = base_scoring._part_key({"model_sha256": "a" * 64}, ["t"], "p" * 64)
    write_once(base_scoring._part_path(out, "incumbent"),
               {"schema": "tagger-base-scores-part/1", "key": key,
                "scores": base_scoring._json_keys({"t": {9: 0.5, 10: 0.25}})})
    assert base_scoring.resumed_part(out, "incumbent", key) == {"t": {9: 0.5, 10: 0.25}}
    other = base_scoring._part_key({"model_sha256": "b" * 64}, ["t"], "p" * 64)
    assert base_scoring.resumed_part(out, "incumbent", other) is None
    assert base_scoring.resumed_part(out, "candidate", key) is None
    base_scoring._part_path(out, "candidate").write_text("{not json")
    assert base_scoring.resumed_part(out, "candidate", key) is None
