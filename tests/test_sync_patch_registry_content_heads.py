"""publish_release registers this deployment's content heads from tagger/patches/content_heads.json;
an install without tagger/patches (the public tree) registers none — before 1.292.0 the list was
code, and publishing on a new install failed on tags it does not have."""
import asyncio
import json

import pytest

from tagger import sync_patch_registry as s


class _Conn:
    def __init__(self):
        self.rows = []

    async def execute(self, stmt, params):
        self.rows.append(params)


def test_no_patches_directory_registers_nothing(tmp_path, monkeypatch):
    monkeypatch.setattr(s, "PATCHES", tmp_path / "absent")
    monkeypatch.setattr(s, "CONTENT_HEADS", tmp_path / "absent" / "content_heads.json")
    conn = _Conn()
    assert asyncio.run(s.sync_registry(conn)) == 0 and conn.rows == []


def test_the_content_heads_file_is_registered_as_written(tmp_path, monkeypatch):
    heads = [{"tag": "some_tag", "version": 3, "ckpt": "x/some_tag-v3.pt", "metrics": {"loo_auc": 0.9},
              "gt": {"n_train": 10}, "why": "because", "created_at": "2026-07-19", "created_by": "someone"}]
    (tmp_path / "content_heads.json").write_text(json.dumps(heads))
    monkeypatch.setattr(s, "PATCHES", tmp_path)
    monkeypatch.setattr(s, "CONTENT_HEADS", tmp_path / "content_heads.json")
    monkeypatch.setattr(s, "MODELS", tmp_path / "models")
    conn = _Conn()
    assert asyncio.run(s.sync_registry(conn)) == 1
    row = conn.rows[0]
    assert (row["tag"], row["version"], row["head_type"], row["created_by"]) == ("some_tag", 3, "content_head", "someone")
    assert str(row["created_at"]) == "2026-07-19" and row["model_data"] is None


@pytest.mark.skipif(not s.CONTENT_HEADS.is_file(), reason="tagger/patches is this deployment's, not in the public tree")
def test_this_deployment_keeps_the_heads_it_registered_before():
    heads = json.loads(s.CONTENT_HEADS.read_text())
    assert [(h["tag"], h["version"]) for h in heads] == [("chastity_cage", 2), ("chastity_belt", 1)]
