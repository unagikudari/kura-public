#!/usr/bin/env python3
"""schema_refs — fail when live source still references a DB table that a
migration dropped and that no longer exists.

Plan: entity-retirement-lifecycle (P1). Derivation-first: git + the schema are
the single source of truth; there is no hand-maintained registry to drift. Had
this guarded kura at migration 0037 (which dropped three importer state
tables), the ~8 lingering references would have
gone red the instant the drop landed.

Two precise inputs, one rule:
  • dropped_names — every table named in an op.drop_table / DROP TABLE /
    rename-away across the migration history (INTENT: a migration deliberately
    removed it). drop ops are written literally, so this is reliable.
  • current_tables — the tables that ACTUALLY exist, taken from the live schema
    (a committed --snapshot, else --db introspection). This is authoritative and
    avoids the fragility of folding create_table (projects create tables via
    helpers / raw SQL that a parser misses, undercounting "current").
A reference is flagged iff its name ∈ dropped_names AND ∉ current_tables — so
live tables and arbitrary identifiers are never false-positived. SQL refs are
matched only in keyword position (FROM/JOIN/INTO/UPDATE <name>); pure prose
mentioning a name does not trip it, and a line may carry `refguard:ignore`.

Usage:
    schema_refs.py --migrations <dir> --src <dir> [--src ...]
                   (--snapshot <file> | --db <dsn>)
                   [--ext .py,.sh,.sql] [--format text|json]
Exit 1 if any live reference to a dropped-and-gone table is found, else 0.
Exit 2 if a declared input path is missing — see require_declared_inputs().
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

# ── migration DROP parsing (intent: a migration deliberately removed it) ─────
_DROP = re.compile(r"""op\.drop_table\(\s*['"]([a-zA-Z_][\w]*)['"]""")
_SQL_DROP = re.compile(r"""DROP\s+TABLE\s+(?:IF\s+EXISTS\s+)?["']?([a-zA-Z_][\w]*)""", re.I)
# rename_table(old, new): the OLD name no longer exists as a table.
_RENAME = re.compile(r"""op\.rename_table\(\s*['"]([a-zA-Z_][\w]*)['"]\s*,\s*['"]([a-zA-Z_][\w]*)['"]""")


def _refuse(msg: str):
    """Exit 2 — "I could not answer" — kept distinct from 1 = "I found violations".

    A caller testing for non-zero is unaffected; one that reads 1 as "violations
    found" no longer mistakes a refusal for a failing-but-complete scan."""
    print(f"refguard: {msg}", file=sys.stderr)
    raise SystemExit(2)


def require_declared_inputs(migrations, src_dirs, snapshot) -> None:
    """Refuse when a path the invocation DECLARED as input does not exist.

    Measured on the sibling guard (kura issue #53): a `--src` naming a directory
    absent from the checkout produced output byte-identical to a full scan and
    exit 0, so a CI job kept asserting a property over a tree it never opened. The
    same hole is here: rglob over a missing Path folds to zero candidates, and
    "scanned, found nothing" is spelled exactly like "did not scan".

    `--files` is deliberately NOT checked. Its caller is the pre-commit hook
    passing `git diff --cached --name-only`, which lists staged DELETIONS too; a
    deleted path holds no reference, so skipping it is the right answer, and
    refusing would fail every commit that removes a .py file. A missing ROOT
    invalidates the declared scope; a missing listed FILE is that file's absence.

    Left open on purpose: a root that exists but yields zero files, reported as a
    per-root count rather than refused — zero is legitimate for a guard shipped to
    any repo, and an unsatisfiable refusal only teaches callers to narrow globs."""
    if not migrations.is_dir():
        _refuse(f"--migrations does not exist (or is not a directory): {migrations}"
                " — the set of dropped tables is unknown, so nothing can be judged.")
    missing = [str(d) for d in src_dirs if not d.is_dir()]
    if missing:
        _refuse("--src does not exist (or is not a directory): " + ", ".join(missing)
                + " — refusing to report a clean scan of a tree that was never read.")
    if snapshot is not None and not snapshot.is_file():
        _refuse(f"--snapshot not found: {snapshot} — the current-table set is "
                "unknown, so a dropped name cannot be told from a live one.")


def _upgrade_body(text: str) -> str:
    """The source of the migration's def upgrade() only. drop_table inside
    def downgrade() is a ROLLBACK op (runs only on revert) — counting it as a
    real drop is a false positive, so we never scan downgrade()."""
    m = re.search(r"\ndef\s+upgrade\s*\(", text)
    if not m:
        return ""
    rest = text[m.end():]
    nxt = re.search(r"\ndef\s+\w+\s*\(", rest)  # next top-level def (e.g. downgrade)
    return rest[: nxt.start()] if nxt else rest


def dropped_names(migrations_dir: Path) -> set[str]:
    """Tables a migration's upgrade() explicitly dropped or renamed away.
    drop/rename ops are written literally (not via helpers), so a regex is
    reliable here once scoped to the upgrade body."""
    out: set[str] = set()
    for p in migrations_dir.glob("*.py"):
        if p.name == "__init__.py":
            continue
        up = _upgrade_body(p.read_text(encoding="utf-8", errors="replace"))
        out.update(_DROP.findall(up))
        out.update(_SQL_DROP.findall(up))
        out.update(m.group(1) for m in _RENAME.finditer(up))  # old name
    return out


def live_tables(snapshot: Path | None, dsn: str | None) -> set[str]:
    """Authoritative set of CURRENTLY-EXISTING tables. Prefer a committed
    snapshot (git = SoT, no DB needed in CI); else introspect the live DB."""
    if snapshot:
        return {
            ln.strip() for ln in snapshot.read_text(encoding="utf-8").splitlines()
            if ln.strip() and not ln.lstrip().startswith("#")
        }
    if dsn:
        import psycopg2  # local-only path (pre-commit on a box with the DB)
        with psycopg2.connect(dsn) as conn, conn.cursor() as cur:
            # relkind: r=table v=view m=matview p=partitioned f=foreign — all are
            # legitimate FROM targets, so all count as "exists" (pg_tables alone
            # would miss views/matviews a query can reference).
            cur.execute("SELECT c.relname FROM pg_class c JOIN pg_namespace n "
                        "ON n.oid=c.relnamespace WHERE n.nspname='public' "
                        "AND c.relkind IN ('r','v','m','p','f')")
            return {r[0] for r in cur.fetchall()}
    _refuse("need --snapshot <file> or --db <dsn> for the current schema")


# ── source reference scanning ───────────────────────────────────────────────
_TABLE_REF = re.compile(r"\b(?:FROM|JOIN|INTO|UPDATE)\s+([a-zA-Z_][\w]*)", re.I)
_TABLENAME = re.compile(r"""__tablename__\s*=\s*['"]([a-zA-Z_][\w]*)['"]""")
_IGNORE = "refguard:ignore"
_COMMENT_PREFIX = ("#", "--", "//", "*")


def _eligible(p: Path, exts: set[str], skip_dirs: set[str]) -> bool:
    return p.is_file() and p.suffix in exts and not any(part in skip_dirs for part in p.parts)


def iter_files(src_dirs: list[Path], files: list[Path], exts: set[str],
               skip_dirs: set[str], per_root: dict | None = None):
    """Candidate files from explicit --files (pre-commit: staged paths) or by
    walking --src dirs (full-tree audit) — deduped across roots and globally
    sorted, as before.

    `per_root`, when passed, is filled with {root: eligible-file-count}. A root
    that contributed nothing then appears in the report instead of vanishing into
    a total; the existence refusal above is the gate, this is the evidence."""
    if files:
        cand = [p for p in files if _eligible(p, exts, skip_dirs)]
    else:
        cand = []
        for root in src_dirs:
            got = [p for p in root.rglob("*") if _eligible(p, exts, skip_dirs)]
            if per_root is not None:
                per_root[str(root)] = len(got)
            cand.extend(got)
    yield from sorted(set(cand))


def scan_refs(src_dirs, files, exts: set[str], skip_dirs: set[str], per_root=None):
    """Yield (table, path, lineno, line) for every table-position reference."""
    for p in iter_files(src_dirs, files, exts, skip_dirs, per_root):
        try:
            lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
        except Exception:
            continue
        for i, line in enumerate(lines, 1):
            stripped = line.lstrip()
            if stripped.startswith(_COMMENT_PREFIX) or _IGNORE in line:
                continue
            for m in _TABLE_REF.finditer(line):
                yield m.group(1), p, i, line.strip()
            for m in _TABLENAME.finditer(line):
                yield m.group(1), p, i, line.strip()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--migrations", required=True, type=Path)
    ap.add_argument("--src", action="append", type=Path, default=[],
                    help="dir(s) to walk for the full-tree audit")
    ap.add_argument("--files", nargs="*", type=Path, default=[],
                    help="explicit files to scan (pre-commit: staged paths). Takes precedence over --src")
    ap.add_argument("--snapshot", type=Path, help="committed file of current table names (git = SoT)")
    ap.add_argument("--db", help="postgres DSN to introspect current tables (local fallback)")
    ap.add_argument("--write-snapshot", type=Path, metavar="FILE",
                    help="introspect --db and (re)write the snapshot file, then exit")
    ap.add_argument("--check-snapshot", action="store_true",
                    help="fail if --snapshot differs from --db (drift self-check), then exit")
    ap.add_argument("--ext", default=".py,.sh,.sql")
    ap.add_argument("--format", choices=("text", "json"), default="text")
    args = ap.parse_args()

    if args.write_snapshot:
        if not args.db:
            _refuse("--write-snapshot requires --db")
        tables = sorted(live_tables(None, args.db))
        header = ("# refguard schema snapshot — current public relations (git = SoT).\n"
                  "# Regenerate after any schema migration:\n"
                  "#   schema_refs.py --db <dsn> --write-snapshot <this file>\n")
        args.write_snapshot.write_text(header + "\n".join(tables) + "\n", encoding="utf-8")
        print(f"refguard: wrote {len(tables)} relations → {args.write_snapshot}")
        return 0

    if args.check_snapshot:
        # Self-enforce snapshot freshness: a schema migration that forgot to
        # regenerate the snapshot fails HERE — so nobody has to "remember" to.
        if not (args.snapshot and args.db):
            _refuse("--check-snapshot requires --snapshot and --db")
        if not args.snapshot.is_file():
            _refuse(f"--snapshot not found: {args.snapshot} — an absent snapshot "
                    "cannot be called fresh.")
        snap = live_tables(args.snapshot, None)
        live = live_tables(None, args.db)
        if snap != live:
            missing, extra = sorted(live - snap), sorted(snap - live)
            print("refguard: snapshot is STALE vs live schema — regenerate it "
                  f"(--write-snapshot {args.snapshot}).", file=sys.stderr)
            if missing:
                print(f"  in DB but not snapshot: {', '.join(missing)}", file=sys.stderr)
            if extra:
                print(f"  in snapshot but not DB: {', '.join(extra)}", file=sys.stderr)
            return 1
        print(f"refguard: snapshot fresh ({len(snap)} relations).")
        return 0

    exts = {e if e.startswith(".") else f".{e}" for e in args.ext.split(",")}
    skip_dirs = {args.migrations.name, "versions", ".git", "node_modules", "__pycache__", "legacy", "refguard"}

    if not args.src and not args.files:
        _refuse("need --src <dir> (audit) or --files <path...> (pre-commit)")
    require_declared_inputs(args.migrations, args.src, args.snapshot)
    current = live_tables(args.snapshot, args.db)
    dropped_gone = dropped_names(args.migrations) - current
    per_root: dict = {}
    violations = [
        {"table": t, "file": str(p), "line": n, "code": code}
        for (t, p, n, code) in scan_refs(args.src, args.files, exts, skip_dirs, per_root)
        if t in dropped_gone
    ]

    if args.format == "json":
        print(json.dumps({
            "scanned": per_root,
            "current_tables": sorted(current),
            "dropped_gone": sorted(dropped_gone),
            "violations": violations,
        }, ensure_ascii=False, indent=2))
    else:
        scanned = ", ".join(f"{r}: {n}" for r, n in per_root.items()) or "--files"
        print(f"refguard/schema_refs: {len(current)} current tables, "
              f"{len(dropped_gone)} dropped-and-gone, {len(violations)} live reference(s). "
              f"[scanned {scanned}]")
        for v in violations:
            print(f"  ✗ {v['file']}:{v['line']}  references dropped table "
                  f"'{v['table']}'\n      {v['code']}")
        if violations:
            gone = sorted({v["table"] for v in violations})
            print(f"\nDropped tables still referenced: {', '.join(gone)}")
            print("Fix: remove the reference, or add 'refguard:ignore' on the line "
                  "if it is intentional historical prose.")
    return 1 if violations else 0


if __name__ == "__main__":
    sys.exit(main())
