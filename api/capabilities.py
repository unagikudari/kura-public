"""Which optional capabilities this node runs (release/capability_policy.json, gate G7).

Image tagging is optional: `disabled` is a normal configuration, not a degraded one.
With KURA_TAGGING_ENABLED=0 the API serves the archive without the tagging surfaces
(the worker claim/complete routes under /api/internal/tagging and the tagger release,
policy and artifact routes under /api/tagger). Unset means enabled, as today.
"""
from __future__ import annotations

import os
from collections.abc import Mapping

_OFF = {"0", "false", "no", "off", "disabled"}


def tagging_enabled(environ: Mapping[str, str] | None = None) -> bool:
    environ = os.environ if environ is None else environ
    return (environ.get("KURA_TAGGING_ENABLED") or "1").strip().lower() not in _OFF
