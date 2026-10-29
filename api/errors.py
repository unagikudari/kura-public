# Description: Structured API errors that name the next action — the kura half of the fleet's llm-friendly-errors rule.
# Kind: lib
# Effects: read-only
# Requires: none
# Runs-on: the API host
# Version: 1.0
"""Errors that tell the caller what to do next.

`BOOTSTRAP_COMMON.md` has carried this rule for the whole fleet:

    llm-friendly errors: API error に必ず fix(次の 1 action)を添える、
    裸の error code は dead-end で禁止 (Errors should never pass silently — Zen)

broker mechanised it — `api/errors.py` there produces a uniform envelope and
a test refuses one without a `fix`. kura did not: measured 2026-09-05, 226 of its
`raise HTTPException` sites, 220 carried a bare string. The rule was declared once
and enforced in one of three repositories, which is how an agent spent a working day
inside kura routing around surfaces it could not enter — the 401 it kept meeting
said `Missing or invalid X-Api-Key` and nothing about where the key lived.

A refusal is the one message the blocked caller is guaranteed to read. That makes it
the cheapest place in the system to put the answer, and a bare code the most
expensive: it ends the caller's path without opening another.

    {"detail": {"error": "<slug>", "context": {...}, "fix": "<one action>", "ref": "<doc>"}}

`error`   — machine-readable slug; a caller branches on this, never on prose.
`context` — structured values (received / allowed / current); no sentences.
`fix`     — ONE next action. Required unless the slug alone is the action.
`ref`     — where the reasoning lives, when the fix cannot carry it.

WHY A RATCHET AND NOT A FLAG DAY
--------------------------------
220 call sites are not converted in one commit — a change that large is reviewed by
nobody and lands by exhaustion. `tests/test_error_envelope.py` records today's count
and fails if it grows. New code takes the envelope because the test makes the old
shape the more expensive one; existing sites convert as they are touched. The rule
starts holding at the boundary immediately, which is where new agents meet it.
"""
from __future__ import annotations

from typing import Any


class KuraError(Exception):
    """An API error that names its own remedy.

    Raise this instead of `HTTPException(status, "some sentence")`. The handler in
    `api/main.py` renders it; nothing else needs to know the envelope's shape.
    """

    __slots__ = ("status", "error", "context", "fix", "ref")

    def __init__(self, status: int, error: str, *, context: dict[str, Any] | None = None,
                 fix: str | None = None, ref: str | None = None) -> None:
        super().__init__(error)
        self.status = status
        self.error = error
        self.context = context or {}
        self.fix = fix
        self.ref = ref

    def body(self) -> dict[str, Any]:
        out: dict[str, Any] = {"error": self.error}
        if self.context:
            out["context"] = self.context
        if self.fix:
            out["fix"] = self.fix
        if self.ref:
            out["ref"] = self.ref
        return out


# ── the refusals an agent meets first ────────────────────────────────────────
# These exist as helpers because the credential ones were the measured dead ends:
# an agent that cannot get past them cannot reach anything else, so they are the
# ones that must never be bare. The VALUE is never included — only where it lives
# and the loader that prints nothing, because `cat` on that file is how a secret
# reaches a transcript.

_ENV_FILE = "~/kura/api/env on the API host"
_LOADER = "set -a; . ~/kura/api/env; set +a"


def missing_api_key(header: str) -> KuraError:
    return KuraError(401, "missing_or_invalid_api_key",
                     context={"header": header, "credential": "KURA_API_KEY",
                              "lives_in": _ENV_FILE},
                     fix=f"{_LOADER}   then send  {header}: $KURA_API_KEY")


def missing_user_token() -> KuraError:
    return KuraError(401, "missing_or_invalid_authorization",
                     context={"header": "Authorization: Token <base64(user:token)>",
                              "credential": "KURA_USER + KURA_TOKEN",
                              "lives_in": _ENV_FILE},
                     fix=f"{_LOADER}   then send the Token header built from them")


def not_found(resource: str, **context: Any) -> KuraError:
    return KuraError(404, "not_found", context={"resource": resource, **context},
                     fix=f"Verify the {resource} exists; check the identifier for typos.")


def bad_request(error: str, *, fix: str, **context: Any) -> KuraError:
    """A 400 whose `fix` is mandatory in the signature — the shape this module
    exists to make unavoidable, expressed as a parameter rather than a convention."""
    return KuraError(400, error, context=context or None, fix=fix)


# ── upload refusals (kura#90 follow-up) ──────────────────────────────────────
# The ratchet in tests/test_error_envelope.py found three bare sites added after
# its baseline was set. Converting them here rather than at the call site keeps
# the wording with the rule, the way missing_api_key already does: a caller that
# meets this refusal reads the same sentence wherever it is raised from.
#
# Both of these are told apart by the CALLER's next action, which is why they are
# two errors and not one 4xx with a prose detail. An empty body means the request
# never carried the file; a body of leading zeros means the file arrived and is
# not content. Those need different remedies, and a bare "empty file" made them
# look like the same problem.


def empty_upload() -> KuraError:
    return KuraError(400, "empty_upload", context={"bytes": 0},
                     fix="attach the file to the `file` field — the request body carried none.")


def zero_byte_content(size: int) -> KuraError:
    """A file that arrived whole and is all zeros where its magic bytes belong.

    Measured shape: a pre-allocated placeholder written before its content
    arrived, or a copy truncated to zeros. The bytes are there, so `size` is real — reporting it is
    what tells the caller this is not the empty-body case above."""
    return KuraError(422, "zero_byte_content",
                     context={"bytes": size, "leading_bytes_checked": 16},
                     fix="re-fetch the source: this file's leading bytes are all zero, "
                         "which is a pre-allocated placeholder or a truncated copy, not media.")


def tag_name_taken_concurrently(requested: list[str]) -> KuraError:
    """Lost a rename race: the savepoint rolled back, so the tag keeps its old names.

    Distinct from a plain 409 because the caller's next action is specific — the
    write did NOT partially apply, so retrying is safe once the current names are
    re-read. A bare "Name already in use" left it ambiguous whether anything had
    changed."""
    return KuraError(409, "tag_name_taken_concurrently",
                     context={"requested": list(requested)},
                     fix="re-read the tag (GET /api/tag/<name>) and retry the rename with a "
                         "name that is still free — the tag is unchanged, nothing partially applied.")


def unknown_upload_fields(fields: list[str], accepted: list[str]) -> KuraError:
    """A form field /api/upload does not take — refused before the file is read, so
    a caller built for a protocol this route no longer records cannot lose it."""
    return KuraError(422, "unknown_upload_fields",
                     context={"fields": fields, "accepted": accepted},
                     fix="send only the accepted fields; an extension's own facts go to that "
                         "extension's upload route.")
