#!/usr/bin/env python3
# Description: resumability primitives for the v-next deterministic runner (idempotency / ledger / dead-letter)
# Kind: library
# Effects: filesystem only (atomic JSON state files under a caller-supplied dir); no DB, no network
# Runs-on: trainer (consumed by tagger/vnext_runner.py; testable anywhere)
"""tagger/vnext_state.py — Step 6 (adopted finding C5) building blocks for
plan tagger-vnext-deterministic-runner.

The runner itself (Step 10) is out of this change's scope; these are the
pure/small pieces it will compose:

  - IdempotencyKey: (tag, tagger_lifecycle_event id). INV-R11 — the kura
    lifecycle is the ONE source of truth and the broker task is a redeliverable
    transport, so the same dispatch delivered twice must measure once. The
    lifecycle_event id (not the broker task id) identifies the *dispatch
    decision*; a re-delivery of the same decision carries the same id.
  - ordered_work_items: deterministic processing order (tag ascending, then
    event id) so two runners / two ticks agree on sequence.
  - should_noop: re-delivery rule — re-read the kura stage; anything but
    'training' means the lifecycle has moved on and the task completes as a
    no-op (INV-R11).
  - AttemptLedger: per-key attempt history + done-marker + dead-letter, one
    JSON file written atomic-replace (tmp + os.replace, same flavour as
    reconciler_disposition.json) so a crash mid-write can never leave a
    truncated ledger that forgets an attempt.

Per-item measurement *checkpoints* are deliberately NOT a new store: the
post-id-keyed emb/logit/pixai caches in vnext_arms.py already are the
checkpoint (plan Step 6: 「post_id キーの既存キャッシュを per-item checkpoint
とする」) — an interrupted measurement re-runs and skips everything cached.
"""
from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Sequence

# After this many recorded attempts for one idempotency key, the item goes to
# dead-letter instead of being retried (plan Step 6: 上限超過は E6 経由で
# dead-letter — the escalation predicate itself lives in vnext_escalate.py,
# Step 17, out of this change's scope). Ops knob, not an ML threshold, so a
# module constant (same call as reconciler.STALE_TRAINING_MINUTES).
MAX_ATTEMPTS_DEFAULT = 3


@dataclass(frozen=True, order=True)
class IdempotencyKey:
    """Identity of one dispatch decision: (tag, lifecycle_event id).

    Frozen + ordered so it can key dicts and sort deterministically.
    """
    tag: str
    lifecycle_event_id: int

    def as_str(self) -> str:
        return f"{self.tag}:{self.lifecycle_event_id}"

    @classmethod
    def from_str(cls, s: str) -> "IdempotencyKey":
        tag, _, ev = s.rpartition(":")
        if not tag:
            raise ValueError(f"not an idempotency key: {s!r}")
        return cls(tag=tag, lifecycle_event_id=int(ev))


def ordered_work_items(items: Iterable[IdempotencyKey]) -> list[IdempotencyKey]:
    """Deterministic processing order: tag ascending, then event id. Plan
    Step 6 fixes tag-ascending so any two runs drain a backlog identically."""
    return sorted(items)


def should_noop(current_stage: str | None) -> bool:
    """Re-delivery rule (INV-R11): a claimed task whose tag is no longer in
    stage 'training' must complete as a no-op — the lifecycle (the single
    source of truth) has moved on, and measuring anyway would act on a
    decision that no longer exists. None (no ledger history) is also a no-op:
    a dispatch always records candidate->training first, so a missing stage
    means this delivery is orphaned."""
    return current_stage != "training"


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def atomic_write_json(path: Path, doc: dict) -> None:
    """Atomic-replace JSON write: tmp file in the SAME directory (os.replace
    must not cross filesystems) + fsync + replace. A reader never observes a
    truncated file; a crash leaves either the old or the new version."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(doc, f, ensure_ascii=False, indent=1, sort_keys=True)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


class AttemptLedger:
    """Attempt history + completion + dead-letter for idempotent work items.

    One JSON document:
      {"items": {"<tag>:<event_id>": {
          "attempts": [{"at": iso, "outcome": "...", "detail": ...}, ...],
          "done": bool,          # a completed measurement -- never re-run
          "dead": null | {"at": iso, "reason": "..."}}}}

    Every mutation persists immediately via atomic replace. The ledger is
    intentionally not a queue (rejected alternative K: the broker inbox IS the
    queue); it only answers "have I finished / how often have I tried / did I
    give up on this key".
    """

    def __init__(self, path: Path | str, max_attempts: int = MAX_ATTEMPTS_DEFAULT):
        self.path = Path(path)
        self.max_attempts = int(max_attempts)
        self._doc: dict = {"items": {}}
        if self.path.exists():
            self._doc = json.loads(self.path.read_text(encoding="utf-8"))
            self._doc.setdefault("items", {})

    # -- internals ---------------------------------------------------------
    def _item(self, key: IdempotencyKey) -> dict:
        return self._doc["items"].setdefault(
            key.as_str(), {"attempts": [], "done": False, "dead": None})

    def _persist(self) -> None:
        atomic_write_json(self.path, self._doc)

    # -- queries -----------------------------------------------------------
    def attempts(self, key: IdempotencyKey) -> int:
        it = self._doc["items"].get(key.as_str())
        return len(it["attempts"]) if it else 0

    def is_done(self, key: IdempotencyKey) -> bool:
        it = self._doc["items"].get(key.as_str())
        return bool(it and it.get("done"))

    def is_dead(self, key: IdempotencyKey) -> bool:
        it = self._doc["items"].get(key.as_str())
        return bool(it and it.get("dead"))

    def should_attempt(self, key: IdempotencyKey) -> bool:
        """One gate for the runner: attempt only when not done, not
        dead-lettered, and under the attempt cap. This is what makes a
        double-delivered (tag, event id) measure exactly once."""
        return not (self.is_done(key) or self.is_dead(key)
                    or self.attempts(key) >= self.max_attempts)

    # -- mutations (each persists atomically) ------------------------------
    def record_attempt(self, key: IdempotencyKey, outcome: str, detail=None) -> None:
        it = self._item(key)
        it["attempts"].append({"at": _utcnow(), "outcome": outcome, "detail": detail})
        self._persist()

    def last_detail(self, key: IdempotencyKey) -> dict:
        """直近の attempt の detail (無ければ空 dict)。

        hold_streak / gt_bucket のような「前回の判定で分かったこと」を
        ledger 以外の第二の状態ファイルに持たないための入口 (E6 の
        hysteresis が読む)。attempts は append-only なので履歴は失われない。
        """
        for att in reversed(self._item(key)["attempts"]):
            if isinstance(att.get("detail"), dict):
                return att["detail"]
        return {}

    def mark_done(self, key: IdempotencyKey) -> None:
        self._item(key)["done"] = True
        self._persist()

    def dead_letter(self, key: IdempotencyKey, reason: str) -> None:
        """Park a key permanently (attempt cap exhausted, or a structurally
        unprocessable item). Dead-lettering is loud-by-record: the entry stays
        in the ledger for the escalation path to report; it is never silently
        dropped (INV-R3 flavour)."""
        self._item(key)["dead"] = {"at": _utcnow(), "reason": reason}
        self._persist()

    def dead_letters(self) -> dict[str, dict]:
        return {k: v["dead"] for k, v in self._doc["items"].items() if v.get("dead")}


def run_once(ledger: AttemptLedger, key: IdempotencyKey, current_stage: str | None,
             work) -> str:
    """Idempotent execution wrapper the runner (Step 10) will use per item.

    Returns one of: 'noop-stage' (lifecycle moved on -- INV-R11 re-delivery
    rule), 'noop-done' (already measured), 'dead' (previously dead-lettered or
    cap now exhausted), 'ok', 'failed'. `work` is a zero-arg callable that
    performs the measurement; its exception marks a failed attempt (and
    dead-letters on cap exhaustion) but is re-raised for the caller's
    escalation path -- this wrapper decides bookkeeping, not policy.
    """
    if should_noop(current_stage):
        return "noop-stage"
    if ledger.is_done(key):
        return "noop-done"
    if ledger.is_dead(key):
        return "dead"
    if ledger.attempts(key) >= ledger.max_attempts:
        ledger.dead_letter(key, f"attempt cap {ledger.max_attempts} exhausted")
        return "dead"
    try:
        work()
    except BaseException as e:
        ledger.record_attempt(key, "failed", detail=repr(e))
        if ledger.attempts(key) >= ledger.max_attempts:
            ledger.dead_letter(key, f"attempt cap {ledger.max_attempts} exhausted: {e!r}")
        raise
    ledger.record_attempt(key, "ok")
    ledger.mark_done(key)
    return "ok"
