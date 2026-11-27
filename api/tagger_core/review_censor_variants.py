"""Prefer a usable uncensored rendition of a verified same-image pair.

The existing censor_pair ledger records confirmed twins. Perceptual
matches alone are proposals, since a low dHash distance can join different
drawings and a mosaic can change the hash substantially.
"""
from __future__ import annotations

from api.services.pool_visibility import post_visible_sql
from api.tagger_core.post_exclusion import not_excluded


CENSOR_REVIEW_TAGS = ("censored", "uncensored", "mosaic_censoring", "bar_censor", "censor")
_TAGS = ", ".join(f"'{tag}'" for tag in CENSOR_REVIEW_TAGS)
_MARKERS = ", ".join(f"'{tag}'" for tag in CENSOR_REVIEW_TAGS if tag != "uncensored")


def use_uncensored_pairs(post_id):
    """SQL row set of confirmed, currently eligible alternatives to this image."""
    return f"""
        SELECT pair.uncensored_post_id
        FROM censor_pair pair
        JOIN post alt ON alt.id=pair.uncensored_post_id
        WHERE pair.censored_post_id={post_id} AND pair.confirmed
          AND pair.uncensored_post_id<>pair.censored_post_id
          AND alt.status='ok' AND alt.type='image' AND {not_excluded('alt.id')}
          AND ({post_visible_sql('alt.id')})
          AND EXISTS (
            SELECT 1 FROM post_tag pt JOIN tag_name n USING(tag_id)
            WHERE pt.post_id=pair.censored_post_id AND n.name IN ({_MARKERS}))
          AND NOT EXISTS (
            SELECT 1 FROM post_tag pt JOIN tag_name n USING(tag_id)
            WHERE pt.post_id=alt.id AND n.name IN ({_MARKERS}))
          AND ((alt.media_location='pg' AND alt.data IS NOT NULL) OR EXISTS (
            SELECT 1 FROM post_rendition r
            WHERE r.post_id=alt.id AND r.kind='view'))
    """


def review_candidate(post_id, tag_expression):
    return f"({tag_expression} IN ({_TAGS}) OR NOT EXISTS ({use_uncensored_pairs(post_id)}))"


def excluded_with_cursor(cursor, post_ids):
    cursor.execute(f"""
        SELECT DISTINCT p.censored_post_id FROM censor_pair p
        WHERE p.censored_post_id=ANY(%s) AND EXISTS ({use_uncensored_pairs('p.censored_post_id')})
    """, (list(post_ids),))
    return {row[0] for row in cursor.fetchall()}
