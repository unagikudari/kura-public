"""Query canonical censor-pair exclusions for tagger review jobs."""
from __future__ import annotations

from api.tagger_core.review_censor_variants import CENSOR_REVIEW_TAGS, excluded_with_cursor
from tagger.kura_db import connect_canonical


def excluded_for_tag(tag, post_ids):
    if tag in CENSOR_REVIEW_TAGS or not post_ids:
        return set()

    with connect_canonical(connect_timeout=5) as conn:
        conn.set_session(readonly=True)
        with conn.cursor() as cursor:
            cursor.execute("SET LOCAL statement_timeout='5s'")
            return excluded_with_cursor(cursor, post_ids)
