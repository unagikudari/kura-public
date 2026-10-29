"""Read-only reconciler observations shared by the dashboard and agent API.

A dispatch/lifecycle transition is not evidence that a trainer is running.
The live control row takes precedence over the last completed tick's snapshot.
"""
from datetime import datetime, timezone
import json
from pathlib import Path

from sqlalchemy import text

SNAPSHOT_PATH = Path(__file__).resolve().parents[2] / "tagger/reconciler_disposition.json"
# Two 15-minute auto-cycle periods, plus timer accuracy. This is observation
# freshness, never a deadline that authorizes lifting a training restriction.
MAX_AGE_SECONDS = 32 * 60


def read_snapshot(path=SNAPSHOT_PATH, *, now=None):
    result = {"scope": "automatic_reconciler", "includes_dedicated_jobs": False,
              "observation_state": "missing", "generated_at": None,
              "age_seconds": None, "max_age_seconds": MAX_AGE_SECONDS,
              "tags": {}, "min_gt_negative": None}
    try:
        doc = json.loads(Path(path).read_text())
        stamp = datetime.fromisoformat(doc["generated_at"])
        if stamp.tzinfo is None:
            raise ValueError("timestamp must include timezone")
        tags = doc["tags"]
        if not isinstance(tags, dict) or any(
            not isinstance(row, dict) or not isinstance(row.get("disposition"), str)
            or (row.get("reason") is not None and not isinstance(row["reason"], str))
            for row in tags.values()
        ):
            raise ValueError("invalid tag dispositions")
        minimum = doc.get("min_gt_negative")
        if minimum is not None and (type(minimum) is not int or minimum < 0):
            raise ValueError("invalid minimum negative count")
        age = ((now or datetime.now(timezone.utc)) - stamp).total_seconds()
        if age < -60:
            raise ValueError("timestamp is in the future")
        result.update(observation_state="current" if age <= MAX_AGE_SECONDS else "stale",
                      generated_at=stamp.isoformat(), age_seconds=max(0, age),
                      tags=tags, min_gt_negative=minimum)
    except FileNotFoundError:
        pass
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        result["observation_state"] = "invalid"
    return result


async def progress(db, *, path=SNAPSHOT_PATH, now=None):
    result = read_snapshot(path, now=now)
    control = (await db.execute(text(
        "SELECT paused, halt_reason FROM tagger_reconciler_state WHERE id"
    ))).mappings().one_or_none()
    result.update(paused=True if control is None else bool(control["paused"]),
                  halt_reason="control_row_missing" if control is None else control["halt_reason"],
                  execution_state="unobserved")
    result["current"] = not result["paused"] and result["observation_state"] == "current"
    # Keep old tags as historical evidence, explicitly not current decisions.
    return result


def describe_decision(row):
    """Plain text only; the HTML caller must escape both known and future reasons."""
    disposition = row.get("disposition", "unknown")
    reason = row.get("reason") or "理由未記録"
    if reason == "downstream_saturated":
        return f"学習待ち行列の上限（{row.get('backlog', '?')}/{row.get('backlog_max', '?')}）"
    labels = {"decision_defer": "再開条件待ち", "decision_block": "決定により停止",
              "decision_dismiss": "対象外の決定", "dispatch_failed": "学習依頼の送信失敗",
              "in_flight": "学習依頼済・実行未確認", "dispatch": "学習依頼済・実行未確認",
              "hold": "待機", "held": "保留", "hold_noop": "保留の記録失敗"}
    reason = {"awaiting_dedicated_retrain": "専用の再学習待ち",
              "awaiting_trainer_capability": "学習機能の対応待ち",
              "dispatch_cap": "今回の依頼件数の上限",
              "insufficient_labels": "教師データ不足"}.get(reason, reason)
    return f"{labels.get(disposition, disposition)}: {reason}"
