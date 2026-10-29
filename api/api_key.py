"""Dedicated service credential and legacy router-level read-gate mode.

The shared command boundary always requires a valid machine or human credential,
regardless of this legacy gate's observe/enforce setting. Observe is retained for
internal read-client compatibility, not as an anonymous command bypass.
The key is independent of filename, dashboard and session secrets.
"""
from __future__ import annotations

import logging
import os
import secrets

log = logging.getLogger(__name__)

HEADER_NAME = "X-Api-Key"
_ENV = "KURA_API_KEY"
_ENFORCE_ENV = "KURA_API_KEY_ENFORCE"

_warned: set[str] = set()


def _warn_once(key: str, message: str) -> None:
    if key not in _warned:
        _warned.add(key)
        log.warning("%s", message)


def enforcing() -> bool:
    """True only when KURA_API_KEY_ENFORCE is exactly 'enforce'. Anything else —
    unset, 'observe', a typo — is observe, because the safe default for a gate
    that can stop the fleet is to watch, not to reject."""
    return (os.environ.get(_ENFORCE_ENV) or "").strip().lower() == "enforce"


def configured_key() -> str | None:
    """The API key, or None when none is configured.

    Read per call (not captured at import) so a restart is all it takes to change
    it, and so a test can set the variable without module reloads.

    Returns None ALSO when the value equals a filename/HMAC secret. The dashboard
    key learned this the hard way (api/dashboard_key.py): a comment said the keys
    were separate and the code joined them. Refuse the re-coupling by name.
    """
    value = os.environ.get(_ENV) or ""
    if not value:
        return None
    for other in ("KURA_FS_NAME_SALT", "KURA_SECRET", "KURA_DASHBOARD_KEY", "KURA_SESSION_SECRET"):
        if value == (os.environ.get(other) or "\0"):
            _warn_once(f"same-as-{other}",
                       f"{_ENV} is set to the same value as {other} — refused. "
                       "Generate a distinct value; sharing a key is how one weak "
                       "secret became two (kura#43 item 2).")
            return None
    return value


def key_matches(candidate: str | None) -> bool:
    """Constant-time compare against the configured key. False when nothing is
    configured (a closed gate is not opened by sending an empty key) or when the
    candidate is empty."""
    key = configured_key()
    if not key or not candidate:
        return False
    return secrets.compare_digest(candidate, key)
