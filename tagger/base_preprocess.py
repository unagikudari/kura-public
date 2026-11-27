"""Input preprocessing contracts for release bases (tagger/base_models.json).

One implementation shared by offline scoring (tagger/base_scoring.py), the
release checkpoint (build_release records the contract) and the wd14 worker
(tagger/nodec_autotag_worker.py applies it), so a base is deployed exactly as it
was evaluated.
"""
UNNORMALIZED = "resize-totensor-unnormalized"
WD_V3 = "wd-v3"


def unnormalized_transform(size):
    """Today's release pipeline: every logit patch was trained on it."""
    from torchvision import transforms

    pipeline = transforms.Compose([transforms.Resize((size, size)), transforms.ToTensor()])
    return lambda image: pipeline(image.convert("RGB"))


def wd_v3_transform(size, mean, std):
    """As the WD v3 taggers are published: white alpha composite, white square
    pad, bicubic resize, normalise, RGB->BGR."""
    from PIL import Image
    from torchvision import transforms

    tensor = transforms.Compose([transforms.ToTensor(), transforms.Normalize(mean, std)])

    def apply(image):
        image = image.convert("RGBA")
        canvas = Image.new("RGBA", image.size, (255, 255, 255, 255))
        canvas.alpha_composite(image)
        rgb = canvas.convert("RGB")
        side = max(rgb.size)
        square = Image.new("RGB", (side, side), (255, 255, 255))
        square.paste(rgb, ((side - rgb.width) // 2, (side - rgb.height) // 2))
        return tensor(square.resize((size, size), Image.BICUBIC))[[2, 1, 0]]
    return apply


def transform_for(contract, size, mean=None, std=None):
    """The transform a checkpoint's contract names; absent means today's pipeline."""
    contract = contract or UNNORMALIZED
    if contract == UNNORMALIZED:
        return unnormalized_transform(size)
    if contract == WD_V3:
        if mean is None or std is None:
            raise ValueError("wd-v3 preprocessing needs the model's mean and std")
        return wd_v3_transform(size, tuple(mean), tuple(std))
    raise ValueError(f"unknown preprocessing contract {contract!r}")
