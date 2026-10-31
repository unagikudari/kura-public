"""The web app must render the API's refusal, not `[object Object]`.

Why this exists (2026-09-06, found in review of PR #101 round 1).

`api/errors.py` answers a `KuraError` with a structured detail:

    {"detail": {"error": "<slug>", "context": {...}, "fix": "<one action>"}}

and every un-converted site with `{"detail": "<sentence>"}`. Both shapes are live at
once by design — `tests/test_error_envelope.py` is a ratchet, so call sites become
objects a few at a time — which means every reader of `detail` has to handle both, and
the object share only grows.

No reader did. All 31 failure sites in `web/` read `detail` as a string and handed it
straight to `new Error(...)`:

    throw new Error((err as any).detail ?? `API error: ${res.status}`);

`new Error(<object>)` stringifies to `[object Object]`. Measured against the live API on
2026-09-06: `POST /api/upload` with no credential returns the object shape, and that
line produced exactly `[object Object]`. So the `fix` sentence — the single thing the
envelope exists to deliver to whoever is blocked — was the part the UI dropped, and it
dropped it precisely for the refusals someone had bothered to convert.

The two tests below are the two halves that have to hold: nobody reads `detail` raw
again, and the shared reader actually renders both live shapes.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
WEB_SRC = REPO / "web" / "src"
HELPER = WEB_SRC / "lib" / "apiError.ts"

# The shapes that turned a structured detail into a string: `(err as any).detail ?? ` (the
# 28 sites in api.ts and 3 in a site adapter) and `detail.detail || ` (the login action).
#
# The receiver matters, and this is the honest limit of the check. `detail` is not a
# reserved word — `web/src/routes/lora/` reads `mw.detail` and `w.detail` off lora status
# WARNING objects, which are domain values that merely share the name and are already
# strings. Flagging those would be a false alarm, and a check that cries wolf on correct
# code gets suppressed rather than obeyed.
#
# So this matches the tell of a parsed HTTP body: an `as any` cast, or a receiver named
# the way parsed bodies are named here. A future site that reads the envelope through a
# differently named, properly typed receiver escapes it. That is a real gap, stated
# rather than hidden — the shapes it does and does not catch are pinned below.
_RAW_DETAIL_READ = re.compile(
    r"(?:as any\)|\b(?:err|detail|body|json|payload)\b)\.detail\s*(?:\?\?|\|\|)\s")


def _web_sources() -> list[Path]:
    return sorted(p for p in WEB_SRC.rglob("*")
                  if p.suffix in {".ts", ".svelte"} and "node_modules" not in p.parts)


def test_the_population_is_not_empty() -> None:
    """A scan that reads no files passes for the wrong reason."""
    assert _web_sources(), f"no .ts/.svelte sources found under {WEB_SRC}"
    assert HELPER.is_file(), f"the shared reader is missing: {HELPER}"


def test_no_web_source_turns_a_raw_detail_into_a_message() -> None:
    offenders = {}
    for path in _web_sources():
        if path == HELPER:
            continue                    # the one place allowed to look inside `detail`
        hits = [i for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
                if _RAW_DETAIL_READ.search(line)]
        if hits:
            offenders[str(path.relative_to(REPO))] = hits
    assert not offenders, (
        "these read the API's `detail` directly. When the site they call has been "
        "converted to the KuraError envelope, `detail` is an OBJECT and this renders as "
        "`[object Object]` — the caller is shown nothing, least of all the `fix`:\n"
        + "\n".join(f"    {p}: line(s) {', '.join(map(str, ls))}" for p, ls in sorted(offenders.items()))
        + "\nFix: import { apiErrorMessage } from '#lib/apiError.ts' and pass the parsed "
        "body plus the fallback —\n"
        "    throw new Error(apiErrorMessage(err, `API error: ${res.status}`));"
    )


# ── the scan's predicate, pinned ─────────────────────────────────────────────
# Both directions are stated as data: the shapes that broke, and the lora shape that must
# NOT be reported. A guard that flags correct code is worse than none, because the next
# person silences it.
@pytest.mark.parametrize("flagged, line", [
    # what this exists to catch — the 31 sites, before
    (True, "    throw new Error((err as any).detail ?? `API error: ${res.status}`);"),
    (True, "      return fail(status, { error: detail.detail || 'ログインに失敗しました', email });"),
    (True, "  const msg = body.detail ?? 'failed';"),
    # a lora status WARNING object: a domain value that shares the name
    (False, "      out.push({ ...base, kind: 'markerless_exit', reason: mw.detail ?? 'process gone',"),
    (False, '        <span class="chip warn" title={`${w.detail ?? \'\'} (last: ${w.last_seen ?? \'?\'})`}>'),
    # the fixed form
    (False, "    throw new Error(apiErrorMessage(err, `API error: ${res.status}`));"),
])
def test_the_scan_reads_the_shape_it_claims_to(flagged, line) -> None:
    assert bool(_RAW_DETAIL_READ.search(line)) is flagged


# ── the renderer itself, against both live shapes ────────────────────────────
# A guard saying "everyone calls the helper" is worth nothing if the helper is wrong, so
# the helper is executed. Node runs the TypeScript directly (type stripping); where node
# is absent this SKIPS rather than fails — an absent instrument is not a negative result.
_CASES = [
    # the real 401 body, copied from the live API on 2026-09-06 (no credential involved:
    # the refusal for a missing one is itself a KuraError)
    ({"detail": {"error": "missing_or_invalid_authorization",
                 "context": {"credential": "KURA_USER + KURA_TOKEN"},
                 "fix": "set -a; . ~/kura/api/env; set +a"}},
     "missing_or_invalid_authorization — set -a; . ~/kura/api/env; set +a"),
    # a site still on the old shape
    ({"detail": "Name already in use"}, "Name already in use"),
    # error with no fix — the slug alone is the action
    ({"detail": {"error": "not_found"}}, "not_found"),
    # nothing usable: the caller's own fallback, never an empty message
    ({"detail": ""}, "FALLBACK"),
    ({}, "FALLBACK"),
    (None, "FALLBACK"),
]


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed here")
def test_the_helper_renders_every_live_detail_shape(tmp_path: Path) -> None:
    script = tmp_path / "run.mjs"
    script.write_text(
        f"import {{ apiErrorMessage }} from {json.dumps(str(HELPER))};\n"
        "const cases = JSON.parse(process.argv[2]);\n"
        "console.log(JSON.stringify(cases.map(b => "
        "new Error(apiErrorMessage(b, 'FALLBACK')).message)));\n",
        encoding="utf-8")
    proc = subprocess.run(
        ["node", "--experimental-strip-types", str(script),
         json.dumps([body for body, _ in _CASES])],
        capture_output=True, text=True, timeout=120)
    if proc.returncode != 0:
        pytest.skip(f"node could not run the helper ({proc.stderr.strip()[:200]})")
    got = json.loads(proc.stdout.strip().splitlines()[-1])
    assert got == [expected for _, expected in _CASES], (
        "the shared reader does not render the API's live detail shapes.\n"
        f"  expected: {[e for _, e in _CASES]}\n  got:      {got}"
    )
