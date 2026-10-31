"""Reading a post's original bytes, wherever the original is (docs/notes/kura-on-nodeb-plan.md,
"Originals on another host", rev 8).

An original under `DATA_DIR/posts` is one of:

    local    a regular file                                  → read it
    cas      a symlink into …/objects/sha256/<xx>/<sha>       → the content-addressed archive
    legacy   a symlink into LEGACY_ORIGINAL_ROOT (kura-media) → the older archive layout

On the node the archive disk is attached to, the symlinks resolve and are read as files. On a
node without it (`KURA_ARCHIVE_OBJECTS_URL` set), archived originals are fetched from
kura-archive-objects on that node: the whole body is spooled into a cache file, checked
against the post's SHA-256 (else its MD5), and only then used. The fetch runs in its own
bounded executor (FETCH_SLOTS at once, FETCH_QUEUE waiting, FETCH_DEADLINE seconds in all),
so a slow or absent archive node can hold a few threads for a bounded time and never the
API's shared pool. Any failure is OriginalUnavailable; callers answer 503 or defer.

Nothing here writes to the archive or follows a symlink to write or delete.
"""
from __future__ import annotations

import contextlib
import fcntl
import hashlib
import logging
import os
import re
import socket
import threading
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

from api import config

NAME = re.compile(r"^(\d+)_([0-9a-f]{16})\.([A-Za-z0-9]{1,8})$")
CAS = re.compile(r"/objects/sha256/([0-9a-f]{2})/([0-9a-f]{64})$")
FETCH_SLOTS = 4
FETCH_QUEUE = 8
FETCH_DEADLINE = 20.0
MAX_SPOOL = 1024 ** 3


log = logging.getLogger(__name__)


class OriginalUnavailable(Exception):
    """The original exists in the archive but could not be read and verified now."""


@dataclass(frozen=True)
class Location:
    kind: str            # local | cas | legacy
    path: Path           # the name under DATA_DIR/posts
    sha: str | None = None
    legacy_name: str | None = None


@dataclass(frozen=True)
class Identity:
    sha256: str | None   # hex
    md5: str | None      # hex
    size: int | None


def classify(path: Path) -> Location | None:
    """Where the original behind this name is, from the link text only (no access to the
    target, which may be on another machine)."""
    try:
        if not path.is_symlink():
            return Location("local", path) if path.is_file() else None
        text = os.readlink(path)
    except OSError:
        return None
    m = CAS.search(text)
    if m and m[2].startswith(m[1]):
        return Location("cas", path, sha=m[2])
    root = config.LEGACY_ORIGINAL_ROOT
    if root is not None:
        target = Path(text)
        if target.parent == root and NAME.fullmatch(target.name):
            return Location("legacy", path, legacy_name=target.name)
    return None


def remote() -> bool:
    return bool(config.ARCHIVE_OBJECTS_URL)


# ── verified spool cache ─────────────────────────────────────────────────────
#
# Entries are published only after verification and re-verified on every use. A reader holds
# a shared flock on the entry for as long as it serves it (a Lease); eviction takes an
# exclusive non-blocking flock and skips anything leased, so a uvicorn worker never deletes a
# file another worker is sending. In-progress downloads are dot-files and never evicted.


@dataclass
class Lease:
    path: Path
    fd: int
    _released: bool = False

    @property
    def serve_path(self) -> str:
        """The leased inode itself, whatever happens to `path` afterwards (a concurrent
        publish or an eviction replaces or removes the name, never this descriptor)."""
        return f"/proc/self/fd/{self.fd}"

    def release(self) -> None:          # idempotent
        if self._released:
            return
        self._released = True
        with contextlib.suppress(OSError):
            os.close(self.fd)


def _cache_path(loc: Location, ident: Identity) -> Path:
    # Keyed by the expected identity, not only the post: a replaced original never reuses
    # an old spool (codex review of the rev 8 design).
    key = ident.sha256 or ident.md5 or "none"
    name = loc.sha or loc.legacy_name or loc.path.name
    return config.ORIGINAL_CACHE_DIR / key[:2] / f"{key}-{name}"


def _lease(path: Path, ident: Identity) -> Lease | None:
    """A shared lock on a verified cache entry, or None (absent, replaced, or bad)."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError:
        return None
    try:
        fcntl.flock(fd, fcntl.LOCK_SH)
        if os.fstat(fd).st_ino != os.stat(path).st_ino:     # evicted and re-created meanwhile
            raise OSError("entry replaced")
        if not _verify_fd(fd, ident):
            raise OSError("entry does not verify")
        return Lease(path, fd)
    except OSError:
        os.close(fd)
        return None


def _evict(limit: int) -> None:
    root = config.ORIGINAL_CACHE_DIR
    if not root.is_dir():
        return
    entries = []
    for p in root.glob("*/*"):
        if p.name.startswith("."):          # a download in progress
            continue
        with contextlib.suppress(FileNotFoundError):
            st = p.stat()
            entries.append((st.st_atime, st.st_size, p))
    total = sum(size for _, size, _ in entries)
    for _, size, p in sorted(entries):
        if total <= limit:
            break
        try:
            fd = os.open(p, os.O_RDONLY | os.O_NOFOLLOW)
        except OSError:
            continue
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)  # leased by a reader → skip
            os.unlink(p)
            total -= size
        except OSError:
            pass
        finally:
            os.close(fd)


def _verify_fd(fd: int, ident: Identity) -> bool:
    st = os.fstat(fd)
    if ident.size is not None and st.st_size != ident.size:
        return False
    sha, md5 = hashlib.sha256(), hashlib.md5()
    offset = 0
    while block := os.pread(fd, 1 << 20, offset):
        sha.update(block)
        md5.update(block)
        offset += len(block)
    if ident.sha256:
        return sha.hexdigest() == ident.sha256
    if ident.md5:
        return md5.hexdigest() == ident.md5
    return False


def verify(path: Path, ident: Identity) -> bool:
    fd = os.open(path, os.O_RDONLY)
    try:
        return _verify_fd(fd, ident)
    finally:
        os.close(fd)


def _target(loc: Location) -> tuple[str, int, str, bool]:
    url = urllib.parse.urlsplit(config._numeric_host_url(config.ARCHIVE_OBJECTS_URL))
    if loc.kind == "cas":
        path = f"{url.path}/objects/sha256/{loc.sha[:2]}/{loc.sha}"
    else:
        path = f"{url.path}/legacy/posts/{loc.legacy_name}"
    tls = url.scheme == "https"
    return url.hostname, url.port or (443 if tls else 80), path, tls


def _download(loc: Location, ident: Identity, deadline: float, out) -> None:
    """Stream the object into `out`. The socket timeout is the time left before the deadline
    on every read, so a stalled or trickling archive node cannot outlive it."""
    import http.client
    import socket
    host, port, path, tls = _target(loc)

    def left() -> float:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise OriginalUnavailable("deadline passed")
        return remaining

    conn = (http.client.HTTPSConnection if tls else http.client.HTTPConnection)(host, port, timeout=left())
    # One absolute deadline over connect, headers and body: a timer shuts the socket down when
    # it passes, which ends any blocking read, however slowly the other side trickles
    # (codex review of 1.269.1: per-read timeouts let trickled headers run past it).
    cut = threading.Event()

    def stop() -> None:
        cut.set()
        sock_now = conn.sock
        if sock_now is not None:
            with contextlib.suppress(OSError):
                sock_now.shutdown(socket.SHUT_RDWR)

    timer = threading.Timer(max(0.0, deadline - time.monotonic()), stop)
    timer.daemon = True
    timer.start()
    try:
        conn.connect()
        conn.request("GET", path, headers={"X-Archive-Key": config.ARCHIVE_READ_KEY})
        sock = conn.sock          # the response may take the socket over from the connection
        sock.settimeout(left())
        resp = conn.getresponse()
        if resp.status != 200:
            raise OriginalUnavailable(f"archive answered {resp.status}")
        if config.ARCHIVE_ID and resp.getheader("X-Archive-Id") != config.ARCHIVE_ID:
            raise OriginalUnavailable("archive id differs from KURA_ORIGINAL_ARCHIVE_ID")
        limit = min(ident.size or MAX_SPOOL, MAX_SPOOL)
        got = 0
        while not resp.isclosed():     # the response closes the socket once the body ends
            sock.settimeout(left())
            block = resp.read1(1 << 20)
            if not block:
                break
            got += len(block)
            if got > limit:
                raise OriginalUnavailable("archive answered more bytes than the original has")
            out.write(block)
        if cut.is_set():
            raise OriginalUnavailable("deadline passed")
    except (OSError, http.client.HTTPException, socket.timeout) as exc:
        if cut.is_set():
            raise OriginalUnavailable("deadline passed") from None
        raise OriginalUnavailable(f"{type(exc).__name__}: {exc}") from None
    finally:
        timer.cancel()
        conn.close()


def _fetch(loc: Location, ident: Identity, deadline: float) -> Lease:
    """Runs in the fetch executor; holds its admission slot until it returns."""
    try:
        target = _cache_path(loc, ident)
        lease = _lease(target, ident)
        if lease is not None:
            return lease
        tmp = target.with_name(f".{target.name}.{os.getpid()}.{threading.get_ident()}.part")
        try:
            # The local cache failing (ENOSPC or EIO on write, flush, fsync, rename, the read
            # that verifies) says nothing about the original: it is unavailable now, the same as
            # an archive that does not answer, and a tagger holds the post instead of spending
            # its attempts on it (#156). Program errors are not OSError and still raise.
            target.parent.mkdir(parents=True, exist_ok=True)
            with tmp.open("xb") as out:
                _download(loc, ident, deadline, out)
                out.flush()
                os.fsync(out.fileno())
            if not verify(tmp, ident):
                raise OriginalUnavailable("fetched bytes do not match the post's identity")
            os.replace(tmp, target)
            lease = _lease(target, ident)       # leased before anything may evict it
        except OSError as e:
            raise OriginalUnavailable(f"the local original cache failed: {e.strerror or e}") from e
        finally:
            with contextlib.suppress(OSError):
                tmp.unlink()
        if lease is None:
            raise OriginalUnavailable("cache entry vanished after publishing")
        try:
            _evict(config.ORIGINAL_CACHE_BYTES)
        except OSError as e:                    # the fetch is done and leased; a full sweep can wait
            log.warning("original cache eviction failed: %s", e)
        return lease
    finally:
        _admitted.release()


_executor = ThreadPoolExecutor(max_workers=FETCH_SLOTS, thread_name_prefix="original-fetch")
_admitted = threading.BoundedSemaphore(FETCH_SLOTS + FETCH_QUEUE)


def _admit() -> None:
    if not _admitted.acquire(blocking=False):
        raise OriginalUnavailable("too many archive fetches waiting")


def acquire(loc: Location, ident: Identity) -> Lease:
    """A verified local copy of an archived original, leased until release()."""
    _admit()
    deadline = time.monotonic() + FETCH_DEADLINE
    try:
        future = _executor.submit(_fetch, loc, ident, deadline)
    except BaseException:
        _admitted.release()
        raise
    try:
        return future.result(timeout=FETCH_DEADLINE + 1)
    except BaseException as exc:
        future.add_done_callback(_release_late)
        if isinstance(exc, TimeoutError):
            raise OriginalUnavailable("archive fetch timed out") from None
        raise


def _release_late(future) -> None:
    if future.cancelled() or future.exception() is not None:
        return
    future.result().release()


async def acquire_async(loc: Location, ident: Identity) -> Lease:
    """As acquire(), waiting through asyncio so no thread of the API's shared pool is held."""
    import asyncio
    _admit()
    deadline = time.monotonic() + FETCH_DEADLINE
    try:
        work = _executor.submit(_fetch, loc, ident, deadline)
    except BaseException:
        _admitted.release()
        raise
    try:
        return await asyncio.wait_for(asyncio.shield(asyncio.wrap_future(work)), timeout=FETCH_DEADLINE + 1)
    except BaseException as exc:          # timeout, or the caller was cancelled
        # The fetch may still hand back a lease; whoever is gone cannot release it. The
        # callback is on the executor's future, so it runs in the fetch thread even when the
        # event loop has already stopped.
        work.add_done_callback(_release_late)
        if isinstance(exc, asyncio.TimeoutError):
            raise OriginalUnavailable("archive fetch timed out") from None
        raise


def fetch_fresh(loc: Location, ident: Identity) -> bytes:
    """The object as the archive node serves it right now: a new connection, no cache, the
    archive id checked on the answer, the bytes verified. For the archive job's read-back
    before it lets a local original go (codex review of 1.270.0: a cache hit said yes while
    the archive was unreachable)."""
    if not remote():
        raise OriginalUnavailable("KURA_ARCHIVE_OBJECTS_URL is not set")
    if not config.ARCHIVE_ID:
        raise OriginalUnavailable("KURA_ORIGINAL_ARCHIVE_ID is not set")
    import io
    buf = io.BytesIO()
    _download(loc, ident, time.monotonic() + FETCH_DEADLINE, buf)
    data = buf.getvalue()
    if ident.size is not None and len(data) != ident.size:
        raise OriginalUnavailable("archive answered a different size")
    if ident.sha256 and hashlib.sha256(data).hexdigest() != ident.sha256:
        raise OriginalUnavailable("archive answered different bytes")
    if not ident.sha256 and ident.md5 and hashlib.md5(data).hexdigest() != ident.md5:
        raise OriginalUnavailable("archive answered different bytes")
    return data


def releasing_file_response(lease: Lease, media_type: str | None):
    """A FileResponse for the leased descriptor that releases the lease however the response
    ends — sent, a 400/416 for a bad Range (Starlette returns those without running
    `background`), or a client that went away (codex review of 1.269.1)."""
    from starlette.responses import FileResponse

    class _Releasing(FileResponse):
        async def __call__(self, scope, receive, send):
            try:
                await super().__call__(scope, receive, send)
            finally:
                lease.release()

    try:
        return _Releasing(lease.serve_path, media_type=media_type, stat_result=os.fstat(lease.fd))
    except BaseException:
        lease.release()
        raise


def original_bytes(loc: Location, ident: Identity) -> bytes:
    if loc.kind == "local" or not remote():
        if not loc.path.exists():
            raise OriginalUnavailable("the archive this original points at is not reachable here")
        return loc.path.read_bytes()
    lease = acquire(loc, ident)
    try:
        return os.pread(lease.fd, os.fstat(lease.fd).st_size, 0)
    finally:
        lease.release()


def identity_from_row(sha256: bytes | None, checksum_md5: str | None, checksum: str | None,
                      size: int | None) -> Identity:
    md5 = checksum_md5 or (checksum if re.fullmatch(r"[0-9a-f]{32}", checksum or "") else None)
    return Identity(bytes(sha256).hex() if sha256 else None, md5, size)
