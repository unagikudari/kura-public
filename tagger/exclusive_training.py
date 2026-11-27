"""Compatibility entry point for the core exclusive_training service."""
import sys

from api.tagger_core import exclusive_training as _impl

if __name__ == "__main__":
    _impl.main()
else:
    sys.modules[__name__] = _impl
