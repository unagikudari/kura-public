#!/usr/bin/env python3
"""dead_scripts — flag scripts that nothing references: no systemd unit runs
them, no module imports them, no shell/python invokes them.

Plan: entity-retirement-lifecycle (P4). Mechanises the manual orphan hunt that
found five orphaned importer/uploader scripts in one session. A script
is LIVE if any search root references it as `import <stem>`, `from <stem>`, or
`<stem>.py` (a path / exec / unit ExecStart). Everything else is an orphan
candidate — reported, never auto-deleted (a CLI entry point invoked only by a
human is a candidate, not a certainty; the `[main]` flag marks those).

Usage:
    dead_scripts.py --scripts <dir> --search <dir|file> [--search ...]
                    [--ext .py] [--format text|json]
Exit 1 if any orphan candidate is found (report-only; wire as a warning, not a
hard gate, since CLI entry points legitimately have no in-repo caller).
Exit 2 if a declared input path is missing — see require_declared_inputs().
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

_SKIP_DIRS = {".git", "node_modules", "__pycache__", "legacy"}


def _refuse(msg: str):
    """Exit 2 — "I could not answer" — kept distinct from 1 = "I found candidates"."""
    print(f"refguard: {msg}", file=sys.stderr)
    raise SystemExit(2)


def require_declared_inputs(scripts: Path, search: list[Path]) -> None:
    """Refuse when a path the invocation DECLARED as input does not exist.

    Two distinct vacuities live here, and this guard fails in BOTH directions —
    which is why neither may be silent:

      * a missing --scripts audits nothing and reports "0 scripts audited, 0
        orphan candidate(s)", exit 0. Indistinguishable from a clean tree, the
        same shape kura issue #53 measured in endpoint_refs.
      * a missing --search root shrinks the corpus that proves a script LIVE, so
        scripts start looking orphaned. That direction is noisy rather than
        silent, but it is still an answer derived from files nobody read — and
        acting on it means deleting a script that something does reference.

    A --search root may be a file or a directory (units are passed individually),
    so existence is the test, not is_dir()."""
    if not scripts.is_dir():
        _refuse(f"--scripts does not exist (or is not a directory): {scripts}"
                " — refusing to report zero orphans out of zero scripts audited.")
    missing = [str(r) for r in search if not r.exists()]
    if missing:
        _refuse("--search path does not exist: " + ", ".join(missing)
                + " — a reference that proves a script live may sit in it, so every "
                  "orphan below would be an artefact of not looking.")


def _iter_search_files(roots: list[Path], exts: set[str]):
    extra = {".sh", ".service", ".timer", ".env"}  # also scan units/shell
    for root in roots:
        if root.is_file():
            yield root
            continue
        for p in root.rglob("*"):
            if not p.is_file() or any(part in _SKIP_DIRS for part in p.parts):
                continue
            if p.suffix in exts or p.suffix in extra or p.suffix == "":
                yield p


def references(stem: str, blob: str) -> bool:
    """Does this text reference the script by import or path?"""
    return bool(
        re.search(rf"\b{re.escape(stem)}\.py\b", blob)
        or re.search(rf"^\s*import\s+{re.escape(stem)}\b", blob, re.M)
        or re.search(rf"^\s*from\s+{re.escape(stem)}\s+import\b", blob, re.M)
        or re.search(rf"\bimport\s+{re.escape(stem)}\b", blob)
    )


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--scripts", required=True, type=Path, help="dir of scripts to audit")
    ap.add_argument("--search", required=True, action="append", type=Path,
                    help="roots to search for references (repo dirs, systemd unit dir, ...)")
    ap.add_argument("--ext", default=".py")
    ap.add_argument("--format", choices=("text", "json"), default="text")
    args = ap.parse_args()

    require_declared_inputs(args.scripts, args.search)
    exts = {e if e.startswith(".") else f".{e}" for e in args.ext.split(",")}
    targets = [p for p in sorted(args.scripts.glob("*.py")) if p.name != "__init__.py"]

    # Read every search file once.
    corpus: list[tuple[Path, str]] = []
    for f in _iter_search_files(args.search, exts):
        try:
            corpus.append((f, f.read_text(encoding="utf-8", errors="replace")))
        except Exception:
            continue

    orphans = []
    for t in targets:
        stem = t.stem
        live = any(f != t and references(stem, blob) for f, blob in corpus)
        if not live:
            body = t.read_text(encoding="utf-8", errors="replace")
            orphans.append({
                "script": str(t),
                "has_main": ("__main__" in body),
                "lines": body.count("\n") + 1,
            })

    if args.format == "json":
        print(json.dumps({"searched_files": len(corpus), "orphans": orphans},
                         ensure_ascii=False, indent=2))
    else:
        print(f"dead_scripts: {len(targets)} scripts audited against {len(corpus)} "
              f"search file(s), {len(orphans)} orphan candidate(s) "
              f"(no unit/import/exec reference).")
        for o in orphans:
            tag = "[main-cli]" if o["has_main"] else "[no-main]"
            print(f"  ? {o['script']}  {tag}  ({o['lines']} lines)")
        if orphans:
            print("\nReview each: a true orphan is safe to delete; a [main-cli] may be a "
                  "human/ad-hoc entry point. Not a hard gate.")
    return 1 if orphans else 0


if __name__ == "__main__":
    sys.exit(main())
