# Description: content-addressed artifact store on disk (plan:tagger-release-is-the-whole-ensemble rev16).
# Kind: lib
# Effects: mutating (writes files under an artifact root); no DB, no network in the core
# Requires: none in the core; callers inject fetch/verify callbacks
"""artifact_store.py — bytes live on disk, addressed by their own sha256.

This generalises `release_client.py` from "the one model file" to "any artifact".
`release_client` already had the right discipline (write .partial -> fsync ->
sha256 -> load+selftest -> atomic rename -> refuse if nothing verified) but its
paths are hardcoded to `wd14-fixed.pt` / `selected_tags.csv`, which is the
"a release is one model file" assumption the plan removes.

Two properties do all the work here:

  * **the filename IS the identity.** A file at `<root>/<sha>` is valid iff
    `sha256(bytes) == sha`. Verification needs no metadata, no DB, and no trust
    in whoever wrote the file -- an admin who edits the bytes cannot also forge
    the hash. This is the plan's "immutability is enforced by detection, not by
    permission" reduced to one line of code.
  * **bytes never live in the DB.** `tagger_release.model_data` (374 MB/row,
    1.9 GB total) and `tagger_patch.model_data` (1.4 GB) are in `kura_mesh_pub`,
    so every blob crosses the replication stream to every node holding a kura
    DB. Content-addressed files do not replicate; each node fetches and verifies
    its own copy.

The store deliberately knows nothing about *what* an artifact is. Whether a blob
is a base model, a content head, or a patch is a property of the release that
references it, not of the bytes -- so `kind` is not a parameter here.
"""
from __future__ import annotations

import hashlib
import os
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Iterator

_CHUNK = 1 << 20

# Sibling of MODELS_DIR (infer.py), resolved the same way so a node that
# relocates its model cache relocates its artifacts too.
ARTIFACTS_DIR = Path(
    os.environ.get("KURA_ARTIFACT_DIR")
    or (Path(os.environ["KURA_MODELS_DIR"]).parent / "artifacts"
        if os.environ.get("KURA_MODELS_DIR")
        else Path.home() / ".cache" / "kura-tagger" / "artifacts")
)


class ArtifactStoreError(Exception):
    """The store cannot supply verified bytes; the caller must refuse to run."""


class ArtifactVerifyError(ArtifactStoreError):
    """Bytes were present but did not hash to the sha they were filed under."""


@dataclass(frozen=True)
class Corrupt:
    """One stored file whose contents no longer match its own name."""
    sha256: str
    path: Path
    actual_sha256: str


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(_CHUNK), b""):
            h.update(chunk)
    return h.hexdigest()


def _valid_sha(sha: str) -> bool:
    return len(sha) == 64 and all(c in "0123456789abcdef" for c in sha)


def _new_partial(root: Path, sha: str) -> Path:
    # Each writer owns its staging name, even for byte-identical uploads.
    fd, name = tempfile.mkstemp(prefix=f".partial-{sha}-", dir=root)
    os.close(fd)
    return Path(name)


class ArtifactStore:
    """Flat content-addressed directory: `<root>/<sha256>`.

    Flat rather than fanned-out (`ab/cdef...`) on purpose: the fleet holds tens
    of artifacts, not millions, and a flat directory keeps `verify_all()` and
    manual inspection trivial. If that ever stops being true the layout can
    change without touching callers, because callers only ever pass a sha.
    """

    def __init__(self, root: Path):
        self.root = Path(root)

    # ── location ─────────────────────────────────────────────────────────
    def path_for(self, sha256: str) -> Path:
        if not _valid_sha(sha256):
            raise ValueError(f"not a sha256 hex digest: {sha256!r}")
        return self.root / sha256

    def have(self, sha256: str) -> bool:
        """Present *and* intact. A file whose bytes drifted from its name is
        not 'had' -- reporting it as present would let a caller skip the fetch
        and then use bytes that failed verification."""
        p = self.path_for(sha256)
        return p.is_file() and sha256_file(p) == sha256

    def __iter__(self) -> Iterator[str]:
        if not self.root.is_dir():
            return iter(())
        return iter(sorted(p.name for p in self.root.iterdir()
                           if p.is_file() and _valid_sha(p.name)))

    # ── write ────────────────────────────────────────────────────────────
    def put_bytes(self, data: bytes, expected_sha256: str) -> Path:
        """Write bytes under their own hash, atomically, refusing a mismatch.

        The temp file is fsync'd before the rename so a crash cannot leave a
        short file under a name that claims a full one -- a torn file would
        otherwise be indistinguishable from tampering at the next verify.
        """
        actual = sha256_bytes(data)
        if actual != expected_sha256:
            raise ArtifactVerifyError(
                f"sha256 mismatch: caller said {expected_sha256}, bytes are {actual}")

        final = self.path_for(expected_sha256)
        if final.is_file() and sha256_file(final) == expected_sha256:
            return final  # already have these exact bytes; writing again is pointless

        self.root.mkdir(parents=True, exist_ok=True)
        partial = _new_partial(self.root, expected_sha256)
        try:
            with open(partial, "wb") as f:
                f.write(data)
                f.flush()
                os.fsync(f.fileno())
            on_disk = sha256_file(partial)
            if on_disk != expected_sha256:
                # Bytes were right in memory but wrong on disk -- a failing
                # device, not a caller bug. Never promote it.
                raise ArtifactVerifyError(
                    f"sha256 mismatch after write: expected {expected_sha256}, on disk {on_disk}")
            os.replace(partial, final)
        finally:
            partial.unlink(missing_ok=True)
        return final

    # ── read ─────────────────────────────────────────────────────────────
    def get(self, sha256: str,
            fetch_fn: Callable[[str], bytes] | None = None) -> Path:
        """Return a verified local path, fetching once if absent.

        Fail-closed: if the artifact is absent and cannot be fetched and
        verified, this raises rather than returning an unverified path. The
        caller's release simply is not adoptable on this node yet.
        """
        p = self.path_for(sha256)
        if p.is_file():
            on_disk = sha256_file(p)
            if on_disk == sha256:
                return p
            raise ArtifactVerifyError(
                f"stored artifact {sha256} has drifted: file hashes to {on_disk}")
        if fetch_fn is None:
            raise ArtifactStoreError(f"artifact {sha256} not present and no fetch_fn given")
        try:
            data = fetch_fn(sha256)
        except Exception as e:  # noqa: BLE001 -- any transport failure is a fetch failure
            raise ArtifactStoreError(f"fetch of {sha256} failed: {e!r}") from e
        return self.put_bytes(data, sha256)

    # ── verification (the periodic verifier's core) ───────────────────────
    def verify_all(self, only: Iterable[str] | None = None) -> list[Corrupt]:
        """Recompute every stored file's hash and report those that drifted.

        This is the whole tamper-detection story: because identity is the
        content hash, nobody -- including someone who can edit the file and the
        database -- can change the bytes without the recomputation disagreeing.
        Returns findings instead of raising so a caller can report all of them.
        """
        names = list(only) if only is not None else list(self)
        corrupt: list[Corrupt] = []
        for sha in names:
            p = self.path_for(sha)
            if not p.is_file():
                continue
            actual = sha256_file(p)
            if actual != sha:
                corrupt.append(Corrupt(sha256=sha, path=p, actual_sha256=actual))
        return corrupt

    # ── housekeeping ─────────────────────────────────────────────────────
    def sweep_partials(self) -> int:
        """Remove leftover `.partial-*` files from interrupted writes."""
        if not self.root.is_dir():
            return 0
        n = 0
        for p in self.root.iterdir():
            if p.is_file() and p.name.startswith(".partial-"):
                p.unlink(missing_ok=True)
                n += 1
        return n

    def usage_bytes(self) -> int:
        return sum(p.stat().st_size for p in self.root.iterdir()
                   if p.is_file() and _valid_sha(p.name)) if self.root.is_dir() else 0


def default_store() -> ArtifactStore:
    """The node's artifact store. Callers that need a different root (tests,
    migrations staging elsewhere) construct ArtifactStore directly."""
    return ArtifactStore(ARTIFACTS_DIR)


def ingest_file(store: ArtifactStore, src: Path, *, move: bool = False) -> str:
    """Put an existing file into the store under its own hash; return the sha.

    Used by the migration to move the blobs that currently sit in
    `tagger_release.model_data` / `tagger_patch.model_data` out of the
    replicated tables and onto disk.
    """
    sha = sha256_file(src)
    final = store.path_for(sha)
    if final.is_file() and sha256_file(final) == sha:
        if move:
            src.unlink(missing_ok=True)
        return sha
    store.root.mkdir(parents=True, exist_ok=True)
    partial = _new_partial(store.root, sha)
    try:
        if move:
            shutil.move(str(src), str(partial))
        else:
            shutil.copyfile(src, partial)
        with open(partial, "rb") as f:
            os.fsync(f.fileno())
        on_disk = sha256_file(partial)
        if on_disk != sha:
            raise ArtifactVerifyError(
                f"sha256 changed while ingesting {src}: {sha} -> {on_disk}")
        os.replace(partial, final)
    finally:
        partial.unlink(missing_ok=True)
    return sha
