"""The installed Alembic entry point must load revisions without a PYTHONPATH wrapper."""
import os
from pathlib import Path
import subprocess
import sys


def test_console_entry_point_loads_revision_imports():
    root = Path(__file__).resolve().parents[1]
    env = dict(os.environ)
    env.pop("PYTHONPATH", None)
    # Executing the console script (rather than python -m) does not add cwd to sys.path.
    cli = Path(sys.executable).parent / "alembic"
    result = subprocess.run(
        [str(cli), "heads"], cwd=root, env=env, text=True, capture_output=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert "(head)" in result.stdout
