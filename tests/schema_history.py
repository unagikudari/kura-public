"""A migration file the test reads, or a skip where the tree does not ship it.

The public tree starts from the baseline schema and leaves out every migration the
baseline already contains (release/public_artifact_policy.json, schema-history). A
test that loads one of those files runs here and skips there.
"""
from pathlib import Path

import pytest

VERSIONS = Path(__file__).resolve().parents[1] / "api" / "alembic" / "versions"


def migration(filename: str) -> Path:
    path = VERSIONS / filename
    if not path.exists():
        pytest.skip(f"{filename} is schema history this tree does not ship")
    return path
