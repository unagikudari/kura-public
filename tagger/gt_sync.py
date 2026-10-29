"""Moved to api/tagger_core/gt_sync.py (2026-10-25, release gate G7: the core must not
import the tagger package). This file stays because kura-tagger-auto-cycle.service runs
it by path; imported, it is the same module object as api.tagger_core.gt_sync."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from api.tagger_core import gt_sync as _impl  # noqa: E402

if __name__ == "__main__":
    sys.exit(_impl.main())
sys.modules[__name__] = _impl
