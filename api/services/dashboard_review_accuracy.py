"""Project the locally generated read-only comparison report for the dashboard."""
from pathlib import Path

from api.services.dashboard_base_comparison import read_json

REPORT = Path.home() / ".local/state/kura/review-accuracy.json"


def read_review_accuracy(path=None):
    document = read_json(path or REPORT)
    if not isinstance(document, dict) or document.get("schema") != "tagger-review-accuracy-report/1":
        return None
    rows = document.get("sets")
    if not isinstance(rows, list) or len(rows) > 100:
        return None
    try:
        return [{
            "queue_sha256": row["queue_sha256"], "tag": row["tag"], "origin": row["origin"],
            "use": row["use"], "eligible_for_improvement_claim": row["eligible_for_improvement_claim"] is True,
            "training_overlap": row["training_overlap"], "compared": row["coverage"]["compared"],
            **{side: {key: row[side][key] for key in (
                 "false_tag_rate", "missed_tag_rate", "predicted_present", "human_present")}
               for side in ("before", "after")  },
        } for row in rows]
    except (TypeError, KeyError):
        return None
