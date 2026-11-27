"""Compatibility import for the shared canonical_evaluation module."""
import sys

from api.tagger_core import canonical_evaluation as _impl

sys.modules[__name__] = _impl
