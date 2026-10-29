"""Checkpoint failure/restart contracts, without GPU or model downloads."""
import contextlib
import io
import pickle
import sys
from types import SimpleNamespace

import pytest
from PIL import Image

from tagger import vnext_arms as arms


@pytest.fixture
def torch_stub(monkeypatch):
    stub = SimpleNamespace(save=pickle.dump, load=lambda p: pickle.loads(p.read_bytes()))
    monkeypatch.setitem(sys.modules, 'torch', stub)
    return stub


def test_checkpoint_replaces_only_after_complete_serialization(tmp_path, torch_stub, monkeypatch):
    path = tmp_path / 'nested' / 'cache.pt'
    arms._save_feature_cache({1: [0.1]}, path)
    original = path.read_bytes()

    def interrupted(cache, stream):
        assert path.read_bytes() == original
        stream.write(b'incomplete')
        raise OSError('disk full')

    monkeypatch.setattr(torch_stub, 'save', interrupted)
    with pytest.raises(OSError, match='disk full'):
        arms._save_feature_cache({1: [0.1], 2: [0.2]}, path)
    assert path.read_bytes() == original
    assert list(path.parent.iterdir()) == [path]
    monkeypatch.setattr(torch_stub, 'save', pickle.dump)
    arms._save_feature_cache({1: [0.1], 2: [0.2]}, path)
    assert torch_stub.load(path) == {1: [0.1], 2: [0.2]}


@pytest.mark.parametrize('operation', ['fsync', 'replace'])
def test_failed_publication_preserves_previous_checkpoint(tmp_path, torch_stub, monkeypatch, operation):
    path = tmp_path / 'cache.pt'
    arms._save_feature_cache({1: 1}, path)

    def fail(*args):
        raise OSError('injected failure')

    monkeypatch.setattr(arms.os, operation, fail)
    with pytest.raises(OSError, match='injected failure'):
        arms._save_feature_cache({1: 1, 2: 2}, path)
    assert torch_stub.load(path) == {1: 1}
    assert list(tmp_path.iterdir()) == [path]


@pytest.fixture
def extractor(monkeypatch, tmp_path, torch_stub):
    """Use real image decoding, deterministic fake features and clock."""
    clock = [0.0]
    requested = []
    buffer = io.BytesIO()
    Image.new('RGB', (2, 2)).save(buffer, format='PNG')
    blob = buffer.getvalue()

    class Tensor(list):
        def to(self, device):
            return self
        def float(self):
            return self
        def cpu(self):
            return self

    class Backbone:
        def reset_classifier(self, count):
            pass
        def eval(self):
            return self
        def to(self, device):
            return self
        def __call__(self, images):
            clock[0] += 10.0
            return Tensor([42] * len(images))

    def query(conn, sql, args):
        requested.extend(args[0])
        return [(pid, blob) for pid in args[0]]

    torch_stub.no_grad = contextlib.nullcontext
    torch_stub.stack = Tensor
    monkeypatch.setitem(sys.modules, 'timm', SimpleNamespace(create_model=lambda *a, **kw: Backbone()))
    monkeypatch.setattr(arms, '_device', lambda: 'cpu')
    monkeypatch.setattr(arms, '_image_transform', lambda: lambda image: image)
    monkeypatch.setattr(arms, 'q', query)
    monkeypatch.setattr(arms, 'CACHE_DIR', tmp_path)
    monkeypatch.setattr(arms.time, 'monotonic', lambda: clock[0])
    monkeypatch.setenv('VNEXT_EMB_BATCH', '1')
    return requested


def test_extraction_checkpoints_periodically_and_flushes_final_batch(tmp_path, extractor, monkeypatch):
    path = tmp_path / 'features.pt'
    saves = []
    actual = arms._save_feature_cache

    def record(cache, path):
        saves.append(set(cache))
        actual(cache, path)

    monkeypatch.setattr(arms, '_save_feature_cache', record)
    result = arms._extract_features(None, list(range(7)), path, classifier_reset=True, label='test')
    assert saves == [set(range(3)), set(range(6)), set(range(7))]
    assert result == {i: 42 for i in range(7)}
    extractor.clear()
    assert arms._extract_features(None, list(range(8)), path, classifier_reset=True, label='resume') == {i: 42 for i in range(8)}
    assert extractor == [7]
    assert saves[-1] == set(range(8))


def test_interrupted_extraction_resumes_from_last_good_checkpoint(tmp_path, extractor, monkeypatch):
    path = tmp_path / 'features.pt'
    actual = arms._save_feature_cache
    attempts = []

    def fail_second(cache, path):
        attempts.append(set(cache))
        if len(attempts) == 2:
            raise OSError('interrupted checkpoint')
        actual(cache, path)

    monkeypatch.setattr(arms, '_save_feature_cache', fail_second)
    with pytest.raises(OSError, match='interrupted checkpoint'):
        arms._extract_features(None, list(range(7)), path, classifier_reset=True, label='test')
    extractor.clear()
    monkeypatch.setattr(arms, '_save_feature_cache', actual)
    result = arms._extract_features(None, list(range(7)), path, classifier_reset=True, label='resume')
    assert extractor == [3, 4, 5, 6]
    assert result == {i: 42 for i in range(7)}
