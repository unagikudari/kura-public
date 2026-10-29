"""The tagger's situation must be reachable without a browser, and its numbers
must not be able to say the work is finished while the queue is full.

Both halves come from measured failures on 2026-09-05:

  * an agent asked "is tagging running", could not reach the dashboard (it 303s
    to a SvelteKit login), reached for `auto_tagme` instead — a provenance marker that
    reads like a queue — and reported 68% unprocessed. The real queue column said
    something else entirely.
  * the dashboard's own completion figure read **1308.9%**, and its companion
    "remaining" read **0** while 941,116 posts were queued. The percentage divided
    a release's cumulative audit history by the *current* count of posts whose
    status is done; the sweep re-queues processed posts, so they stay in the
    numerator and leave the denominator, and `max(0, …)` turned the inversion
    into a claim of completion.

These run with no database: the derivations are pure SQL builders plus arithmetic,
so the arithmetic is what is pinned here. The SQL itself is exercised by the live
endpoint (`GET /api/tagger/situation`), which is in the OpenAPI schema — the first
version of this work put the facts on an `include_in_schema=False` route, which is
how they stayed unfindable in the first place.
"""
from __future__ import annotations

import asyncio
import inspect

import pytest

from api.services import tagger_situation


class _Row(dict):
    """A mappings() row: dict access is all the code uses."""


class _Result:
    def __init__(self, rows):
        self._rows = rows

    def mappings(self):
        return self

    def all(self):
        return self._rows

    def one(self):
        return self._rows[0]

    def scalar_one(self):
        return self._rows[0]

    def scalar_one_or_none(self):
        return self._rows[0] if self._rows else None


class _DB:
    """Answers each query with the next canned result, and records the SQL."""

    def __init__(self, results):
        self.results = list(results)
        self.sql = []

    async def execute(self, stmt, params=None):
        self.sql.append(str(stmt))
        return _Result(self.results.pop(0))


def test_completion_cannot_exceed_one_hundred_percent():
    """The 1308.9% case, with the numbers that produced it.

    processed=999,472 posts carried an audit row for release 14; 76,358 posts were
    `tagging_status='done'` at that instant; 1,027,004 posts existed. The old
    denominator was the middle number.
    """
    db = _DB([[_Row(processed=999_472, total_posts=1_027_004)]])
    out = asyncio.run(tagger_situation.sweep_coverage(db, 14))
    assert out["pct"] <= 100.0, "a completion percentage over 100 means the two counts are different populations"
    assert round(out["pct"], 1) == 97.3
    assert out["remaining"] == 1_027_004 - 999_472


def test_remaining_is_not_clamped_into_a_false_zero():
    """`max(0, done - processed)` printed `0 remaining` while the queue held
    941,116. Remaining must be derived from the same population as processed, so
    a zero here means the archive really is covered."""
    db = _DB([[_Row(processed=10, total_posts=1_000)]])
    out = asyncio.run(tagger_situation.sweep_coverage(db, 14))
    assert out["remaining"] == 990, "remaining follows the archive, not the current done-count"

    db = _DB([[_Row(processed=1_000, total_posts=1_000)]])
    assert asyncio.run(tagger_situation.sweep_coverage(db, 14))["remaining"] == 0


def test_the_queue_is_the_status_column_and_says_what_it_is_not():
    """`queued` counts pending+claimed, and carries the sentence that stops the
    reading that misled: it is not 'never tagged'."""
    db = _DB([[_Row(status="pending", n=900), _Row(status="claimed", n=41),
               _Row(status="done", n=59)]])
    out = asyncio.run(tagger_situation.queue_state(db))
    assert out["queued"] == 941 and out["done"] == 59 and out["total_posts"] == 1_000
    assert "NOT" in out["queued_means"]


def test_the_provenance_marker_carries_its_own_disclaimer():
    """`auto_tagme` will be looked up whatever this module does — it reads like a
    queue. It ships with `is_the_queue: False` so the number cannot travel alone."""
    db = _DB([[696_481]])
    out = asyncio.run(tagger_situation.provenance_marker_counts(db))
    assert out["auto_tagme_posts"] == 696_481
    assert out["is_the_queue"] is False
    # It names where the real queue is (post_tagging_work since the tagging-work move).
    assert "post_tagging_work" in out["meaning"]


def test_character_coverage_reports_the_three_facts_that_only_mean_something_together():
    """Coverage, applications in the last day, and which hosts carry pixai. The
    state worth seeing is the contradiction — every host loaded, zero applied —
    and it is only a contradiction when the three arrive together."""
    db = _DB([
        [40],                                            # tag_category lookup by name
        [_Row(covered=329_985, total=1_027_004)],
        [],                                              # no character adds in 24h
        [_Row(host="primary", has_pixai=True), _Row(host="nodec", has_pixai=True)],
    ])
    out = asyncio.run(tagger_situation.character_coverage(db))
    assert round(out["pct"], 1) == 32.1
    assert out["applied_24h_total"] == 0
    assert out["pixai_hosts_loaded"] == 2 and out["pixai_hosts_seen"] == 2
    assert "CPU" in out["note"], "the log line that misread as a provider is named here"


def test_the_worker_query_carries_the_declared_provider():
    """`tagger_expected_worker.expected_provider` is where 'is this host on its
    GPU' has a dated, declared answer. A 2026-07-20 note saying nodec had degraded
    to CPU was still being quoted as current on 2026-09-05, when nodec was on CUDA."""
    db = _DB([[], []])
    asyncio.run(tagger_situation.worker_nodes(db))
    assert "expected_provider" in db.sql[0]
    assert "tagger_expected_worker" in db.sql[0] and "tagger_node_state" in db.sql[0]


def test_every_derivation_is_shared_not_copied():
    """The dashboard renders these facts as HTML and the endpoint as JSON. Both
    must call this module: two copies of one query is how the two renderings drift
    apart, which this repository has already paid for elsewhere."""
    import pathlib
    root = pathlib.Path(__file__).resolve().parents[1]
    pools = (root / "api" / "routers" / "pools.py").read_text(encoding="utf-8")
    assert "tagger_situation" in pools, "the HTML dashboard must use the shared derivation"
    # and the old inverted ratio must be gone, not merely unused
    assert "AS total_done" not in pools, "the done-count denominator that produced 1308.9% is still here"


def test_the_situation_route_is_discoverable():
    """The facts lived on `include_in_schema=False` before, which is why nothing
    found them. Whatever else changes, this route stays in the schema."""
    from api.routers import tagger as tagger_router
    routes = {r.path: r for r in tagger_router.router.routes}
    assert "/api/tagger/situation" in routes
    assert routes["/api/tagger/situation"].include_in_schema is True


def test_the_character_category_is_looked_up_by_name_not_by_id():
    """The id was 40 and was written into three queries. A renumbering would not
    raise there — it would return zero, and "no character tags anywhere" is a
    sentence this system can produce for real reasons, so it would be believed."""
    db = _DB([[40]])
    assert asyncio.run(tagger_situation.character_category_id(db)) == 40
    assert "tag_category" in db.sql[0] and "name = :n" in db.sql[0]


def test_a_missing_category_raises_instead_of_reporting_zero_coverage():
    db = _DB([[None]])
    with pytest.raises(LookupError) as e:
        asyncio.run(tagger_situation.character_category_id(db))
    assert "nothing is tagged" in str(e.value)


def test_no_character_category_id_is_hardcoded_anywhere():
    """Both renderings must reach the category through the lookup. A literal that
    survives in one of them is the drift this consolidation exists to end."""
    import pathlib, re
    root = pathlib.Path(__file__).resolve().parents[1]
    for rel in ("api/services/tagger_situation.py", "api/routers/pools.py"):
        src = (root / rel).read_text(encoding="utf-8")
        assert not re.search(r"category_id\s*=\s*40", src), f"{rel} still pins the id"
