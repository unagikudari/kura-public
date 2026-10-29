"""A node says whether it should hold files. The role does not say it.

trainer-cc refused KURA_MEDIA_ROLE as the key with a measurement:
replica_observe means "serve local bytes and record it", and on that node 42,726
rows — 4.2% of the archive — fall through to the legacy FS URL. A replica_observe
node can legitimately hold FS media, so deriving "has no media" from the role
reads a claim the role never makes.

The verdict split this builds on (unreadable vs no-files) landed separately as
b8108f9, from the same review finding. This is the other half: without a
declaration, "could not read the directory" is permanently ambiguous on a node
that is not supposed to have one, and a permanent WARN on a correct node is how
people learn to stop reading WARNs.
"""
from __future__ import annotations

import ast
import pathlib

import pytest

REPO = pathlib.Path(__file__).resolve().parent.parent


def _guard() -> str:
    src = (REPO / "api" / "main.py").read_text()
    g = src[src.index("def _check_fs_name_salt"):]
    return g[:g.index("\nasync def ")]


def test_the_guard_never_reads_the_media_role():
    """The wrong key, kept wrong on purpose. If this fails, someone reached for
    the role again and the measurement above says it does not answer."""
    assert "MEDIA_ROLE" not in _guard(), (
        "replica_observe does not mean 'holds no FS media' — trainer serves "
        "42,726 FS-backed rows under exactly that role. Read KURA_FS_MEDIA.")


def test_a_node_that_declares_none_is_not_warned_at_forever():
    g = _guard()
    branch = g[g.index('FS_MEDIA == "none"'):][:900]
    assert "_log.info" in branch
    assert "_log.warning" not in branch.split("else:")[0]


def test_declaring_expected_with_no_readable_directory_refuses():
    """It said it serves content from disk and there is no disk. Every FS-backed
    URL 404s, and new uploads land under a salt nothing proved."""
    g = _guard()
    i = g.index('FS_MEDIA == "expected"')
    assert "raise RuntimeError" in g[i:i + 800]


def test_the_refusal_names_both_repairs():
    """Both repairs must be in the SAME message, and every refusal must be read.

    Two false-pass paths, both found the hard way. The first version matched the
    source text and failed on the docstring explaining the rule. The second — this
    one's predecessor — joined every refusal and asked whether both phrases
    appeared anywhere in the concatenation, so "Fix DATA_DIR" in one raise and
    "declare KURA_FS_MEDIA=none" in another would have satisfied it while the
    operator who hit the refusal saw only half the answer (primary-gemini, round 1).

    It also asserted `len(msgs) >= 3` against a list built by silently skipping
    any raise it could not parse — so a refactor to `err = RuntimeError(...)`
    would drop a message from the list and from the count together, and the
    check would pass having read less.
    """
    import ast

    def text(node):
        if isinstance(node, ast.Constant):
            return str(node.value)
        if isinstance(node, ast.JoinedStr):
            return "".join(text(v) for v in node.values)
        if isinstance(node, ast.FormattedValue):
            return " "
        return ""

    tree = ast.parse((REPO / "api" / "main.py").read_text())
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef) and n.name == "_check_fs_name_salt")
    raises = [n for n in ast.walk(fn) if isinstance(n, ast.Raise)]
    assert raises, "the guard raises nothing at all"

    msgs = []
    for r in raises:
        args = getattr(r.exc, "args", None)
        assert args, (
            "a refusal was written in a form this test cannot read (bare re-raise, "
            "or a pre-built exception). It must be able to read every one, or it "
            "reports on the subset it happened to parse.")
        msgs.append(text(args[0]))

    declared = [m for m in msgs if "KURA_FS_MEDIA=expected" in m]
    assert len(declared) == 1, (
        f"expected exactly one refusal for a node that declared it holds media, "
        f"found {len(declared)}")
    only = declared[0]
    assert "Fix DATA_DIR" in only
    assert "declare KURA_FS_MEDIA=none" in only, (
        "the refusal must name both repairs in the same breath — fix the path, "
        "or say the node holds nothing. Half an answer is what the operator gets.")


def _reload_config(monkeypatch, **env):
    """Reload api.config under a controlled environment, and leave none behind.

    The first version of these tests wrote os.environ directly and reloaded
    without restoring, so KURA_FS_MEDIA stayed set for everything that ran
    afterwards (trainer-cc measured it with a probe appended to this file).
    Nothing was failing yet — the sibling file re-derives per test — but the next
    test anywhere that imports api.config expecting no declaration would have
    inherited one silently. That is exactly the shape this PR removes from the
    product, and it had been reintroduced in the tests.
    """
    import importlib
    import sys
    sys.path.insert(0, str(REPO))
    for k, v in env.items():
        if v is None:
            monkeypatch.delenv(k, raising=False)
        else:
            monkeypatch.setenv(k, v)
    import api.config
    return importlib.reload(api.config)


def test_an_unrecognised_declaration_is_refused_not_ignored(monkeypatch):
    """A typo must not read as silence: `exptected` is neither value, and
    falling back to undeclared gives the operator who opted INTO the refusal the
    old behaviour with nothing to notice (primary-gemini, round 1)."""
    import importlib
    import api.config
    try:
        for bad in ("exptected", "true", "no", "expect", "EXPECTED!"):
            with pytest.raises(RuntimeError) as e:
                _reload_config(monkeypatch, KURA_FS_NAME_SALT="x", KURA_FS_MEDIA=bad)
            assert "KURA_FS_MEDIA" in str(e.value)
        # "None " is NOT in that list: .strip().lower() normalises it to "none",
        # a declaration rather than a typo. Running the test caught that the
        # first version asserted a refusal the code correctly declines to make.
        for good in ("", "none", "expected", "  NONE  ", "None "):
            _reload_config(monkeypatch, KURA_FS_NAME_SALT="x", KURA_FS_MEDIA=good)
    finally:
        # A failed assertion inside either loop would otherwise leave api.config
        # holding the last value it was reloaded with — including a bad one that
        # raised halfway through module execution.
        monkeypatch.undo()
        importlib.reload(api.config)


def test_declaring_none_while_holding_verified_files_disagrees():
    g = _guard()
    assert "disagree" in g[g.index('FS_MEDIA == "none"'):][:1200]


def test_an_undeclared_node_keeps_the_previous_behaviour():
    """Unknown is not green, and also not a new failure: a node that says
    nothing gets exactly what it got before this key existed."""
    g = _guard()
    assert 'elif verdict == "unreadable":' in g
    assert 'elif verdict == "no-files":' in g


def test_empty_means_undeclared_not_none(monkeypatch):
    """A node must SAY it holds nothing; silence is not a declaration."""
    import importlib
    import api.config
    try:
        cfg = _reload_config(monkeypatch, KURA_FS_NAME_SALT="x", KURA_FS_MEDIA=None)
        assert cfg.FS_MEDIA == ""
        cfg = _reload_config(monkeypatch, KURA_FS_NAME_SALT="x", KURA_FS_MEDIA="  NONE  ")
        assert cfg.FS_MEDIA == "none"
    finally:
        monkeypatch.undo()
        importlib.reload(api.config)


def test_these_tests_leave_no_declaration_behind(monkeypatch):
    """The probe trainer-cc used to find the leak, kept as a test.

    It has to run after the two above to mean anything, which pytest's file
    order gives it. If either of them stops restoring, this fails here rather
    than in whatever unrelated test imports api.config next — for the
    environment AND for the module, which took a second round to make true.
    """
    import importlib
    import os
    import sys
    sys.path.insert(0, str(REPO))
    import api.config
    assert os.environ.get("KURA_FS_MEDIA") is None, (
        f"KURA_FS_MEDIA is still set to {os.environ.get('KURA_FS_MEDIA')!r} — a "
        "test above leaked a declaration into everything that runs after it")
    # BEFORE the reload, and that ordering is the whole test. trainer-cc
    # mutation-tested the first version: with both `finally` restores deleted it
    # still passed, because reloading first REPAIRS the stale module and then
    # reports it clean. The docstring claimed a failure would land here and, for
    # the module half, it could not. An assertion that repairs what it measures
    # is not an assertion.
    assert api.config.FS_MEDIA == "", (
        f"api.config still holds {api.config.FS_MEDIA!r} — a test above reloaded "
        "it under a declaration and never reloaded it back")
    assert importlib.reload(api.config).FS_MEDIA == ""
