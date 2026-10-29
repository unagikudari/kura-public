# Description: tag-ID-keyed freeze validation for wd14 patch merges (pure, no torch/numpy).
# Kind: lib
# Effects: none
# Requires: none
# Runs-on: any
"""registry_freeze.py — the save-gate for build_release.py (plan:kura-tagger-registry rev2 Step 3).

Merging per-tag logit patches into wd14 is only safe if every row the merge did
NOT touch is provably unchanged. Row *position* is not a safe identity for that
check: adding a brand-new tag (smegma, licking_testicle) appends a row, so "row
count is invariant" is the wrong invariant, and a base-model re-download could in
principle reorder tag_id assignments in a rebuild's base vocabulary. So identity
here is the *tag*, not its position: real danbooru tags key off the CSV's
`tag_id` column; brand-new tags absent from the stock vocab share the CSV's `-1`
sentinel tag_id and key off `name` instead (see identity_key).

Every check here operates on plain python data (dicts / tuples of float) so it
can be unit-tested with the system interpreter, no torch/numpy/safetensors
required — those only enter in build_release.py's IO glue, at the boundary.
"""
from __future__ import annotations


class FreezeViolation(Exception):
    """Raised when a candidate merge would touch tags outside its own allowlist."""


def identity_key(tag_id: int | None, name: str) -> str:
    """Primary key for a tag row: the danbooru tag_id, or `name` when tag_id is
    absent/the `-1` sentinel used for tags with no danbooru vocabulary entry
    (multiple such rows legitimately share tag_id=-1, so it cannot be the key)."""
    if tag_id is None or int(tag_id) == -1:
        return f"name:{name}"
    return f"id:{tag_id}"


def classify_op(logit_index: int, n_stock: int) -> str:
    """A patch either overwrites an existing stock row (`update`) or appends a
    brand-new one immediately past the stock vocabulary (`add`)."""
    return "update" if logit_index < n_stock else "add"


def check_uniqueness(rows: list[dict]) -> None:
    """rows: [{"tag_id": int|None, "name": str}, ...] in final row order.
    Raises if two rows resolve to the same identity key."""
    seen: dict[str, int] = {}
    for i, r in enumerate(rows):
        k = identity_key(r.get("tag_id"), r["name"])
        if k in seen:
            raise FreezeViolation(
                f"duplicate tag identity {k!r} at rows {seen[k]} and {i} — "
                "merged vocabulary is not well-formed")
        seen[k] = i


def check_completeness(base_rows: list[dict], merged_rows: list[dict]) -> None:
    """Every base (stock) row must survive, unchanged, as an identity-order
    prefix of merged_rows — new tags may only ever be appended after it. A
    reordered, dropped, or mutated stock row fails this before any weight is
    even inspected."""
    if len(merged_rows) < len(base_rows):
        raise FreezeViolation(
            f"merged vocabulary ({len(merged_rows)} rows) is smaller than "
            f"base ({len(base_rows)} rows) — a stock tag was dropped")
    for i, base in enumerate(base_rows):
        m = merged_rows[i]
        b_key = (base.get("tag_id"), base.get("name"), base.get("category"))
        m_key = (m.get("tag_id"), m.get("name"), m.get("category"))
        if b_key != m_key:
            raise FreezeViolation(
                f"stock row {i} changed identity: base={b_key} merged={m_key} "
                "— vocabulary was reordered or mutated")


def check_frozen_cells(
    base_vectors: dict[str, tuple[tuple[float, ...], float]],
    merged_vectors: dict[str, tuple[tuple[float, ...], float]],
    allowed_keys: set[str],
) -> None:
    """base_vectors / merged_vectors: identity_key -> (weight_row, bias).
    Every base key NOT in allowed_keys must be byte-for-byte identical in the
    merged model. This is the entire justification for shipping a patch as "a
    wd14 with N tags fixed" rather than a wholly new, unverified model."""
    for k, (bw, bb) in base_vectors.items():
        if k in allowed_keys:
            continue
        if k not in merged_vectors:
            raise FreezeViolation(f"non-allowlisted row {k!r} missing from merged model")
        mw, mb = merged_vectors[k]
        if tuple(mw) != tuple(bw) or mb != bb:
            raise FreezeViolation(
                f"non-allowlisted row {k!r} changed weight/bias — freeze violated, "
                "this is not a wd14-with-a-few-tags-fixed, it is a different model")


def check_allowlist_rows_present(
    allowed_keys: set[str],
    merged_vectors: dict[str, tuple[tuple[float, ...], float]],
) -> None:
    """Sanity check the other direction: every row the manifest allowlisted was
    actually written into the merged model (a build that silently drops an
    allowlisted patch is not "extra safe", it is wrong)."""
    missing = sorted(k for k in allowed_keys if k not in merged_vectors)
    if missing:
        raise FreezeViolation(f"allowlisted rows missing from merged model: {missing}")


def validate_merge(
    base_rows: list[dict],
    merged_rows: list[dict],
    base_vectors: dict[str, tuple[tuple[float, ...], float]],
    merged_vectors: dict[str, tuple[tuple[float, ...], float]],
    allowed_keys: set[str],
) -> None:
    """Run the full save-gate. Raises FreezeViolation on ANY violation; a caller
    that catches nothing here and reaches the save step is safe to save."""
    check_uniqueness(merged_rows)
    check_completeness(base_rows, merged_rows)
    check_frozen_cells(base_vectors, merged_vectors, allowed_keys)
    check_allowlist_rows_present(allowed_keys, merged_vectors)
