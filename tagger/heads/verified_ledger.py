"""The curated ground-truth ledger — one human judgment about one (concept, post).

kura#64: the old rows were `{c, pid, v, pool, t}` — concept, post, pos/neg, pool,
time. They recorded WHAT was judged and WHICH WAY, and nothing about HOW the
judgment was reached or BY WHOM. So a machine guess (a head applied a tag) and a
human judgment (a person reviewed the pool and left it) sat in the same column,
indistinguishable, and the trainer read both as "curated". Three tags turned out
to be 100% machine-applied while claiming to be human ground truth.

This module is the one place that says what a ledger row IS, and the row now
carries its own provenance so a later reader — a person or the trainer — can tell
curated truth from a machine guess by reading the row alone, no cross-reference:

    concept    the tag being judged            (e.g. "phimosis")
    post_id    the post it is judged on
    label      "positive" | "negative"         the judgment
    source     HOW the judgment was reached     (see SOURCES below)
    actor      WHO made it                      (a username, or a machine id)
    pool       the pool it came through, or None
    at         ISO-8601 timestamp

`source` is the field kura#64 adds, and it is REQUIRED. It is not free text: it is
one of a closed set, and only the `human_*` members count as curated ground truth.
A row whose source is machine, unknown, or absent is a candidate, never a teacher —
`is_curated` is what the loader asks, and it answers by the row, not by a table
join that can go stale.

SOURCES (closed set; widening it is a schema-version bump, not an edit):
    human_pool_review  a person reviewed the pool and LEFT the tag on the post.
                       Evidence: the pool carries human review; the absence of a
                       removal is the positive judgment. Used for `positive`.
    human_removed      a person REMOVED the tag — recorded in post_tag_audit with
                       an actor that is a human. The removal itself is the
                       evidence. Used for `negative`.
    human_import       hand-curated outside any pool (e.g. a hand-built seed set).
                       The actor names who imported it.
    machine_applied    a head applied this tag; no human has confirmed it. NOT
                       curated — this is the demotion target for the old rows that
                       duplicated the machine applied-log.

The schema is versioned. `SCHEMA` pins it; a reader that meets a newer schema it
does not understand must refuse rather than guess, the same discipline the rest of
this fleet uses for typed evidence.
"""
from __future__ import annotations

from typing import Any

SCHEMA = "kura-verified-gt/2"

#: The closed source set. Membership in HUMAN_SOURCES is the whole definition of
#: "curated" — there is no second place that decides it.
HUMAN_SOURCES = frozenset({"human_pool_review", "human_removed", "human_import"})
MACHINE_SOURCES = frozenset({"machine_applied"})
SOURCES = HUMAN_SOURCES | MACHINE_SOURCES

LABELS = frozenset({"positive", "negative"})

_REQUIRED = ("schema", "concept", "post_id", "label", "source", "actor", "at")


class LedgerError(ValueError):
    """A row that does not conform. Nothing partial is accepted."""


def problems(row: object) -> list[str]:
    """Every way `row` fails the schema, or [] when it is a valid row.

    Returns all problems at once (not the first) so a backfill or a test sees the
    whole gap rather than fixing them one round-trip at a time.
    """
    if not isinstance(row, dict):
        return [f"row must be an object, got {type(row).__name__}"]
    out: list[str] = []
    if row.get("schema") != SCHEMA:
        out.append(f"schema pin mismatch: {row.get('schema')!r} (want {SCHEMA!r})")
    missing = [k for k in _REQUIRED if k not in row]
    if missing:
        out.append(f"missing fields: {missing}")
    if row.get("label") not in LABELS:
        out.append(f"label {row.get('label')!r} not in {sorted(LABELS)}")
    if row.get("source") not in SOURCES:
        out.append(f"source {row.get('source')!r} not in {sorted(SOURCES)}")
    pid = row.get("post_id")
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        out.append(f"post_id must be a positive int, got {pid!r}")
    for k in ("concept", "actor", "at"):
        if k in row and (not isinstance(row[k], str) or not row[k]):
            out.append(f"{k} must be a non-empty string")
    # negative-with-human_pool_review and positive-with-human_removed are the two
    # combinations that would misread the evidence: a pool review's evidence is the
    # ABSENCE of a removal (positive), a removal's evidence is the removal itself
    # (negative). Cross them and the source no longer means what it says.
    if row.get("source") == "human_pool_review" and row.get("label") == "negative":
        out.append("human_pool_review cannot justify a negative (its evidence is a tag left in place)")
    if row.get("source") == "human_removed" and row.get("label") == "positive":
        out.append("human_removed cannot justify a positive (its evidence is a removal)")
    return out


def parse(row: object) -> dict:
    """Validate and return the row, or raise LedgerError listing every problem."""
    errs = problems(row)
    if errs:
        raise LedgerError("; ".join(errs))
    return row  # type: ignore[return-value]


def is_curated(row: dict) -> bool:
    """True iff this row is human ground truth the trainer may learn from.

    A machine row, an unknown source, a malformed row: all False. The trainer asks
    this and nothing else — the answer is in the row, not in a join.
    """
    return isinstance(row, dict) and row.get("source") in HUMAN_SOURCES and not problems(row)


def make_row(*, concept: str, post_id: int, label: str, source: str, actor: str,
             at: str, pool: int | None = None) -> dict:
    """Build a conforming row. Raises before returning anything malformed, so a
    writer cannot emit a row this module would later reject on read."""
    row: dict[str, Any] = {
        "schema": SCHEMA, "concept": concept, "post_id": post_id, "label": label,
        "source": source, "actor": actor, "at": at, "pool": pool,
    }
    return parse(row)


def migrate_legacy(old: dict, *, machine_applied: bool, actor_for_human: str) -> dict:
    """Turn a pre-#64 row `{c, pid, v, pool, t}` into a schema-2 row.

    The one thing the old row could not say — provenance — is supplied by the
    caller from the only evidence that survives: whether the (concept, post) also
    appears in the machine applied-log. A row that duplicates a machine application
    is demoted to `machine_applied`; one that does not is independent human work
    (the hand-imports and seed pools kura#64 kept), recorded under `actor_for_human`.

    Label words are widened from the terse pos/neg to positive/negative. The
    source-vs-label consistency is enforced by parse(): a legacy negative that is
    NOT a machine row becomes `human_removed` (a person took it off), a legacy
    positive becomes `human_pool_review` (a person left it on).
    """
    label = "positive" if old.get("v") == "pos" else "negative"
    if machine_applied:
        source = "machine_applied"
        actor = "machine"
    elif label == "negative":
        source = "human_removed"
        actor = actor_for_human
    else:
        source = "human_pool_review"
        actor = actor_for_human
    return make_row(concept=old["c"], post_id=int(old["pid"]), label=label,
                    source=source, actor=actor, at=old.get("t", ""), pool=old.get("pool"))
