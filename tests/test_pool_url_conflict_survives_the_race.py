"""kura#70 follow-up — the pre-check answers the common case; the constraint answers the race.

`update_pool` now reads who owns a `primarySourceUrl` before writing it, and refuses with
409 naming the owner. That closes the case the 135 production 500s came from. It does not
close the window between that SELECT and the commit: another request can take the URL in
between, and then the database speaks instead — as a 500, the exact failure the fix
removes, only rarely. Review round 1 (finding 5) asked for both halves, and a rare 500 is
the worse one because nobody reproduces it.

Only `pool_url_url_unique` is folded into the 409. Catching IntegrityError broadly would
answer "that URL is taken" for any constraint failure — a wrong answer that reads like a
right one, which is more expensive than the 500 it replaced.

No database and no env var: `_commit_or_url_conflict` talks to a session, so it gets one.
"""
from __future__ import annotations

import asyncio

import pytest
from fastapi import HTTPException
from sqlalchemy.exc import IntegrityError

from api.routers.pools import POOL_URL_UNIQUE, _commit_or_url_conflict

URL = "https://example.invalid/kura70/raced"


class _Result:
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value


class _Session:
    def __init__(self, commit_error=None, owner=None):
        self.commit_error = commit_error
        self.owner = owner
        self.rolled_back = False
        self.committed = False

    async def commit(self):
        if self.commit_error is not None:
            err, self.commit_error = self.commit_error, None
            raise err
        self.committed = True

    async def rollback(self):
        self.rolled_back = True

    async def execute(self, _stmt):
        return _Result(self.owner)


def _integrity(constraint: str) -> IntegrityError:
    return IntegrityError("UPDATE ...", {}, Exception(
        f'duplicate key value violates unique constraint "{constraint}"'))


def test_the_happy_path_still_commits():
    db = _Session()
    asyncio.run(_commit_or_url_conflict(db, URL))
    assert db.committed and not db.rolled_back


def test_losing_the_race_answers_409_and_names_the_winner():
    db = _Session(commit_error=_integrity(POOL_URL_UNIQUE), owner=11)
    with pytest.raises(HTTPException) as e:
        asyncio.run(_commit_or_url_conflict(db, URL))
    assert e.value.status_code == 409
    assert "11" in str(e.value.detail) and URL in str(e.value.detail)
    assert db.rolled_back, "the failed transaction is rolled back before re-reading"


def test_the_winner_is_re_read_not_guessed():
    """If the row is gone again by the time we look (the winner rolled back too), the
    refusal must not invent a pool id."""
    db = _Session(commit_error=_integrity(POOL_URL_UNIQUE), owner=None)
    with pytest.raises(HTTPException) as e:
        asyncio.run(_commit_or_url_conflict(db, URL))
    assert e.value.status_code == 409
    assert "another pool" in str(e.value.detail)


def test_an_unrelated_constraint_is_not_reported_as_a_url_conflict():
    db = _Session(commit_error=_integrity("pool_name_name_key"))
    with pytest.raises(IntegrityError):
        asyncio.run(_commit_or_url_conflict(db, URL))
    assert not db.rolled_back, "an error we do not own is not swallowed or half-handled"


def test_a_request_that_set_no_url_does_not_borrow_this_refusal():
    """`update_pool` calls this on every PUT, including those that never touched
    primarySourceUrl. Such a request has no URL to blame, so a url-unique violation from
    somewhere else must propagate as itself."""
    db = _Session(commit_error=_integrity(POOL_URL_UNIQUE))
    with pytest.raises(IntegrityError):
        asyncio.run(_commit_or_url_conflict(db, None))
