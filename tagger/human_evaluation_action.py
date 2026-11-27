"""Compatibility import for the shared human_evaluation_action module."""
import sys

from api.tagger_core import human_evaluation_action as _impl

sys.modules[__name__] = _impl
