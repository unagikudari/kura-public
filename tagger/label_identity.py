"""Compatibility import for the shared label_identity module."""
import sys

from api.tagger_core import label_identity as _impl

sys.modules[__name__] = _impl
