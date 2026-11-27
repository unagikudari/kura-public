"""Read the registered EVA02 comparison without importing the model worker."""
import asyncio
import json
import math
from pathlib import Path
import re

from sqlalchemy import text

from api.services.human_review import digest
from api.services.review_queues import ReviewQueueRefused
from api.services.review_set_registry import evaluation_answers

ROOT = Path.home() / ".local/state/kura"
REF = re.compile(r"[0-9a-f]{64}")


def read_json(path):
    try:
        with path.open("rb") as source:
            value = source.read(1024 * 1024 + 1)
        if len(value) <= 1024 * 1024:
            return json.loads(value)
    except (OSError, ValueError):
        pass
    return None


def read_verdict(path, answer_digests):
    doc = read_json(path)
    if not isinstance(doc, dict):
        return None
    try:
        if (doc["schema"] != "tagger-base-switch-verdict/1"
                or doc["answers_sha256"] != answer_digests):
            return None
        sha = doc.pop("sha256")
        if sha != digest(doc):
            return None
        verdict = doc["verdict"]
        reasons = verdict["reasons"]
        if (type(verdict["admissible"]) is not bool or not isinstance(reasons, list)
                or any(not isinstance(reason, str) for reason in reasons)):
            return None
        results = {}
        for row in doc["results"]:
            tag = row["tag"]
            delta = row["difference"]["f1"]
            if (tag in results or row["complete"] is not True
                    or type(row["significant_regression"]) is not bool
                    or (delta is not None and (type(delta) not in (int, float) or not math.isfinite(delta)))):
                return None
            results[tag] = dict(f1_change=delta, regression=row["significant_regression"])
        if results.keys() != answer_digests.keys():
            return None
        return dict(admissible=verdict["admissible"], reasons=reasons, tags=results)
    except (KeyError, TypeError, ValueError, AttributeError):
        return None


async def read_comparison(db, root=None):
    if root is None:
        root = ROOT
    references = await asyncio.to_thread(read_json, root / "eva02-r3-references.json")
    if (not isinstance(references, dict) or len(references) != 20
            or any(not isinstance(ref, str) or not REF.fullmatch(ref) for ref in references.values())
            or len(set(references.values())) != 20):
        return dict(state="unavailable")
    registered = (await db.execute(text("""
        SELECT n.name, q.queue_sha256 FROM tagger_review_set q
        JOIN tag_name n ON n.tag_id=q.tag_id AND n.ord=0
        WHERE q.purpose='base_comparison' AND q.queue_sha256=ANY(:refs)
    """), {"refs": list(references.values())})).all()
    if dict(registered) != references:
        return dict(state="unavailable")
    rows = []
    answers_sha = {}
    for tag, ref in sorted(references.items()):
        try:
            snapshot = await evaluation_answers(db, ref)
        except ReviewQueueRefused:
            return dict(state="unavailable")
        if not snapshot:
            return dict(state="unavailable")
        answers = snapshot["answers"]
        answers_sha[tag] = snapshot["sha256"]
        rows.append(dict(tag=tag, answered=sum(a["decision"] in ("present", "absent") for a in answers),
                         total=len(answers)))
    answered = sum(row["answered"] for row in rows)
    total = sum(row["total"] for row in rows)
    complete = total > 0 and answered == total
    verdict = (await asyncio.to_thread(read_verdict, root / "eva02-r3-verdict.json", answers_sha)
               if complete else None)
    return dict(state="available", answered=answered, total=total, tags=rows,
                complete=complete, verdict=verdict)
