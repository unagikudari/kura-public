"""The machine-to-machine write gate (kura#43 item 1): mode + key semantics.

observe (default) never breaks a caller; enforce rejects; a key that equals a
filename/session secret is refused (no silent re-coupling, kura#43 item 2); an
unset key in enforce mode is a 500, not a silent open.
"""
import asyncio
import types

import pytest
from fastapi import HTTPException

from api.errors import KuraError

from api import api_key, auth


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for k in ("KURA_API_KEY", "KURA_API_KEY_ENFORCE", "KURA_FS_NAME_SALT",
              "KURA_SECRET", "KURA_DASHBOARD_KEY", "KURA_SESSION_SECRET"):
        monkeypatch.delenv(k, raising=False)
    api_key._warned.clear()
    auth._api_key_warned.clear()


def _req(headers=None):
    return types.SimpleNamespace(
        headers=headers or {},
        url=types.SimpleNamespace(path="/api/machine-only/dispatch"),
    )


def _call(req):
    return asyncio.run(auth.require_api_key(req))


# ── mode default ─────────────────────────────────────────────────────────────
def test_enforcing_defaults_to_observe(monkeypatch):
    assert api_key.enforcing() is False
    monkeypatch.setenv("KURA_API_KEY_ENFORCE", "observe")
    assert api_key.enforcing() is False
    monkeypatch.setenv("KURA_API_KEY_ENFORCE", "enforce")
    assert api_key.enforcing() is True


# ── observe never breaks ─────────────────────────────────────────────────────
def test_observe_allows_missing_key(monkeypatch):
    monkeypatch.setenv("KURA_API_KEY", "a-strong-key")
    assert _call(_req()) is None            # missing header, observe -> allowed


def test_observe_allows_wrong_key(monkeypatch):
    monkeypatch.setenv("KURA_API_KEY", "a-strong-key")
    assert _call(_req({"X-Api-Key": "nope"})) is None


def test_observe_allows_even_when_key_unset(monkeypatch):
    # observe's whole job is not to break things while the key is distributed.
    assert _call(_req()) is None


# ── enforce rejects ──────────────────────────────────────────────────────────
def test_enforce_rejects_missing_key(monkeypatch):
    monkeypatch.setenv("KURA_API_KEY", "a-strong-key")
    monkeypatch.setenv("KURA_API_KEY_ENFORCE", "enforce")
    with pytest.raises(KuraError) as ei:  # 2026-09-05: the gate now
        # raises the structured envelope so the refusal names where the
        # key lives; a bare 401 was the measured dead end.
        _call(_req())
    assert ei.value.status == 401
    assert ei.value.body()["context"]["credential"] == "KURA_API_KEY"


def test_enforce_rejects_wrong_key(monkeypatch):
    monkeypatch.setenv("KURA_API_KEY", "a-strong-key")
    monkeypatch.setenv("KURA_API_KEY_ENFORCE", "enforce")
    with pytest.raises(KuraError) as ei:  # 2026-09-05: the gate now
        # raises the structured envelope so the refusal names where the
        # key lives; a bare 401 was the measured dead end.
        _call(_req({"X-Api-Key": "nope"}))
    assert ei.value.status == 401
    assert ei.value.body()["context"]["credential"] == "KURA_API_KEY"


def test_enforce_allows_correct_key(monkeypatch):
    monkeypatch.setenv("KURA_API_KEY", "a-strong-key")
    monkeypatch.setenv("KURA_API_KEY_ENFORCE", "enforce")
    assert _call(_req({"X-Api-Key": "a-strong-key"})) is None


def test_enforce_without_key_is_500_not_open(monkeypatch):
    monkeypatch.setenv("KURA_API_KEY_ENFORCE", "enforce")  # no KURA_API_KEY
    with pytest.raises(HTTPException) as ei:
        _call(_req())
    assert ei.value.status_code == 500


# ── no silent re-coupling (kura#43 item 2) ───────────────────────────────────
def test_key_equal_to_a_secret_is_refused(monkeypatch):
    monkeypatch.setenv("KURA_FS_NAME_SALT", "sixchr")
    monkeypatch.setenv("KURA_API_KEY", "sixchr")     # same as the filename salt
    assert api_key.configured_key() is None
    # enforce mode with an effectively-unconfigured key -> 500, not open.
    monkeypatch.setenv("KURA_API_KEY_ENFORCE", "enforce")
    with pytest.raises(HTTPException) as ei:
        _call(_req({"X-Api-Key": "sixchr"}))
    assert ei.value.status_code == 500


def test_correct_key_passes_and_empty_never_matches(monkeypatch):
    monkeypatch.setenv("KURA_API_KEY", "a-strong-key")
    assert api_key.key_matches("a-strong-key") is True
    assert api_key.key_matches("") is False
    assert api_key.key_matches(None) is False
