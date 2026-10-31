"""How a pool is identified by its primary source URL — SQL shared by every path
that finds or creates a pool by URL."""

# Canonical primary pool lookup — the SINGLE dedup identity for get-or-create-
# pool-by-source-url paths. The normalization is BYTE-IDENTICAL to migration
# 0046's ux_pool_url_primary_canonical index (strip `?p=N`/`&p=N` pagination and
# a trailing slash), so a pre-insert lookup finds exactly the pool the index
# would otherwise collide on. Every creation path must try this AFTER an exact-url
# miss and BEFORE inserting, so canonical-variant URLs (trailing slash / page
# param) reuse the existing pool instead of creating a duplicate sibling — and
# never trip the unique index mid-insert. Placeholder identities (manual:pool/*,
# legacy:*) are excluded, matching the index predicate.
CANONICAL_POOL_LOOKUP_SQL = """
    SELECT pool_id FROM pool_url
    WHERE is_primary AND url !~ '^manual:pool/' AND url !~ '^legacy:'
      AND regexp_replace(regexp_replace(url, '[?&]p=[0-9]+', '', 'g'), '/+$', '')
        = regexp_replace(regexp_replace(:u,  '[?&]p=[0-9]+', '', 'g'), '/+$', '')
    LIMIT 1
"""
