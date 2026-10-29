#!/usr/bin/env python3
"""endpoint_refs — fail when live source references an HTTP API path that the
app no longer serves.

Plan: entity-retirement-lifecycle (P3). Sibling of schema_refs.py, same shape:
the set of CURRENT routes is authoritative (the running app's OpenAPI, captured
into a committed --snapshot = git SoT); a client reference to an `/api/...` path
that matches no current route is flagged. This catches references to endpoints a
refactor removed — e.g. kura Phase 6 dropped an events sink endpoint while three
importer scripts still POSTed to it.

Scope: the app's whole served surface — `/api/...`, `/dashboards/...`, `/mcp/...`
(TRACKED_PREFIXES). kura#44: while this was `/api`-only, the largest dead surface
(dashboard.py's retired import dashboard) sat outside the instrument meant to
find dead surfaces.

Precision: only tracked-prefix string literals (≥2 path segments) are candidates, so
arbitrary strings never trip it. A ref matches a route if their path segments
agree on the shared prefix, with `{param}` (either side) as a wildcard — lenient
enough that valid dynamic paths (`f".../api/posts/{id}"`) pass while a genuinely
gone prefix (`/api/events/...`) matches nothing. Comments and `refguard:ignore`
lines are skipped.

The CURRENT-routes set comes from a committed --snapshot (git = SoT, no running
app in CI). Regenerate the snapshot from `--app module:attr` (preferred: app.routes
includes include_in_schema=False routes, so no hidden route must be hand-added) or
from `--openapi-url` (omits hidden routes).

Usage:
    endpoint_refs.py --src <dir> [--src ...] | --files <path...>
                     (--snapshot <file> | --openapi-url <url> | --app <module:attr>)
                     [--write-snapshot FILE (--app <module:attr> | --openapi-url <url>)]
                     [--ext .py,.sh] [--format text|json]
Exit 1 if any live reference to a gone endpoint is found, else 0.
Exit 2 if a declared input path is missing — see require_declared_inputs().
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.request
from pathlib import Path

_IGNORE = "refguard:ignore"
_COMMENT_PREFIX = ("#", "--", "//", "*")
# kura#44: この guard の対象面。/api だけを見ていたので、**最大の死面**
# (dashboard.py 2,922 行 = /dashboards/*) と /mcp が丸ごと不可視だった。
# 「drift を捕まえる装置自身が drift の外にいた」形。app が serve する面は
# すべて対象にする。
# /header-frame は kura#42 で退役し (2026-09-04)、ここからも外した。
# 一度は「serve されたら知りたい面」として残そうとしたが、primary-gemini の指摘で
# 撤回 — **復活の検知は追跡ではなく、その逆側の検査が担っている**。kura の
# test_every_served_prefix_is_tracked_or_declared_not_ours は「serve されている
# のに tracked でもなく framework 由来とも宣言されていない面」で落ちるので、
# 追跡から外した面が再び生えれば、そこで捕まる。残していれば TRACKED が
# 「今 serve している面」から「過去に存在した面の墓場」に変質していた。
TRACKED_PREFIXES = ("/api", "/dashboards", "/mcp")
# literal tracked path up to the first interpolation/brace/quote/space/backtick.
_REF = re.compile(
    r"""(?:{})/[A-Za-z0-9_./-]*(?:\{{[A-Za-z0-9_]+\}}[A-Za-z0-9_./-]*)*""".format(
        "|".join(re.escape(p) for p in TRACKED_PREFIXES)))


def _tracked(path: str) -> bool:
    return any(path == p or path.startswith(p + "/") for p in TRACKED_PREFIXES)


def _refuse(msg: str):
    """Exit 2 — "I could not answer" — kept distinct from 1 = "I found violations".

    A caller testing for non-zero is unaffected; one that reads 1 as "violations
    found" no longer mistakes a refusal for a failing-but-complete scan."""
    print(f"refguard: {msg}", file=sys.stderr)
    raise SystemExit(2)


def require_declared_inputs(src_dirs, snapshot) -> None:
    """Refuse when a path the invocation DECLARED as input does not exist.

    Measured on kura (issue #53): `--src scripts --src web/src` on a checkout with
    no web/ printed the same bytes as a full scan — "166 live routes, 0 reference(s)
    to gone endpoints." — and exited 0. The CI job asserting "No client code
    references a removed API endpoint" stayed green over client code it never
    opened. Widening the guard's scope had built the path by which the guard
    disappears: a partial clone, a sparse checkout, or a directory rename the
    workflow file did not follow all silently empty it.

    `--files` is deliberately NOT checked this way. Its caller is the pre-commit
    hook passing `git diff --cached --name-only`, which lists staged DELETIONS
    too; a deleted path holds no reference, so skipping it is the right answer,
    and refusing would fail every commit that removes a .py file. The asymmetry is
    the point — a missing ROOT invalidates the scope the caller declared, while a
    missing listed FILE is just that file's own absence.

    Left open on purpose: a root that exists but yields zero files. It is reported
    as a per-root count (see iter_files) rather than refused, because zero is a
    legitimate state for a guard shipped to any repo — a tree may hold no .sh —
    and an unsatisfiable refusal only teaches callers to narrow their globs until
    it passes."""
    missing = [str(p) for p in src_dirs if not p.is_dir()]
    if missing:
        _refuse("--src does not exist (or is not a directory): " + ", ".join(missing)
                + " — refusing to report a clean scan of a tree that was never read.")
    if snapshot is not None and not snapshot.is_file():
        _refuse(f"--snapshot not found: {snapshot} — the current-route set is "
                "unknown, so no reference can be judged.")


def _segs(path: str) -> list[str]:
    return [s for s in path.strip("/").split("/") if s]


def routes_from_openapi(url: str) -> set[str]:
    with urllib.request.urlopen(url, timeout=15) as r:  # noqa: S310 (local trusted)
        spec = json.loads(r.read())
    return {p for p in spec.get("paths", {}) if _tracked(p)}


def routes_from_app(app_path: str) -> set[str]:
    """Import a FastAPI app (`module:attr`, attr defaults to `app`) and collect
    every tracked-prefix route template from `app.routes` — INCLUDING `include_in_schema=False`
    routes that OpenAPI omits (e.g. kura's live /api/pool). This is the derivation-
    first snapshot source: it needs no hand-added hidden-route entries.

    Recurses into included routers / mounts, version-tolerant:
      - FastAPI ≥0.137 / Starlette ≥1.3 nests each `app.include_router()` as a
        `_IncludedRouter` whose sub-routes live under `.original_router.routes`
        (the top-level `app.routes` no longer flattens them — the exact reason a
        naive enumeration saw only `/api/docs`).
      - Older FastAPI flattens leaf APIRoutes straight into `app.routes`.
      - Starlette `Mount`/sub-apps expose `.routes`.

    Requires the app to be importable (its env on sys.path / in the environment);
    run this under the target app's venv. Importing does NOT run lifespan, so no
    DB connection is made."""
    import importlib
    import os

    # This script is usually invoked by absolute path, so sys.path[0] is the
    # refguard dir, not the target repo. Make the cwd (the repo root the caller
    # runs from — same as uvicorn's WorkingDirectory) importable so `api.main`
    # resolves exactly as it does in production.
    cwd = os.getcwd()
    if cwd not in sys.path:
        sys.path.insert(0, cwd)

    module_name, _, attr = app_path.partition(":")
    module = importlib.import_module(module_name)
    app = getattr(module, attr or "app")

    found: set[str] = set()

    def walk(routes) -> None:
        for rt in routes:
            included = getattr(rt, "original_router", None)
            sub = getattr(included, "routes", None) if included is not None else None
            if sub is None and type(rt).__name__ not in ("APIRoute", "Route", "WebSocketRoute"):
                sub = getattr(rt, "routes", None)  # Mount / sub-app / old-FastAPI router
            if sub:
                walk(sub)
                continue
            path = getattr(rt, "path", None)
            if isinstance(path, str) and _tracked(path):
                found.add(path)

    walk(app.routes)
    return found


def live_routes(snapshot: Path | None, openapi_url: str | None, app_path: str | None = None) -> set[str]:
    """Authoritative current route templates. Prefer a committed snapshot
    (git = SoT, no running app needed in CI); else derive from an imported app
    (--app, includes hidden routes); else the live app's OpenAPI."""
    if snapshot:
        return {
            ln.strip() for ln in snapshot.read_text(encoding="utf-8").splitlines()
            if ln.strip() and not ln.lstrip().startswith("#")
        }
    if app_path:
        return routes_from_app(app_path)
    if openapi_url:
        return routes_from_openapi(openapi_url)
    _refuse("need --snapshot <file>, --app <module:attr>, or --openapi-url <url>")


def _seg_match(a: str, b: str) -> bool:
    return a == b or a.startswith("{") or b.startswith("{")


def ref_is_served(ref: str, routes: list[list[str]]) -> bool:
    """A ref is served if some route agrees on the whole shared-length prefix
    ({param} = wildcard on either side). Lenient: a ref shorter or longer than a
    route still matches as long as the overlap aligns."""
    rs = _segs(ref)
    if len(rs) < 2:  # just "/api" or "/api/x" with x empty — too generic to judge
        return True
    for ts in routes:
        n = min(len(rs), len(ts))
        if n >= 2 and all(_seg_match(rs[i], ts[i]) for i in range(n)):
            return True
    return False


def _eligible(p, exts, skip_dirs) -> bool:
    return p.is_file() and p.suffix in exts and not any(part in skip_dirs for part in p.parts)


def iter_files(src_dirs, files, exts, skip_dirs, per_root=None):
    """Candidate files — deduped across roots and globally sorted, as before.

    `per_root`, when passed, is filled with {root: eligible-file-count}. A root
    that contributed nothing then shows up in the report instead of vanishing
    into a total; the existence refusal above is the gate, this is the evidence."""
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


def scan_refs(src_dirs, files, exts, skip_dirs, per_root=None):
    for p in iter_files(src_dirs, files, exts, skip_dirs, per_root):
        try:
            lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
        except Exception:
            continue
        for i, line in enumerate(lines, 1):
            if line.lstrip().startswith(_COMMENT_PREFIX) or _IGNORE in line:
                continue
            for m in _REF.finditer(line):
                # Only OUR API: a kura/relative ref has the path at a literal
                # boundary (quote / backtick / f-string `}` / paren / space) right
                # before /api/. If the preceding char is a domain char (alnum or
                # '.'), it's an external host (example.net/api, example.cc/api) — skip.
                prev = line[m.start() - 1] if m.start() > 0 else ""
                if prev.isalnum() or prev == ".":
                    continue
                yield m.group(0).rstrip("./-"), p, i, line.strip()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--src", action="append", type=Path, default=[])
    ap.add_argument("--files", nargs="*", type=Path, default=[])
    ap.add_argument("--snapshot", type=Path)
    ap.add_argument("--openapi-url")
    ap.add_argument("--app", metavar="MODULE:ATTR",
                    help="import a FastAPI app and derive routes from app.routes "
                         "(includes include_in_schema=False routes; run under its venv). "
                         "Preferred over --openapi-url for --write-snapshot.")
    ap.add_argument("--write-snapshot", type=Path, metavar="FILE")
    ap.add_argument("--ext", default=".py,.sh")
    ap.add_argument("--format", choices=("text", "json"), default="text")
    args = ap.parse_args()

    if args.write_snapshot:
        # Prefer --app: app.routes includes hidden (include_in_schema=False) routes,
        # so the snapshot needs no hand-maintained additions (derivation-first).
        if args.app:
            routes = sorted(routes_from_app(args.app))
            src_line = f"#   endpoint_refs.py --app {args.app} --write-snapshot <this file>  (run under the app's venv)"
            derive_line = "# Derived from app.routes — INCLUDES include_in_schema=False routes; no hand edits needed.\n"
        elif args.openapi_url:
            routes = sorted(routes_from_openapi(args.openapi_url))
            src_line = f"#   endpoint_refs.py --openapi-url {args.openapi_url} --write-snapshot <this file>"
            derive_line = ("# Derived from OpenAPI — OMITS include_in_schema=False routes; prefer --app to\n"
                           "# avoid hand-adding hidden routes (see plan entity-retirement-lifecycle P5 #3).\n")
        else:
            _refuse("--write-snapshot requires --app MODULE:ATTR (preferred) or --openapi-url <url>")
        header = ("# refguard endpoint snapshot — current "
                  + "/".join(p.lstrip("/") for p in TRACKED_PREFIXES)
                  + " routes (git = SoT).\n"
                  + derive_line
                  + "# Regenerate when routes change:\n"
                  + src_line + "\n")
        args.write_snapshot.write_text(header + "\n".join(routes) + "\n", encoding="utf-8")
        print(f"refguard: wrote {len(routes)} routes → {args.write_snapshot}")
        return 0

    if not args.src and not args.files:
        _refuse("need --src <dir> or --files <path...>")
    require_declared_inputs(args.src, args.snapshot)
    exts = {e if e.startswith(".") else f".{e}" for e in args.ext.split(",")}
    # "refguard" skips the guard package itself — its docstrings/comments carry
    # illustrative /api/... examples (e.g. the dropped events sink) that are
    # prose, not live refs. A guard must never flag its own examples.
    skip_dirs = {".git", "node_modules", "__pycache__", "legacy", "refguard"}
    routes = [_segs(r) for r in live_routes(args.snapshot, args.openapi_url, args.app)]

    seen, violations, per_root = set(), [], {}
    for ref, p, n, code in scan_refs(args.src, args.files, exts, skip_dirs, per_root):
        if not ref_is_served(ref, routes):
            violations.append({"ref": ref, "file": str(p), "line": n, "code": code})
            seen.add(ref)

    if args.format == "json":
        print(json.dumps({"scanned": per_root, "gone_refs": sorted(seen),
                          "violations": violations},
                         ensure_ascii=False, indent=2))
    else:
        scanned = ", ".join(f"{r}: {n}" for r, n in per_root.items()) or "--files"
        print(f"refguard/endpoint_refs: {len(routes)} live routes, "
              f"{len(violations)} reference(s) to gone endpoints. "
              f"[scanned {scanned}]")
        for v in violations:
            print(f"  ✗ {v['file']}:{v['line']}  → {v['ref']} (no live route)\n      {v['code']}")
        if violations:
            print(f"\nGone endpoints still referenced: {', '.join(sorted(seen))}")
            print("Fix: drop the call / migrate to the successor route, or mark "
                  "intentional historical prose with 'refguard:ignore'.")
    return 1 if violations else 0


if __name__ == "__main__":
    sys.exit(main())
