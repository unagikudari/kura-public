"""The curated-GT ledger schema (kura#64): a row must carry provenance, and only
human sources count as curated. Machine guesses and provenance-less rows are
candidates, never teachers."""
import pytest

from tagger.heads import verified_ledger as L


def _row(**over):
    base = dict(concept="phimosis", post_id=123, label="positive",
                source="human_import", actor="owner", at="2026-07-20T00:00:00")
    base.update(over)
    return L.make_row(**base)


def test_a_well_formed_human_row_is_curated():
    assert L.is_curated(_row()) is True


def test_machine_source_is_not_curated():
    r = _row(source="machine_applied", actor="machine")
    assert L.problems(r) == []          # it is a VALID row
    assert L.is_curated(r) is False     # it is just not curated


def test_missing_source_is_rejected_and_not_curated():
    r = _row()
    del r["source"]
    assert L.problems(r)                # missing source is a problem
    assert L.is_curated(r) is False


def test_unknown_schema_refuses():
    r = _row()
    r["schema"] = "kura-verified-gt/99"
    assert any("schema pin" in p for p in L.problems(r))
    assert L.is_curated(r) is False


def test_source_and_label_must_agree():
    with pytest.raises(L.LedgerError):
        L.make_row(concept="x", post_id=1, label="negative",
                   source="human_pool_review", actor="k", at="t")
    with pytest.raises(L.LedgerError):
        L.make_row(concept="x", post_id=1, label="positive",
                   source="human_removed", actor="k", at="t")


def test_post_id_must_be_positive_int():
    for bad in (0, -1, "12", 1.5, True):
        r = _row()
        r["post_id"] = bad
        assert any("post_id" in p for p in L.problems(r)), bad


def test_unknown_source_is_rejected():
    r = _row(source="human_import")
    r["source"] = "vibes"
    assert any("source" in p for p in L.problems(r))


# ── legacy migration (the backfill's core decision) ──────────────────────────
def test_legacy_machine_dup_is_demoted():
    old = {"c": "dark_labia_minora", "pid": 500, "v": "pos", "pool": 7, "t": "2026-07-20T00:00:00"}
    row = L.migrate_legacy(old, machine_applied=True, actor_for_human="owner")
    assert row["source"] == "machine_applied"
    assert L.is_curated(row) is False           # the fake-curated labia rows


def test_legacy_independent_positive_is_human_pool_review():
    old = {"c": "phimosis", "pid": 900, "v": "pos", "pool": None, "t": "2026-07-20T00:00:00"}
    row = L.migrate_legacy(old, machine_applied=False, actor_for_human="owner")
    assert row["source"] == "human_pool_review" and row["label"] == "positive"
    assert L.is_curated(row) is True


def test_legacy_independent_negative_is_human_removed():
    old = {"c": "small_penis", "pid": 901, "v": "neg", "pool": 3, "t": "2026-07-20T00:00:00"}
    row = L.migrate_legacy(old, machine_applied=False, actor_for_human="owner")
    assert row["source"] == "human_removed" and row["label"] == "negative"
    assert L.is_curated(row) is True
