"""Immutable learning evidence and its canonical node-local location."""
import hashlib
import json
import os
from pathlib import Path
import tempfile


SNAPSHOT_SCHEMA = "vnext-dedicated-snapshot/1"


class ProtocolError(ValueError):
    pass


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     allow_nan=False).encode()).hexdigest()


def write_bytes_once(path, payload):
    """Publish complete bytes atomically with a no-overwrite link, even on races."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".dedicated-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(payload)
            f.flush()
            os.fsync(f.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError:
            if path.read_bytes() != payload:
                raise ProtocolError(f"immutable output exists: {path}; use a new output path")
    finally:
        os.unlink(temporary)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)


def write_once(path, body):
    """Never overwrite evidence, including on rerun. Identical writes are OK."""
    doc = dict(body, sha256=digest(body))
    payload = json.dumps(doc, sort_keys=True, indent=1, allow_nan=False) + "\n"
    write_bytes_once(path, payload.encode())
    return doc


def read_verified(path, schema=None):
    doc = json.loads(Path(path).read_text())
    body = {k: v for k, v in doc.items() if k != "sha256"}
    if doc.get("sha256") != digest(body) or (schema and doc.get("schema") != schema):
        raise ProtocolError(f"snapshot digest/schema mismatch: {path}; restore original evidence")
    return doc


def job_root(tag, event):
    key = hashlib.sha256(f"{tag}@{event}".encode()).hexdigest()[:24]
    state = Path(os.environ.get('XDG_STATE_HOME') or Path.home()/'.local/state')
    return state/'kura-tagger'/'learning-jobs'/key
