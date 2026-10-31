"""The one place that answers "what opens the ?key= surfaces" (kura#43 item 2).

Two files carried this line, character for character:

    _KEY = os.environ.get("KURA_DASHBOARD_KEY") or os.environ.get("KURA_SECRET") or "change-me"

and the comment above one of them said the dashboard key was kept separate from
KURA_SECRET so that either could be rotated without invalidating the other.

Measured on primary, 2026-09-04: KURA_DASHBOARD_KEY was not set. The fallback was live.
So the value travelling in every `?key=` — into nginx access logs, browser history
and any referer — was the szurubooru content-HMAC secret itself, the one that names
files on disk. And since rotating that secret 404s every content URL, the coupling
made rotation impossible, which is the exact opposite of what the comment promised.
A comment can say two things are separate; only the code decides.

The same measurement found the production secret to be a six-character dictionary
word, which the boot guard let through because it only refuses the literal string
"change-me". That is being handled separately, and it is why this module refuses to
inherit anything: a weak secret in one place should not become a weak key in two.

Unset now means CLOSED. Not "open with the other secret", and not "open with a
published default".
"""
from __future__ import annotations

import logging
import os
import secrets

log = logging.getLogger(__name__)

#: Cookie the ?key= query is promoted into, shared by every surface using this key.
COOKIE_NAME = "kura_dashboard_key"

#: ページ内 fetch が鍵を渡すときの経路。?key= は URL に載るので access log にも
#: Referer にも残る — cookie が保存されない経路 (secure cookie + 平文 HTTP) では
#: query が唯一の手段だった。header ならどちらの経路でも通り、URL には出ない。
HEADER_NAME = "X-Dashboard-Key"

_ENV = "KURA_DASHBOARD_KEY"

#: What to tell an OPERATOR. It goes to the server log, never to a response: both
#: review lanes flagged that handing an unauthenticated caller the env-file path,
#: the variable names, and the fact that one of them names files on disk is a
#: disclosure — and that differing between "closed" and "wrong key" is an oracle
#: for whether the deployment is configured at all. The reason a missing variable
#: went unnoticed was that another secret silently stood in for it, and a log line
#: fixes that without telling the caller anything.
UNSET_HINT = (
    f"{_ENV} is not set, so the ?key= surfaces are closed. Set it in api/env and "
    "restart kura-api. It must NOT be the same value as KURA_SECRET — that one "
    "names files on disk and cannot be rotated independently once they are shared."
)

_warned: set[str] = set()


def _warn_once(key: str, message: str) -> None:
    """One line per condition per process. A per-request warning would bury the
    condition it reports under its own volume."""
    if key not in _warned:
        _warned.add(key)
        log.warning("%s", message)


def configured_key() -> str | None:
    """The dashboard key, or None when the surface is closed.

    Read per call rather than captured at import so a restart is the only thing
    needed to change it — and so a test can set the variable without reloading two
    modules that happened to snapshot it at different times.

    Returns None ALSO when the value equals KURA_SECRET. The first draft only said
    in prose that the two must differ, and prose is exactly what failed the first
    time: this module exists because a comment claiming the keys were separate sat
    directly above the code that joined them. An operator who pastes the same value
    gets a closed surface and a log line, not a silent re-coupling.
    """
    value = os.environ.get(_ENV) or ""
    if not value:
        _warn_once("unset", UNSET_HINT)
        return None
    # Both, and by name, so retiring KURA_SECRET does not silently drop half the
    # check: the salt is what actually names files now (kura#43-2).
    for other in ("KURA_FS_NAME_SALT", "KURA_SECRET"):
        if value == (os.environ.get(other) or "\0"):
            _warn_once(f"same-as-{other}",
                       f"{_ENV} is set to the same value as {other}, so the ?key= "
                       "surfaces are closed. That value names files on disk and "
                       "cannot be rotated while it is also a URL parameter — "
                       "generate a separate one and restart kura-api.")
            return None
    return value


def key_matches(candidate: str | None) -> bool:
    """Constant-time compare against the configured key.

    False when nothing is configured: a closed surface must not be opened by
    sending an empty key, and `compare_digest("", "")` is True."""
    key = configured_key()
    if not key or not candidate:
        return False
    return secrets.compare_digest(candidate, key)
