"""A 401 is the one message the blocked caller is guaranteed to read.

Naming only the header tells a caller which word to type and leaves it hunting for
the value. On 2026-09-05 an agent worked inside kura for a day with the key sitting
in a file it never opened: it hit the tagger dashboard's browser login and gave up,
answered a queue question from a tag that merely sounded like one, and routed around
every surface it could not reach. The credential was two lines away the whole time.

So the refusal carries where the credential lives and how to load it without printing
it. Never the value: `set -a; . file; set +a` is in the message precisely because
`cat` on that file is how a secret reaches a transcript.

These tests pin both halves — that the way in is named, and that nothing in the
message could be the secret itself.
"""
from __future__ import annotations

import pytest
from api.errors import KuraError


def _detail_of(exc_info) -> dict:
    detail = exc_info.value.body()
    assert isinstance(detail, dict), (
        "the refusal must be structured — a prose sentence cannot be read by the "
        "caller that is most likely to be a program")
    return detail


def test_the_api_key_refusal_names_the_credential_and_its_file(monkeypatch):
    from api import api_key, auth

    monkeypatch.setattr(api_key, "key_matches", lambda presented: False)
    monkeypatch.setattr(api_key, "enforcing", lambda: True)
    monkeypatch.setattr(api_key, "configured_key", lambda: "something")

    class _Req:
        headers: dict[str, str] = {}
        class url:            # noqa: D106 - the two attributes the guard reads
            path = "/api/machine-only"

    import asyncio
    with pytest.raises(KuraError) as e:
        asyncio.run(auth.require_api_key(_Req()))
    d = _detail_of(e)
    assert d["context"]["credential"] == "KURA_API_KEY"
    assert "api/env" in d["context"]["lives_in"]
    assert d["context"]["header"] == api_key.HEADER_NAME
    assert "set -a" in d["fix"], (
        "the loader must be the one that prints nothing — a refusal that suggests "
        "`cat` teaches the habit that puts secrets in transcripts")


def test_the_refusal_carries_no_secret_value(monkeypatch):
    """It says where the key is, never what it is. A message that echoed the
    presented value would leak a near-miss key into every log that keeps 401s."""
    from api import api_key, auth

    secret = "sk-this-must-never-appear"
    monkeypatch.setattr(api_key, "key_matches", lambda presented: False)
    monkeypatch.setattr(api_key, "enforcing", lambda: True)
    monkeypatch.setattr(api_key, "configured_key", lambda: secret)

    class _Req:
        headers = {api_key.HEADER_NAME: secret}
        class url:            # noqa: D106
            path = "/api/machine-only"

    import asyncio
    with pytest.raises(KuraError) as e:
        asyncio.run(auth.require_api_key(_Req()))
    assert secret not in str(_detail_of(e))


def test_enforce_without_a_configured_key_still_refuses_loudly(monkeypatch):
    """Unchanged behaviour, pinned because the branch sits beside the one edited:
    choosing to enforce while nothing can be checked is a misconfiguration that must
    surface as a 500, not a gate that quietly lets everything through."""
    from api import api_key, auth

    monkeypatch.setattr(api_key, "key_matches", lambda presented: False)
    monkeypatch.setattr(api_key, "enforcing", lambda: True)
    monkeypatch.setattr(api_key, "configured_key", lambda: None)

    class _Req:
        headers: dict[str, str] = {}
        class url:            # noqa: D106
            path = "/api/machine-only"

    import asyncio
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as e:
        asyncio.run(auth.require_api_key(_Req()))
    assert e.value.status_code == 500
