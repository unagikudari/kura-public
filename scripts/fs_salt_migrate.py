#!/usr/bin/env python3
# Description: kura#61 salt migration — backfill the other name (hard link), audit the state, clean up the legacy name, and decide Phase C from access logs.
# Kind: oneshot
# Effects: mutating (backfill/cleanup); read-only (audit/tally/--dry-run)
# Requires: DATA_DIR, KURA_FS_NAME_SALT (+ _NEXT in Phase A / _LEGACY in Phase B)
# Runs-on: the node that holds the media (primary)
# Version: 1.0
"""kura#61: give every content file a second name without stopping delivery.

    backfill   Phase A. For every file named by the reader's salt that lacks the
               other name, add it as a hard link (link to a tmp name, rename over —
               never a window without the name). Idempotent, resumable, throttled.
    audit      Count, per (post, ext) name pair: both names / reader-only / other-only /
               diverged / chained / broken / tmp residue. Progress is the disk, not a
               ledger; the numbers are monotone in a phase or something is writing
               under one name only.
    guard      The boot guard's verdict over EVERY name (what the next restart would
               decide) — the rehearsal a 20-name sample cannot be.
    cleanup    Phase C. Unlink the LEGACY name of every file that has both names.
               Refuses unless KURA_FS_NAME_SALT_LEGACY is set (that is what says
               "the reader moved on").
    tally      Decide whether Phase C may start, from the web server's access log:
               first-party requests for legacy-named URLs (bots, and requests that
               exist to check the instrument, excluded) over the last 7 days, plus
               the bounded fallback (30 days since Phase B and the daily total below
               a threshold). Prints `start: yes|no|unknown`.

               It PROVES the log observes the content path before answering: one
               legacy-named GET is fired at the production ingress and must come
               back out of the log that was read. Without that, "nobody asked" and
               "nothing was recorded" are the same sentence — and this is the only
               gate on an irreversible unlink of every legacy name.

Names are derived by api.services.files only — this script never builds one.
"""
from __future__ import annotations

import argparse
import datetime as dt
import fcntl
import json
import os
import pathlib
import re
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from api.config import CUSTOM_THUMBNAILS_DIR, DATA_DIR, POSTS_DIR, THUMBNAILS_DIR  # noqa: E402
from api.services import files  # noqa: E402
from api.services.hashing import get_legacy_hash, get_secondary_hash, get_security_hash, migration_phase  # noqa: E402

_FILENAME = re.compile(r"^(\d+)_([0-9a-f]{16})\.([A-Za-z0-9]+)$")
_TMP = re.compile(r"\.tmp\.\d+$")
_DIRS = {"content": POSTS_DIR, "thumbnail": THUMBNAILS_DIR, "custom-thumbnail": CUSTOM_THUMBNAILS_DIR}
_BOT_UA = re.compile(r"bot|crawl|spider|slurp|fetch|preview|scan|python-requests|curl/|wget/", re.I)


def _scan(kind: str):
    """Yield (post_id, hash, ext, path) for every real (non-tmp) file of `kind`."""
    d = DATA_DIR / _DIRS[kind]
    if not d.is_dir():
        return
    with os.scandir(d) as it:
        for e in it:
            # a symlink to a file IS a name of that file (71% of posts on the canonical node)
            if not (e.is_file(follow_symlinks=True) or e.is_symlink()):
                continue
            if _TMP.search(e.name):
                yield None, None, None, pathlib.Path(e.path)
                continue
            m = _FILENAME.match(e.name)
            if m:
                yield int(m.group(1)), m.group(2), m.group(3), pathlib.Path(e.path)


def audit(kinds=("content", "thumbnail", "custom-thumbnail")) -> dict:
    """The state of the disk, by kind, counted in FILES = (post_id, ext) name pairs —
    a post with a `.jpg` and a `.webp` is two files and two rows. `other` is the
    secondary salt's name when a migration phase is set; outside a phase only
    reader/unknown/tmp are counted."""
    out = {}
    for kind in kinds:
        c = {"reader_and_other": 0, "reader_only": 0, "other_only": 0, "diverged": 0, "chained": 0,
             "broken_pair": 0, "unknown_salt": 0, "tmp_residue": 0, "symlink_names": 0, "broken_symlinks": 0}
        # The unit is a NAME PAIR, keyed by (post_id, ext) — not a post. Measured on the
        # canonical node 2026-09-05: 1,392 posts carry TWO reader-salt names with
        # different extensions (a `.jpg` original behind a symlink and a local `.webp`);
        # keyed by post_id alone, whichever name scandir yielded last was compared
        # against the other extension's second name and reported as "diverged" —
        # a defect of this audit, not of the disk. The other name of a file keeps its
        # extension (files._name with the same mime), so (post_id, ext) is the pair.
        seen: dict[tuple[int, str], dict[str, pathlib.Path]] = {}
        for pid, h, ext, path in _scan(kind):
            if pid is None:
                c["tmp_residue"] += 1
                continue
            if path.is_symlink():
                c["symlink_names"] += 1
                if not path.exists():
                    c["broken_symlinks"] += 1
            role = "reader" if h == get_security_hash(pid) else ("other" if h == get_secondary_hash(pid) else "unknown")
            if role == "unknown":
                c["unknown_salt"] += 1
                continue
            seen.setdefault((pid, ext), {})[role] = path
        for roles in seen.values():
            if set(roles) == {"reader", "other"}:
                r, o = roles["reader"], roles["other"]
                if files.verify_names(o, r) and files.verify_names(r, o):
                    c["reader_and_other"] += 1   # accepted in both directions (rev 6 predicate)
                elif any(x.is_symlink() and not x.exists() for x in (r, o)):
                    c["broken_pair"] += 1        # a missing target, not two files (round 1, finding 3)
                elif files.same_file(r, o):
                    c["chained"] += 1            # one name is a symlink to the other: reachable, not independent
                else:
                    c["diverged"] += 1           # two names, two files — the state that must not exist
            elif set(roles) == {"reader"}:
                c["reader_only"] += 1
            else:
                c["other_only"] += 1
        out[kind] = c
    out["phase"] = migration_phase() or "none"
    return out


class _OneMigrationAtATime:
    """Refuse to run a second mutating pass while one is running. On 2026-09-05 the
    Phase A backfill was started from an interactive pane while a sibling pane of the
    same lane was answering "not started" — a lock turns "please do not run it twice"
    into a refusal the second runner sees."""

    def __init__(self, what: str):
        self.what = what
        self.path = DATA_DIR / ".fs_salt_migrate.lock"

    def __enter__(self):
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        self.fh = open(self.path, "a+")
        try:
            fcntl.flock(self.fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self.fh.seek(0); holder = self.fh.read().strip()
            self.fh.close()
            raise SystemExit(f"another fs_salt_migrate run holds {self.path} ({holder or 'pid unknown'}); refusing to run {self.what} concurrently")
        self.fh.seek(0); self.fh.truncate(); self.fh.write(f"pid {os.getpid()} {self.what} {dt.datetime.now(dt.timezone.utc).isoformat()}"); self.fh.flush()
        return self

    def __exit__(self, *exc):
        fcntl.flock(self.fh, fcntl.LOCK_UN); self.fh.close()
        return False


def guard(sample: int | None = None) -> dict:
    """Ask the boot guard's question of the WHOLE tree (kura#61 1.199.4 made it phase-aware).

    A 20-name spread sample is what boot runs; it lands on a second name with probability
    ≈0.00015 after ten links and ≈1 after the full backfill, so the sample cannot rehearse
    the guard — and its positions move as uploads change the total, which is how the
    2026-09-05 refusal "recovered" by luck for five minutes. This is the rehearsal: every
    name, the verdict the next restart would reach.
    """
    verdict, checked, matched = files.verify_salt_names_the_files(sample)
    return {"verdict": verdict, "checked": checked, "matched_reader": matched, "sample": sample,
            "phase": migration_phase() or "none", "next_restart_boots": verdict == "ok"}


def backfill(kinds, batch: int, sleep: float, dry_run: bool, limit: int | None = None) -> dict:
    """Phase A: add the other name to every reader-named file that lacks it."""
    if migration_phase() is None:
        raise SystemExit("no migration phase is set (KURA_FS_NAME_SALT_NEXT or _LEGACY); nothing to backfill")
    done = skipped = diverged = repaired = refused = broken_reader = 0
    for kind in kinds:
        n = 0
        for pid, h, ext, path in _scan(kind):
            if pid is None or h != get_security_hash(pid):
                continue
            other = files._name(kind, pid, get_secondary_hash(pid), None if kind != "content" else _mime_for_ext(ext))
            if path.is_symlink() and not path.exists():
                broken_reader += 1                     # the reader's own name has no file: nothing to give a second name
                continue
            if os.path.lexists(other):
                if files.verify_names(path, other):
                    skipped += 1                       # already a second name (rev 6 predicate)
                    continue
                if other.is_symlink() and not other.exists():
                    if not dry_run:                    # a broken second name: repair, do not count as two files
                        os.unlink(other)
                        files.link_secondary(path, other)
                    repaired += 1
                    continue
                diverged += 1                          # two names, two files (or a chained name): report, never overwrite
                continue
            if not dry_run:
                try:
                    files.link_secondary(path, other)
                except files.SecondNameRejected:
                    refused += 1                       # no second name passes verify_names (EXDEV on a regular file)
                    continue
            done += 1; n += 1
            if limit and done >= limit:
                return {"linked": done, "already": skipped, "diverged": diverged, "repaired": repaired,
                        "refused": refused, "broken_reader": broken_reader, "dry_run": dry_run, "stopped_at_limit": True}
            if batch and n % batch == 0:
                time.sleep(sleep)
    return {"linked": done, "already": skipped, "diverged": diverged, "repaired": repaired,
            "refused": refused, "broken_reader": broken_reader, "dry_run": dry_run}


def cleanup(kinds, batch: int, sleep: float, dry_run: bool) -> dict:
    """Phase C: drop the legacy name where the reader's name exists."""
    if migration_phase() != "B":
        raise SystemExit("KURA_FS_NAME_SALT_LEGACY is not set — the reader has not moved to the new salt; refusing to unlink anything")
    removed = kept = 0
    for kind in kinds:
        n = 0
        for pid, h, ext, path in _scan(kind):
            if pid is None or h != get_legacy_hash(pid):
                continue
            reader = files._name(kind, pid, get_security_hash(pid), None if kind != "content" else _mime_for_ext(ext))
            # rev 6: unlink an old name only when the pair passes the acceptance predicate —
            # reachability AND independence. `same_file` alone is true for a reader that is a
            # symlink TO the legacy name, and unlinking that legacy name deletes the file.
            if not os.path.lexists(reader) or not files.verify_names(path, reader):
                kept += 1          # the only name left, a different file, or a chained name — never unlink it
                continue
            if not dry_run:
                os.unlink(path)    # the NAME only; the file stays under the reader's name
            removed += 1; n += 1
            if batch and n % batch == 0:
                time.sleep(sleep)
    return {"unlinked": removed, "kept_as_only_name": kept, "dry_run": dry_run}


def _mime_for_ext(ext: str) -> str | None:
    for mime, e in files.MIME_TO_EXT.items():
        if e == ext:
            return mime
    return None


_LOG = re.compile(r'^(?P<ip>\S+) \S+ \S+ \[(?P<ts>[^\]]+)\] "(?P<method>\S+) (?P<path>\S+)[^"]*" (?P<status>\d{3}) \S+ "(?P<ref>[^"]*)" "(?P<ua>[^"]*)"')


#: The canary's UA. A detector with no known kill cannot report zero: before
#: believing "nobody asked for a legacy name", tally makes ONE such request
#: itself, through the production ingress, and refuses to answer unless it can
#: find that request in the log it read.
#:
#: Why this exists (2026-09-05): tally was reading nginx's access log while
#: production served /data/ from `tailscale serve ... path <media dir>`, which
#: never touches nginx. The log held 22 lines, all local scripts, so
#: `first_party_last_days` was [0]*7 and `start` was "yes" — on an instrument
#: that structurally could not see the traffic it was measuring. That answer was
#: the only gate on unlinking 2,074,210 names.
_CANARY_UA = "kura-fs-salt-tally-canary/"

#: post_id 0 exists in no tree, so the canary 404s and cannot be mistaken for
#: demand for a real file. It is still a LEGACY-shaped name, which is the point:
#: coverage is proven for the exact class of request tally counts, not merely
#: "some line reached the log".
_CANARY_POST_ID = 0

#: The UA an OPERATOR gives a request that checks the instrument rather than uses
#: it — the curl in the cutover procedure, a hand-fired legacy GET, anything whose
#: purpose is "did the switch work". Same treatment as the canary: it is a request
#: about the instrument, not demand for a legacy name.
#:
#: Why this exists (2026-09-06): verifying the cutover, a pane fired a legacy-named
#: GET with a hand-set Referer and a browser-shaped UA. tally counted it as
#: first-party demand, so `cond_first_party_zero` went false and the day the
#: instrument went in became a day the gate says somebody wanted a legacy name.
#: Checking the gate moved the gate. Keeping that out is a mechanism, not a rule
#: the next operator has to remember.
_VERIFY_UA = "kura-verify-probe/"

#: Matched as an exact PREFIX, and deliberately NOT folded into `_BOT_UA`. That
#: regex answers "is this a machine", which is a different question with different
#: consequences: a substring/regex exclusion would let any traffic that merely
#: mentions the string hide real demand, and would tie the exclusion to how the
#: prober's client happens to spell itself. Bots and operators stay separate.
_PROBE_UA_PREFIXES = (_CANARY_UA, _VERIFY_UA)


def _open_log(p: pathlib.Path):
    """Rotations are part of the window: .1 and .gz are read like the live file."""
    if p.suffix == ".gz":
        import gzip
        return gzip.open(p, "rt", errors="replace")
    return p.open(errors="replace")


def _log_files(patterns) -> list[pathlib.Path]:
    """Accept one path, a glob, or several of either.

    `--access-log /var/log/nginx/access.log*` unquoted is expanded by the shell into
    many arguments; quoted it arrives as one glob for us to expand. Round-1 finding
    E3: taking a single string made the first form an argparse error and the naive
    single-file form silently short of the window. Both spellings now work."""
    import glob as _glob
    if isinstance(patterns, (str, pathlib.Path)):
        patterns = [patterns]
    out: set[pathlib.Path] = set()
    for pat in patterns:
        pat = str(pat)
        hits = _glob.glob(pat)
        out.update(pathlib.Path(p) for p in (hits or [pat]) if os.path.isfile(p))
    return sorted(out)


def fire_canary(ingress: str, timeout: float = 10.0) -> dict:
    """One legacy-named GET through the production ingress. 404 is the expected answer."""
    import urllib.error
    import urllib.request
    ua = f"{_CANARY_UA}{int(time.time())}"
    path = f"/data/{POSTS_DIR}/{_CANARY_POST_ID}_{get_legacy_hash(_CANARY_POST_ID)}.jpg"
    url = ingress.rstrip("/") + path
    status, err = None, None
    try:
        req = urllib.request.Request(url, headers={"User-Agent": ua})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            status = r.status
    except urllib.error.HTTPError as e:
        status = e.code                      # 404 is success for this purpose
    except Exception as e:                   # noqa: BLE001 — unreachable ingress is a finding, not a crash
        err = f"{e.__class__.__name__}: {e}"
    return {"ua": ua, "url": url, "status": status, "error": err}


def tally(access_log: str, days: int, origin: str, phase_b_started: dt.date | None,
          fallback_days: int, threshold: int, now: dt.date | None = None,
          ingress: str = "", canary: dict | None = None,
          settle_s: float = 2.0, instrumented_since: dt.date | None = None) -> dict:
    """Count legacy-named URL requests per day; decide Phase C.

    Answers `start: yes | no | unknown`. `unknown` is not a failure — it is the
    state that keeps "the instrument did not observe this" out of `yes`, which is
    what a gate on an irreversible unlink has to do (#66 principle 3).
    """
    if migration_phase() != "B":
        raise SystemExit("KURA_FS_NAME_SALT_LEGACY is not set — nothing is legacy yet")
    now = now or dt.date.today()
    since = now - dt.timedelta(days=days - 1)

    if canary is None and ingress:
        canary = fire_canary(ingress)
        time.sleep(settle_s)                 # the log line is written after the response

    files_read = _log_files(access_log)
    per_day: dict[str, dict[str, int]] = {}
    # Coverage is the SPAN the log reaches, not the set of days that happened to
    # carry traffic (round-1 finding E1). A quiet day writes no line, so counting
    # days-with-requests made a low-traffic window permanently short: the gate
    # would answer `unknown` forever with the instrument working perfectly —
    # a false unknown is as much a failure as a false yes, it just fails silent.
    log_first: dt.date | None = None
    log_last: dt.date | None = None
    future_dated = 0
    canary_seen = False
    canary_ua = (canary or {}).get("ua")
    for f in files_read:
        for line in _open_log(f):
            m = _LOG.match(line)
            if not m:
                continue
            ua = m.group("ua")
            try:
                day = dt.datetime.strptime(m.group("ts").split()[0], "%d/%b/%Y:%H:%M:%S").date()
            except ValueError:
                continue
            if canary_ua and canary_ua in ua:
                canary_seen = True
            if day > now:
                # A line dated after "now" means the log's clock and ours disagree
                # (round-1 finding E2). Left unbounded it also let a later day fill
                # the coverage span while `fp_days` never scanned it, so a legacy
                # request could sit in the log and outside the count at once.
                future_dated += 1
                continue
            log_first = day if log_first is None else min(log_first, day)
            log_last = day if log_last is None else max(log_last, day)
            if ua.startswith(_PROBE_UA_PREFIXES):
                continue                     # never count our own probes as demand
            fm = _FILENAME.match(pathlib.PurePosixPath(m.group("path").split("?", 1)[0]).name)
            if not fm:
                continue
            pid, h = int(fm.group(1)), fm.group(2)
            if h != get_legacy_hash(pid):
                continue
            if day < since:
                continue
            d = per_day.setdefault(day.isoformat(), {"total": 0, "bot": 0, "first_party": 0})
            d["total"] += 1
            if _BOT_UA.search(ua):
                d["bot"] += 1
            elif origin and origin in m.group("ref"):
                d["first_party"] += 1

    fp_days = [per_day.get((since + dt.timedelta(days=i)).isoformat(), {}).get("first_party", 0) for i in range(days)]
    cond_a = all(v == 0 for v in fp_days)
    cond_b = False
    if phase_b_started:
        elapsed = (now - phase_b_started).days
        worst = max((v.get("total", 0) for v in per_day.values()), default=0)
        cond_b = elapsed >= fallback_days and worst <= threshold

    # Coverage gates BOTH conditions. cond_b is not the safe one: on an empty log
    # `worst` is 0, so it goes true on elapsed alone — the same zero-from-nothing
    # as cond_a, just slower.
    spans = bool(log_first and log_last and log_first <= since and log_last >= now)
    coverage = {"canary": canary, "canary_seen": canary_seen,
                "log_first_day": log_first.isoformat() if log_first else None,
                "log_last_day": log_last.isoformat() if log_last else None,
                "window": [since.isoformat(), now.isoformat()], "spans_window": spans,
                "future_dated_lines": future_dated,
                "log_files": [str(f) for f in files_read]}
    coverage["instrumented_since"] = instrumented_since.isoformat() if instrumented_since else None
    err = (canary or {}).get("error")
    status = (canary or {}).get("status")
    reason = None
    if canary is None:
        reason = "no canary was fired — coverage of the content path is unproven"
    elif err:
        # An ingress that could not be reached is NOT "the log does not see the
        # path" (round-1 D5): the two send the operator to opposite places — check
        # the URL/network vs check the log pipeline. Collapsing them is the same
        # wrong-remedy defect this whole change exists to remove.
        reason = f"canary probe to ingress failed: {err}"
    elif status == 200:
        # 404 is the expected answer. A body means post_id 0 resolves to a real
        # file, so the probe is asking for something that could be in demand —
        # the premise that makes the canary harmless is gone (round-1 D4).
        reason = "canary probe was SERVED (HTTP 200) — post_id 0 exists, so the probe is not inert"
    elif status != 404:
        reason = f"canary probe returned HTTP {status} — the ingress did not answer as expected"
    elif not canary_seen:
        reason = "log does not observe the content path"
    elif instrumented_since is None:
        reason = ("--instrumented-since was not given — the window cannot be shown to lie "
                  "after the content path was instrumented")
    elif (now - instrumented_since).days < days:
        # round-1 D1, the blocking finding. `observed` is filled by ANY request,
        # and the old route still put `/` hits in this log, so the day count is
        # satisfied by traffic from before the cutover. Without this clause the
        # gate can open on the very day the instrument goes in: canary passes
        # today, six pre-cutover days fill the window, legacy demand reads 0.
        # The 7 days must be counted from the instrument, not from the calendar.
        reason = (f"content path instrumented since {instrumented_since.isoformat()} "
                  f"({(now - instrumented_since).days}d < {days}d)")
    elif future_dated:
        # Not merely tidiness: every day boundary in this answer, including the
        # one that decides `fp_days`, is drawn with the same clock.
        reason = (f"{future_dated} log line(s) are dated after {now.isoformat()} — "
                  f"the log's clock and this run's disagree, so the window is not trustworthy")
    elif not spans:
        reason = (f"log spans {log_first} … {log_last}, which does not cover "
                  f"{since.isoformat()} … {now.isoformat()} — read the rotations "
                  f"(--access-log accepts a glob and several paths)")
    start = "unknown" if reason else ("yes" if (cond_a or cond_b) else "no")
    return {"per_day": per_day, "first_party_last_days": fp_days,
            "cond_first_party_zero": cond_a, "cond_bounded_fallback": cond_b,
            "coverage": coverage, "reason": reason, "start": start}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("backfill", "cleanup"):
        s = sub.add_parser(name)
        s.add_argument("--kind", action="append", choices=list(_DIRS), default=None)
        s.add_argument("--batch", type=int, default=500)
        s.add_argument("--sleep", type=float, default=0.2)
        s.add_argument("--limit", type=int, default=None)
        s.add_argument("--dry-run", action="store_true")
    sub.add_parser("audit")
    g = sub.add_parser("guard", help="the boot guard's verdict over the whole tree (sample 0 = every name)")
    g.add_argument("--sample", type=int, default=0)
    t = sub.add_parser("tally", description=(
        "Requests whose UA starts with '" + _VERIFY_UA + "' are excluded from the count "
        "as checks OF the instrument rather than demand through it. Give that prefix to "
        "any request you fire to verify the log path: -A '" + _VERIFY_UA + "<who>'."))
    t.add_argument("--access-log", required=True, nargs="+",
                   help="one or more paths/globs. Rotations (.1, .gz) are part of the "
                        "window; reading only the live file silently shortens it. Both "
                        "'/var/log/nginx/access.log*' (quoted, expanded here) and an "
                        "unquoted wildcard (expanded by the shell) work")
    t.add_argument("--ingress", required=True,
                   help="the PRODUCTION base URL content is served from "
                        "(e.g. https://<host>:8443). tally fires one legacy-named GET at it "
                        "and refuses to answer unless that request appears in the log it read")
    t.add_argument("--days", type=int, default=7)
    t.add_argument("--origin", default="", help="own origin host, to count first-party requests by Referer")
    t.add_argument("--phase-b-started", default=None, help="YYYY-MM-DD")
    t.add_argument("--instrumented-since", default=None,
                   help="YYYY-MM-DD — the day the content path started being logged. "
                        "REQUIRED for a yes: the window has to lie after the instrument "
                        "went in, and pre-cutover traffic to other paths would otherwise "
                        "fill the day count (round-1 finding D1)")
    t.add_argument("--settle-s", type=float, default=2.0,
                   help="seconds to wait for the canary's line to be flushed to the log "
                        "before reading it; raise it where the log buffers")
    t.add_argument("--fallback-days", type=int, default=30)
    t.add_argument("--threshold", type=int, default=10)
    a = ap.parse_args(argv)
    kinds = tuple(a.kind) if getattr(a, "kind", None) else ("content", "thumbnail", "custom-thumbnail")
    if a.cmd == "audit":
        out = audit()
    elif a.cmd == "guard":
        out = guard(None if a.sample == 0 else a.sample)
    elif a.cmd == "backfill":
        with _OneMigrationAtATime("backfill"):
            out = backfill(kinds, a.batch, a.sleep, a.dry_run, a.limit)
    elif a.cmd == "cleanup":
        with _OneMigrationAtATime("cleanup"):
            out = cleanup(kinds, a.batch, a.sleep, a.dry_run)
    else:
        started = dt.date.fromisoformat(a.phase_b_started) if a.phase_b_started else None
        instrumented = dt.date.fromisoformat(a.instrumented_since) if a.instrumented_since else None
        out = tally(a.access_log, a.days, a.origin, started, a.fallback_days, a.threshold,
                    ingress=a.ingress, settle_s=a.settle_s, instrumented_since=instrumented)
    print(json.dumps(out, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
