"""The archival path preserves source bytes and fails closed on stale marks."""
import json
import stat

import pytest

from tagger.heads.archive_verified_ledger import digest, export


@pytest.fixture
def files(tmp_path):
    source, reviewed, report, output = [tmp_path / name for name in ("source", "review", "report", "archive")]
    identity = {"concept": "fixture", "post_id": 123, "label": "positive"}
    source.write_text(json.dumps(dict(identity, schema="kura-verified-gt/2", source="human_pool_review")) + "\n")
    reviewed.write_text(json.dumps(dict(
        identity, schema=3, legacy_row=1, eligible_for_training=False,
        reason_codes=["canonical_gt_conflict", "legacy_evidence_unverified", "youth_tag"],
        disposition="quarantined", canonical_verdict="negative", evidence=None,
    )) + "\n")
    report.write_text(json.dumps({
        "source_sha256": digest(source.read_bytes()),
        "totals": {"rows": 1, "quarantined": 1, "machine": 0, "excluded": 0,
                   "unverified": 0, "youth": 1, "youth_old_human": 1, "optout": 0, "verified": 0},
    }))
    return source, reviewed, report, output


def test_archives_conflict_with_youth_reason_and_preserves_original(files):
    source, _, _, output = files
    original = source.read_bytes()
    header = export(*files)
    stored_header, row = map(json.loads, output.read_text().splitlines())
    assert stored_header == header
    assert header["dispositions"] == {"quarantined": 1}
    assert row["canonical_verdict_at_review"] == "negative"
    assert "youth_tag" in row["reason_codes"] and row["eligible_for_training"] is False
    assert stat.S_IMODE(output.stat().st_mode) == 0o600
    assert source.read_bytes() == original


def test_source_change_requires_review(files):
    source, _, _, output = files
    source.write_bytes(source.read_bytes() + b"\n")
    with pytest.raises(ValueError, match="source ledger changed"):
        export(*files)
    assert not output.exists()


def test_refuses_mismatched_row_and_does_not_create_archive(files):
    _, reviewed, _, output = files
    reviewed.write_text(reviewed.read_text().replace("123", "456"))
    with pytest.raises(ValueError, match="does not match"):
        export(*files)
    assert not output.exists()


def test_existing_output_is_preserved(files):
    *_, output = files
    output.write_text("existing")
    with pytest.raises(FileExistsError):
        export(*files)
    assert output.read_text() == "existing"
