"""A retracted human deletion must not leave a stale negative GT label.

_sync_human_corrections read every human post_tag_audit row touching a tag and
upserted one label per row, with NO ORDER BY -- so when a person removed a tag
and put it back minutes later, whichever row the database happened to return
last decided the verdict. Measured on post 364188: 'anal_object_insertion'
removed 04:02:29, re-added 04:06:49 by the same person, GT stuck at 'negative'
([[gt-negative-never-retracted]], reported by trainer in the round-9 review).

Fix: order newest-first per post and let the first row that actually decides the
tag win. A rename row (same concept added and removed, e.g. Paruru/paruru) still
decides nothing and falls through to the next-newest event.
"""
import os
import pathlib
import re

import pytest

GT_SYNC = pathlib.Path(__file__).resolve().parents[1] / "api" / "tagger_core" / "gt_sync.py"
SRC = GT_SYNC.read_text()


def test_correction_query_orders_newest_first():
    # Must sort by post then recency, matching ix_post_tag_audit_post_id
    # (post_id, created_at DESC, id DESC) so the scan stays index-ordered.
    assert re.search(
        r"ORDER BY pa\.post_id, pa\.created_at DESC, pa\.id DESC", SRC
    ), "human-correction query must order newest-first per post"


def test_only_the_newest_deciding_event_is_applied():
    # Ordering alone is not enough -- without the per-post guard every older row
    # would still be upserted afterwards and overwrite the newest verdict.
    assert "seen: set[int] = set()" in SRC
    assert "if r.post_id in seen:" in SRC
    assert "seen.add(r.post_id)" in SRC


def test_rename_rows_do_not_consume_the_post():
    """A rename must not mark the post decided.

    `seen.add()` has to come AFTER the rename `continue`, otherwise a later
    rename would swallow a genuine correction sitting underneath it.
    """
    body = SRC.split("async def _sync_human_corrections", 1)[1]
    body = body.split("async def ", 1)[0]
    rename_skip = body.index("if tag.lower() in rename_concepts:")
    seen_add = body.index("seen.add(r.post_id)")
    assert rename_skip < seen_add, \
        "the rename skip must precede seen.add(), or renames consume the post"


def _dsn():
    return os.environ.get("KURA_TEST_DSN") or os.environ.get("KURA_DB_DSN")


@pytest.mark.skipif(not _dsn(), reason="no kura DB DSN available")
def test_no_stale_negatives_among_tags_gt_sync_actually_rederives():
    """The empirical form: for every (post, tag) gt_sync is in scope for, a GT
    'negative' must not contradict a newest-event-is-an-add history.

    Scoped to status='candidate' on purpose -- gt_sync only re-derives open
    candidates ("a targeted sync ... not a full-history backfill", module
    docstring), so held/promoted/dismissed tags legitimately keep whatever was
    stored when they were last in scope.
    """
    import psycopg2
    cx = psycopg2.connect(_dsn())
    cur = cx.cursor()
    cur.execute("""
        WITH latest AS (
          SELECT DISTINCT ON (pa.post_id, hc.tag)
                 pa.post_id, hc.tag,
                 (hc.tag = ANY(pa.added_tags)) AS latest_is_add
          FROM tagger_head_candidate hc
          JOIN pool_post pp ON pp.pool_id = hc.source_pool_id
          JOIN post_tag_audit pa ON pa.post_id = pp.post_id AND pa.actor_type = 'human'
          JOIN post po ON po.id = pa.post_id AND po.status = 'ok'
          WHERE hc.status = 'candidate'
            AND (hc.tag = ANY(pa.added_tags) OR hc.tag = ANY(pa.removed_tags))
          ORDER BY pa.post_id, hc.tag, pa.created_at DESC, pa.id DESC
        )
        SELECT count(*) FROM latest l
        JOIN tagger_gt_label g ON g.post_id = l.post_id AND g.tag = l.tag
        WHERE l.latest_is_add AND g.verdict = 'negative'
    """)
    stale = cur.fetchone()[0]
    cx.close()
    assert stale == 0, (
        f"{stale} GT negative(s) contradict a newest-event-is-an-add history on "
        f"tags gt_sync re-derives -- a retraction was not ingested"
    )
