"""Compatibility import for the shared execution_receipt module."""
import sys

from api.tagger_core import execution_receipt as _impl

sys.modules[__name__] = _impl
