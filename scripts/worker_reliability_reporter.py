#!/usr/bin/env python3
"""Worker reliability reporter — emits kind:session-stumbles on observed patterns.

The existing lesson_watcher._collect_session_stumbles() already consumes
session_log entries tagged kind:session-stumbles. This module lets
broker_cli_worker.py feed that pipeline with its own reliability observations
so that non-CC executors (codex/gemini/ollama) also contribute to LESSONS.

Design: docs/design/worker_reliability_emitter.md

PoC scope: patterns `heartbeat-self-restart` and `loop-error` only.
"""
from __future__ import annotations

import atexit
import hashlib
import json
import logging
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

log = logging.getLogger("worker_reliability")

DEFAULT_THROTTLE_SECONDS = 3600          # one post per fingerprint per hour
DEFAULT_MIN_OCCURRENCES = 1              # emit after first observation (PoC)
MAX_SAMPLE_LINES = 8                     # cap raw log lines carried in stumble body
MESSAGE_PREFIX_LEN = 80                  # canonicalized prefix length used for fingerprint


def _canonicalize(msg: str) -> str:
    """Strip volatile bits so equivalent errors hash identically."""
    s = msg.strip()
    s = re.sub(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", "<uuid>", s, flags=re.IGNORECASE)
    s = re.sub(r"\bPID=\d+\b", "PID=<n>", s)
    s = re.sub(r"\b\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z?\b", "<ts>", s)
    s = re.sub(r"\b\d+\b", "<n>", s)
    return s[:MESSAGE_PREFIX_LEN]


class WorkerReliabilityReporter:
    def __init__(
        self,
        client: Any,
        agent_id: str,
        host: str,
        executor: str,
        state_path: Path | str,
        throttle_seconds: int = DEFAULT_THROTTLE_SECONDS,
    ) -> None:
        self.client = client
        self.agent_id = agent_id
        self.host = host
        self.executor = executor
        self.state_path = Path(state_path)
        self.throttle_seconds = throttle_seconds
        self._buffer: dict[str, dict] = {}   # fingerprint -> aggregated observation
        self._state = self._load_state()
        atexit.register(self._atexit_flush)

    # ------------------------------------------------------------------
    # State persistence
    # ------------------------------------------------------------------

    def _load_state(self) -> dict:
        try:
            return json.loads(self.state_path.read_text())
        except (FileNotFoundError, json.JSONDecodeError):
            return {"last_emitted": {}}

    def _save_state(self) -> None:
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            self.state_path.write_text(json.dumps(self._state, indent=2))
        except OSError as exc:
            log.warning("failed to save reliability state: %s", exc)

    # ------------------------------------------------------------------
    # Fingerprint
    # ------------------------------------------------------------------

    def _fingerprint(self, pattern: str, message: str) -> str:
        canonical = _canonicalize(message)
        h = hashlib.sha1(f"{pattern}|{self.executor}|{self.host}|{canonical}".encode("utf-8"))
        return h.hexdigest()[:12]

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def record(self, pattern: str, message: str, context: dict | None = None) -> None:
        """Record an observation. Internally aggregates and throttles."""
        fp = self._fingerprint(pattern, message)
        now_iso = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        entry = self._buffer.get(fp)
        if entry is None:
            entry = {
                "fingerprint": fp,
                "pattern": pattern,
                "first_seen": now_iso,
                "last_seen": now_iso,
                "count": 0,
                "sample_message": message,
                "samples": [],
                "last_context": context or {},
            }
            self._buffer[fp] = entry
        entry["last_seen"] = now_iso
        entry["count"] += 1
        if len(entry["samples"]) < MAX_SAMPLE_LINES:
            entry["samples"].append(message)
        if context:
            entry["last_context"] = context

    def flush(self) -> int:
        """Emit aggregated observations whose throttle window has elapsed. Returns emit count."""
        if not self._buffer:
            return 0
        now_epoch = time.time()
        emitted = 0
        remaining: dict[str, dict] = {}
        for fp, entry in self._buffer.items():
            last_emit = self._state["last_emitted"].get(fp, 0)
            if now_epoch - last_emit < self.throttle_seconds:
                # still throttled — keep accumulating count but don't emit
                remaining[fp] = entry
                continue
            if self._emit(entry):
                self._state["last_emitted"][fp] = now_epoch
                emitted += 1
            else:
                # post failed — keep in buffer, try next flush
                remaining[fp] = entry
        self._buffer = remaining
        if emitted:
            self._save_state()
        return emitted

    # ------------------------------------------------------------------
    # Emit
    # ------------------------------------------------------------------

    def _emit(self, entry: dict) -> bool:
        ctx = entry.get("last_context") or {}
        ctx_lines = "\n".join(f"    {k}: {v}" for k, v in ctx.items()) or "    (none)"
        samples = "\n".join(f"    - {s[:200]}" for s in entry["samples"])
        content = (
            f"Worker reliability observation on {self.host}/{self.agent_id} ({self.executor})\n\n"
            f"Pattern: {entry['pattern']}\n"
            f"Fingerprint: {entry['fingerprint']}\n"
            f"First seen: {entry['first_seen']}\n"
            f"Last seen: {entry['last_seen']}\n"
            f"Occurrences in window: {entry['count']}\n"
            f"Throttle window: {self.throttle_seconds}s\n\n"
            f"Sample message: {entry['sample_message'][:400]}\n\n"
            f"Context at last observation:\n{ctx_lines}\n\n"
            f"Signals:\n{samples}\n"
        )
        payload = {
            "type": "session_log",
            "scope": "global",
            "importance": "medium",
            "source": "broker_cli_worker",
            "source_executor": self.executor,
            "host": self.host,
            "source_agent_id": self.agent_id,
            "tags": [
                "kind:session-stumbles",
                "source:worker-auto",
                f"executor:{self.executor}",
                f"host:{self.host}",
                f"pattern:{entry['pattern']}",
                f"fingerprint:{entry['fingerprint']}",
            ],
            "content": content,
        }
        try:
            self.client.create_memory(payload)
            log.info("emitted stumble pattern=%s fp=%s count=%d",
                     entry["pattern"], entry["fingerprint"], entry["count"])
            return True
        except Exception as exc:
            log.warning("emit failed (pattern=%s fp=%s): %s",
                        entry["pattern"], entry["fingerprint"], exc)
            return False

    # ------------------------------------------------------------------
    # Atexit hook
    # ------------------------------------------------------------------

    def _atexit_flush(self) -> None:
        try:
            if self._buffer:
                self.flush()
        except Exception as exc:
            log.warning("atexit flush failed: %s", exc)
