"""Media authority role — kura#50 (#38 phase 0).

A node serves post media bytes from PG only if it is the media AUTHORITY.
The June pg-native design (plan:kura-pg-native-media-schema) assumed one
standalone node; replication (2026-07) created replicas whose `post.data`
is a copy of a cache. Before that copy is excluded from the publication and
reclaimed (kura#50 Phase 1-2), every serving process must be provably in one
of three explicit states — never an implicit "serves whatever is local":

    authority        — serve local PG bytes (primary; the default, unchanged)
    replica_observe  — serve local bytes BUT emit a structured census line
                       per serve (the Phase-0 observation window instrument;
                       coverage is proven by a synthetic probe appearing here)
    replica_redirect — never touch local bytes; 307 every content/thumbnail
                       request to KURA_MEDIA_AUTHORITY. No local fallback
                       path exists in code (plan R2'': a malformed state must
                       refuse at boot, not degrade at request time).

Validation runs at import — a worker with an invalid role or a redirect role
without a well-formed authority URL refuses to BOOT, so a fleet of workers can
never contain a silently misconfigured member (the attestation gate then
proves the healthy members' identity via /api/media-authority, pid-exact).
"""
from __future__ import annotations

import hashlib
import logging
import os
import time

VALID_ROLES = ("authority", "replica_observe", "replica_redirect")

MEDIA_ROLE: str = os.environ.get("KURA_MEDIA_ROLE", "authority")
MEDIA_AUTHORITY: str = os.environ.get("KURA_MEDIA_AUTHORITY", "").rstrip("/")
_STARTED_AT: float = time.time()


def _validate() -> None:
    if MEDIA_ROLE not in VALID_ROLES:
        raise RuntimeError(
            f"KURA_MEDIA_ROLE={MEDIA_ROLE!r} is not one of {VALID_ROLES} — "
            f"refusing to boot with an unclassifiable media role (kura#50 R2'')")
    if MEDIA_ROLE == "replica_redirect" and not (
            MEDIA_AUTHORITY.startswith("http://") or MEDIA_AUTHORITY.startswith("https://")):
        raise RuntimeError(
            f"KURA_MEDIA_ROLE=replica_redirect requires KURA_MEDIA_AUTHORITY to be an "
            f"http(s) URL, got {MEDIA_AUTHORITY!r} — refusing to boot rather than "
            f"fall back to serving local bytes (kura#50 R2'')")


_validate()


def config_digest() -> str:
    """What the attestation gate compares across workers: the role config's
    identity, not the process's opinion of itself."""
    return "sha256:" + hashlib.sha256(
        f"{MEDIA_ROLE}|{MEDIA_AUTHORITY}".encode()).hexdigest()


def probe_payload() -> dict:
    """One worker's attestation. pid is the load-bearing field — the gate
    requires the set of pids observed here to EXACTLY equal the set of live
    worker pids enumerated independently from the systemd cgroup (kura#50
    R2''': a request-count threshold is not a coverage proof)."""
    from api.build_info import as_dict as _build_info_dict
    return {
        "original_archive_policy": 1,  # immutable CAS targets + cooperating file locks
        "role": MEDIA_ROLE,
        "authority": MEDIA_AUTHORITY or None,
        "pid": os.getpid(),
        "config_digest": config_digest(),
        "started_at": _STARTED_AT,
        "build": _build_info_dict().get("commit"),
        # How this worker reads an archived original (api/services/originals.py): an archive
        # job on a node without the disk switches aliases only if every serving worker reads
        # them remotely from the expected archive (kura-on-nodeb plan rev 8).
        "original_reader": _original_reader(),
    }


def _original_reader() -> dict:
    from api import config
    return {"remote": bool(config.ARCHIVE_OBJECTS_URL), "archive_id": config.ARCHIVE_ID or None}


def redirect_target(path: str) -> str:
    return f"{MEDIA_AUTHORITY}{path}"


#: The census instrument (kura#50 R1''). One line per local-bytes serve while
#: observing; the census validator first PROVES coverage by making a synthetic
#: request and finding its line, then counts a declared window. Structured and
#: stable — the gate greps this exact prefix.
_observe_logger = logging.getLogger("kura.media.replica_observe")


def _iso_now() -> str:
    import datetime
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


if MEDIA_ROLE == "replica_observe":
    # The census requires proof the instrument PREDATES its window: a start
    # line with its own timestamp. Restart gaps remain a stated residual —
    # a crashed instrument logs nothing and silence reads clean; the gate
    # bounds this by requiring the earliest start line to be older than the
    # window it certifies.
    _observe_logger.warning("media-replica-observe-start ts=%s pid=%d", _iso_now(), os.getpid())


def note_local_media_serve(kind: str, post_id: int, probe_nonce: str | None = None) -> None:
    """probe_nonce comes from the X-Kura-Census-Probe request header: the census
    gate stamps its synthetic requests with a nonce so the census can separate
    them from ORGANIC serves of the same post exactly — exclusion by post_id
    conflated the two and could report a false zero (impl r1 blocking)."""
    if MEDIA_ROLE == "replica_observe":
        if probe_nonce:
            _observe_logger.warning("media-replica-serve ts=%s kind=%s post=%d probe=%s",
                                    _iso_now(), kind, post_id, probe_nonce)
        else:
            _observe_logger.warning("media-replica-serve ts=%s kind=%s post=%d",
                                    _iso_now(), kind, post_id)
