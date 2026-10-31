#!/usr/bin/env python3
"""kura#51 Phase A — did everything come back after the PostgreSQL restart?

Plan rev4 A''' requires the reconnection sequence to be a GATE, not a checklist:
enabling pgaudit needs `shared_preload_libraries`, a POSTMASTER-context setting,
so the cluster must be restarted. Measured on primary 2026-09-03, that cluster is
not kura's alone — it carries `broker` (39 connections) with the four mesh
slots the fleet publishes through, plus broker_spine, apps, review_* and the
grill* databases. A restart therefore interrupts the broker mesh's primary node, and
`postgresql@18-main` is `Restart=no`, so nothing brings it back on its own.

Usage (both on the node being restarted):

    baseline --dsn <dsn> --out before.json      # BEFORE the restart
    verify   --baseline before.json --out artifact.json   # AFTER

`baseline` requires an explicit `--expect-slots` / `--expect-subscriptions`
inventory and refuses on any mismatch; the artifact pins it so `verify` cannot
later be pointed at a thinner node. Without that contract a wrong DSN measures
less and still passes.

`verify` also checks that the restart DID WHAT IT WAS FOR: `--expect-setting
shared_preload_libraries=pgaudit` refuses when the setting did not take effect,
and refuses when it cannot be read at all. Otherwise the gate would certify a
restart that changed nothing and the census window would open on an instrument
that never loaded.

`verify` compares like for like and REFUSES (exit 2) on any regression:
every slot that was active is active again, every enabled subscription has a
live worker, every database that answered `SELECT 1` answers again, and every
declared HTTP surface returns the same build commit. Absent evidence is a
refusal, never a pass: a slot that vanished is not "not regressed".

Only the local node is measured, and that is deliberate — a remote subscriber
reattaching shows up HERE as its slot going active again, so the local view
needs no cross-node credentials (which this fleet's pg_hba does not grant
anyway). Its limit is stated rather than hidden: an active slot proves the
consumer RECONNECTED, not that its apply worker is healthy afterwards, and a
refusal means something is not as the baseline recorded it — not that the
restart is proven to be the cause.
"""
from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import pathlib
import subprocess
import sys
import urllib.request

REFUSE = 2


def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _refuse(msg: str) -> int:
    print(f"REFUSE: {msg}", file=sys.stderr)
    return REFUSE


def _dsn_for_db(dsn: str, db: str) -> str:
    """Point a libpq URI at another database.

    `psql "<uri>" -d <db>` does NOT do this: psql then reads the URI as a
    USERNAME and the connection fails with a peer-auth error, so every
    per-database probe silently returned "unreachable" (measured, first live
    run). The dbname belongs in the URI.
    """
    head, _, _tail = dsn.partition("?")
    query = dsn[len(head):]
    scheme, sep, rest = head.partition("://")
    if not sep:
        return dsn                      # keyword/value DSN: leave it alone
    authority, slash, _old_db = rest.partition("/")
    if not slash:
        return f"{scheme}://{authority}/{db}{query}"
    return f"{scheme}://{authority}/{db}{query}"


def _psql(dsn: str, q: str, db: str | None = None) -> tuple[int, str]:
    target = _dsn_for_db(dsn, db) if db else dsn
    r = subprocess.run(["psql", target, "-Atc", q], capture_output=True, text=True)
    return r.returncode, (r.stdout if r.returncode == 0 else r.stderr).strip()


def _rows(dsn: str, q: str) -> list[str]:
    rc, out = _psql(dsn, q)
    if rc != 0:
        raise RuntimeError(f"psql failed: {out}")
    return [ln for ln in out.splitlines() if ln]


def _measure(dsn: str, urls: list[str]) -> dict:
    dbs = _rows(dsn, "SELECT datname FROM pg_database WHERE NOT datistemplate ORDER BY datname")
    reachable = {}
    for db in dbs:
        rc, _ = _psql(dsn, "SELECT 1", db=db)
        reachable[db] = (rc == 0)
    slots = {}
    # active::int, not active::text: a boolean renders as "true"/"false" and the
    # first version compared against "t", reading every live slot as inactive.
    for row in _rows(dsn, "SELECT slot_name||'|'||active::int::text||'|'||"
                          "coalesce(database,'-') "
                          "FROM pg_replication_slots ORDER BY slot_name"):
        name, active, database = row.split("|")
        slots[name] = {"active": active == "1", "database": database}
    subs = {}
    for row in _rows(dsn, "SELECT s.subname||'|'||s.subenabled::int::text||'|'||"
                          "(SELECT count(*) FROM pg_stat_subscription st "
                          " WHERE st.subid = s.oid AND st.pid IS NOT NULL)::text "
                          "FROM pg_subscription s ORDER BY s.subname"):
        name, enabled, workers = row.split("|")
        subs[name] = {"enabled": enabled == "1", "workers": int(workers)}
    builds = {}
    for url in urls:
        try:
            with urllib.request.urlopen(url, timeout=10) as r:
                builds[url] = json.loads(r.read()).get("commit")
        except Exception as exc:  # noqa: BLE001
            builds[url] = f"unreachable: {type(exc).__name__}"
    return {"databases_reachable": reachable, "slots": slots,
            "subscriptions": subs, "builds": builds}


def _digest(doc: dict) -> str:
    body = {k: v for k, v in doc.items() if k != "sha256"}
    return hashlib.sha256(json.dumps(body, sort_keys=True,
                                     ensure_ascii=False).encode()).hexdigest()


def _parse_inventory(spec: str) -> set[str]:
    """`a,b,c` -> {a,b,c}; the literal `none` -> the empty set.

    `none` has to be spelled out: an empty string would let "I forgot" look like
    "I checked and there are none" (trainer-codex, kura#51 round 1).
    """
    spec = spec.strip()
    if spec == "none":
        return set()
    names = {part.strip() for part in spec.split(",") if part.strip()}
    if not names:
        raise RuntimeError("inventory is empty; write 'none' if that is what you mean")
    return names


def _inventory_problems(p: dict, want_slots: set[str], want_subs: set[str]) -> list[str]:
    """The declared inventory IS the coverage contract.

    Without it, a wrong DSN or a drifted topology produces a baseline that
    observes less and still verifies green — coverage silently shrinks. So the
    caller states what this node is supposed to carry, the baseline refuses on
    any mismatch, and the artifact pins it so `verify` cannot be pointed
    somewhere thinner afterwards.
    """
    bad = []
    have_slots, have_subs = set(p["slots"]), set(p["subscriptions"])
    for label, have, want in (("replication slot", have_slots, want_slots),
                              ("subscription", have_subs, want_subs)):
        missing, extra = sorted(want - have), sorted(have - want)
        if missing:
            bad.append(f"declared {label}(s) not present: {missing} — wrong DSN, wrong "
                       f"node, or the topology drifted")
        if extra:
            bad.append(f"undeclared {label}(s) present: {extra} — the inventory is out "
                       f"of date, and an unlisted one is one nobody promised to check")
    return bad


def _vacuity_problems(p: dict) -> list[str]:
    """Evidence that is absent is not evidence that nothing regressed.

    The first live run recorded 0 reachable databases and 0 active slots — both
    measurement bugs — and then verified green against that emptiness. A
    baseline that observed nothing can only certify nothing, so it is refused
    at both ends.
    """
    bad = []
    if not any(p["databases_reachable"].values()):
        bad.append("no database answered SELECT 1 — the probe, not the cluster, is broken")
    if p["slots"] and not any(s["active"] for s in p["slots"].values()):  # noqa: E501
        bad.append(f"{len(p['slots'])} replication slot(s) exist and NONE is active — "
                   f"either the mesh is already down or the measurement is wrong; "
                   f"do not restart on top of this")
    if p["subscriptions"] and not any(s["workers"] for s in p["subscriptions"].values()):
        bad.append("subscriptions exist but none has a live worker")
    live = [u for u, v in p["builds"].items()
            if not (isinstance(v, str) and v.startswith("unreachable"))]
    if p["builds"] and not live:
        bad.append("no declared HTTP surface answered")
    return bad


def cmd_baseline(a) -> int:
    want_slots = _parse_inventory(a.expect_slots)
    want_subs = _parse_inventory(a.expect_subscriptions)
    payload = _measure(a.dsn, a.url)
    payload["declared_slots"] = sorted(want_slots)
    payload["declared_subscriptions"] = sorted(want_subs)
    doc = {"kind": "pg-restart-baseline", "produced_at": _now(),
           "subject_rev": "kura#51 Phase A", "payload": payload}
    problems = _inventory_problems(payload, want_slots, want_subs) + \
        _vacuity_problems(payload)
    if problems:
        for v in problems:
            print(f"  ✗ {v}", file=sys.stderr)
        return _refuse("this baseline does not match the declared inventory, so it "
                       "cannot certify a recovery of it")
    doc["sha256"] = _digest(doc)
    pathlib.Path(a.out).write_text(json.dumps(doc, indent=1, ensure_ascii=False) + "\n")
    p = doc["payload"]
    print(f"baseline: {sum(p['databases_reachable'].values())}/{len(p['databases_reachable'])} "
          f"databases reachable, {sum(s['active'] for s in p['slots'].values())}/"
          f"{len(p['slots'])} slots active, {len(p['subscriptions'])} subscriptions, "
          f"{len(p['builds'])} http surface(s)")
    print(f"artifact: {a.out} sha256={doc['sha256']}")
    return 0


def _check_expected_settings(dsn: str, expectations: list[str]) -> list[str]:
    """`name=value` pairs the restart was supposed to bring into effect.

    Reading a GUC can itself be refused: `SHOW shared_preload_libraries` needs
    `pg_read_all_settings`, and the kura role does not have it (measured on primary
    2026-09-03 — "pg_read_all_settingsロールの権限を持つロールのみが…"). An
    unreadable expectation is a REFUSAL, not a skip: the whole point of the
    restart is unverified otherwise.
    """
    problems = []
    for pair in expectations:
        if "=" not in pair:
            problems.append(f"--expect-setting takes name=value, got {pair!r}")
            continue
        name, want = pair.split("=", 1)
        rc, got = _psql(dsn, f"SELECT current_setting('{name}')")
        if rc != 0:
            problems.append(f"cannot read setting {name!r}: {got.splitlines()[0] if got else '?'} "
                            f"— grant pg_read_all_settings to this role or pass a "
                            f"--settings-dsn that can read it; an unverifiable "
                            f"expectation is not a satisfied one")
        else:
            # GUC lists read back with spaces after the commas
            # ("pgaudit, pg_stat_statements"), so an untrimmed comparison refuses a
            # setting that IS in effect (primary-codex, kura#51 round 1). A gate that
            # cries wolf gets bypassed, so normalise both sides.
            tokens = {t.strip() for t in got.split(",")}
            if want.strip() not in tokens:
                problems.append(f"setting {name} is {got!r} (tokens {sorted(tokens)}); "
                                f"the restart was supposed to make it include "
                                f"{want.strip()!r} — it did not take effect")
    return problems


def cmd_verify(a) -> int:
    base = json.loads(pathlib.Path(a.baseline).read_text())
    if _digest(base) != base.get("sha256"):
        return _refuse(f"baseline {a.baseline} digest mismatch — tampered or corrupt")
    if base.get("kind") != "pg-restart-baseline":
        return _refuse(f"{a.baseline} is not a pg-restart-baseline")
    before = base["payload"]
    if "declared_slots" not in before or "declared_subscriptions" not in before:
        return _refuse(f"baseline {a.baseline} carries no declared inventory — it was "
                       f"produced before the inventory contract existed. Retake it")
    vac = _vacuity_problems(before)
    if vac:
        for v in vac:
            print(f"  ✗ {v}", file=sys.stderr)
        return _refuse(f"baseline {a.baseline} observed nothing — it cannot certify "
                       f"a recovery. Retake it before the restart")
    urls = list(before["builds"])
    after = _measure(a.dsn, urls)

    problems: list[str] = []
    for db, was in before["databases_reachable"].items():
        now = after["databases_reachable"].get(db)
        if now is None:
            problems.append(f"database {db} disappeared")
        elif was and not now:
            problems.append(f"database {db} answered SELECT 1 before and does not now")

    for name, was in before["slots"].items():
        now = after["slots"].get(name)
        if now is None:
            problems.append(f"replication slot {name} is gone (db={was['database']}) — "
                            f"a missing slot is not a reconnected one")
        elif was["active"] and not now["active"]:
            problems.append(f"replication slot {name} (db={was['database']}) was active "
                            f"before the restart and its consumer has not reattached")

    for name, was in before["subscriptions"].items():
        now = after["subscriptions"].get(name)
        if now is None:
            problems.append(f"subscription {name} is gone")
        elif was["enabled"] and not now["enabled"]:
            problems.append(f"subscription {name} was enabled and is now disabled")
        elif was["workers"] > 0 and now["workers"] == 0:
            problems.append(f"subscription {name} had {was['workers']} live worker(s) "
                            f"and now has none")

    for url, was in before["builds"].items():
        now = after["builds"].get(url)
        if isinstance(was, str) and was.startswith("unreachable"):
            continue          # it was already down; the restart did not break it
        if now != was:
            problems.append(f"{url} reported {was!r} before and {now!r} now")

    problems += _inventory_problems(after, set(before["declared_slots"]),
                                    set(before["declared_subscriptions"]))
    problems += _check_expected_settings(a.settings_dsn or a.dsn, a.expect_setting)

    if problems:
        for p in problems:
            print(f"  ✗ {p}", file=sys.stderr)
        return _refuse(f"{len(problems)} thing(s) are not in the state the baseline "
                       f"recorded (this compares before and after; it does not by "
                       f"itself prove the restart caused each one) — "
                       f"fix or roll back (remove pgaudit from shared_preload_libraries "
                       f"and restart) before starting the census window")

    doc = {"kind": "pg-restart-reconnect", "produced_at": _now(),
           "subject_rev": base["sha256"],
           "payload": {"baseline_sha256": base["sha256"],
                       "databases_checked": len(before["databases_reachable"]),
                       "slots_reattached": sorted(n for n, s in before["slots"].items()
                                                  if s["active"]),
                       "subscriptions_live": sorted(before["subscriptions"]),
                       "builds": after["builds"],
                       "settings_verified": list(a.expect_setting), "regressions": 0,
                       "scope": "local node only: a slot going active again shows the "
                                "remote consumer reattached, NOT that its apply worker "
                                "is healthy afterwards"}}
    doc["sha256"] = _digest(doc)
    pathlib.Path(a.out).write_text(json.dumps(doc, indent=1, ensure_ascii=False) + "\n")
    print(f"verify: the declared inventory is in the state the baseline recorded "
          f"({len(doc['payload']['slots_reattached'])} slot(s) active again, "
          f"{len(doc['payload']['subscriptions_live'])} subscription(s) with a live "
          f"worker). Local view only — remote apply health is not covered.")
    print(f"artifact: {a.out} sha256={doc['sha256']}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("baseline", help="run BEFORE the restart")
    p.add_argument("--dsn", required=True)
    p.add_argument("--expect-slots", required=True, metavar="NAME,NAME|none",
                   help="the replication slots this node is supposed to carry. The "
                        "declared inventory IS the coverage contract: without it a "
                        "wrong DSN or a drifted topology yields a thinner baseline "
                        "that still verifies green")
    p.add_argument("--expect-subscriptions", required=True, metavar="NAME,NAME|none",
                   help="likewise for subscriptions; write 'none' explicitly")
    p.add_argument("--url", action="append", default=[],
                   help="HTTP surface exposing /api/build (repeatable)")
    p.add_argument("--out", required=True)
    p.set_defaults(fn=cmd_baseline)

    p = sub.add_parser("verify", help="run AFTER the restart")
    p.add_argument("--dsn", required=True)
    p.add_argument("--baseline", required=True)
    p.add_argument("--expect-setting", action="append", required=True,
                   metavar="NAME=VALUE",
                   help="REQUIRED, repeatable: a GUC the restart was supposed to bring "
                        "into effect, e.g. shared_preload_libraries=pgaudit. Optional "
                        "made the contract advisory — verify then passed with "
                        "settings_verified: [] and certified a restart whose purpose "
                        "was never checked. An unreadable setting REFUSES too: the "
                        "purpose must be verified, not assumed")
    p.add_argument("--settings-dsn", default=None,
                   help="DSN able to read those GUCs (needs pg_read_all_settings); "
                        "defaults to --dsn")
    p.add_argument("--out", required=True)
    p.set_defaults(fn=cmd_verify)

    a = ap.parse_args()
    try:
        return a.fn(a)
    except RuntimeError as exc:
        return _refuse(str(exc))


if __name__ == "__main__":
    sys.exit(main())
