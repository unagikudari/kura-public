"""Compatibility import for the shared stratified_evaluation module."""
import sys

from api.tagger_core import stratified_evaluation as _impl

sys.modules[__name__] = _impl
