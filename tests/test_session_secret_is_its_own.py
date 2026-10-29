"""The session cookie was signed with the key that names files on disk (kura#59).

`web/src/lib/server/session.ts` signed the session cookie — which carries a kura
UserToken — with HMAC-SHA256 using `KURA_SECRET`, falling back to the literal
string `'change-me'`. Both halves were wrong:

  * KURA_SECRET also names every content file, `{post_id}_{16hex}.{ext}`, so it
    could not be rotated without renaming about two million files. It was
    therefore never rotated, and on this deployment its value is a six-character
    dictionary word that has already appeared in plaintext in an nginx access log
    (kura#43-2). Anyone holding it could sign a cookie for any user.
  * the fallback signs with a key printed in the source. An unset variable did not
    mean "no key", it meant "everybody's key".

So the session gets its own variable, with no fallback and a refusal to reuse
KURA_SECRET. Unset now means sessions do not work, which is noticed in minutes,
rather than sessions anyone can forge, which is noticed never.

These are source-level checks rather than runtime ones: the file is TypeScript and
this suite is Python. They pin the properties that a later edit could quietly undo
— a fallback creeping back, or the two keys being unified again by copy-paste.
"""
from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SESSION_TS = REPO / "web" / "src" / "lib" / "server" / "session.ts"
LOGIN_TS = REPO / "web" / "src" / "routes" / "login" / "+page.server.ts"


def _src() -> str:
    return SESSION_TS.read_text(encoding="utf-8")


def test_the_session_uses_its_own_variable():
    src = _src()
    assert "env.KURA_SESSION_SECRET" in src


def _code_lines(src: str) -> list[tuple[int, str]]:
    """Lines that execute. The file explains the incident it fixes, so prose
    mentioning the banned forms is not itself a violation — the same distinction
    the DSN guard needed (kura#43-3)."""
    out: list[tuple[int, str]] = []
    for i, line in enumerate(src.splitlines(), 1):
        t = line.strip()
        if t.startswith("//") or t.startswith("*") or t.startswith("/*"):
            continue
        out.append((i, line))
    return out


def test_there_is_no_fallback_key():
    """THE INCIDENT'S SECOND HALF. `|| 'change-me'` meant an unconfigured
    deployment signed with a value published in this file."""
    code = "\n".join(l for _, l in _code_lines(_src()))
    assert not re.search(r"KURA_SESSION_SECRET\s*\|\|", code), (
        "`||` after the variable is a fallback by another spelling")
    assert not re.search(r"=\s*env\.\w+\s*\|\|\s*['\"]", code), (
        "a literal fallback key is back")


def test_it_refuses_to_share_the_filename_salt():
    """A comment saying "do not reuse KURA_SECRET" is what the dashboard key had,
    directly above the code that reused it. This is the mechanism instead.

    Both names, by name: after the kura#43-2 split the variable that names two
    million files is KURA_FS_NAME_SALT. Checking only the retired KURA_SECRET
    left the re-join this guard exists to refuse (2026-09-04 review finding) —
    and in the deployed web env, where neither is present, it was inert."""
    src = _src()
    for name in ("env.KURA_FS_NAME_SALT", "env.KURA_SECRET"):
        assert name in src, f"{name} を照合しなくなった — 貼り直しの再結合が通る"
    guard = src[src.index("function secret()"):src.index("export interface Session")]
    assert "return null" in guard, "一致したときに拒まないなら guard ではない"


def test_an_unset_key_makes_every_cookie_invalid():
    """readSession must not verify against anything when there is no key. A
    fallback there would accept cookies signed with the published value."""
    src = _src()
    read = src[src.index("export function readSession"):]
    read = read[:read.index("export function authHeaderFromSession")]
    assert "if (key === null) return null;" in read
    assert "secret()" not in read.split("const key = secret();")[1], (
        "the key is resolved once and reused; a second call could differ")


def test_signing_without_a_key_raises_instead_of_signing():
    src = _src()
    sign = src[src.index("export function signSession"):]
    sign = sign[:sign.index("export function readSession")]
    assert "throw new SessionSecretMissing()" in sign


def test_the_error_names_the_variable_and_the_one_it_must_not_be():
    """An operator reading this needs both halves: which variable, and that
    copying the other one back in is the mistake it exists to prevent."""
    src = _src()
    assert "KURA_SESSION_SECRET is not set" in src
    assert "must not be KURA_FS_NAME_SALT" in src


def test_login_reports_a_missing_key_as_a_server_problem():
    """The credentials were right and the server cannot mint a session. A 500
    reads like a login failure and sends the user to try their password again."""
    src = LOGIN_TS.read_text(encoding="utf-8")
    assert "signSession" in src
    assert "fail(503" in src, "a config gap is 503, not 400/500"
    assert "catch" in src


def test_nothing_else_in_web_still_signs_with_the_shared_secret():
    """The search that missed this in the first place was `grep api/`. This one
    covers web/src, which is where it was."""
    hits = []
    for p in sorted((REPO / "web" / "src").rglob("*.ts")):
        text = p.read_text(encoding="utf-8", errors="replace")
        if "env.KURA_SECRET" not in text:
            continue
        for i, line in _code_lines(text):
            # READING the variable is what matters. Naming it in an operator
            # message is the message doing its job.
            if "env.KURA_SECRET" not in line:
                continue
            if "v === other" in line or "env.KURA_SECRET]" in line:
                continue   # the refusal to reuse it, not a signer
            hits.append(f"{p.relative_to(REPO)}:{i}")
    assert not hits, "web code still reads KURA_SECRET here:\n  " + "\n  ".join(hits)


def test_the_browser_harness_mints_with_the_same_key():
    """FOUND BY REVIEW, AFTER I GUESSED WRONG ABOUT IT.

    web/tests/kura_harness.py::mint_session_cookie forges a kura_session cookie
    for the Playwright smoke tests. It signed with KURA_SECRET, falling back to
    'change-me' — the same two mistakes as the server. Left alone, this branch
    would have made every cookie the harness mints get rejected, and the smoke
    tests would have failed for a reason with nothing to do with what they test.

    I was asked to check this file and answered that it "looks like a different
    use" without reading it. That is the fourth time today I treated having
    looked at something as having searched it."""
    src = (REPO / "web" / "tests" / "kura_harness.py").read_text(encoding="utf-8")
    assert "KURA_SESSION_SECRET" in src
    code = "\n".join(l for l in src.splitlines() if not l.lstrip().startswith("#"))
    assert 'os.environ.get("KURA_SECRET")' not in code
    assert '"change-me"' not in code, "the harness must not forge with a published key either"
