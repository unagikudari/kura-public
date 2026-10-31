"""Image tagging is optional (release/capability_policy.json, gate G7).

Two things make `disabled` a normal configuration rather than a claim:
- the core (api/**) never imports the tagger package, so it boots on a tree without it;
  the core-side state the API owns (artifact store, transitions, governed config, GT sync)
  lives in api/tagger_core/ since 2026-10-25, and tagger/ imports it from there;
- KURA_TAGGING_ENABLED=0 leaves the tagging surfaces unmounted.

Each check runs api.main in a fresh interpreter with the tagger package made unimportable.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from api import capabilities

REPO = Path(__file__).resolve().parents[1]

_PROBE = r"""
import importlib.abc, json, sys
class NoTagger(importlib.abc.MetaPathFinder):
    def find_spec(self, name, path=None, target=None):
        if name == "tagger" or name.startswith("tagger."):
            raise ImportError(f"the core imported {name}")
        return None
sys.meta_path.insert(0, NoTagger())
from api.main import app
paths = sorted(app.openapi()["paths"])
print(json.dumps(paths))
"""


def _routes(enabled: str | None) -> list[str]:
    env = dict(os.environ)
    env.pop("KURA_TAGGING_ENABLED", None)
    if enabled is not None:
        env["KURA_TAGGING_ENABLED"] = enabled
    out = subprocess.run([sys.executable, "-c", _PROBE], cwd=REPO, env=env,
                         capture_output=True, text=True, timeout=120)
    assert out.returncode == 0, out.stderr[-2000:]
    return json.loads(out.stdout.strip().splitlines()[-1])


@pytest.mark.parametrize("value,expected", [
    (None, True), ("1", True), ("true", True), ("0", False), ("false", False),
    ("off", False), (" Disabled ", False), ("", True),
])
def test_the_flag(value, expected):
    env = {} if value is None else {"KURA_TAGGING_ENABLED": value}
    assert capabilities.tagging_enabled(env) is expected


def test_the_core_boots_without_the_tagger_package_and_without_tagging():
    routes = _routes("0")
    assert any(p.startswith("/api/posts") for p in routes)
    assert not any(p.startswith(("/api/internal/tagging", "/api/tagger")) for p in routes)


def test_enabled_mounts_the_tagging_surfaces_still_without_importing_the_tagger():
    routes = _routes(None)
    assert any(p.startswith("/api/internal/tagging") for p in routes)
    assert any(p.startswith("/api/tagger") for p in routes)


def test_the_old_tagger_paths_are_the_same_modules():
    """Codex 1.266.0 (non-blocking): the compatibility aliases share state with the new home."""
    import importlib

    for name in ("artifact_kind", "artifact_store", "transitions", "config", "gt_sync"):
        old = importlib.import_module(f"tagger.{name}")
        new = importlib.import_module(f"api.tagger_core.{name}")
        assert old is new, name


def test_gt_sync_keeps_its_by_path_entry_point():
    """kura-tagger-auto-cycle.service runs tagger/gt_sync.py by path. Running it here would
    start a real GT sync, so check the entry instead: loaded from its path (not as __main__)
    it resolves to the moved module, and as __main__ it calls that module's main()."""
    import importlib.util

    spec = importlib.util.spec_from_file_location("gt_sync_by_path", REPO / "tagger" / "gt_sync.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    import api.tagger_core.gt_sync as moved
    assert module._impl is moved
    entry = (REPO / "tagger" / "gt_sync.py").read_text()
    assert 'if __name__ == "__main__":\n    sys.exit(_impl.main())' in entry
