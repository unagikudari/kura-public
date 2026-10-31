"""Pool display-name derivation — THE single spelling (kura#42).

This function existed twice (pools.py:163, upload.py:333) and drifted: each
copy knew a rule the other did (one a per-site name, the other the '.zip'
strip). Pool-route ingestion used the pools copy, /api/upload used its own — so
one archived set arrived as '79_Megumin.zip' through one door and '79_Megumin'
through the other: the exact same-creator-two-spellings split the upload copy's
comment was written to prevent. One implementation, union of both histories,
both routers delegate.
"""
from __future__ import annotations

import re  # noqa: F401  (generic 既定は正規表現を使わないが、規則追加の入口として残す)

from api import site_rules


def derive_pool_name_from_url(url: str) -> str:
    """Pick a stable display name for a pool created from a collection URL.

    A site adapter (``site_rules.pool_name``) may name the pool from its URL
    — e.g. '<site>-user-<id>' for an account page, or a set name with an
    archive's '.zip' stripped (the pool is the SET, not the archive — a
    folder-shipped set is '47_Fern', so a zip-shipped one must be
    '79_Megumin', not '79_Megumin.zip'). Without an adapter rule:

    fallback                      → last meaningful path segment (truncated)
    """
    named = site_rules.pool_name(url)
    if named:
        return named
    seg = url.rstrip("/").rsplit("/", 1)[-1] or url
    return seg[:200]
