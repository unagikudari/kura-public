"""Broker federation client — resolves a presented key to its broker identity.

kura holds no human accounts of its own; a human signs in with their broker
account key and kura asks the broker "who is this?" via GET /whoami. One
credential then spans broker + apps + kura, with issuance/revocation owned by
the broker alone.

Hits the LOCAL broker replica (config.BROKER_WHOAMI_URL, default
http://localhost:8000) — fast, same-node, no TLS/remote-host surprises. Uses
stdlib urllib (httpx is not installed in the kura-api venv and nodeb has no pip
step on deploy); the blocking call runs off the event loop via asyncio.to_thread.

Two distinct failure modes so callers can tell a bad key from an outage:
  BrokerAuthError  — broker rejected the key (401): the key is wrong.
  BrokerUnreachable — broker could not be contacted: not the user's fault.
"""

import asyncio
import json
import urllib.error
import urllib.request

from api.config import BROKER_WHOAMI_URL


class BrokerAuthError(Exception):
    """The broker rejected the presented key (HTTP 401)."""


class BrokerUnreachable(Exception):
    """The broker could not be reached (timeout / connection / non-401 error)."""


def _whoami_blocking(key: str) -> dict:
    req = urllib.request.Request(
        f"{BROKER_WHOAMI_URL.rstrip('/')}/whoami",
        headers={"X-API-Key": key},
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        if e.code == 401:
            raise BrokerAuthError("broker rejected the key") from e
        raise BrokerUnreachable(f"broker /whoami returned HTTP {e.code}") from e
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as e:
        raise BrokerUnreachable(f"broker /whoami unreachable: {e}") from e


async def whoami(key: str) -> dict:
    """Resolve `key` to the broker identity dict
    {api_client, email, executor, agent_id, trust_tier}.

    Raises BrokerAuthError on a rejected key, BrokerUnreachable on an outage.
    `email` is non-null only for human-account keys (trust_tier == "admin")."""
    if not key:
        raise BrokerAuthError("no key provided")
    return await asyncio.to_thread(_whoami_blocking, key)
