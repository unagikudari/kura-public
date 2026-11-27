"""Compatibility import for the shared experiment_status module."""
import sys

from api.tagger_core import experiment_status as _impl

sys.modules[__name__] = _impl
