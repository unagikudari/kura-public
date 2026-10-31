"""Tests for tagger/build_release.py (plan kura-tagger-registry rev2 Step 3).

Run with a torch-capable interpreter, e.g.:
    ~/.local/share/kura-tagger/venv/bin/python3 -m pytest tests/test_build_release.py -q

`merge()` and the manifest/csv/digest helpers need no network access -- only
build_and_save() (not exercised here) downloads from the HF hub.
"""
import json
import sys
from pathlib import Path

import pytest

torch = pytest.importorskip("torch")

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from tagger import build_release as br  # noqa: E402
from tagger import registry_freeze as rf  # noqa: E402


def _base_rows(n=5):
    return [{"tag_id": i, "name": f"tag{i}", "category": 0, "count": 100} for i in range(n)]


def _stock_tensors(n=5, dim=3):
    w = torch.arange(n * dim, dtype=torch.float32).reshape(n, dim)
    b = torch.arange(n, dtype=torch.float32) * 0.1
    return w, b


def _loaded_entry(tag, version, logit_index, weight, bias, base_repo="stub/repo", base_revision="abc123"):
    entry = {"tag": tag, "version": version, "patch_sha256": "unused-in-merge"}
    meta = {"tag": tag, "version": version, "status": "approved", "logit_index": logit_index,
            "base_repo": base_repo, "base_revision": base_revision, "category": 0}
    return entry, meta, weight, bias


# ── manifest / csv / digest helpers (no network, no torch even) ─────────────

def test_read_manifest_returns_approved_list(tmp_path):
    p = tmp_path / "MANIFEST.json"
    p.write_text(json.dumps({"approved": [{"tag": "a", "version": 1, "patch_sha256": "x"}]}))
    assert br._read_manifest(p) == [{"tag": "a", "version": 1, "patch_sha256": "x"}]


def test_read_manifest_empty_when_no_approved_key(tmp_path):
    p = tmp_path / "MANIFEST.json"
    p.write_text(json.dumps({}))
    assert br._read_manifest(p) == []


def test_resolve_base_rejects_multiple_repos():
    metas = [{"base_repo": "a/x", "base_revision": None}, {"base_repo": "b/y", "base_revision": None}]
    with pytest.raises(br.BuildError, match="multiple base repos"):
        br._resolve_base(metas)


def test_resolve_base_rejects_conflicting_pinned_revisions():
    metas = [{"base_repo": "a/x", "base_revision": "r1"}, {"base_repo": "a/x", "base_revision": "r2"}]
    with pytest.raises(br.BuildError, match="conflicting base revisions"):
        br._resolve_base(metas)


def test_resolve_base_returns_repo_and_none_when_all_unpinned():
    metas = [{"base_repo": "a/x", "base_revision": None}, {"base_repo": "a/x"}]
    assert br._resolve_base(metas) == ("a/x", None)


def test_resolve_base_returns_agreed_pin():
    metas = [{"base_repo": "a/x", "base_revision": "r1"}, {"base_repo": "a/x", "base_revision": "r1"}]
    assert br._resolve_base(metas) == ("a/x", "r1")


def test_read_csv_rows_roundtrip(tmp_path):
    p = tmp_path / "selected_tags.csv"
    p.write_text("tag_id,name,category,count\n1,foo,0,10\n-1,newtag,0,0\n")
    rows = br._read_csv_rows(p)
    assert rows == [
        {"tag_id": 1, "name": "foo", "category": 0, "count": 10},
        {"tag_id": -1, "name": "newtag", "category": 0, "count": 0},
    ]


def test_patch_set_digest_is_order_independent():
    a = [{"tag": "x", "version": 1, "patch_sha256": "s1"}, {"tag": "y", "version": 1, "patch_sha256": "s2"}]
    b = list(reversed(a))
    assert br._patch_set_digest(a) == br._patch_set_digest(b)


def test_patch_set_digest_changes_when_a_patch_changes():
    a = [{"tag": "x", "version": 1, "patch_sha256": "s1"}]
    b = [{"tag": "x", "version": 2, "patch_sha256": "s1-different"}]
    assert br._patch_set_digest(a) != br._patch_set_digest(b)


# ── merge() core (torch, no network) ────────────────────────────────────────

def test_merge_update_overwrites_only_its_own_row():
    base_rows = _base_rows(5)
    stock_w, stock_b = _stock_tensors(5, 3)
    loaded = [_loaded_entry("tag2", 1, logit_index=2, weight=[9.0, 9.0, 9.0], bias=5.0)]

    new_w, new_b, merged_rows, allowed, patched, n_stock, n_out = br.merge(
        base_rows, stock_w, stock_b, loaded)

    assert n_stock == 5 and n_out == 5
    assert torch.equal(new_w[2], torch.tensor([9.0, 9.0, 9.0]))
    assert new_b[2].item() == 5.0
    # every other row must be byte-identical to stock
    for i in [0, 1, 3, 4]:
        assert torch.equal(new_w[i], stock_w[i])
        assert new_b[i].item() == stock_b[i].item()
    assert patched == {"tag2": 2}
    assert allowed == {"id:2"}
    assert merged_rows == base_rows  # no rows appended


def test_merge_add_appends_a_contiguous_new_row():
    base_rows = _base_rows(5)
    stock_w, stock_b = _stock_tensors(5, 3)
    loaded = [_loaded_entry("newtag", 1, logit_index=5, weight=[1.0, 2.0, 3.0], bias=-1.0)]

    new_w, new_b, merged_rows, allowed, patched, n_stock, n_out = br.merge(
        base_rows, stock_w, stock_b, loaded)

    assert n_out == 6
    assert merged_rows[5] == {"tag_id": -1, "name": "newtag", "category": 0, "count": 0}
    assert torch.equal(new_w[5], torch.tensor([1.0, 2.0, 3.0]))
    assert allowed == {"name:newtag"}
    for i in range(5):
        assert torch.equal(new_w[i], stock_w[i])


def test_merge_rejects_noncontiguous_add():
    base_rows = _base_rows(5)
    stock_w, stock_b = _stock_tensors(5, 3)
    # logit_index 6 skips row 5 -- not the next free row.
    loaded = [_loaded_entry("newtag", 1, logit_index=6, weight=[1.0, 2.0, 3.0], bias=-1.0)]
    with pytest.raises(br.BuildError, match="not the next free row"):
        br.merge(base_rows, stock_w, stock_b, loaded)


def test_merge_rejects_two_patches_targeting_same_row():
    base_rows = _base_rows(5)
    stock_w, stock_b = _stock_tensors(5, 3)
    loaded = [
        _loaded_entry("tag2", 1, logit_index=2, weight=[1.0, 1.0, 1.0], bias=0.0),
        _loaded_entry("tag2_impostor", 1, logit_index=2, weight=[2.0, 2.0, 2.0], bias=0.0),
    ]
    with pytest.raises(br.BuildError, match="same tag identity"):
        br.merge(base_rows, stock_w, stock_b, loaded)


def test_merge_output_passes_freeze_validation_end_to_end():
    """merge() + registry_freeze.validate_merge composed, the same way
    build_and_save() calls them -- proves the two modules agree on identity
    keys and that a correct merge is accepted."""
    base_rows = _base_rows(5)
    stock_w, stock_b = _stock_tensors(5, 3)
    loaded = [
        _loaded_entry("tag2", 1, logit_index=2, weight=[9.0, 9.0, 9.0], bias=5.0),
        _loaded_entry("newtag", 1, logit_index=5, weight=[1.0, 2.0, 3.0], bias=-1.0),
    ]
    new_w, new_b, merged_rows, allowed, patched, n_stock, n_out = br.merge(
        base_rows, stock_w, stock_b, loaded)
    base_vec = br._vectors(base_rows, stock_w, stock_b)
    merged_vec = br._vectors(merged_rows, new_w, new_b)
    rf.validate_merge(base_rows, merged_rows, base_vec, merged_vec, allowed)  # must not raise


def test_load_patch_rejects_sha256_mismatch(tmp_path):
    from safetensors.torch import save_file
    tag_dir = tmp_path / "tag"
    tag_dir.mkdir()
    save_file({"w": torch.zeros(3), "b": torch.zeros(1)}, str(tag_dir / "v1.safetensors"))
    (tag_dir / "v1.json").write_text(json.dumps({
        "tag": "tag", "version": 1, "status": "approved", "logit_index": 0,
        "base_repo": "a/x", "base_revision": None, "sha256": "wrong",
    }))
    with pytest.raises(br.BuildError, match="patch_sha256 mismatch"):
        br._load_patch("tag", 1, expected_sha256="also-wrong", patches_dir=tmp_path)


def test_load_patch_rejects_non_approved_status(tmp_path):
    from safetensors.torch import save_file
    import hashlib
    tag_dir = tmp_path / "tag"
    tag_dir.mkdir()
    st_path = tag_dir / "v1.safetensors"
    save_file({"w": torch.zeros(3), "b": torch.zeros(1)}, str(st_path))
    sha = hashlib.sha256(st_path.read_bytes()).hexdigest()
    (tag_dir / "v1.json").write_text(json.dumps({
        "tag": "tag", "version": 1, "status": "rejected", "logit_index": 0,
        "base_repo": "a/x", "base_revision": None, "sha256": sha,
    }))
    with pytest.raises(br.BuildError, match="status='rejected'"):
        br._load_patch("tag", 1, expected_sha256=sha, patches_dir=tmp_path)


def test_appended_character_row_preserves_category():
    entry = _loaded_entry('new_character', 1, 5, [1., 2., 3.], 0.)
    entry[1]['category'] = 4
    result = br.merge(_base_rows(), *_stock_tensors(), [entry])
    assert result[2][-1]['category'] == 4
    assert result[2][:-1] == _base_rows()


@pytest.mark.parametrize('category', [None, True, '4', 9, -1])
def test_appended_row_requires_explicit_supported_category(category):
    entry = _loaded_entry('new_character', 1, 5, [1., 2., 3.], 0.)
    entry[1]['category'] = category
    with pytest.raises(br.BuildError, match='category'):
        br.merge(_base_rows(), *_stock_tensors(), [entry])


def test_merge_with_no_patches_is_the_base_model():
    """--base-only: a new install's first release is the base, row for row."""
    base_rows = _base_rows(5)
    stock_w, stock_b = _stock_tensors(5, 3)
    new_w, new_b, merged_rows, allowed, patched, n_stock, n_out = br.merge(base_rows, stock_w, stock_b, [])
    assert (n_stock, n_out, patched, allowed) == (5, 5, {}, set())
    assert torch.equal(new_w, stock_w) and torch.equal(new_b, stock_b)
    assert merged_rows == base_rows


def test_a_missing_manifest_points_at_base_only(tmp_path, monkeypatch):
    monkeypatch.setattr(br, "MANIFEST_PATH", tmp_path / "MANIFEST.json")
    with pytest.raises(br.BuildError, match="--base-only"):
        br.build_and_save(tmp_path / "out")


def test_an_emptied_manifest_still_refuses_without_base_only(tmp_path, monkeypatch):
    manifest = tmp_path / "MANIFEST.json"
    manifest.write_text('{"approved": []}')
    monkeypatch.setattr(br, "MANIFEST_PATH", manifest)
    monkeypatch.setattr(br, "_read_manifest", lambda: br.json.loads(manifest.read_text())["approved"])
    with pytest.raises(br.BuildError, match="no approved entries"):
        br.build_and_save(tmp_path / "out")


def test_base_only_bundles_no_veto_heads():
    """--base-only promises the unmodified base: a host's veto-heads directory must not ride along.
    (Building for real downloads the 400 MB base, so this pins the branch in the source.)"""
    src = Path(br.__file__).read_text()
    assert "veto_heads = {} if base_only else load_veto_dir(_veto_dir)" in src
    assert "_retired = {} if base_only else _load_retired_gates()" in src
