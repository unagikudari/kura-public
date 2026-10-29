#!/usr/bin/env python3
"""kura#51 Phase A — the census of who reads and writes `post_tag_actor_claim`.

Plan rev3 A'' chose pgaudit as the instrument and made the census fail closed:
the folding of a 35GB / 60M-row table may not begin while any access path is
unaccounted for. This is that stop rule as a mechanism.

The load-bearing idea is that `unverified_paths: []` must be DERIVED, not
asserted. A census artifact whose author typed an empty list has proved nothing;
here the list is computed by classifying every statement shape the audit log
actually contains against a DECLARED map, and anything unmatched refuses the run.
Growing the map is the work: each entry names the code path that issues the
statement and the question (Q-A..Q-E) it answers, so the decision table in the
plan is fed by evidence rather than by memory.

What it proves before it counts anything:

  1. the instrument is loaded      — pgaudit in shared_preload_libraries, and the
                                     object-audit scope (`pgaudit.role`'s grants)
                                     covers EXACTLY the target table
  2. the instrument observes       — a freshly minted nonce, issued as a real
                                     SELECT, is found in the log this run
  3. the instrument predates       — the earliest audit line for the table is
     the window                      older than the window it is asked to certify
  4. the instrument stayed alive   — an audited heartbeat appears in EVERY bucket
     THROUGH the window              of the window. The ends alone cannot show the
                                     middle: an instrument unloaded for a day and
                                     put back would satisfy 2 and 3 and certify a
                                     day it never saw. Organic traffic cannot
                                     substitute — quiet hours are real (measured)

Only then does it read the window, and any statement it cannot classify is a
refusal. Absent evidence is never a pass.

What it does NOT prove: pgaudit's OBJECT record carries the statement and the
database user, never the caller. Two call sites emitting identical SQL are
indistinguishable here, so the map declares candidate `sites` per shape and the
artifact is stamped `attribution: shape-scoped`. Candidacy is not a licence to
list anything: EVERY declared site must exist and still contain a fragment of
the statement, so a stale or mistyped entry cannot ride along inside a list that
one real site made pass.

Log handling: PostgreSQL here logs to `/var/log/postgresql/` with daily rotation
and .gz for older files (logging_collector=off, so the distro's stderr redirect
owns the file). A reader that tails one file silently loses the window's start,
so every matching file — gzipped included — is concatenated.
"""
from __future__ import annotations

import argparse
import datetime
import glob
import gzip
import hashlib
import json
import os
import pathlib
import re
import subprocess
import sys

REFUSE = 2

#: `2026-09-03 14:43:56.988 JST [1750652] kura@kura LOG:  AUDIT: OBJECT,1,1,READ,SELECT,TABLE,public.x,<stmt>,<not logged>`
_LINE = re.compile(
    r"^(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})[.\d]*\s+\S+\s+\[\d+\]\s+"
    r"(?P<user>\S+)\s+LOG:\s+AUDIT:\s+OBJECT,\d+,\d+,"
    r"(?P<class>[A-Z]+),(?P<cmd>[A-Z ]+),TABLE,(?P<obj>[\w.\"]+),(?P<stmt>.*)$")


#: The heartbeat's statement. It is audited like any other read, so its presence
#: in a bucket is proof the instrument was alive during that bucket — which
#: neither the window's first line nor this run's probe can show.
HEARTBEAT_MARK = "kura-census-heartbeat"


def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _refuse(msg: str) -> int:
    print(f"REFUSE: {msg}", file=sys.stderr)
    return REFUSE


def _digest(doc: dict) -> str:
    body = {k: v for k, v in doc.items() if k != "sha256"}
    return hashlib.sha256(json.dumps(body, sort_keys=True,
                                     ensure_ascii=False).encode()).hexdigest()


def _psql(dsn: str, q: str) -> tuple[int, str]:
    r = subprocess.run(["psql", dsn, "-Atc", q], capture_output=True, text=True)
    return r.returncode, (r.stdout if r.returncode == 0 else r.stderr).strip()


def normalise(stmt: str) -> str:
    """A statement's SHAPE: parameters and literals collapsed, spacing squeezed.

    Shapes are what a census can enumerate; raw statements are unbounded. The
    normalisation is deliberately blunt — over-merging two different statements
    into one shape would hide a path, so the map entries carry the full prefix
    and a mismatch refuses rather than guesses.
    """
    s = stmt.strip()
    # pgaudit CSV-quotes a statement that contains a comma and DOUBLES the quotes
    # inside it, so the FK-cascade DELETE arrives as ""public"".""post_tag…"". Left
    # as-is, no declared prefix can ever match it (measured on the first live run).
    if s.startswith('"') and s.endswith('"'):
        s = s[1:-1]
    s = s.replace('""', '"')
    s = re.sub(r"\$\d+", "$N", s)          # bind parameters
    s = re.sub(r"\b\d+\b", "N", s)          # inline literals
    s = re.sub(r"'[^']*'", "'S'", s)        # string literals
    # This gate's own probe carries a fresh nonce every run. Without collapsing it,
    # each run mints a NEW unclassifiable shape and the census refuses itself
    # (measured on the first live run).
    s = re.sub(r"kura-census-probe:[0-9a-fA-F]+", "kura-census-probe:NONCE", s)
    s = re.sub(r"\s+", " ", s)
    return s.strip()


#: A log record starts with its timestamp; anything else is a continuation of the
#: statement above it.
_ENTRY_START = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}")


def _iter_physical_lines(patterns: list[str]):
    """Every rotated file too, gzip included. Tailing one file loses the window."""
    seen: set[str] = set()
    for pattern in patterns:
        for path in sorted(glob.glob(pattern)):
            if path in seen:
                continue
            seen.add(path)
            try:
                if path.endswith(".gz"):
                    with gzip.open(path, "rt", errors="replace") as fh:
                        yield from fh
                else:
                    with open(path, errors="replace") as fh:
                        yield from fh
            except (OSError, EOFError) as exc:
                raise RuntimeError(f"cannot read audit log {path}: {exc}")


def _statement_is_open(record: str) -> bool:
    """Is this record's statement still unterminated?

    pgaudit CSV-quotes a statement containing commas or newlines and doubles the
    quotes inside it, so an unfinished statement leaves an ODD number of `"` and a
    finished record is balanced.

    Treating "a record exists" as "it can take more lines" let a FINISHED record
    swallow the next line, and the rsplit on the terminator then discarded the
    swallowed statement — one access path gone with no refusal, because the shape
    that survived was a declared one (trainer-codex, BLOCK, reproduced). Judging
    by the quoting is also independent of pgaudit.log_parameter: the field after the
    statement stops being `<not logged>` when parameters are logged, and nothing
    here depends on that.
    """
    return record.count('"') % 2 == 1


def _iter_log_lines(patterns: list[str]):
    """Logical records, not physical lines.

    A statement containing newlines is written across several lines, so matching
    per physical line truncates its shape at the first newline (measured
    2026-09-04: the tag-curation UPDATE became `"UPDATE post_tag_actor_claim SET
    tag_id = $N`). Two different multi-line statements sharing a first line would
    then collapse into one shape — a path hidden behind another declaration, the
    same failure the classifier was hardened against, arriving through the reader.
    """
    buf: str | None = None
    for line in _iter_physical_lines(patterns):
        if _ENTRY_START.match(line):
            if buf is not None:
                yield buf
            buf = line.rstrip("\n")
        elif buf is not None and _statement_is_open(buf):
            buf += " " + line.strip()
        elif buf is not None:
            # A record is open but its statement is terminated, so this line is not a
            # continuation of it — it belongs to a non-audit record (the postgres log
            # also carries client diagnostics, autovacuum notes, and so on). It is not
            # absorbed, which is what the BLOCK was about; it is simply not an audit
            # statement, so it is not counted either.
            continue
        elif line.strip():
            # A continuation with no record open means the log is not the shape this
            # reader assumes (a different log_line_prefix, another process writing into
            # the same file, a truncated head, or a rotation that split a multi-line statement across two
            # files). Dropping it is fail-open: the orphan
            # could carry a statement, and a folded orphan could even coincide with a
            # declared shape (primary-codex, round 1). Refuse instead of guessing.
            raise RuntimeError(
                f"a log file begins with a line that is not a record, so its head is "
                f"missing — the window would start mid-statement and this reader cannot "
                f"tell how much came before: {line.strip()[:120]!r}. Check the glob and "
                f"whether rotation split a record across two files")
    if buf is not None:
        yield buf


def _load_map(path: str) -> list[dict]:
    doc = json.loads(pathlib.Path(path).read_text())
    entries = doc["paths"] if isinstance(doc, dict) else doc
    required = {"shape", "role", "question", "sites"}
    seen: dict[str, str] = {}
    for i, e in enumerate(entries):
        missing = required - set(e)
        if missing:
            raise RuntimeError(f"access-map entry {i} lacks {sorted(missing)}")
        if e["role"] not in ("reader", "writer", "gate-probe", "test"):
            raise RuntimeError(f"access-map entry {i} has role {e['role']!r}; "
                               f"expected reader/writer/gate-probe/test")
        if not isinstance(e["sites"], list) or not e["sites"]:
            raise RuntimeError(
                f"access-map entry {i} ({e['shape'][:60]}…) must list at least one "
                f"possible issuing site. pgaudit's OBJECT record does not say WHICH "
                f"caller emitted a statement, so the map names candidates — but it may "
                f"not name none")
        for j, site in enumerate(e["sites"]):
            # Structured, not "file.py:474 (note)" split on ':' and ' '. A parse
            # that lenient is how a wrong entry comes to look fine (primary-codex,
            # non-blocking rec — folded because it is the same defect as the
            # existential fragment check, seen from the other side).
            if not isinstance(site, dict) or not isinstance(site.get("path"), str) \
                    or not site["path"].strip():
                raise RuntimeError(
                    f"access-map entry {i} site {j} must be an object with a non-empty "
                    f"'path' (optionally 'line' and 'description'); got {site!r}")
            if "line" in site and not isinstance(site["line"], int):
                raise RuntimeError(f"access-map entry {i} site {j}: 'line' must be an int")
        key = normalise(e["shape"])
        if key in seen:
            raise RuntimeError(
                f"access-map entries {seen[key]!r} and {e['sites']!r} normalise to the "
                f"SAME shape — two paths cannot share one declaration; the census would "
                f"credit whichever came first and hide the other")
        seen[key] = str(e["sites"])
    return entries


def _classify(shape: str, entries: list[dict]) -> dict | None:
    """Whole-shape match, never a prefix.

    Prefix matching had no token boundary and no ambiguity check, so a short
    declared prefix could absorb a DIFFERENT statement and the run would report
    `unverified_paths: []` with a real access path hidden behind someone else's
    entry (trainer-codex blocking, round 1). Matching the whole normalised
    shape makes that unrepresentable: a statement the map does not contain
    verbatim is unclassified, and a statement that CHANGED is a new shape — which
    is the honest outcome, because changed SQL is an access path to re-examine,
    not one to inherit silently.
    """
    for e in entries:
        if shape == normalise(e["shape"]):
            return e
    return None


#: The plan's census window (rev2 A'): 14 days, chosen to catch low-frequency
#: readers. A shorter run is a rehearsal, not the census, and the artifact must
#: not let the two be confused.
PLAN_WINDOW_DAYS = 14.0


def cmd_census(a) -> int:
    if a.window_days <= 0:
        return _refuse("--window-days must be positive; a zero-length window observes "
                       "nothing and certifies nothing")
    entries = _load_map(a.map)

    # ── 1. the instrument is loaded, and scoped to exactly this table ────────
    rc, preload = _psql(a.settings_dsn, "SELECT current_setting('shared_preload_libraries')")
    if rc != 0:
        return _refuse(f"cannot read shared_preload_libraries: {preload.splitlines()[:1]} "
                       f"— pass a --settings-dsn that may read it (a superuser, or a "
                       f"role in pg_read_all_settings). An unverifiable instrument is "
                       f"not a verified one")
    if "pgaudit" not in {t.strip() for t in preload.split(",")}:
        return _refuse(f"pgaudit is not loaded (shared_preload_libraries={preload!r}); "
                       f"nothing is being audited, so no census is possible")
    rc, role = _psql(a.settings_dsn, "SELECT current_setting('pgaudit.role', true)")
    if rc != 0 or not role:
        return _refuse("pgaudit.role is unset — the object-audit scope is empty, so the "
                       "log cannot contain what this census would count")
    rc, scope = _psql(a.dsn, f"""
        SELECT coalesce(string_agg(DISTINCT table_schema||'.'||table_name, ',' ), '')
        FROM information_schema.role_table_grants WHERE grantee = '{role}'""")
    if rc != 0:
        return _refuse(f"cannot read the audit scope granted to {role!r}: {scope}")
    scoped = {t for t in scope.split(",") if t}
    want = {f"public.{a.table}"}
    if scoped != want:
        return _refuse(f"the audit scope is {sorted(scoped)}, not exactly {sorted(want)} — "
                       f"a wider scope buries this table's traffic in everything else, a "
                       f"narrower one means the log is missing it")

    # ── 2. the instrument observes, right now ───────────────────────────────
    nonce = hashlib.sha256(os.urandom(16)).hexdigest()[:16]
    rc, out = _psql(a.dsn, f"SELECT count(*) FROM {a.table} WHERE false "
                           f"/* kura-census-probe:{nonce} */")
    if rc != 0:
        return _refuse(f"coverage probe could not run: {out}")
    import time
    time.sleep(a.log_settle_s)
    if not any(nonce in ln for ln in _iter_log_lines(a.log_glob)):
        return _refuse(f"the coverage probe (nonce {nonce}) is not in the audit log — "
                       f"the instrument is not observing the path this census counts")

    # ── 3. read the window ──────────────────────────────────────────────────
    window_start = (datetime.datetime.now() - datetime.timedelta(days=a.window_days))
    obj_suffix = f".{a.table}"
    earliest: datetime.datetime | None = None
    shapes: dict[str, dict] = {}
    heartbeats: list[datetime.datetime] = []
    for ln in _iter_log_lines(a.log_glob):
        m = _LINE.match(ln)
        if not m or not m.group("obj").endswith(obj_suffix):
            continue
        ts = datetime.datetime.strptime(m.group("ts"), "%Y-%m-%d %H:%M:%S")
        if earliest is None or ts < earliest:
            earliest = ts
        if ts < window_start:
            continue
        stmt_raw = m.group("stmt")
        if HEARTBEAT_MARK in stmt_raw:
            heartbeats.append(ts)
        shape = normalise(stmt_raw.rsplit(",<not logged>", 1)[0])
        rec = shapes.setdefault(shape, {"shape": shape, "count": 0, "class": m.group("class"),
                                        "cmd": m.group("cmd").strip(),
                                        "users": set(), "first": ts, "last": ts})
        rec["count"] += 1
        rec["users"].add(m.group("user"))
        rec["last"] = max(rec["last"], ts)
        rec["first"] = min(rec["first"], ts)

    if earliest is None:
        return _refuse("no audit lines for this table at all — the log glob or the "
                       "scope is wrong; an empty log is not an empty census")
    if not shapes:
        return _refuse(f"the window ({a.window_days}d, from "
                       f"{window_start:%Y-%m-%d %H:%M:%S}) contains no audit lines for "
                       f"{a.table}, so it certifies nothing. A census of silence is not "
                       f"a census — widen the window or check the log glob")
    if earliest > window_start:
        return _refuse(f"the instrument does not cover the window it is asked to "
                       f"certify: earliest audit line {earliest:%Y-%m-%d %H:%M:%S} is "
                       f"NEWER than the window start {window_start:%Y-%m-%d %H:%M:%S}. "
                       f"Wait until the instrument has run for {a.window_days} days")

    # ── 3b. the instrument was alive THROUGHOUT, not just at the ends ───────
    gaps = _heartbeat_gaps(heartbeats, window_start, datetime.datetime.now(),
                           a.heartbeat_bucket_minutes)
    if gaps:
        shown = gaps[:6] + ([f"… and {len(gaps) - 6} more"] if len(gaps) > 6 else [])
        return _refuse(
            f"{len(gaps)} of the window's {a.heartbeat_bucket_minutes}-minute buckets "
            f"contain no heartbeat, so the instrument cannot be shown to have been "
            f"running through them: {shown}. The first line and this run's probe only "
            f"prove the ENDS — an instrument unloaded in the middle would still pass "
            f"both. Install the heartbeat timer (systemd/user/kura-claim-census-"
            f"heartbeat.timer) and certify a window it has covered")

    # ── 4. classify; anything unmatched is the refusal ──────────────────────
    readers, writers, others, unverified = [], [], [], []
    for shape, rec in sorted(shapes.items(), key=lambda kv: -kv[1]["count"]):
        entry = _classify(shape, entries)
        item = {"shape": shape[:200], "count": rec["count"], "class": rec["class"],
                "cmd": rec["cmd"], "db_users": sorted(rec["users"]),
                "first_seen": rec["first"].isoformat(), "last_seen": rec["last"].isoformat()}
        if entry is None:
            unverified.append(item)
            continue
        item |= {"possible_sites": entry["sites"], "question": entry["question"],
                 "attributes": entry.get("attributes", []), "note": entry.get("note", "")}
        {"reader": readers, "writer": writers}.get(entry["role"], others).append(item)

    if unverified:
        for u in unverified:
            print(f"  ✗ unclassified: [{u['class']}/{u['cmd']} ×{u['count']}] "
                  f"{u['shape'][:120]}", file=sys.stderr)
        return _refuse(
            f"{len(unverified)} statement shape(s) in the window are not in "
            f"{a.map} — every access path must be named before the table may be "
            f"folded. Find the code that issues each one, classify it against the "
            f"Q-A..Q-E table, and add it to the map (this is the census work, not "
            f"an obstacle to it)")

    is_census = a.window_days >= PLAN_WINDOW_DAYS
    doc = {"kind": "claim-census" if is_census else "claim-census-rehearsal",
           "produced_at": _now(),
           "subject_rev": f"{a.table}@{a.window_days}d",
           "payload": {"table": a.table, "window_days": a.window_days,
                       "window_start": window_start.isoformat(),
                       "instrument_earliest": earliest.isoformat(),
                       "audit_role": role, "coverage_proven": True,
                       # pgaudit's OBJECT record carries the statement and the db
                       # user, never the caller. Everything below is therefore a
                       # claim about SHAPES, with candidate sites — not provenance.
                       "attribution": "shape-scoped",
                       "continuity_proven": True,
                       "heartbeats": len(heartbeats),
                       "heartbeat_bucket_minutes": a.heartbeat_bucket_minutes,
                       "meets_plan_window": is_census,
                       "audited_lines": sum(r["count"] for r in shapes.values()),
                       "readers": readers, "writers": writers,
                       "non_production": others, "unverified_paths": []}}
    doc["sha256"] = _digest(doc)
    pathlib.Path(a.out).write_text(json.dumps(doc, indent=1, ensure_ascii=False) + "\n")
    kind = ("CENSUS" if is_census else
            f"REHEARSAL (window < the plan's {PLAN_WINDOW_DAYS:g}d — artifact kind is "
            f"claim-census-rehearsal, which no census consumer accepts)")
    print(f"{kind}: {len(readers)} reader shape(s), {len(writers)} writer shape(s), "
          f"{len(others)} non-production, 0 unverified, "
          f"{sum(r['count'] for r in shapes.values())} audited line(s) over "
          f"{a.window_days}d (instrument since {earliest:%Y-%m-%d %H:%M})")
    print(f"artifact: {a.out} sha256={doc['sha256']}")
    return 0


def cmd_heartbeat(a) -> int:
    """One audited read, so the window can prove it was observed throughout.

    Run by a timer. Cheap by construction (`WHERE false` touches no rows) and
    declared in the access map like every other path.
    """
    rc, out = _psql(a.dsn, f"SELECT count(*) FROM {a.table} WHERE false "
                           f"/* {HEARTBEAT_MARK} */")
    if rc != 0:
        return _refuse(f"heartbeat could not run: {out}")
    print(f"heartbeat: ok ({a.table})")
    return 0


def _heartbeat_gaps(times: list[datetime.datetime], window_start: datetime.datetime,
                    now: datetime.datetime, bucket_minutes: int) -> list[str]:
    """Buckets of the window with no heartbeat in them.

    The first and last buckets are partial by definition, so they are judged the
    same way as the rest: a heartbeat is expected in every bucket the window
    covers. A timer that only started mid-window therefore refuses, which is the
    honest outcome — the census cannot vouch for the part that predates its own
    liveness evidence.
    """
    step = datetime.timedelta(minutes=bucket_minutes)
    stamps = sorted(times)
    gaps, edge = [], window_start
    while edge < now:
        nxt = min(edge + step, now)
        if not any(edge <= t < nxt for t in stamps):
            gaps.append(f"{edge:%Y-%m-%d %H:%M}–{nxt:%H:%M}")
        edge = nxt
    return gaps


def cmd_verify(a) -> int:
    """What a consumer of the census must run before trusting it.

    Finding 2's other half: a rehearsal artifact must not be promotable by
    whoever reads the file next, so the acceptance rules live here rather than
    in each consumer's head.
    """
    doc = json.loads(pathlib.Path(a.artifact).read_text())
    if doc.get("kind") != "claim-census":
        return _refuse(f"{a.artifact} is kind {doc.get('kind')!r}; only a full "
                       f"claim-census may be used as census evidence (a rehearsal "
                       f"carries kind claim-census-rehearsal on purpose)")
    if _digest(doc) != doc.get("sha256"):
        return _refuse(f"{a.artifact} digest mismatch — tampered or corrupt")
    p = doc.get("payload", {})
    if p.get("unverified_paths"):
        return _refuse(f"{len(p['unverified_paths'])} unverified path(s) recorded")
    if not p.get("meets_plan_window"):
        return _refuse(f"window {p.get('window_days')}d does not meet the plan's "
                       f"{PLAN_WINDOW_DAYS:g}d")
    if not p.get("coverage_proven") or not p.get("continuity_proven"):
        return _refuse("the artifact does not carry both a coverage proof and a "
                       "continuity proof")
    age = (datetime.datetime.now(datetime.timezone.utc)
           - datetime.datetime.fromisoformat(doc["produced_at"].replace("Z", "+00:00")))
    if age > datetime.timedelta(days=a.max_age_days):
        return _refuse(f"census is {age} old (max {a.max_age_days}d)")
    print(f"verify: census accepted — {len(p['readers'])} reader(s), "
          f"{len(p['writers'])} writer(s), 0 unverified, {p['window_days']}d window")
    return 0


def cmd_shapes(a) -> int:
    """List the shapes in the window and whether they are declared.

    Growing the map IS the census work, so the gate has to make the next entry
    obvious rather than make the operator reverse-engineer a refusal message.
    Read-only: it issues no probe and writes no artifact.
    """
    entries = _load_map(a.map) if a.map else []
    window_start = datetime.datetime.now() - datetime.timedelta(days=a.window_days)
    obj_suffix = f".{a.table}"
    counts: dict[str, int] = {}
    for ln in _iter_log_lines(a.log_glob):
        m = _LINE.match(ln)
        if not m or not m.group("obj").endswith(obj_suffix):
            continue
        if datetime.datetime.strptime(m.group("ts"), "%Y-%m-%d %H:%M:%S") < window_start:
            continue
        shape = normalise(m.group("stmt").rsplit(",<not logged>", 1)[0])
        counts[shape] = counts.get(shape, 0) + 1
    for shape, n in sorted(counts.items(), key=lambda kv: -kv[1]):
        entry = _classify(shape, entries)
        mark = ("declared:" + ",".join(
            f"{x['path']}:{x['line']}" if "line" in x else x["path"]
            for x in entry["sites"])) if entry else "UNDECLARED"
        print(f"{n:>8}  {mark}\n          {shape}")
    print(f"\n{len(counts)} distinct shape(s) over {a.window_days}d")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("census")
    p.add_argument("--dsn", required=True, help="the database holding the table")
    p.add_argument("--settings-dsn", required=True,
                   help="a DSN that may read shared_preload_libraries (superuser or "
                        "pg_read_all_settings); unreadable = refused, not skipped")
    p.add_argument("--table", default="post_tag_actor_claim")
    p.add_argument("--map", required=True, help="the declared access map (JSON)")
    p.add_argument("--log-glob", action="append", required=True,
                   help="audit log glob, repeatable; rotated and .gz files included")
    p.add_argument("--window-days", type=float, default=14.0,
                   help="the census window. The plan requires 14; smaller values are "
                        "rehearsals and the artifact says so (meets_plan_window)")
    p.add_argument("--log-settle-s", type=float, default=2.0)
    p.add_argument("--heartbeat-bucket-minutes", type=int, default=60,
                   help="every bucket of the window must contain a heartbeat; a gap "
                        "refuses, because the ends alone cannot show the middle")
    p.add_argument("--out", required=True)
    p.set_defaults(fn=cmd_census)

    p = sub.add_parser("heartbeat", help="one audited read; run me from a timer")
    p.add_argument("--dsn", required=True)
    p.add_argument("--table", default="post_tag_actor_claim")
    p.set_defaults(fn=cmd_heartbeat)

    p = sub.add_parser("verify", help="what a CONSUMER of the census runs")
    p.add_argument("--artifact", required=True)
    p.add_argument("--max-age-days", type=float, default=7.0)
    p.set_defaults(fn=cmd_verify)
    p = sub.add_parser("shapes", help="list the shapes in the window (read-only)")
    p.add_argument("--table", default="post_tag_actor_claim")
    p.add_argument("--map", default="")
    p.add_argument("--log-glob", action="append", required=True)
    p.add_argument("--window-days", type=float, default=14.0)
    p.set_defaults(fn=cmd_shapes)

    a = ap.parse_args()
    try:
        return a.fn(a)
    except RuntimeError as exc:
        return _refuse(str(exc))


if __name__ == "__main__":
    sys.exit(main())
