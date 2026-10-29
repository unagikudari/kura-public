# Description: Retry a tagging claim/completion across stage 3's EXECUTE window, so workers never see it.
# Kind: lib
# Effects: mutating (reruns the caller's transaction, then commits it)
# Requires: kura DB
# Runs-on: the API host
# Version: 2.0
"""Hide stage 3's swap window from the tagging workers.

docs/notes/tagging-work-table-plan.md, stage 3, revokes EXECUTE on the four governed
functions from kura for a few seconds while their bodies are swapped. A call in that
window fails with SQLSTATE 42501. The worker's completion has no retry
(tagger/loop.py), so a failure there throws the work away until its lease runs out.
This module reruns the whole business transaction instead, from the top (stage 2).

- Only 42501, and only when the function the route calls has really lost EXECUTE.
  The message cannot be matched: production's lc_messages is ja_JP.UTF-8 and only a
  superuser may change it. So after rolling back, a fresh transaction asks
  has_function_privilege(). False means the window: wait and retry. True means the
  window may just have closed: retry once more, and if that also fails with 42501 it
  is some other missing privilege, which is raised as before.
- Nothing is held while waiting: the transaction is rolled back first, and the
  privilege check is its own transaction, ended before the sleep.
- Whole transaction: the attempt callable starts from nothing (locks, attempts, the
  claim backend, the guard and heartbeat) and does NOT commit. run() commits after a
  successful attempt, outside the retry: a failure while committing (whatever its
  SQLSTATE, or a lost connection with an unknown outcome) is raised as before and
  never rerun.
- Deadline: everything before the commit, DB waits included (row locks, the advisory
  lock, the privilege check, the sleep), runs under one asyncio deadline of
  DEADLINE_SECONDS from the start of the request — the first attempt too — well inside
  the worker's 60 s HTTP timeout. When it passes, the work in flight is cancelled, the
  session's connection is invalidated (a cancelled query leaves it unusable) and
  WindowExpired is raised; the routes turn it into an empty claim or a 503 completion.
  The commit itself is not under the deadline: cancelling it would make the outcome
  unknown. Each expiry is logged on the "kura.governed_window" logger with
  EXPIRED_MARKER, so they can be counted.
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import TypeVar

from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

_log = logging.getLogger("kura.governed_window")

DEADLINE_SECONDS = 20.0
WAIT_SECONDS = 0.25
EXPIRED_MARKER = "tagging_governed_window_expired"

CLAIM = "public.tagger_governed_claim_auto_tag(integer, integer)"
COMPLETE_OK = "public.tagger_governed_complete_ok(integer)"
COMPLETE_RETRY = "public.tagger_governed_complete_retry(integer, text, text)"

T = TypeVar("T")


class WindowExpired(Exception):
    """The request's deadline passed before its transaction could commit."""


def insufficient_privilege(exc: BaseException) -> bool:
    return isinstance(exc, DBAPIError) and getattr(exc.orig, "sqlstate", None) == "42501"


def now() -> float:
    """The clock run()'s deadline is measured on (the event loop's)."""
    return asyncio.get_running_loop().time()


async def _lacks_execute(db: AsyncSession, function: str) -> bool:
    try:
        allowed = (await db.execute(
            text("SELECT has_function_privilege(current_user, :f, 'EXECUTE')"), {"f": function}
        )).scalar_one()
    finally:
        await db.rollback()
    return not allowed


async def _until_committable(db: AsyncSession, attempt: Callable[[], Awaitable[T]], function: str) -> T:
    """Attempts until one gets through without the window's 42501. Unbounded; run() bounds it."""
    rechecked = False
    while True:
        try:
            return await attempt()
        except DBAPIError as exc:
            if not insufficient_privilege(exc):
                raise
            await db.rollback()
            if not await _lacks_execute(db, function):
                if rechecked:
                    raise
                rechecked = True
            else:
                await asyncio.sleep(WAIT_SECONDS)


async def run(
    db: AsyncSession,
    attempt: Callable[[], Awaitable[T]],
    function: str,
    *,
    route: str,
    started: float | None = None,
) -> T:
    """Run attempt() until it passes the window, then commit once.

    `function` is the regprocedure of the governed function the attempt calls;
    `started` is now() at the start of the request.
    """
    started = now() if started is None else started
    try:
        async with asyncio.timeout_at(started + DEADLINE_SECONDS):
            result = await _until_committable(db, attempt, function)
    except TimeoutError as exc:
        await db.invalidate()
        _log.warning("%s route=%s function=%s waited_s=%.1f",
                     EXPIRED_MARKER, route, function, now() - started)
        raise WindowExpired from exc
    await db.commit()
    return result
