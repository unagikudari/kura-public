"""A test that needs kura-api must skip when kura-api is not there.

Why this exists (2026-09-04). `kura-checkout-pull.timer` fast-forwards `~/kura` and
restarts `kura-api` whenever the running process is older than HEAD. So the service is
down for a few seconds *right after a push* — which is the moment the suite gets run
before a deploy. A live-API test with no reachability guard turns that window into a
red suite, and the red is indistinguishable from a real defect: it says
`ConnectionRefusedError`, not "the service you need is restarting".

That is not hypothetical. A merge went out with `1 failed` on 2026-09-04 because the
failure was read as flake, re-run green, and shipped. The re-run was luck of timing.
Only `tests/test_artist_category_hint.py` lacked the guard; the other four live suites
already had it, so the convention existed and one file simply missed it.

A convention that four files follow and the fifth does not is not a mechanism. This is
the mechanism: any test module that names a kura-api base URL must also carry a
reachability skip, and a new one cannot be added without it.

Scope note: this checks *declaration*, not behaviour — it cannot prove the guard is
wired correctly, only that the module refuses to run blind. Behaviour is each suite's
own business.
"""
from __future__ import annotations

import ast
import re

import pytest
from pathlib import Path

TESTS = Path(__file__).resolve().parent

# A module depends on the live API if it binds a module-level base URL for it. The two
# spellings in the tree are KURA_TEST_BASE and KURA_API_BASE; both land on :8001.
_BASE_BIND = re.compile(
    r"^(?:BASE|API_BASE)\s*=\s*os\.environ\.get\(\s*[\"'](?:KURA_TEST_BASE|KURA_API_BASE)[\"']",
    re.MULTILINE,
)

# The guard may be a module-level pytestmark or a skip inside a fixture/helper; both are
# in use in this tree. What matters is that unreachability produces a skip.
#
# This used to also require the English phrase "not reachable" in the source. That read
# the WORDING of the reason rather than the mechanism, and on 2026-09-06 it called
# tests/test_bulk_tag_remove.py unguarded while that file carried
#
#     pytestmark = pytest.mark.skipif(not _reachable(), reason="kura-api に届かない ...")
#
# — a correct guard whose reason is written in Japanese. The repository had already
# recorded this exact lesson days earlier, in tests/test_error_envelope.py:
# "By AST, not by grepping the text ... That mistake was made and caught on 2026-09-05
# in a sibling test, an hour after it was written." A prose match is a proxy for the
# property, and a proxy fails in whichever direction its wording happens to differ.
#
# So the check is now: does the module compute its skip condition? A `skipif` whose
# condition is a literal (`skipif(True, ...)`) declares nothing — it is the shape of a
# guard without one. A computed condition is a probe. This stays a DECLARATION check, as
# the scope note above says: it cannot prove the probe asks the right question, only that
# the module refuses to run blind.
def _declares_a_skip(src: str) -> bool:
    """True when the module declares, in code, that it may refuse to run.

    Two shapes are in this tree and both count, exactly as the note above says:

      * a computed `skipif` — `pytest.mark.skipif(not _reachable(), ...)`. Computed is
        the point: `skipif(True, ...)` is the shape of a guard without a probe.
      * a `pytest.skip(...)` call inside a fixture or helper — the form
        test_invm_human_pin.py and test_post_tag_locked.py use.

    What is deliberately NOT inspected is the reason string. That is what the previous
    version read, and it is why a correct guard written in Japanese was reported as a
    missing one.
    """
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return False
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
            continue
        if node.func.attr == "skip":
            return True                     # fixture/helper form
        if node.func.attr == "skipif":
            cond = node.args[0] if node.args else next(
                (kw.value for kw in node.keywords if kw.arg == "condition"), None)
            if cond is not None and not isinstance(cond, ast.Constant):
                return True                 # computed marker form
    return False


_SKIP = re.compile(r"pytest\.(?:skip|mark\.skipif)")


def _live_api_modules() -> list[Path]:
    out = []
    for path in sorted(TESTS.glob("test_*.py")):
        if path.name == Path(__file__).name:
            continue
        if _BASE_BIND.search(path.read_text(encoding="utf-8")):
            out.append(path)
    return out


def test_the_population_is_not_empty():
    """A pattern that matches nothing passes for the wrong reason."""
    mods = _live_api_modules()
    assert mods, (
        "no test module binds KURA_TEST_BASE/KURA_API_BASE — either the live-API suites "
        "were removed or the spelling changed and this check went blind"
    )


def test_every_live_api_module_skips_when_the_api_is_absent():
    missing = []
    for path in _live_api_modules():
        src = path.read_text(encoding="utf-8")
        if not (_SKIP.search(src) and _declares_a_skip(src)):
            missing.append(path.name)
    assert not missing, (
        "these modules talk to a running kura-api but do not skip when it is absent, so "
        "a restart window (kura-checkout-pull.timer restarts kura-api after every push) "
        "reads as a defect in the code under test: " + ", ".join(missing) + "\n"
        "Fix: add the probe the other live suites use —\n"
        "    def _api_up() -> bool:\n"
        "        try:\n"
        "            urllib.request.urlopen(f\"{BASE}/api/build\", timeout=5)\n"
        "            return True\n"
        "        except Exception:\n"
        "            return False\n"
        "    pytestmark = pytest.mark.skipif(not _api_up(), "
        "reason=f\"kura-api not reachable at {BASE}\")"
    )


# ── the predicate itself, pinned (2026-09-06) ────────────────────────────────
# The previous version read the reason's WORDING, so it reported a correctly guarded
# module as unguarded because the reason was written in Japanese. A replacement that is
# never exercised on its own inputs would just be a different untested proxy, so the four
# shapes that decide the answer are stated here as data.
@pytest.mark.parametrize("declares, src", [
    # computed condition, reason in any language — the marker form
    (True, 'import pytest\npytestmark = pytest.mark.skipif(not _up(), reason="届かない")\n'),
    # a skip inside a helper — the form test_invm_human_pin.py uses
    (True, 'import pytest\ndef _n():\n    if not _up():\n        pytest.skip("届かない")\n'),
    # the SHAPE of a guard with no probe: a literal condition. English prose must not save it.
    (False, 'import pytest\npytestmark = pytest.mark.skipif(True, reason="not reachable")\n'),
    # prose alone — what the old regex accepted and this must not
    (False, '"""kura-api not reachable なら skip する"""\nBASE = "x"\n'),
])
def test_the_predicate_reads_the_mechanism_not_the_wording(declares, src):
    assert _declares_a_skip(src) is declares
