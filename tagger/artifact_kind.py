"""Moved to api/tagger_core/artifact_kind.py (2026-10-25, release gate G7: the core must not import
the tagger package). This alias keeps `tagger.artifact_kind` working for code outside this repo
and for checkouts that have not caught up; it is the same module object, not a copy."""
import sys

from api.tagger_core import artifact_kind as _impl

sys.modules[__name__] = _impl
