"""Identity of the canonical ground-truth rows used for learning decisions."""

import hashlib
import json
import os
from collections import Counter
from pathlib import Path


def label_identity(rows):
    payload = json.dumps(sorted(rows), separators=(',', ':'))
    return hashlib.sha256(b'kura-gt-labels/1\n' + payload.encode('ascii')).hexdigest()


def labels_changed(previous, current):
    """An unknown historical identity cannot establish a change."""
    return (isinstance(previous, str) and len(previous) == 64
            and all(c in "0123456789abcdef" for c in previous)
            and previous != current)


def registration_label_identity(tag, registration, *, root=None):
    """Recover a legacy baseline only from the registration's exact frozen snapshot."""
    gt = registration["gt"] or {}
    previous = gt.get("label_sha256")
    if previous is not None:
        return previous
    event = registration["lifecycle_event_id"]
    pin = (registration["metrics"] or {}).get("snapshot_sha256")
    if type(event) is not int or event < 1 or not isinstance(pin, str):
        return None

    from .learning_artifacts import SNAPSHOT_SCHEMA, read_verified

    if root is None:
        state = Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local/state")
        root = state / "kura-tagger" / "learning-jobs"
    key = hashlib.sha256(f"{tag}@{event}".encode()).hexdigest()[:24]
    try:
        doc = read_verified(root / key / "snapshot.json", SNAPSHOT_SCHEMA)
        if doc["sha256"] != pin or doc["tag"] != tag or doc["status"] != "prepared":
            return None
        rows = [(r["id"], r["verdict"], r["derived_from"]) for r in doc["gt"]]
        counts = Counter(verdict for _, verdict, _ in rows)
        if any(type(gt.get(v)) is not int or gt[v] != counts[v] for v in ("positive", "negative")):
            return None
        return label_identity(rows)
    except (OSError, ValueError, TypeError, KeyError):
        return None


async def read_label_rows(db, tag):
    from sqlalchemy import text
    from api.tagger_core.post_exclusion import training_label

    result = await db.execute(text(
        "SELECT post_id, verdict, derived_from FROM tagger_gt_label g WHERE tag = :tag AND " + training_label("g")
    ), {"tag": tag})
    return [tuple(row) for row in result.all()]
