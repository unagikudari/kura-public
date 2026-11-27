#!/usr/bin/env python3
"""Export the reviewed legacy heads ledger annotations without changing its source."""
import argparse
from collections import Counter
import hashlib
import json
import os
from pathlib import Path
import tempfile

SCHEMA = "kura-heads-legacy-archive/1"
REASONS = {
    "youth_tag", "training_opt_out", "canonical_gt_conflict", "post_unavailable",
    "legacy_evidence_unverified",
}
LABELS = {"positive", "negative"}
HUMAN = {"human_removed", "human_pool_review", "human_import"}


def digest(data):
    return hashlib.sha256(data).hexdigest()


def entries(data):
    return (json.loads(line) for line in data.splitlines())


def archive_rows(source, reviewed):
    for index, (old, mark) in enumerate(zip(entries(source), entries(reviewed), strict=True), 1):
        key = ("concept", "post_id", "label")
        if (old["schema"] != "kura-verified-gt/2" or mark["schema"] != 3 or mark["legacy_row"] != index
                or any(old[field] != mark[field] for field in key)
                or old["label"] not in LABELS or mark["eligible_for_training"] is not False):
            raise ValueError(f"legacy row {index} does not match the reviewed archive")
        human = old["source"] in HUMAN
        if not human and old["source"] != "machine_applied":
            raise ValueError(f"unsupported legacy source at row {index}")

        reasons = set(mark["reason_codes"])
        opposing = mark.get("canonical_verdict")
        conflict = "canonical_gt_conflict" in reasons
        if (reasons - REASONS or ("legacy_evidence_unverified" in reasons) != human
                or conflict != (opposing is not None)
                or conflict and (not human or opposing not in LABELS or opposing == old["label"])
                or mark["evidence"] is not None):
            raise ValueError(f"inconsistent review marks at row {index}")
        if conflict:
            disposition = "quarantined"
        elif reasons - {"legacy_evidence_unverified"}:
            disposition = "excluded"
        else:
            disposition = "unverified" if human else "machine"
        if mark["disposition"] != disposition:
            raise ValueError(f"review disposition mismatch at row {index}")

        row = {field: old[field] for field in key}
        row.update(legacy_row=index, legacy_source=old["source"], disposition=disposition,
                   reason_codes=sorted(reasons), eligible_for_training=False)
        if conflict:
            row["canonical_verdict_at_review"] = opposing
        yield row


def export(source_path, reviewed_path, report_path, output_path):
    source = source_path.read_bytes()
    reviewed = reviewed_path.read_bytes()
    report = json.loads(report_path.read_bytes())
    if digest(source) != report["source_sha256"]:
        raise ValueError("source ledger changed; obtain a new read-only review before archiving")
    rows = list(archive_rows(source, reviewed))
    counts = Counter(row["disposition"] for row in rows)
    youth = sum("youth_tag" in row["reason_codes"] for row in rows)
    youth_human = sum("youth_tag" in row["reason_codes"] and row["legacy_source"] in HUMAN for row in rows)
    optout = sum("training_opt_out" in row["reason_codes"] for row in rows)
    totals = report["totals"]
    if any(totals[key] != value for key, value in {
        "rows": len(rows), "quarantined": counts["quarantined"], "excluded": counts["excluded"],
        "unverified": counts["unverified"], "machine": counts["machine"], "youth": youth,
        "youth_old_human": youth_human, "optout": optout, "verified": 0,
    }.items()):
        raise ValueError("review report totals do not match the reviewed rows")
    header = {
        "schema": SCHEMA, "source_sha256": digest(source), "reviewed_sha256": digest(reviewed),
        "rows": len(rows), "dispositions": dict(sorted(counts.items())),
        "youth": youth, "youth_old_human": youth_human, "optout": optout,
    }
    # Same-directory staging limits access to the operator and links atomically
    # only if the output does not exist. The source is opened only for reading.
    with tempfile.NamedTemporaryFile(dir=output_path.parent, prefix=".heads-archive-", delete=False) as tmp:
        staged = Path(tmp.name)
        try:
            for row in (header, *rows):
                tmp.write((json.dumps(row, sort_keys=True) + "\n").encode())
            tmp.flush()
            os.fsync(tmp.fileno())
            if source_path.read_bytes() != source:
                raise ValueError("source ledger changed during archive export")
            os.link(staged, output_path)
        finally:
            staged.unlink()
    return header


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("source", "reviewed", "report", "output"):
        parser.add_argument(f"--{name}", type=Path, required=True)
    args = parser.parse_args()
    try:
        header = export(args.source, args.reviewed, args.report, args.output)
    except (OSError, ValueError, KeyError, TypeError) as error:
        parser.exit(1, f"archive refused: {error}\n")
    print(json.dumps(header, sort_keys=True))


if __name__ == "__main__":
    main()
