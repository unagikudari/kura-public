"""Where the tagger's direct database readers find kura.

Most of the tagger reaches kura through its HTTP API (tagger/api_target.py). The
training side does not: tagger/vnext_runner.py, vnext_arms.py and vnext_triage.py read
the database directly, from two places that used to be written into each call:

    read       bulk reads that may come from a nearby copy (a replica)
               KURA_READ_DSN       default: 127.0.0.1 on a node listed in
                                   canonical.read_replicas, else the canonical node
    canonical  tables the replica does not carry, and writes
               KURA_CANONICAL_DSN  default: canonical.host / canonical.pg_port

The defaults come from tagger/deploy/nodes.json "canonical", read from the checkout by
every entry point (workers, vnext runner/arms/triage, training, the heads scripts). Moving
kura changes that block; nothing else has to be edited per node. An explicit
KURA_READ_DSN / KURA_CANONICAL_DSN in a node's env still wins (and then has to be changed
by hand: scripts/canonical_inventory.py lists such lines).

Values are libpq connection strings (key=value or postgresql:// URIs), quoted so a
shell that sources the file reads them whole.

Not routed through here: register_ensemble.py and heads/promote_verified.py read
DATABASE_URL from the node's api/env (listed by scripts/canonical_inventory.py when it
names the canonical host).
"""
from __future__ import annotations

import json
import os
from collections.abc import Mapping
from pathlib import Path

DEFAULT_READ_DSN = "host=127.0.0.1 dbname=kura user=kura password=kura"


_DECLARATION = Path(__file__).resolve().parent / "deploy" / "nodes.json"


def declared_canonical(environ: Mapping[str, str] | None = None) -> dict:
    """The canonical kura node as tagger/deploy/nodes.json declares it ("canonical").

    Read from the checkout, not from env: every entry point (worker, vnext runner, triage,
    training) imports this module, so all of them follow one declaration, and no stale
    EnvironmentFile line can outrank it (codex review of 1.268.0). KURA_NODES_DECLARATION
    points at another file (tests)."""
    environ = os.environ if environ is None else environ
    path = Path((environ.get("KURA_NODES_DECLARATION") or "").strip() or _DECLARATION)
    try:
        return json.loads(path.read_text(encoding="utf-8"))["canonical"]
    except FileNotFoundError:
        # The inventory is private deployment and not in the public artifact
        # (release/public_artifact_policy.json); there the connection is configured
        # explicitly. Resolved when used, never at import (codex review of 1.268.1).
        raise RuntimeError(
            f"no canonical kura node declared ({path} is absent): set KURA_CANONICAL_DSN "
            "(and KURA_READ_DSN), or KURA_CANONICAL_HOST and KURA_CANONICAL_PGPORT") from None


def canonical_host(environ: Mapping[str, str] | None = None) -> str:
    """KURA_CANONICAL_HOST when set, else the declared node. The explicit value wins over the
    declaration: a node env carrying it is an override that a move has to change, and
    scripts/canonical_inventory.py lists it (a bare `=<host>` assignment)."""
    environ = os.environ if environ is None else environ
    return (environ.get("KURA_CANONICAL_HOST") or "").strip() or declared_canonical(environ)["host"]


def canonical_pg_port(environ: Mapping[str, str] | None = None) -> int:
    environ = os.environ if environ is None else environ
    explicit = (environ.get("KURA_CANONICAL_PGPORT") or "").strip()
    return int(explicit or declared_canonical(environ)["pg_port"])


def _declared_read_replicas(environ: Mapping[str, str]) -> list[str]:
    """No declaration (a public build configured by env) means no replica."""
    try:
        return declared_canonical(environ).get("read_replicas", [])
    except RuntimeError:
        return []


def default_canonical_dsn(environ: Mapping[str, str] | None = None) -> str:
    return (f"host={canonical_host(environ)} port={canonical_pg_port(environ)} "
            "dbname=kura user=kura password=kura")


def _this_host() -> str:
    import socket
    return socket.gethostname().split(".")[0]


def read_dsn(environ: Mapping[str, str] | None = None, host: str | None = None) -> str:
    """KURA_READ_DSN when set; else the local copy on a node the declaration lists in
    canonical.read_replicas; on the canonical node its own database over 127.0.0.1; else
    the canonical node over the network. Today that is trainer →
    its replica, every other node → primary; after the move, dropping trainer from the list
    sends its reads to nodeb (docs/notes/kura-on-nodeb-plan.md)."""
    environ = os.environ if environ is None else environ
    explicit = (environ.get("KURA_READ_DSN") or "").strip()
    if explicit:
        return explicit
    host = host or _this_host()
    if host in _declared_read_replicas(environ):
        return DEFAULT_READ_DSN
    if (environ.get("KURA_CANONICAL_DSN") or "").strip():
        return canonical_dsn(environ)       # explicit configuration (a public build, or a pin)
    if host == canonical_host(environ):     # on the canonical node itself: its own socket
        return f"host=127.0.0.1 port={canonical_pg_port(environ)} dbname=kura user=kura password=kura"
    return canonical_dsn(environ)


def canonical_dsn(environ: Mapping[str, str] | None = None) -> str:
    environ = os.environ if environ is None else environ
    return (environ.get("KURA_CANONICAL_DSN") or "").strip() or default_canonical_dsn(environ)


def connect_read(**kwargs):
    import psycopg2
    return psycopg2.connect(read_dsn(), **kwargs)


def connect_canonical(**kwargs):
    import psycopg2
    return psycopg2.connect(canonical_dsn(), **kwargs)
