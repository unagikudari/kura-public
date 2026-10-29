"""Unit tests for tagger/registry_freeze.py (plan kura-tagger-registry rev2 Step 3).

Pure-logic tests, no torch/numpy — mirrors tests/test_lora_ops.py's convention
of testing the save-gate in isolation from the ML glue that surrounds it.
"""
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from tagger import registry_freeze as rf  # noqa: E402


def _row(tag_id, name, category=0):
    return {"tag_id": tag_id, "name": name, "category": category}


# ── identity_key / classify_op ───────────────────────────────────────────────

def test_identity_key_uses_tag_id_when_present():
    assert rf.identity_key(470575, "1girl") == "id:470575"


def test_identity_key_falls_back_to_name_for_sentinel_tag_id():
    # smegma and licking_testicle both carry tag_id=-1 in the CSV (new tags,
    # no danbooru vocabulary entry) -- identity must not collide on that.
    assert rf.identity_key(-1, "smegma") == "name:smegma"
    assert rf.identity_key(-1, "licking_testicle") == "name:licking_testicle"
    assert rf.identity_key(-1, "smegma") != rf.identity_key(-1, "licking_testicle")


def test_identity_key_falls_back_to_name_when_tag_id_none():
    assert rf.identity_key(None, "smegma") == "name:smegma"


def test_classify_op_update_vs_add():
    assert rf.classify_op(4010, n_stock=10861) == "update"   # phimosis, existing row
    assert rf.classify_op(10861, n_stock=10861) == "add"     # smegma, appended row
    assert rf.classify_op(10862, n_stock=10861) == "add"     # licking_testicle


# ── check_uniqueness ─────────────────────────────────────────────────────────

def test_uniqueness_ok_for_distinct_ids_and_sentinel_names():
    rows = [_row(1, "a"), _row(2, "b"), _row(-1, "smegma"), _row(-1, "licking_testicle")]
    rf.check_uniqueness(rows)  # must not raise


def test_uniqueness_rejects_duplicate_tag_id():
    rows = [_row(1, "a"), _row(1, "a_dup")]
    with pytest.raises(rf.FreezeViolation, match="duplicate tag identity"):
        rf.check_uniqueness(rows)


def test_uniqueness_rejects_duplicate_sentinel_name():
    # Two new-tag rows that collide on name (not distinguishable by tag_id=-1).
    rows = [_row(-1, "smegma"), _row(-1, "smegma")]
    with pytest.raises(rf.FreezeViolation, match="duplicate tag identity"):
        rf.check_uniqueness(rows)


# ── check_completeness ───────────────────────────────────────────────────────

def _base3():
    return [_row(1, "a"), _row(2, "b"), _row(3, "c")]


def test_completeness_ok_when_base_is_unchanged_prefix_plus_appends():
    base = _base3()
    merged = _base3() + [_row(-1, "newtag")]
    rf.check_completeness(base, merged)  # must not raise


def test_completeness_rejects_dropped_stock_row():
    base = _base3()
    merged = [_row(1, "a"), _row(3, "c")]  # row "b" silently dropped
    with pytest.raises(rf.FreezeViolation, match="smaller than base"):
        rf.check_completeness(base, merged)


def test_completeness_rejects_reordered_rows():
    base = _base3()
    merged = [_row(2, "b"), _row(1, "a"), _row(3, "c")]  # swapped rows 0/1
    with pytest.raises(rf.FreezeViolation, match="changed identity"):
        rf.check_completeness(base, merged)


def test_completeness_rejects_mutated_stock_row_identity():
    base = _base3()
    merged = [_row(1, "a"), _row(2, "b_renamed"), _row(3, "c")]
    with pytest.raises(rf.FreezeViolation, match="changed identity"):
        rf.check_completeness(base, merged)


# ── check_frozen_cells / check_allowlist_rows_present ───────────────────────

def _vec(w, b):
    return (tuple(w), b)


def test_frozen_cells_ok_when_only_allowed_rows_differ():
    base_vec = {"id:1": _vec([1.0, 2.0], 0.1), "id:2": _vec([3.0, 4.0], 0.2)}
    merged_vec = {"id:1": _vec([9.0, 9.0], 9.9), "id:2": _vec([3.0, 4.0], 0.2)}
    rf.check_frozen_cells(base_vec, merged_vec, allowed_keys={"id:1"})  # no raise


def test_frozen_cells_rejects_change_to_non_allowlisted_row():
    # This is the "許可外の行を書き換えたパッチを弾く" negative test: id:2 was not
    # allowlisted by any patch, but the candidate merge changed it anyway.
    base_vec = {"id:1": _vec([1.0, 2.0], 0.1), "id:2": _vec([3.0, 4.0], 0.2)}
    merged_vec = {"id:1": _vec([9.0, 9.0], 9.9), "id:2": _vec([3.0, 4.0], 0.3)}
    with pytest.raises(rf.FreezeViolation, match="freeze violated"):
        rf.check_frozen_cells(base_vec, merged_vec, allowed_keys={"id:1"})


def test_frozen_cells_rejects_missing_non_allowlisted_row():
    base_vec = {"id:1": _vec([1.0], 0.1), "id:2": _vec([2.0], 0.2)}
    merged_vec = {"id:1": _vec([1.0], 0.1)}  # id:2 vanished entirely
    with pytest.raises(rf.FreezeViolation, match="missing from merged model"):
        rf.check_frozen_cells(base_vec, merged_vec, allowed_keys={"id:1"})


def test_allowlist_rows_present_rejects_dropped_allowlisted_patch():
    merged_vec = {"id:1": _vec([1.0], 0.1)}
    with pytest.raises(rf.FreezeViolation, match="allowlisted rows missing"):
        rf.check_allowlist_rows_present({"id:1", "id:2"}, merged_vec)


# ── validate_merge (integration of the four checks) ─────────────────────────

def test_validate_merge_accepts_a_correct_patch_application():
    base_rows = _base3()
    merged_rows = _base3() + [_row(-1, "newtag")]
    base_vec = {"id:1": _vec([1.0], 0.0), "id:2": _vec([2.0], 0.0), "id:3": _vec([3.0], 0.0)}
    merged_vec = dict(base_vec)
    merged_vec["id:2"] = _vec([99.0], 9.0)          # allowlisted update
    merged_vec["name:newtag"] = _vec([5.0], 1.0)    # allowlisted add
    rf.validate_merge(base_rows, merged_rows, base_vec, merged_vec,
                       allowed_keys={"id:2", "name:newtag"})  # no raise


def test_validate_merge_rejects_id_collision_from_two_tags_targeting_same_row():
    # A malformed/malicious patch pair both claiming the same logit_index would
    # surface here as a duplicate identity in the merged row list.
    base_rows = _base3()
    merged_rows = [_row(1, "a"), _row(2, "b"), _row(2, "b_impostor")]
    base_vec = {"id:1": _vec([1.0], 0.0), "id:2": _vec([2.0], 0.0), "id:3": _vec([3.0], 0.0)}
    merged_vec = dict(base_vec)
    with pytest.raises(rf.FreezeViolation, match="duplicate tag identity"):
        rf.validate_merge(base_rows, merged_rows, base_vec, merged_vec, allowed_keys=set())
