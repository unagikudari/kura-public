"""A password in argv is readable by every process on the box (kura#43 item 3).

The code was fixed: both live scripts read `api/env` in Python and their
docstrings cite this issue. What was not fixed is what one of them SAYS.
`tag_desc_reconciler.py` dispatches a task body to primary-cc-primary on a timer, and
that body told the agent to pull the connection string out of api/env with a shell
and pass it to psql as an argument — putting the password in argv, and therefore in
`ps`, on every tick. The leak had moved one level of indirection out: out of the
code, into the sentence the code hands to someone else.

So the guard is on both. A real `os.popen` call is found by walking the AST, which
makes prose about it invisible to the check; and no string constant anywhere may
carry a DSN into a command's arguments, because a string constant is exactly where
an instruction lives.

WHAT THIS IS NOT
----------------
This is a regression test for ONE incident shape, not a general guarantee that no
DSN can leak. Both review lanes made the same point, and they are right: a green
here means the 2026-09-04 form has not come back, and nothing more. Known gaps,
written down so a passing run is not mistaken for coverage (described rather than
quoted, because quoting them would trip the check — as the first draft of this
paragraph did):

  * the two psql flags that take a connection string — these ARE matched below,
    being the nearest neighbours of the shape that shipped; anything further out
    is not.
  * the secret riding an environment variable into the command instead of argv.
    Less bad (only the same user and root read a process's environ) but not
    nothing.
  * a connection URI written out literally with the password inline.
  * argv passed as a list from Python, where there is no shell text to match.
  * the value assembled by concatenation, through an intermediate variable, or
    living in a runbook that is not a Python string at all — a markdown file, a
    docs page, a task dispatched from another repository.

Closing those needs the secret's SOURCE traced to an argv SINK, which is a real
piece of analysis and not this file. Until someone writes it, this is a tripwire on
a path that was actually walked, and it is labelled as one.

Nothing here quotes the banned form, including this docstring, and there is no
exemption for any file. The first draft failed on its own explanation and then
carried a `"kura#43" not in ...` escape hatch — an explicit blanket exemption
sitting directly under a claim of having none, which both lanes flagged. The
explanation was reworded instead.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
# tests/ is scanned too: a runbook pasted into a test docstring is an
# instruction like any other, and this file makes a claim about itself.
ROOTS = ("scripts", "api", "tagger", "tests")
SKIP_PARTS = {".git", "node_modules", "__pycache__", ".venv", "data"}

# A connection string handed to psql as an argument: bare (`psql "$PGURL"`) or
# behind the two flags that take one (`-d "$DSN"`, `--dbname "$DSN"`). The flag
# forms are here because they are the nearest neighbours of the shape that was
# actually shipped — see WHAT THIS IS NOT for everything past them.
_DSN_ARG = re.compile(r"""psql\s+(?:[^\n]*?\s)?(?:-d|--dbname)?\s*["']?\$""")
# building one out of the env file with a shell.
_SHELL_DSN = re.compile(r"""grep\s+(-\w+\s+)*['"]?\^?DATABASE_URL=""")


def _py_files():
    for root in ROOTS:
        base = REPO / root
        if not base.is_dir():
            continue
        for p in sorted(base.rglob("*.py")):
            if any(part in SKIP_PARTS for part in p.parts):
                continue
            yield p


def _string_constants(tree: ast.AST):
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            yield node


def test_no_string_hands_a_dsn_to_a_command_line():
    """THE INCIDENT. Task bodies, help text, runbooks pasted into a docstring —
    every one of them is an instruction someone will follow, so the check covers
    string constants and not just executable code. No file is exempt, this one
    included."""
    offenders = []
    for p in _py_files():
        try:
            tree = ast.parse(p.read_text(encoding="utf-8", errors="replace"))
        except SyntaxError:
            continue
        for node in _string_constants(tree):
            if _DSN_ARG.search(node.value):
                offenders.append(f"{p.relative_to(REPO)}:{node.lineno} psql <dsn> in argv")
            elif _SHELL_DSN.search(node.value):
                offenders.append(f"{p.relative_to(REPO)}:{node.lineno} shell-built DSN")
    assert not offenders, (
        "a DSN reaches a command line here — the password lands in `ps` for every "
        "user on the box:\n  " + "\n  ".join(offenders))


def test_no_code_shells_out_to_read_the_env_file():
    """Walked as CALLS, so a docstring explaining the ban is not itself a
    violation — the earlier fix left exactly such docstrings behind, and a
    text-only grep would have had to be weakened to tolerate them."""
    offenders = []
    for p in _py_files():
        try:
            tree = ast.parse(p.read_text(encoding="utf-8", errors="replace"))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            fn = node.func
            name = (fn.attr if isinstance(fn, ast.Attribute)
                    else fn.id if isinstance(fn, ast.Name) else "")
            if name != "popen":
                continue
            arg = node.args[0] if node.args else None
            text = ""
            if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                text = arg.value
            elif isinstance(arg, ast.JoinedStr):
                text = "".join(v.value for v in arg.values
                               if isinstance(v, ast.Constant) and isinstance(v.value, str))
            if "DATABASE_URL" in text or "api/env" in text:
                offenders.append(f"{p.relative_to(REPO)}:{node.lineno}")
    assert not offenders, (
        "os.popen reads the env file through a shell here; read it in Python:\n  "
        + "\n  ".join(offenders))


def test_the_reconciler_can_answer_the_question_it_used_to_delegate():
    """The task body only stops building a DSN because the script now offers the
    answer. If --list goes away, the body's instruction becomes a dead end and the
    next person rebuilds the psql line."""
    src = (REPO / "scripts" / "tag_desc_reconciler.py").read_text(encoding="utf-8")
    assert "def list_undescribed" in src
    assert '"--list" in sys.argv' in src
    assert "tag_desc_reconciler.py --list" in src, "the task body must point at it"


def test_the_reconciler_task_body_no_longer_carries_the_recipe():
    """Checked with this module's own detector rather than by restating the banned
    string. Spelling it out here made the file fail its own first assertion — which
    is the guard working, and the reason it is written this way."""
    src = (REPO / "scripts" / "tag_desc_reconciler.py").read_text(encoding="utf-8")
    assert not _DSN_ARG.search(src)
    assert not _SHELL_DSN.search(src)
    assert "PGURL=$(" not in src
