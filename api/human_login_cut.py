"""Bounded live source custody for the one-time human credential import.

Configuration names the complete reviewed source inventory, never a stored
snapshot or caller-supplied frozen boolean. Each child keeps its SHARE lock and
rechecks stopped unit/file custody until the target transaction has finished.
"""
from __future__ import annotations
import json
import hashlib
import time
import os
from pathlib import Path
import select
import subprocess
from datetime import datetime, timezone

from api.human_login import ImportKey, KeyRefused, validate_digest


SOURCE_READ_TIMEOUT = 30.0


class SourceCut:
    def __init__(self, manifest: dict, users: list[dict], *, deadline=None):
        self.deadline = deadline
        self._children = []
        self._open = False
        self.keys = ()
        if set(manifest) != {"sources"} or not isinstance(manifest["sources"], list) or not manifest["sources"]:
            raise KeyRefused("source_inventory_invalid")
        ids = [s["id"] for s in manifest["sources"]]
        if len(set(ids)) != len(ids):
            raise KeyRefused("duplicate_source")
        source = (Path(__file__).resolve().parents[1] / "scripts/human_login_source.py").read_text()
        snapshots = []
        self._receipt = {"sources": []}
        try:
            for item in sorted(manifest["sources"], key=lambda x: x["id"]):
                if set(item) != {"id", "ssh_host", "python", "environment_file", "denylist_files", "units", "writer_paths"}:
                    raise KeyRefused("source_inventory_invalid")
                if not Path(item["python"]).is_absolute():
                    raise KeyRefused("source_interpreter_not_absolute")
                config = {k: item[k] for k in ("environment_file", "denylist_files", "units", "writer_paths")}
                if item["ssh_host"] is None:
                    command = [item["python"], "-c", source]
                else:
                    # Shell quoting applies to code/interpreter only, no credentials.
                    import re, shlex
                    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", item["ssh_host"]):
                        raise KeyRefused("invalid_ssh_host")
                    command = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5",
                               item["ssh_host"], shlex.join([item["python"], "-c", source])]
                child = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                         stderr=subprocess.DEVNULL, text=True, close_fds=True)
                self._children.append(child)
                self._write(child, (json.dumps(config) + "\n").encode(), self.deadline)
                snapshot = self._read(child, self.deadline)
                snapshots.append(snapshot)
                self._receipt["sources"].append({
                    "id": item["id"], "backend_pid": snapshot["backend_pid"],
                    "account_snapshot_digest": "sha256:" + hashlib.sha256(
                        json.dumps(snapshot["accounts"], sort_keys=True, separators=(",", ":")).encode()).hexdigest(),
                    "denylist_files": snapshot["files"], "writers": snapshot["writers"],
                    "checked_writer_paths": item["writer_paths"],
                })
            if any(s.get("accounts") != snapshots[0].get("accounts") for s in snapshots):
                raise KeyRefused("source_accounts_disagree")
            denies = set().union(*(set(s["denials"]) for s in snapshots))
            qualified = []
            for email, digest, issued, retired in snapshots[0]["accounts"]:
                if retired is not None or digest is None:
                    continue
                if not isinstance(email, str) or email != email.strip().lower():
                    raise KeyRefused("source_email_invalid")
                if email in denies or "human:" + email in denies:
                    continue
                validate_digest(digest)
                if issued is None:
                    raise KeyRefused("source_timestamp_missing")
                at = datetime.fromisoformat(issued)
                if at.tzinfo is None or at.utcoffset() is None or at > datetime.now(timezone.utc):
                    raise KeyRefused("source_timestamp_invalid")
                matches = [u for u in users if u["email"] and u["email"].lower() == email]
                if len(matches) != 1:
                    raise KeyRefused("source_identity_ambiguous")
                u = matches[0]
                # Existing broker human accounts authenticate as admin. Never
                # promote a differently ranked Kura identity during import.
                if u["rank"] != "administrator" or u["broker_trust_tier"] != "admin":
                    raise KeyRefused("source_rank_conflict")
                qualified.append(ImportKey(digest, u["id"], at, email, u["rank"]))
            self.keys = tuple(sorted(qualified, key=lambda x: x.key_digest))
            if not self.keys or len({k.key_digest for k in self.keys}) != len(self.keys):
                raise KeyRefused("source_set_invalid")
            self._open = True
            self.assert_held()
        except BaseException:
            self.close()
            raise

    @staticmethod
    def _read(child, deadline=None):
        deadline = min(time.monotonic() + SOURCE_READ_TIMEOUT, deadline if deadline is not None else float("inf"))
        data = bytearray()
        while b"\n" not in data:
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not select.select([child.stdout], [], [], remaining)[0]:
                raise KeyRefused("source_custody_timeout")
            chunk = os.read(child.stdout.fileno(), 65536)
            if not chunk:
                raise KeyRefused("source_custody_lost")
            data.extend(chunk)
        line, extra = bytes(data).split(b"\n", 1)
        if extra:
            raise KeyRefused("source_custody_protocol")
        data = line
        try:
            obj = json.loads(data)
        except (ValueError, TypeError, UnicodeDecodeError):
            raise KeyRefused("source_custody_lost") from None
        if not isinstance(obj, dict):
            raise KeyRefused("source_custody_unreadable")
        if "error" in obj:
            raise KeyRefused("source_custody_refused:" + str(obj["error"]))
        return obj

    @staticmethod
    def _write(child, data, deadline=None):
        deadline = min(time.monotonic() + SOURCE_READ_TIMEOUT, deadline if deadline is not None else float("inf"))
        fd = child.stdin.fileno()
        blocking = os.get_blocking(fd)
        os.set_blocking(fd, False)
        try:
            while data:
                left = deadline - time.monotonic()
                if left <= 0 or not select.select([], [fd], [], left)[1]:
                    raise KeyRefused("source_custody_timeout")
                try:
                    written = os.write(fd, data)
                except BlockingIOError:
                    continue
                except BrokenPipeError:
                    raise KeyRefused("source_custody_lost") from None
                data = data[written:]
        finally:
            os.set_blocking(fd, blocking)

    def assert_held(self, *, deadline=None):
        deadline = deadline if deadline is not None else self.deadline
        if not self._open or not self._children:
            raise KeyRefused("source_cut_closed")
        for child in self._children:
            if child.poll() is not None:
                raise KeyRefused("source_custody_lost")
            self._write(child, b"check\n", deadline)
            if self._read(child, deadline) != {"held": True}:
                raise KeyRefused("source_custody_lost")

    def receipt(self, *, deadline=None):
        """Redacted observations, not a reusable source-cut authorization."""
        self.assert_held(deadline=deadline)
        return {**self._receipt, "checked_at": datetime.now(timezone.utc).isoformat()}

    def close(self):
        self._open = False
        for child in self._children:
            if child.stdin and not child.stdin.closed:
                child.stdin.close()
            try:
                child.wait(timeout=5)
            except subprocess.TimeoutExpired:
                child.terminate()
                try:
                    child.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    child.kill(); child.wait(timeout=5)
        self._children.clear()

    def __enter__(self):
        self.assert_held()
        return self

    def __exit__(self, *args):
        self.close()
