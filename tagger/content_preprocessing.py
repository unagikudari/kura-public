"""Versioned content-head input contract; no model downloads or runtime writes.

Legacy inference and legacy feature extraction intentionally have distinct
contracts. Merely assigning new metadata to an old cache is not a migration.
"""
from __future__ import annotations
import copy
import hashlib
import inspect
import json
import math

SCHEMA = 'kura-content-preprocessing/1'
_KEYS = {'schema', 'color', 'exif', 'alpha', 'size', 'resize', 'interpolation',
         'antialias', 'tensor', 'normalization'}


def spec(size, mean=None, std=None):
    return validate({
        'schema': SCHEMA, 'color': 'RGB', 'exif': 'ignore', 'alpha': 'drop',
        'size': [size, size], 'resize': 'stretch', 'interpolation': 'bilinear',
        'antialias': True, 'tensor': {'dtype': 'float32', 'layout': 'CHW', 'range': [0, 1]},
        'normalization': None if mean is None and std is None else
            {'mean': list(mean), 'std': list(std)},
    })


def validate(value):
    if not isinstance(value, dict) or set(value) != _KEYS:
        raise ValueError('preprocessing contract: missing or unknown fields')
    if (value['schema'] != SCHEMA or value['color'] != 'RGB' or
        value['exif'] != 'ignore' or value['alpha'] != 'drop' or
        value['resize'] != 'stretch' or value['interpolation'] != 'bilinear' or
        value['antialias'] is not True or
        value['tensor'] != {'dtype': 'float32', 'layout': 'CHW', 'range': [0, 1]}):
        raise ValueError('unsupported preprocessing contract')
    size = value['size']
    if not isinstance(size, list) or len(size) != 2 or any(
        type(n) is not int or not 1 <= n <= 8192 for n in size):
        raise ValueError('invalid preprocessing size')
    norm = value['normalization']
    if norm is not None:
        if not isinstance(norm, dict) or set(norm) != {'mean', 'std'}:
            raise ValueError('invalid normalization')
        for name in ('mean', 'std'):
            v = norm[name]
            if not isinstance(v, list) or len(v) != 3 or any(
                type(n) not in (int, float) or not math.isfinite(n) for n in v):
                raise ValueError('invalid normalization vector')
        if any(n <= 0 for n in norm['std']):
            raise ValueError('normalization std must be positive')
    return copy.deepcopy(value)


def fingerprint(value):
    encoded = json.dumps(validate(value), sort_keys=True, separators=(',', ':'),
                         allow_nan=False).encode()
    return hashlib.sha256(encoded).hexdigest()


class _RGB:
    def __call__(self, image):
        from PIL import Image
        if not isinstance(image, Image.Image):
            raise TypeError('content preprocessing requires a PIL image')
        return image.convert('RGB')


def transform(value):
    """Same factory for feature extraction, bundle parity checks and serving."""
    from torchvision import transforms as T
    value = validate(value)
    # The contract is PIL-only: historical Resize defaults to bilinear and
    # PIL always antialiases it. Older torchvision lacks one/both keywords.
    # Feature-detect the constructor, not a guessed version cutoff; never
    # swallow TypeError from the transform itself or permit tensor fallback.
    kwargs = {}
    mode = getattr(T, 'InterpolationMode', None)
    if mode is not None:
        kwargs['interpolation'] = mode.BILINEAR
    if 'antialias' in inspect.signature(T.Resize).parameters:
        kwargs['antialias'] = True
    ops = [_RGB(), T.Resize(tuple(value['size']), **kwargs), T.ToTensor()]
    if value['normalization'] is not None:
        ops.append(T.Normalize(**value['normalization']))
    return T.Compose(ops)


def for_model(model):
    """Explicit metadata wins; legacy external models retain their old defaults."""
    if hasattr(model, 'preprocessing'):
        return validate(model.preprocessing)
    return spec(getattr(model, 'img_size', 384),
                getattr(model, 'mean', (0.485, 0.456, 0.406)),
                getattr(model, 'std', (0.229, 0.224, 0.225)))
