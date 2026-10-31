"""CLI subprocess state — survives worker restart.

When `broker_cli_worker --detach-cli` is enabled, long-running CLI subprocesses
(claude --print, etc.) are spawned with stdout/stderr redirected to files and
their state recorded here. On worker restart, the new worker scans for orphan
states, reattaches to still-alive subprocesses, and harvests results.

State file layout:
  /tmp/cli-task-{task_id_short}.state.json    — JSON metadata (atomically written)
  /tmp/cli-task-{task_id_short}.out           — captured stdout
  /tmp/cli-task-{task_id_short}.err           — captured stderr

State JSON schema:
{
  "task_id": "...full-uuid...",
  "agent_id": "...",
  "actor_host": "...",
  "executor": "cc",
  "pid": 12345,
  "pgid": 12345,           # for cleanup; not used by reattach
  "stdout_path": "/tmp/cli-task-26f506f9.out",
  "stderr_path": "/tmp/cli-task-26f506f9.err",
  "started_at": "2026-05-04T...Z",
  "deadline_at": "2026-05-04T...Z",
  "task_kind": "message",
  "content_preview": "first 200 chars"
}
"""
from __future__ import annotations

import json
import os
import signal
import time
from pathlib import Path

STATE_DIR = Path("/tmp")
STATE_PREFIX = "cli-task-"
STATE_SUFFIX = ".state.json"


def _short_id(task_id: str) -> str:
    return (task_id or "unknown").replace("-", "")[:12]


def state_paths(task_id: str) -> dict[str, Path]:
    short = _short_id(task_id)
    base = STATE_DIR / f"{STATE_PREFIX}{short}"
    return {
        "state": base.with_suffix(".state.json"),
        "stdout": base.with_suffix(".out"),
        "stderr": base.with_suffix(".err"),
    }


def write_task_state(state: dict) -> None:
    """Atomically write state JSON. State must contain task_id."""
    paths = state_paths(state["task_id"])
    tmp = paths["state"].with_suffix(".state.json.tmp")
    tmp.write_text(json.dumps(state, indent=2))
    os.replace(tmp, paths["state"])


def clear_task_state(task_id: str, *, keep_output: bool = False) -> None:
    """Remove state file. If keep_output, leave stdout/stderr files for forensics."""
    paths = state_paths(task_id)
    for key in ("state",) if keep_output else ("state", "stdout", "stderr"):
        try:
            paths[key].unlink()
        except FileNotFoundError:
            pass


def is_pid_alive(pid: int) -> bool:
    """Cheap liveness probe via signal 0. Returns False on PID 0/negative."""
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        # PID exists but owned by another user — still alive from kernel POV
        return True


def find_orphan_states(agent_id: str) -> list[dict]:
    """Scan state dir for state files belonging to agent_id.

    Returns list of state dicts (parsed JSON). Caller decides whether to
    reattach (pid alive) or harvest (pid dead → read output, post result).
    """
    out: list[dict] = []
    for f in STATE_DIR.glob(f"{STATE_PREFIX}*{STATE_SUFFIX}"):
        try:
            data = json.loads(f.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        if data.get("agent_id") == agent_id:
            out.append(data)
    return out


def read_captured_output(task_id: str) -> tuple[str, str]:
    """Read stdout / stderr files. Returns ("","") if missing."""
    paths = state_paths(task_id)
    stdout = ""
    stderr = ""
    try:
        stdout = paths["stdout"].read_text(errors="replace")
    except FileNotFoundError:
        pass
    try:
        stderr = paths["stderr"].read_text(errors="replace")
    except FileNotFoundError:
        pass
    return stdout, stderr
