import pytest

torch = pytest.importorskip("torch")
pytest.importorskip("torchvision")

from PIL import Image

from tagger import base_preprocess


def test_absent_contract_is_todays_pipeline():
    image = Image.new("RGB", (10, 4), (255, 0, 0))
    legacy = base_preprocess.transform_for(None, 8)(image)
    named = base_preprocess.transform_for("resize-totensor-unnormalized", 8)(image)
    assert torch.equal(legacy, named) and tuple(legacy.shape) == (3, 8, 8)
    assert float(legacy[0].min()) == 1.0  # unnormalised RGB, red channel first


def test_wd_v3_needs_mean_and_std_and_unknown_contracts_fail():
    with pytest.raises(ValueError, match="mean and std"):
        base_preprocess.transform_for("wd-v3", 8)
    with pytest.raises(ValueError, match="unknown"):
        base_preprocess.transform_for("timm-default", 8)
    x = base_preprocess.transform_for("wd-v3", 8, [0.5] * 3, [0.5] * 3)(Image.new("RGB", (2, 8), (255, 0, 0)))
    assert float(x[2, 4, 4]) == 1.0 and float(x[0, 4, 4]) == -1.0  # BGR order, white padding


def test_worker_applies_the_release_contract(monkeypatch):
    from tagger import nodec_autotag_worker as worker

    seen = []

    class Model:
        def __call__(self, x):
            seen.append(tuple(x.shape))
            return torch.zeros((1, 2))

    def contract(image):
        seen.append("contract")
        return torch.zeros((3, 4, 4))

    worker.infer(Model(), ["1girl", "safe"], [0, 9], 448, Image.new("RGB", (5, 5)), transform=contract)
    assert seen == ["contract", (1, 3, 4, 4)]
