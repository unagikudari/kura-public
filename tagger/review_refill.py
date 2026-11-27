"""Compatibility entry point for the core review_refill service."""
import sys

from api.tagger_core import review_refill as _impl

if __name__ == "__main__":
    _impl.main()
else:
    sys.modules[__name__] = _impl
