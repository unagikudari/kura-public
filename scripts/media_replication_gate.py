#!/usr/bin/env python3
"""The mechanized stop rule for kura#50 (#38) — the gate the reclaim cannot bypass.

kura#49's implementation review ruled that a committed stop rule interpreted by
the operator is no rule at all: the deviation needed durable evidence and
retrospective user authorization. This script is that lesson as a mechanism —
every irreversible step of #50 runs THROUGH a check here, and a check that
cannot prove its precondition exits 2 (refuse) rather than letting anyone argue
purpose. `media_reclaim_run.py` calls `verify-artifacts` at the start of every
batch; a missing, stale or tampered artifact stops the mutation mid-run.

Artifact schema (every check writes one on PASS):
    {"kind": "<check>", "produced_at": "<iso8601Z>",
     "subject_rev": "<git sha or config digest the artifact speaks about>",
     "payload": {...check-specific evidence...},
     "sha256": "<over the whole artifact minus this field — metadata included,
                so freshness/subject substitution breaks the digest>"}

Checks (each a subcommand; node-local ones run ON that node via its own lane):
    attestation        every serving worker attests the expected role — pid set
                       from the systemd cgroup, exact-equal, re-enumerated after
                       the proof to catch churn (R2 rev3)
    census             the replica-observe instrument shows coverage (synthetic
                       probe appears) and zero organic local serves in the window
    topology-publisher post reaches the subscriber via exactly one publication
                       (runs on the publisher)
    topology-subscriber
                       pg_subscription_rel all 'r' (runs on the subscriber; the two
                       halves join over artifacts because this fleet's pg_hba admits
                       the subscriber for REPLICATION only)
    column-list        the (proposed) publication column list still contains the
                       replica identity — PG18 accepts a list that omits it and only
                       fails at the next publisher UPDATE
    replication-health subscription enabled, worker present, lag under threshold
    free-space         target filesystem holds the declared headroom
    null-invariant     (Phase 2+) SUM(octet_length(data|thumbnail)) = 0 and the
                       publication column list digest is unchanged
    verify-artifacts   digest + freshness check over required artifacts — the
                       executor's per-batch entry point
"""
from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import math
import os
import pathlib
import re
import subprocess
import sys
import time
import urllib.request

REFUSE = 2


def _port_of(url: str) -> int:
    """The attested port comes from the probe URL: the surface being attested and
    the surface whose socket holders are enumerated must be the same one."""
    import urllib.parse
    u = urllib.parse.urlparse(url)
    if u.port:
        return u.port
    if u.scheme == "https":
        return 443
    if u.scheme == "http":
        return 80
    raise RuntimeError(f"cannot derive a port from probe URL {url!r} — pass --port")

def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _artifact_digest(art: dict) -> str:
    """Digest over the WHOLE artifact except the sha256 field itself — a digest
    covering only the payload let produced_at/kind/subject_rev be edited without
    detection, i.e. freshness and subject substitution (impl r1 blocking)."""
    body = {k: v for k, v in art.items() if k != "sha256"}
    return hashlib.sha256(
        json.dumps(body, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def _artifact(kind: str, subject_rev: str, payload: dict) -> dict:
    art = {"kind": kind, "produced_at": _now(),
           "subject_rev": subject_rev, "payload": payload}
    art["sha256"] = _artifact_digest(art)
    return art


def _write_artifact(path: str, art: dict) -> None:
    pathlib.Path(path).write_text(json.dumps(art, indent=1, ensure_ascii=False) + "\n")
    print(f"artifact: {path} sha256={art['sha256']}")


def _refuse(msg: str) -> int:
    print(f"REFUSE: {msg}", file=sys.stderr)
    return REFUSE


# ── attestation (R2 rev3) ────────────────────────────────────────────────────

def _cgroup_pids(unit: str) -> set[int]:
    cg = subprocess.run(["systemctl", "--user", "show", "-p", "ControlGroup", "--value", unit],
                        capture_output=True, text=True).stdout.strip()
    if not cg:
        raise RuntimeError(f"unit {unit!r} has no ControlGroup — not running?")
    return {int(p) for p in
            pathlib.Path("/sys/fs/cgroup" + cg + "/cgroup.procs").read_text().split()}


def _cmdline(pid: int) -> str:
    """The process's own argv. Used to recognise a NAMED process model; process
    ancestry is deliberately not consulted (see _classify_workers)."""
    try:
        return pathlib.Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\0", b" ").decode(
            "utf-8", "replace").strip()
    except (FileNotFoundError, ProcessLookupError, PermissionError):
        return ""


def _listening_inodes(port: int) -> set[int]:
    """Socket inodes in TCP_LISTEN on `port`, straight from /proc/net — evidence
    the serving process cannot author about itself."""
    inodes: set[int] = set()
    for path in ("/proc/net/tcp", "/proc/net/tcp6"):
        try:
            lines = pathlib.Path(path).read_text().splitlines()[1:]
        except (FileNotFoundError, PermissionError):
            continue
        for ln in lines:
            f = ln.split()
            if len(f) < 10 or f[3] != "0A":           # 0A = TCP_LISTEN
                continue
            try:
                if int(f[1].rsplit(":", 1)[1], 16) != port:
                    continue
                inodes.add(int(f[9]))
            except (ValueError, IndexError):
                continue
    return inodes


def _socket_inodes_of(pid: int) -> set[int]:
    out: set[int] = set()
    try:
        entries = list(pathlib.Path(f"/proc/{pid}/fd").iterdir())
    except (FileNotFoundError, ProcessLookupError, PermissionError):
        return out
    for e in entries:
        try:
            m = re.fullmatch(r"socket:\[(\d+)\]", os.readlink(e))
        except OSError:
            continue
        if m:
            out.add(int(m.group(1)))
    return out


#: The ONLY multi-holder process model this gate claims to understand. Both
#: patterns are matched against /proc/<pid>/cmdline, never against process
#: ancestry: rev1 excluded "every holder whose parent is outside the cgroup",
#: which silently assumes a root holder stops accepting once it has children.
#: That is false for SO_REUSEPORT and for mixed master/worker servers, and a
#: root that really does accept would then be dropped from the expected set —
#: an under-enumeration, i.e. precisely the coverage hole attestation exists to
#: close (primary-codex blocking finding, round 1).
_UVICORN_MASTER = re.compile(r"(^|/)uvicorn(\s|$)|uvicorn\.main|-m\s+uvicorn")
_SPAWN_WORKER = re.compile(r"multiprocessing\.spawn")


def _classify_workers(holders: set[int], cmdline_of: dict[int, str]) -> set[int]:
    """Which socket holders are expected to attest — or a refusal.

    Holding the listening socket is what makes a process able to answer the
    probe at all, and /proc says so independently of the probe path (R2 rev3).
    But holding is necessary, not sufficient: uvicorn's master inherits the
    socket and never accepts once it has workers. Measured on trainer
    2026-09-03 (kura-api, uvicorn --workers 4): five holders — the master and
    four `multiprocessing.spawn` workers — plus a non-holding
    `multiprocessing.resource_tracker` that the first rule wrongly expected to
    attest, which made the gate demand five attestations from four servers and
    refuse forever.

    Rather than infer "does not serve" from ancestry, recognise ONE named model
    and fail closed on everything else:

      * exactly one holder            -> that holder serves (single-process mode)
      * one uvicorn master + N spawn  -> the N workers serve
      * anything else                 -> refuse; an unrecognised topology is not
                                        something this gate may guess about

    Refusing is safe here: the caller turns it into exit 2, and the operator
    extends the model deliberately rather than discovering later that a serving
    process was never asked to attest.
    """
    if not holders:
        raise RuntimeError("no process holds the listening socket")
    if len(holders) == 1:
        return set(holders)
    masters = {p for p in holders if _UVICORN_MASTER.search(cmdline_of.get(p, ""))}
    workers = {p for p in holders if _SPAWN_WORKER.search(cmdline_of.get(p, ""))}
    if len(masters) == 1 and workers and (masters | workers) == holders:
        return workers
    unknown = holders - masters - workers
    raise RuntimeError(
        f"unrecognised holder topology on the attested port: "
        f"masters={sorted(masters)} spawn_workers={sorted(workers)} "
        f"unclassified={sorted(unknown)} — this gate only knows single-process "
        f"and 'one uvicorn master + N spawn workers'. It refuses rather than "
        f"guess which holders accept: guessing wrong DROPS a serving process "
        f"from the attestation set. Extend the model deliberately "
        f"(cmdlines: {[cmdline_of.get(p, '')[:60] for p in sorted(holders)]})")


def _serving_worker_pids(unit: str, port: int) -> set[int]:
    pids = _cgroup_pids(unit)
    listen = _listening_inodes(port)
    if not listen:
        raise RuntimeError(f"nothing is LISTENing on port {port} — cannot enumerate "
                           f"the serving processes independently of the probe")
    holders = {p for p in pids if _socket_inodes_of(p) & listen}
    if not holders:
        raise RuntimeError(f"no pid in unit {unit!r} holds the socket listening on "
                           f"port {port} — is the probe URL pointing at this unit?")
    return _classify_workers(holders, {p: _cmdline(p) for p in holders})


def cmd_attestation(a) -> int:
    port = a.port or _port_of(a.probe_url)
    expected = _serving_worker_pids(a.unit, port)
    want_digest = a.expect_config_digest
    observed: dict[int, dict] = {}
    deadline = time.monotonic() + a.timeout_s
    while time.monotonic() < deadline and set(observed) != expected:
        try:
            with urllib.request.urlopen(a.probe_url, timeout=5) as r:
                p = json.loads(r.read())
        except Exception as exc:
            print(f"probe error: {exc}", file=sys.stderr)
            time.sleep(0.2)
            continue
        observed[int(p["pid"])] = p
        time.sleep(0.05)
    if set(observed) != expected:
        return _refuse(f"attestation coverage incomplete: observed={sorted(observed)} "
                       f"expected={sorted(expected)} — a request-count threshold is not "
                       f"a coverage proof (kura#50 R2 rev3)")
    stray = {pid for pid, p in observed.items()
             if p["role"] != a.expect_role or (want_digest and p["config_digest"] != want_digest)}
    if stray:
        return _refuse(f"workers {sorted(stray)} attest a different role/config than "
                       f"expected role={a.expect_role}")
    re_enum = _serving_worker_pids(a.unit, port)
    if re_enum != expected:
        return _refuse(f"worker set churned during the proof ({sorted(expected)} -> "
                       f"{sorted(re_enum)}) — rerun")
    _write_artifact(a.out, _artifact("attestation", observed[next(iter(observed))]["config_digest"],
                                     {"unit": a.unit, "role": a.expect_role,
                                      "pids": sorted(expected),
                                      "listen_port": port,
                                      "attestations": list(observed.values())}))
    return 0


# ── census (R1 rev2) ─────────────────────────────────────────────────────────

def cmd_census(a) -> int:
    log = pathlib.Path(a.log_path)
    if not log.exists():
        return _refuse(f"census log {log} absent — coverage unprovable, census fails closed")
    nonce = hashlib.sha256(os.urandom(16)).hexdigest()[:16]
    # Nonce exclusion binds to THIS invocation only (impl r3 blocking): every
    # nonce is public the moment the instrument echoes it into its log, so a
    # prior run's nonce is replayable by any log reader and buys no exclusion.
    # The ledger's remaining job is to split "stale gate probe" from "forged
    # tag" in the refusal — still security-relevant, so it is created 0600 and
    # refused if foreign-owned, group/other-writable, or a symlink (injection).
    ledger = pathlib.Path(a.nonce_ledger)
    ledger.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(str(ledger),
                     os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    except OSError as exc:
        return _refuse(f"nonce ledger {ledger} unusable: {exc}")
    try:
        st = os.fstat(fd)
        if st.st_uid != os.getuid() or st.st_mode & 0o022:
            return _refuse(f"nonce ledger {ledger} is foreign-owned or writable by "
                           f"others (uid={st.st_uid} mode={oct(st.st_mode & 0o777)}) "
                           f"— an injectable ledger is not evidence")
        prior_nonces = set(ledger.read_text().split())
        os.write(fd, (nonce + "\n").encode())
    finally:
        os.close(fd)
    if True:
        # The synthetic probe carries a NONCE the instrument echoes into its log
        # line (probe=<nonce>); organic lines never carry one. Excluding by
        # post_id conflated organic serves of the probed post with the probe
        # itself and could report a false zero (impl r1 blocking).
        req = urllib.request.Request(
            f"{a.coverage_probe_url}/api/post/{a.coverage_post_id}/content",
            headers={"X-Kura-Census-Probe": nonce})
        try:
            urllib.request.urlopen(req, timeout=10).read()
        except Exception as exc:
            return _refuse(f"coverage probe failed: {exc}")
        time.sleep(a.log_settle_s)
        if f"probe={nonce}" not in log.read_text():
            return _refuse("synthetic probe nonce did not appear in the census log — "
                           "the instrument is not observing the serving path")
    window_start = (datetime.datetime.now(datetime.timezone.utc)
                    - datetime.timedelta(days=a.window_days))

    def _ts(line: str):
        m = re.search(r"\bts=(\S+)", line)
        if not m:
            return None
        try:
            return datetime.datetime.fromisoformat(m.group(1).replace("Z", "+00:00"))
        except ValueError:
            return None

    # The instrument must PREDATE the window it certifies: the earliest
    # observe-start line's own timestamp must be <= window_start. (Impl r1:
    # a computed-but-unused window certified whatever the log happened to
    # hold.) Restart gaps remain a stated residual.
    starts = [_ts(l) for l in log.read_text().splitlines()
              if "media-replica-observe-start" in l]
    starts = [s for s in starts if s is not None]
    if not starts or min(starts) > window_start:
        return _refuse(f"instrument does not provably cover the window: earliest "
                       f"observe-start {min(starts).isoformat() if starts else 'absent'} "
                       f"> window start {window_start.isoformat()}")
    organic = 0
    foreign_tags = 0
    stale_tags = 0
    current_seen = 0
    for line in log.read_text().splitlines():
        if "media-replica-serve " not in line:
            continue
        lt = _ts(line)
        if lt is not None and lt < window_start:
            continue
        m = re.search(r" probe=(\S+)", line)
        if m is None:
            organic += 1
        elif m.group(1) == nonce:
            # Exactly one line may carry the fresh nonce: the probe issued
            # above. A second sighting is a same-run replay of a nonce learned
            # from the live log — an organic serve hiding behind our tag.
            current_seen += 1
            if current_seen > 1:
                organic += 1
                foreign_tags += 1
        elif m.group(1) in prior_nonces:
            organic += 1
            stale_tags += 1
        else:
            organic += 1
            foreign_tags += 1
    if organic > 0:
        notes = []
        if foreign_tags:
            notes.append(f"{foreign_tags} carried a probe tag outside this run's "
                         f"single legitimate probe — possible census-evasion forgery")
        if stale_tags:
            notes.append(f"{stale_tags} carried a PRIOR gate nonce — replayable, "
                         f"so not excludable; rotate the census log or let the "
                         f"window pass before re-running")
        extra = f" ({'; '.join(notes)})" if notes else ""
        return _refuse(f"{organic} organic local media serves observed in the log — "
                       f"a consumer exists; enumerate and migrate it before excluding media{extra}")
    _write_artifact(a.out, _artifact("census", f"window={a.window_days}d",
                                     {"log": str(log), "window_days": a.window_days,
                                      "window_start_min": window_start.isoformat(),
                                      "organic_serves": 0, "coverage_proven": True}))
    return 0


# ── topology / replication-health / free-space / null-invariant ──────────────

#: The gate interpolates the table name into SQL. Rather than build a
#: quoting layer for a value that is always `post` here, restrict the accepted
#: form and refuse everything else: schema-qualified, mixed-case and quoted
#: identifiers all change meaning when pasted unquoted, so the gate must not
#: pretend it understands them (primary-codex non-blocking finding, round 3).
_PLAIN_IDENT = re.compile(r"^[a-z_][a-z0-9_]*$")


def _checked_table(name: str) -> str:
    if not _PLAIN_IDENT.match(name):
        raise RuntimeError(
            f"--table {name!r} is not a plain lower-case identifier. This gate "
            f"interpolates the name into SQL and does not quote it, so it refuses "
            f"schema-qualified, mixed-case, reserved or quoted names rather than "
            f"guess what they mean")
    return name


def _psql(dsn: str, q: str) -> str:
    r = subprocess.run(["psql", dsn, "-Atc", q], capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"psql failed: {r.stderr.strip()}")
    return r.stdout.strip()


# ── topology: two node-local halves, not one cross-node call ─────────────────
#
# The combined check wanted a publisher DSN and a subscriber DSN in one process.
# In this fleet that is unreachable by design: primary's pg_hba admits trainer for
# REPLICATION only, so an app connection from the subscriber to the publisher is
# refused (`pg_hba.conf にホスト"127.0.0.1"…のエントリがありません`,
# measured 2026-09-03). Opening pg_hba to satisfy a checker would trade a real
# boundary for a convenience. Each half now runs where its facts live, and
# `verify-artifacts` requires both — the join happens over artifacts, which is
# where every other cross-node fact in this plan already meets.


def cmd_topology_publisher(a) -> int:
    table = _checked_table(a.table)
    pubs = _psql(a.dsn, f"SELECT pubname FROM pg_publication_tables "
                        f"WHERE tablename='{table}'")
    publ = [p for p in pubs.splitlines() if p]
    if len(publ) != 1:
        return _refuse(f"{table} is published by {publ!r} — exactly one publication "
                       f"required (multi-publication column lists are a subscriber-error "
                       f"path, PG18)")
    cols = _psql(a.dsn, f"SELECT coalesce(string_agg(a, ',' ORDER BY a), '') FROM "
                        f"(SELECT unnest(attnames) a FROM pg_publication_tables "
                        f" WHERE tablename='{table}') s")
    _write_artifact(a.out, _artifact("topology-publisher", publ[0],
                                     {"table": table, "publications_for_table": publ,
                                      "columns_digest": hashlib.sha256(cols.encode()).hexdigest()}))
    return 0


def cmd_topology_subscriber(a) -> int:
    total = _psql(a.dsn, "SELECT count(*) FROM pg_subscription_rel")
    bad = _psql(a.dsn, "SELECT count(*) FROM pg_subscription_rel WHERE srsubstate <> 'r'")
    if total == "0":
        return _refuse("this node has no subscription relations — it is not the "
                       "subscriber the artifact would claim it is")
    if bad != "0":
        return _refuse(f"{bad} subscription relations not in state 'r'")
    _write_artifact(a.out, _artifact("topology-subscriber", f"rels={total}",
                                     {"non_ready_rels": 0, "total_rels": int(total)}))
    return 0


# ── column list vs replica identity (the toy rehearsal's finding) ────────────

def _replica_identity(dsn: str, table: str) -> tuple[str, list[str]]:
    """(relreplident, the columns that identity consists of)."""
    kind = _psql(dsn, f"SELECT relreplident FROM pg_class WHERE oid='{table}'::regclass")
    if kind == "d":
        cols = [c for c in _psql(dsn, f"""
            SELECT a.attname FROM pg_index i JOIN pg_attribute a
              ON a.attrelid = i.indrelid AND a.attnum = ANY(i.indkey)
            WHERE i.indrelid = '{table}'::regclass AND i.indisprimary
            ORDER BY a.attname""").splitlines() if c]
        if not cols:
            raise RuntimeError(f"{table} has relreplident='d' but no primary key — "
                               f"UPDATE/DELETE cannot be replicated at all")
        return kind, cols
    if kind == "i":
        cols = [c for c in _psql(dsn, f"""
            SELECT a.attname FROM pg_index i JOIN pg_attribute a
              ON a.attrelid = i.indrelid AND a.attnum = ANY(i.indkey)
            WHERE i.indrelid = '{table}'::regclass AND i.indisreplident
            ORDER BY a.attname""").splitlines() if c]
        if not cols:
            raise RuntimeError(f"{table} has relreplident='i' but no index is marked "
                               f"REPLICA IDENTITY USING INDEX")
        return kind, cols
    if kind == "f":
        return kind, _all_columns(dsn, table)
    raise RuntimeError(f"{table} has relreplident={kind!r} — UPDATE/DELETE are not "
                       f"replicated, so a column list cannot be reasoned about")


def _all_columns(dsn: str, table: str) -> list[str]:
    return [c for c in _psql(dsn, f"SELECT attname FROM pg_attribute WHERE attrelid="
                                  f"'{table}'::regclass AND attnum > 0 AND NOT attisdropped "
                                  f"ORDER BY attname").splitlines() if c]


def _column_list_verdict(identity_kind: str, identity_cols: list[str],
                         published: list[str], exclude: list[str],
                         all_cols: list[str]) -> list[str]:
    """The proposed published column list, or a refusal explaining why not.

    Measured 2026-09-03 on PG 18.6 (toy rehearsal): `ALTER PUBLICATION … ADD
    TABLE post (<list without the identity>)` **succeeds**. Nothing refuses at
    the moment of the change; the damage appears at the next publisher UPDATE as
    `ERROR: cannot update table "post"` — i.e. every write to the table breaks,
    on the AUTHORITY, after the operator has already been told the change worked.
    Restoring an identity-bearing list unblocks it again, so the hazard is
    recoverable — but only if someone connects the outage to the publication.

    That is why this check exists before the transition and not as a warning
    after it.
    """
    have = published or list(all_cols)      # an empty attnames list means "all columns"
    unknown = [c for c in exclude if c not in have]
    if unknown:
        raise RuntimeError(f"cannot exclude {unknown} — not currently published "
                           f"(published: {sorted(have)}). A typo here silently "
                           f"produces a different list than intended")
    proposed = [c for c in have if c not in exclude]
    if identity_kind == "f":
        if exclude:
            raise RuntimeError("REPLICA IDENTITY FULL: every column is part of the "
                               "identity, so no column may be excluded from the "
                               "publication")
        return proposed
    missing = [c for c in identity_cols if c not in proposed]
    if missing:
        raise RuntimeError(
            f"the proposed column list omits the replica identity {missing} — PG18 "
            f"ACCEPTS this DDL and only fails at the next publisher UPDATE with "
            f"'cannot update table', breaking every write on the authority "
            f"(measured, kura#50 toy rehearsal). Refusing before the change")
    return proposed


def cmd_column_list(a) -> int:
    table = _checked_table(a.table)
    exclude = [c.strip() for c in a.exclude.split(",") if c.strip()]
    pubs = [p for p in _psql(a.dsn, f"SELECT pubname FROM pg_publication_tables "
                                    f"WHERE tablename='{table}'").splitlines() if p]
    if len(pubs) != 1:
        return _refuse(f"{table} is published by {pubs!r} — exactly one publication "
                       f"required before its column list can be reasoned about")
    published = [c for c in _psql(a.dsn, f"SELECT unnest(attnames) FROM "
                                         f"pg_publication_tables WHERE tablename="
                                         f"'{table}'").splitlines() if c]
    kind, identity = _replica_identity(a.dsn, table)
    proposed = _column_list_verdict(kind, identity, published, exclude,
                                    _all_columns(a.dsn, table))
    _write_artifact(a.out, _artifact("column-list", pubs[0], {
        "table": table, "identity_kind": kind, "identity_columns": identity,
        "published_columns": sorted(published), "proposed_columns": sorted(proposed),
        "excluded": sorted(exclude), "identity_satisfied": True}))
    return 0


def cmd_replication_health(a) -> int:
    rows = _psql(a.dsn, "SELECT subname||'|'||subenabled FROM pg_subscription")
    disabled = [r for r in rows.splitlines() if r.endswith("|f")]
    if disabled:
        return _refuse(f"disabled subscriptions: {disabled}")
    workers = _psql(a.dsn, "SELECT count(*) FROM pg_stat_subscription WHERE pid IS NOT NULL")
    if int(workers) < 1:
        return _refuse("no live subscription worker")
    _write_artifact(a.out, _artifact("replication-health", "live",
                                     {"subscriptions": rows.splitlines(), "workers": int(workers)}))
    return 0


def cmd_free_space(a) -> int:
    st = os.statvfs(a.path)
    free_gb = st.f_bavail * st.f_frsize / 1e9
    if free_gb < a.need_gb:
        return _refuse(f"free space {free_gb:.0f}GB < required {a.need_gb}GB at {a.path}")
    _write_artifact(a.out, _artifact("free-space", a.path,
                                     {"free_gb": round(free_gb), "need_gb": a.need_gb}))
    return 0


def cmd_null_invariant(a) -> int:
    total = _psql(a.dsn, "SELECT coalesce(sum(octet_length(data)),0)+"
                         "coalesce(sum(octet_length(thumbnail)),0) FROM post")
    cols = _psql(a.dsn if a.publisher_dsn is None else a.publisher_dsn,
                 "SELECT coalesce(string_agg(attnames::text, ';' ORDER BY pubname),'') "
                 "FROM pg_publication_tables WHERE tablename='post'")
    col_digest = hashlib.sha256(cols.encode()).hexdigest()
    if a.expect_columns_digest and col_digest != a.expect_columns_digest:
        return _refuse(f"publication column list drifted (digest {col_digest[:16]}…) — "
                       f"a byte-resurrection path may have opened")
    if total != "0":
        return _refuse(f"replica still holds {total} bytes of media — invariant violated")
    _write_artifact(a.out, _artifact("null-invariant", col_digest,
                                     {"media_bytes": 0, "columns_digest": col_digest}))
    return 0


# ── verify-artifacts — the executor's per-batch entry point ──────────────────

#: Per-kind payload keys an artifact MUST carry — without this any fresh
#: self-consistent JSON satisfied the gate (impl r1 blocking). user-approval is
#: hand-written (kura#49: explicit PRE-approval), so its schema is the contract
#: that a human actually named the scope.
#: bool subclasses int — isinstance let True/False pass as counts, pids and
#: gigabytes, so a wrongly typed artifact stayed self-consistent (impl r3
#: blocking, both lanes). `type(v) is ...` closes that boundary.
def _nonempty_str(v): return isinstance(v, str) and v.strip() != ""
def _zero_int(v): return type(v) is int and v == 0
def _pos_int(v): return type(v) is int and v >= 1
def _true(v): return v is True
def _int_list(v): return isinstance(v, list) and len(v) > 0 and all(type(x) is int for x in v)
def _any_list(v): return isinstance(v, list) and len(v) > 0
def _num(v): return type(v) in (int, float) and math.isfinite(v)

#: Field -> value validator. Key PRESENCE alone let a hand-hashed payload of
#: nulls pass — in particular a user-approval with approved_by/scope/statement
#: all null "established" approval no human gave (impl r2 blocking, both lanes).
ARTIFACT_SCHEMAS = {
    "census": {"organic_serves": _zero_int, "window_days": _pos_int,
               "coverage_proven": _true},
    "attestation": {"pids": _int_list, "role": _nonempty_str, "unit": _nonempty_str},
    "topology-publisher": {"table": _nonempty_str, "publications_for_table": _any_list,
                           "columns_digest": _nonempty_str},
    "topology-subscriber": {"non_ready_rels": _zero_int, "total_rels": _pos_int},
    "column-list": {"table": _nonempty_str, "identity_columns": _any_list,
                    "proposed_columns": _any_list, "identity_satisfied": _true},
    "replication-health": {"subscriptions": _any_list, "workers": _pos_int},
    "free-space": {"free_gb": _num, "need_gb": _num},
    "null-invariant": {"media_bytes": _zero_int, "columns_digest": _nonempty_str},
    "rehydration": {"verified_post_id": _pos_int, "method": _nonempty_str},
    "user-approval": {"approved_by": _nonempty_str, "scope": _nonempty_str,
                      "statement": _nonempty_str},
}


#: ONE declaration per check that is split across nodes: name -> its halves.
#: A check split across nodes must not be satisfiable by half of itself, and
#: "remember to require both" is not a mechanism — it puts the completeness in
#: every caller's argument list, where a new call site forgets it silently
#: (primary-codex blocking finding, round 3).
SPLIT_CHECKS = {
    # topology has to run on two nodes: this fleet's pg_hba admits the subscriber
    # for REPLICATION only, so no single process can see both sides.
    "topology": ("topology-publisher", "topology-subscriber"),
}

#: DERIVED, never hand-written: a hand-maintained pair table can be asymmetric
#: (satisfiable from one side) or half-filled, and the next split check simply
#: does not get added (primary-codex follow-up, round 3). Deriving makes both
#: mistakes unrepresentable.
ARTIFACT_COREQUISITES = {kind: set(halves) - {kind}
                         for halves in SPLIT_CHECKS.values() for kind in halves}

#: Suffixes that name the SIDE of a split check. A kind ending in one of these
#: is a half by construction, so it must belong to a declared group — that is
#: what turns "remember to register it" into a refusal.
SPLIT_SIDE_SUFFIXES = ("-publisher", "-subscriber")


def _validate_split_registration() -> None:
    """Run at import: an unregistered half must not reach a caller.

    A test would only catch this in CI; refusing at import means a gate binary
    that could hand out half-proofs cannot start at all — the same fail-closed
    shape api/media_role.py uses for a malformed role.
    """
    registered = set(ARTIFACT_COREQUISITES)
    for kind in ARTIFACT_SCHEMAS:
        if kind.endswith(SPLIT_SIDE_SUFFIXES) and kind not in registered:
            raise RuntimeError(
                f"artifact kind {kind!r} is named as one side of a split check but is "
                f"not in SPLIT_CHECKS — it would be verifiable on its own, which is "
                f"the fail-open this table exists to prevent. Add it to its group")
    owner: dict[str, str] = {}
    for name, halves in SPLIT_CHECKS.items():
        # DISTINCT halves: ("x-publisher", "x-publisher") has len 2 but derives an
        # EMPTY co-requisite set (`set(halves) - {kind}`), so the half would verify
        # alone — the very fail-open this table prevents (primary-codex, round 4).
        if len(set(halves)) < 2:
            raise RuntimeError(f"split check {name!r} declares {len(set(halves))} "
                               f"distinct half/halves out of {len(halves)} entries "
                               f"({list(halves)}) — a repeated half is not a split, "
                               f"and it derives an empty requirement")
        unknown = [h for h in halves if h not in ARTIFACT_SCHEMAS]
        if unknown:
            raise RuntimeError(f"split check {name!r} names halves with no artifact "
                               f"schema: {unknown}")
        for half in set(halves):
            # The derivation is a dict comprehension: a half in two groups is
            # OVERWRITTEN by the later one and quietly loses a requirement.
            if half in owner:
                raise RuntimeError(
                    f"artifact kind {half!r} is registered in both {owner[half]!r} and "
                    f"{name!r}; the derived table keeps only the last group, so one of "
                    f"its co-requisites would silently disappear. A half belongs to "
                    f"exactly one split")
            owner[half] = name


_validate_split_registration()


def cmd_verify_artifacts(a) -> int:
    now = datetime.datetime.now(datetime.timezone.utc)
    seen_kinds: set[str] = set()
    for spec in a.require:
        if "=" not in spec:
            return _refuse(f"--require takes kind=path pairs, got {spec!r} — an "
                           f"unnamed artifact cannot be held to a schema")
        kind, path = spec.split("=", 1)
        if kind not in ARTIFACT_SCHEMAS:
            return _refuse(f"unknown artifact kind {kind!r} (have: {sorted(ARTIFACT_SCHEMAS)})")
        if kind in seen_kinds:
            return _refuse(f"duplicate artifact kind {kind!r} in requirements")
        seen_kinds.add(kind)
        p = pathlib.Path(path)
        if not p.exists():
            return _refuse(f"required artifact {path} absent")
        art = json.loads(p.read_text())
        if art.get("kind") != kind:
            return _refuse(f"artifact {path} declares kind {art.get('kind')!r}, "
                           f"required as {kind!r} — substitution refused")
        if _artifact_digest(art) != art.get("sha256"):
            return _refuse(f"artifact {path} digest mismatch — tampered or corrupt "
                           f"(the digest covers kind/produced_at/subject_rev/payload)")
        payload = art.get("payload", {})
        missing = set(ARTIFACT_SCHEMAS[kind]) - set(payload)
        if missing:
            return _refuse(f"artifact {path} payload lacks required keys {sorted(missing)} "
                           f"for kind {kind!r}")
        bad = [f for f, ok in ARTIFACT_SCHEMAS[kind].items() if not ok(payload[f])]
        if bad:
            return _refuse(f"artifact {path} payload fields {sorted(bad)} fail their "
                           f"value contract for kind {kind!r} — presence is not proof")
        if kind == "user-approval" and a.expect_approval_scope \
                and payload.get("scope") != a.expect_approval_scope:
            return _refuse(f"user-approval scope {payload.get('scope')!r} != the "
                           f"operation being gated {a.expect_approval_scope!r} — "
                           f"an approval binds to a named scope, not to the air")
        if a.expect_subject_rev and art.get("subject_rev") != a.expect_subject_rev                 and kind in ("attestation", "null-invariant"):
            return _refuse(f"artifact {path} subject_rev {art.get('subject_rev')!r} != "
                           f"expected {a.expect_subject_rev!r}")
        age = now - datetime.datetime.fromisoformat(art["produced_at"].replace("Z", "+00:00"))
        if age > datetime.timedelta(days=a.max_age_days):
            return _refuse(f"artifact {path} is {age} old (max {a.max_age_days}d) — refresh it")
    for kind in sorted(seen_kinds):
        missing = ARTIFACT_COREQUISITES.get(kind, set()) - seen_kinds
        if missing:
            return _refuse(
                f"artifact kind {kind!r} was verified without its co-requisite(s) "
                f"{sorted(missing)} — these halves are one check run on two nodes, "
                f"and half a topology proof is not a topology proof. Produce the "
                f"missing half where its facts live and require it here too")
    print(f"verify-artifacts: {len(a.require)} artifact(s) present, kinds+schemas "
          f"enforced, co-requisites satisfied, untampered, fresh")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("attestation")
    p.add_argument("--unit", default="kura-api")
    p.add_argument("--probe-url", required=True)
    p.add_argument("--port", type=int, default=None,
                   help="listening port whose socket holders are the expected "
                        "attesters (default: the probe URL's port)")
    p.add_argument("--expect-role", required=True)
    p.add_argument("--expect-config-digest", default="")
    p.add_argument("--timeout-s", type=float, default=30.0)
    p.add_argument("--out", required=True)
    p.set_defaults(fn=cmd_attestation)

    p = sub.add_parser("census")
    p.add_argument("--log-path", required=True)
    p.add_argument("--window-days", type=int, default=7)
    p.add_argument("--coverage-probe-url", required=True,
                   help="required: a census without a coverage proof certified "
                        "an unobserved instrument (impl r2 blocking)")
    p.add_argument("--coverage-post-id", type=int, required=True)
    p.add_argument("--nonce-ledger", required=True,
                   help="append-only 0600 file of gate-minted nonces; only THIS "
                        "run's fresh nonce excludes (once) — prior or unknown "
                        "tags count as organic + are flagged")
    p.add_argument("--log-settle-s", type=float, default=2.0)
    p.add_argument("--out", required=True)
    p.set_defaults(fn=cmd_census)

    p = sub.add_parser("topology-publisher", help="run on the PUBLISHER")
    p.add_argument("--dsn", required=True)
    p.add_argument("--table", default="post")
    p.add_argument("--out", required=True)
    p.set_defaults(fn=cmd_topology_publisher)

    p = sub.add_parser("topology-subscriber", help="run on the SUBSCRIBER")
    p.add_argument("--dsn", required=True)
    p.add_argument("--out", required=True)
    p.set_defaults(fn=cmd_topology_subscriber)

    p = sub.add_parser("column-list", help="run on the PUBLISHER, BEFORE the transition")
    p.add_argument("--dsn", required=True)
    p.add_argument("--table", default="post")
    p.add_argument("--exclude", default="",
                   help="comma-separated columns the transition intends to drop; the "
                        "identity check then runs against the PROPOSED list (pre-flight)")
    p.add_argument("--out", required=True)
    p.set_defaults(fn=cmd_column_list)

    p = sub.add_parser("replication-health")
    p.add_argument("--dsn", required=True)
    p.add_argument("--out", required=True)
    p.set_defaults(fn=cmd_replication_health)

    p = sub.add_parser("free-space")
    p.add_argument("--path", required=True)
    p.add_argument("--need-gb", type=float, required=True)
    p.add_argument("--out", required=True)
    p.set_defaults(fn=cmd_free_space)

    p = sub.add_parser("null-invariant")
    p.add_argument("--dsn", required=True)
    p.add_argument("--publisher-dsn", default=None)
    p.add_argument("--expect-columns-digest", default="")
    p.add_argument("--out", required=True)
    p.set_defaults(fn=cmd_null_invariant)

    p = sub.add_parser("verify-artifacts")
    p.add_argument("--require", nargs="+", required=True,
                   help="kind=path pairs; kinds are enforced against ARTIFACT_SCHEMAS")
    p.add_argument("--max-age-days", type=float, default=7.0)
    p.add_argument("--expect-subject-rev", default="")
    p.add_argument("--expect-approval-scope", default="")
    p.set_defaults(fn=cmd_verify_artifacts)

    a = ap.parse_args()
    try:
        return a.fn(a)
    except RuntimeError as exc:
        return _refuse(str(exc))


if __name__ == "__main__":
    sys.exit(main())
