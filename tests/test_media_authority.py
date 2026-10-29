"""kura#50 Phase 0 — media authority role, attestation, gate refusals.

Mutant-first pins:
  serve local bytes despite redirect role  -> test_redirect_role_never_builds_local_path
  boot with an invalid role                -> test_invalid_role_refuses_at_import
  redirect without an authority URL        -> test_redirect_without_authority_refuses
  census instrument silent                 -> test_observe_role_emits_census_line
  authority role emits census noise        -> test_authority_role_is_silent
  artifact tamper passes verify            -> test_verify_artifacts_catches_tamper
  stale artifact passes verify             -> test_verify_artifacts_catches_staleness
  attestation accepts partial coverage     -> test_attestation_set_equality_is_exact
  attestation expects a non-serving child  -> test_the_uvicorn_model_expects_exactly_the_spawn_workers
  attestation drops a serving root         -> test_a_reuseport_style_supervisor_and_child_is_REFUSED_not_guessed
  column list drops the replica identity   -> test_excluding_the_identity_column_is_REFUSED
  half a split check satisfies the gate    -> test_half_a_topology_proof_is_refused_at_the_join
  a future split half goes unregistered    -> test_an_unregistered_split_half_refuses_at_import
  a 'split' of one repeated half           -> test_a_repeated_half_is_not_a_split
"""
from __future__ import annotations

import importlib
import datetime
import json
import logging
import pathlib
import subprocess
import sys

import pytest

REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
GATE = REPO / "scripts" / "media_replication_gate.py"


def _fresh_media_role(monkeypatch, role=None, authority=None):
    if role is None:
        monkeypatch.delenv("KURA_MEDIA_ROLE", raising=False)
    else:
        monkeypatch.setenv("KURA_MEDIA_ROLE", role)
    if authority is None:
        monkeypatch.delenv("KURA_MEDIA_AUTHORITY", raising=False)
    else:
        monkeypatch.setenv("KURA_MEDIA_AUTHORITY", authority)
    import api.media_role as m
    return importlib.reload(m)


def test_default_role_is_authority(monkeypatch):
    m = _fresh_media_role(monkeypatch)
    assert m.MEDIA_ROLE == "authority"


def test_invalid_role_refuses_at_import(monkeypatch):
    with pytest.raises(RuntimeError, match="refusing to boot"):
        _fresh_media_role(monkeypatch, role="replica")


def test_redirect_without_authority_refuses(monkeypatch):
    with pytest.raises(RuntimeError, match="KURA_MEDIA_AUTHORITY"):
        _fresh_media_role(monkeypatch, role="replica_redirect")
    with pytest.raises(RuntimeError, match="KURA_MEDIA_AUTHORITY"):
        _fresh_media_role(monkeypatch, role="replica_redirect", authority="primary:8443")


def test_redirect_target_and_digest(monkeypatch):
    m = _fresh_media_role(monkeypatch, role="replica_redirect",
                          authority="https://canonical.example:8443/")
    assert m.redirect_target("/api/post/7/content") == \
        "https://canonical.example:8443/api/post/7/content"
    m2 = _fresh_media_role(monkeypatch, role="replica_redirect",
                           authority="https://canonical.example:8443")
    assert m.config_digest() == m2.config_digest(), "trailing slash must not fork the digest"


def test_redirect_role_never_builds_local_path():
    """Static pin: in posts.py the redirect guard precedes EVERY db access in
    the content/thumbnail handlers — a replica_redirect worker must not touch
    post.data even to check existence."""
    src = (REPO / "api" / "routers" / "posts.py").read_text()
    for handler in ("get_post_content", "get_post_thumbnail"):
        body = src.split(f"async def {handler}")[1].split("@router.get")[0]
        guard = body.index('replica_redirect')
        first_db = body.index("db.execute")
        assert guard < first_db, f"{handler}: redirect guard must precede any DB access"


def test_observe_role_emits_census_line(monkeypatch, caplog):
    m = _fresh_media_role(monkeypatch, role="replica_observe")
    with caplog.at_level(logging.WARNING, logger="kura.media.replica_observe"):
        m.note_local_media_serve("content", 42)
    assert "media-replica-serve ts=" in caplog.text
    assert "kind=content post=42" in caplog.text


def test_authority_role_is_silent(monkeypatch, caplog):
    m = _fresh_media_role(monkeypatch)
    with caplog.at_level(logging.WARNING, logger="kura.media.replica_observe"):
        m.note_local_media_serve("content", 42)
    assert "media-replica-serve" not in caplog.text


# ── gate: artifact verification is the executor's per-batch stop rule ────────

def _mk_artifact(tmp_path, name, kind="census", produced_at=None, tamper=False,
                 tamper_meta=False):
    import datetime, hashlib
    art = {"kind": kind,
           "produced_at": produced_at or datetime.datetime.now(
               datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
           "subject_rev": "test", "payload": {"organic_serves": 0, "window_days": 7, "coverage_proven": True}}
    body = {k: v for k, v in art.items()}
    art["sha256"] = hashlib.sha256(
        json.dumps(body, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    if tamper:
        art["payload"]["organic_serves"] = 3
    if tamper_meta:
        art["produced_at"] = datetime.datetime.now(
            datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    p = tmp_path / name
    p.write_text(json.dumps(art))
    return str(p)


def _gate(*args):
    return subprocess.run([sys.executable, str(GATE), *args],
                          capture_output=True, text=True)


def test_verify_artifacts_passes_fresh_untampered(tmp_path):
    a = _mk_artifact(tmp_path, "a.json")
    assert _gate("verify-artifacts", "--require", f"census={a}").returncode == 0


def test_verify_artifacts_catches_tamper(tmp_path):
    a = _mk_artifact(tmp_path, "a.json", tamper=True)
    p = _gate("verify-artifacts", "--require", f"census={a}")
    assert p.returncode == 2 and "digest mismatch" in p.stderr


def test_verify_artifacts_catches_metadata_tamper(tmp_path):
    """The digest covers produced_at/kind/subject_rev too — editing freshness
    without breaking the digest was impl r1's second blocking finding."""
    a = _mk_artifact(tmp_path, "a.json", produced_at="2026-08-01T00:00:00Z",
                     tamper_meta=True)
    p = _gate("verify-artifacts", "--require", f"census={a}", "--max-age-days", "7")
    assert p.returncode == 2 and "digest mismatch" in p.stderr


def test_verify_artifacts_catches_staleness(tmp_path):
    a = _mk_artifact(tmp_path, "a.json", produced_at="2026-08-01T00:00:00Z")
    p = _gate("verify-artifacts", "--require", f"census={a}", "--max-age-days", "7")
    assert p.returncode == 2 and "old" in p.stderr


def test_verify_artifacts_catches_absence(tmp_path):
    p = _gate("verify-artifacts", "--require", f"census={tmp_path}/missing.json")
    assert p.returncode == 2 and "absent" in p.stderr




import http.server
import threading


def _census_ts(days_ago=0):
    """Express window membership, without calendar dates that expire."""
    return (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=days_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")


class _MiniInstrument(http.server.BaseHTTPRequestHandler):
    """Echoes the census probe header into the log file, like the real
    replica_observe instrument does — lets census tests run its FULL path
    (probe URL is required now; a skippable probe certified an unobserved
    instrument, impl r2 blocking)."""
    log_file = None

    def do_GET(self):
        nonce = self.headers.get("X-Kura-Census-Probe")
        post = self.path.rsplit("/", 2)[-2]
        with open(self.log_file, "a") as f:
            f.write(f"media-replica-serve ts={_census_ts(0)} kind=content "
                    f"post={post}" + (f" probe={nonce}" if nonce else "") + "\n")
        self.send_response(200); self.end_headers(); self.wfile.write(b"ok")

    def log_message(self, *a):
        pass


def _census(tmp_path, log, extra=()):
    _MiniInstrument.log_file = str(log)
    srv = http.server.HTTPServer(("127.0.0.1", 0), _MiniInstrument)
    thr = threading.Thread(target=srv.serve_forever, daemon=True)
    thr.start()
    try:
        return _gate("census", "--log-path", str(log),
                     "--coverage-probe-url", f"http://127.0.0.1:{srv.server_port}",
                     "--coverage-post-id", "1",
                     "--nonce-ledger", str(tmp_path / "nonces.txt"),
                     "--out", str(tmp_path / "census-out.json"), *extra)
    finally:
        srv.shutdown()


def test_census_refuses_missing_log(tmp_path):
    p = _gate("census", "--log-path", str(tmp_path / "no.log"),
              "--coverage-probe-url", "http://127.0.0.1:1", "--coverage-post-id", "1",
              "--nonce-ledger", str(tmp_path / "n.txt"),
              "--out", str(tmp_path / "c.json"))
    assert p.returncode == 2 and "fails closed" in p.stderr


def test_census_refuses_on_organic_serves(tmp_path):
    log = tmp_path / "api.log"
    log.write_text(f"media-replica-observe-start ts={_census_ts(14)} pid=1\n"
                   f"media-replica-serve ts={_census_ts(1)} kind=content post=999\n")
    p = _census(tmp_path, log)
    assert p.returncode == 2 and "consumer exists" in p.stderr


def test_attestation_set_equality_is_exact(monkeypatch, tmp_path):
    """Pure-logic pin via import: observed ⊂ expected must refuse (round-2
    blocker: request-count thresholds are not coverage proofs)."""
    import importlib.util
    spec = importlib.util.spec_from_file_location("gate_mod", GATE)
    gate = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(gate)
    monkeypatch.setattr(gate, "_serving_worker_pids", lambda unit, port: {100, 101})

    class A:
        unit = "kura-api"; probe_url = "http://x:8001/p"; expect_role = "replica_redirect"
        expect_config_digest = ""; timeout_s = 0.3; out = str(tmp_path / "att.json")
        port = None   # derived from probe_url

    served = {"pid": 100, "role": "replica_redirect", "config_digest": "d"}
    class FakeResp:
        def __init__(self): self._b = json.dumps(served).encode()
        def read(self): return self._b
        def __enter__(self): return self
        def __exit__(self, *a): return False
    monkeypatch.setattr(gate.urllib.request, "urlopen", lambda *a, **k: FakeResp())
    assert gate.cmd_attestation(A()) == 2, "single-pid observation covered only half the set"

    pids = iter([100, 101, 100, 101])
    def rotating(*a, **k):
        served["pid"] = next(pids)
        return FakeResp()
    monkeypatch.setattr(gate.urllib.request, "urlopen", rotating)
    assert gate.cmd_attestation(A()) == 0
    art = json.loads((tmp_path / "att.json").read_text())
    assert sorted(art["payload"]["pids"]) == [100, 101]


def test_census_nonce_lines_are_not_organic(tmp_path):
    """A probe-stamped line must not count as an organic consumer; an unstamped
    line of the SAME post must (impl r1: post_id exclusion made both invisible)."""
    start = f"media-replica-observe-start ts={_census_ts(14)} pid=1\n"
    log = tmp_path / "api.log"
    # A probe tag the gate never minted is NOT excludable — an organic client
    # stamping an arbitrary header must not vanish (impl r2 blocking).
    log.write_text(start + f"media-replica-serve ts={_census_ts(1)} kind=content post=5 probe=forged\n")
    p = _census(tmp_path, log)
    assert p.returncode == 2 and "forgery" in p.stderr
    log.write_text(start)
    p = _census(tmp_path, log)
    assert p.returncode == 0, p.stderr
    log.write_text(start + f"media-replica-serve ts={_census_ts(1)} kind=content post=5\n")
    p = _census(tmp_path, log)
    assert p.returncode == 2 and "consumer exists" in p.stderr


def test_verify_artifacts_enforces_kind_and_schema(tmp_path):
    """impl r1 (primary lane): arbitrary fresh self-consistent JSON satisfied the
    gate. Now the requirement names the kind, the artifact must declare it,
    carry the kind's payload keys, and appear at most once."""
    a = _mk_artifact(tmp_path, "a.json", kind="census")
    p = _gate("verify-artifacts", "--require", f"attestation={a}")
    assert p.returncode == 2 and "substitution refused" in p.stderr
    p = _gate("verify-artifacts", "--require", f"census={a}", f"census={a}")
    assert p.returncode == 2 and "duplicate" in p.stderr
    p = _gate("verify-artifacts", "--require", a)
    assert p.returncode == 2 and "kind=path" in p.stderr
    b = _mk_artifact(tmp_path, "b.json", kind="attestation")  # census payload keys
    p = _gate("verify-artifacts", "--require", f"attestation={b}")
    assert p.returncode == 2 and "lacks required keys" in p.stderr


def test_verify_artifacts_enforces_values_and_scope(tmp_path):
    """Presence-only keys let a null-filled user-approval 'establish' approval
    no human gave (impl r2, both lanes). Values are typed; scope binds."""
    import datetime, hashlib
    def approval(vals, name):
        art = {"kind": "user-approval",
               "produced_at": datetime.datetime.now(
                   datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
               "subject_rev": "test", "payload": vals}
        art["sha256"] = hashlib.sha256(json.dumps(
            {k: v for k, v in art.items() if k != "sha256"},
            sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        f = tmp_path / name
        f.write_text(json.dumps(art))
        return str(f)

    nulls = approval({"approved_by": None, "scope": None, "statement": None}, "n.json")
    p = _gate("verify-artifacts", "--require", f"user-approval={nulls}")
    assert p.returncode == 2 and "value contract" in p.stderr

    ok = approval({"approved_by": "user", "scope": "kura50-phase2-replica-media-null",
                   "statement": "approve"}, "ok.json")
    p = _gate("verify-artifacts", "--require", f"user-approval={ok}",
              "--expect-approval-scope", "some-other-op")
    assert p.returncode == 2 and "binds to a named scope" in p.stderr
    p = _gate("verify-artifacts", "--require", f"user-approval={ok}",
              "--expect-approval-scope", "kura50-phase2-replica-media-null")
    assert p.returncode == 0, p.stderr


def test_census_window_is_enforced(tmp_path):
    """impl r1: the window was computed and never used. Now organic lines are
    window-filtered by their own ts, and the instrument must provably predate
    the window (earliest observe-start <= window_start)."""
    log = tmp_path / "api.log"
    # no start line -> refuse
    log.write_text(f"media-replica-serve ts={_census_ts(1)} kind=content post=5\n")
    p = _census(tmp_path, log, ("--window-days", "7"))
    assert p.returncode == 2 and "provably cover" in p.stderr
    # start line predates window; organic line OLDER than window -> clean
    log.write_text(f"media-replica-observe-start ts={_census_ts(14)} pid=1\n"
                   f"media-replica-serve ts={_census_ts(10)} kind=content post=5\n")
    p = _census(tmp_path, log, ("--window-days", "7"))
    assert p.returncode == 0, p.stderr
    # organic line INSIDE the window -> refuse
    log.write_text(f"media-replica-observe-start ts={_census_ts(14)} pid=1\n"
                   f"media-replica-serve ts={_census_ts(1)} kind=content post=5\n")
    p = _census(tmp_path, log, ("--window-days", "7"))
    assert p.returncode == 2 and "consumer exists" in p.stderr
    # start line YOUNGER than window -> refuse even with no organic lines
    log.write_text(f"media-replica-observe-start ts={_census_ts(1)} pid=1\n")
    p = _census(tmp_path, log, ("--window-days", "7"))
    assert p.returncode == 2 and "provably cover" in p.stderr


def test_census_prior_nonce_is_not_excludable(tmp_path):
    """impl r3 (primary lane): every nonce is public once the instrument echoes it
    into its log, so a PRIOR run's nonce is replayable — only THIS run's fresh
    nonce may be excluded, and only once."""
    start = f"media-replica-observe-start ts={_census_ts(14)} pid=1\n"
    log = tmp_path / "api.log"
    log.write_text(start)
    p = _census(tmp_path, log)
    assert p.returncode == 0, p.stderr
    old_nonce = (tmp_path / "nonces.txt").read_text().split()[0]
    log.write_text(start + f"media-replica-serve ts={_census_ts(1)} "
                           f"kind=content post=5 probe={old_nonce}\n")
    p = _census(tmp_path, log)
    assert p.returncode == 2 and "consumer exists" in p.stderr \
        and "PRIOR gate nonce" in p.stderr


def test_census_refuses_injectable_ledger(tmp_path):
    """impl r3 (primary lane): a world-writable nonce ledger is an injection
    channel — the gate must refuse it, not append to it."""
    log = tmp_path / "api.log"
    log.write_text(f"media-replica-observe-start ts={_census_ts(14)} pid=1\n")
    ledger = tmp_path / "nonces.txt"
    ledger.write_text("")
    ledger.chmod(0o666)
    p = _census(tmp_path, log)
    assert p.returncode == 2 and "injectable ledger" in p.stderr


def test_verify_artifacts_bool_is_not_int(tmp_path):
    """impl r3 (both lanes): bool subclasses int — True/False must not satisfy
    counts, pids, window days, or gigabytes."""
    import datetime, hashlib

    def art_file(kind, payload, name):
        art = {"kind": kind,
               "produced_at": datetime.datetime.now(
                   datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
               "subject_rev": "test", "payload": payload}
        art["sha256"] = hashlib.sha256(json.dumps(
            {k: v for k, v in art.items() if k != "sha256"},
            sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        f = tmp_path / name
        f.write_text(json.dumps(art))
        return str(f)

    cases = [
        ("census", {"organic_serves": False, "window_days": 7, "coverage_proven": True}),
        ("census", {"organic_serves": 0, "window_days": True, "coverage_proven": True}),
        ("attestation", {"pids": [True], "role": "replica_redirect", "unit": "kura-api"}),
        ("replication-health", {"subscriptions": ["s"], "workers": True}),
        ("free-space", {"free_gb": True, "need_gb": 1.0}),
        # json.loads accepts NaN/Infinity: NaN free_gb defeats any later
        # `free < need` refusal (NaN compares False) — must fail the contract.
        ("free-space", {"free_gb": float("nan"), "need_gb": 1.0}),
        ("free-space", {"free_gb": float("inf"), "need_gb": 1.0}),
        ("rehydration", {"verified_post_id": True, "method": "byte-compare"}),
    ]
    for i, (kind, payload) in enumerate(cases):
        f = art_file(kind, payload, f"bool{i}.json")
        p = _gate("verify-artifacts", "--require", f"{kind}={f}")
        assert p.returncode == 2 and "value contract" in p.stderr, \
            (kind, payload, p.stderr)


# ── who is expected to attest (kura#50, measured on trainer 2026-09-03) ──────
#
# kura-api runs `uvicorn --workers 4`. Its cgroup holds SIX pids: the master, the
# four spawn workers, and multiprocessing's resource_tracker. The first rule
# ("parent is inside the cgroup") expected FIVE attesters from FOUR serving
# processes, so `attestation` refused forever — an unsatisfiable gate is a
# deadlock, not a stop rule.
#
# The SECOND rule ("drop every holder whose parent is outside the cgroup") fixed
# that and opened a worse hole, per round-1 review: a root/supervisor that keeps
# accepting alongside its children (SO_REUSEPORT, mixed master/worker) would be
# deleted from the expected set — a real server never asked to attest. Ancestry
# does not tell you who accepts. So the gate now recognises ONE named model from
# /proc/<pid>/cmdline and REFUSES every other holder topology.


def _gate_module():
    import importlib.util
    spec = importlib.util.spec_from_file_location("kura50_gate", GATE)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


#: the exact holder shape measured on trainer (the resource_tracker holds no
#: listening socket, so it never reaches the classifier at all)
TRAINER_HOLDERS = {679651, 679655, 679656, 679657, 679658}
TRAINER_CMD = {
    679651: "/home/kura/.local/share/kura-api/venv/bin/python3 "
            "/home/kura/.local/share/kura-api/venv/bin/uvicorn api.main:app --workers 4",
    679655: "/home/kura/.local/share/kura-api/venv/bin/python3 -c "
            "from multiprocessing.spawn import spawn_main; spawn_main(tracker_fd=7)",
    679656: "/home/kura/.local/share/kura-api/venv/bin/python3 -c "
            "from multiprocessing.spawn import spawn_main; spawn_main(tracker_fd=7)",
    679657: "/home/kura/.local/share/kura-api/venv/bin/python3 -c "
            "from multiprocessing.spawn import spawn_main; spawn_main(tracker_fd=7)",
    679658: "/home/kura/.local/share/kura-api/venv/bin/python3 -c "
            "from multiprocessing.spawn import spawn_main; spawn_main(tracker_fd=7)",
}
TRAINER_WORKERS = {679655, 679656, 679657, 679658}


def test_the_uvicorn_model_expects_exactly_the_spawn_workers():
    g = _gate_module()
    assert g._classify_workers(TRAINER_HOLDERS, TRAINER_CMD) == TRAINER_WORKERS


def test_single_process_mode_attests_the_sole_holder():
    g = _gate_module()
    assert g._classify_workers({4242}, {4242: "uvicorn api.main:app"}) == {4242}


def test_a_reuseport_style_supervisor_and_child_is_REFUSED_not_guessed():
    """Round-1 blocking finding: a root that keeps accepting beside its child
    must never be silently dropped from the expected set. The gate cannot tell
    who accepts here, so it refuses instead of under-enumerating."""
    g = _gate_module()
    holders = {10, 11}
    cmd = {10: "/usr/bin/my-server --reuseport", 11: "/usr/bin/my-server --reuseport --child"}
    with pytest.raises(RuntimeError) as e:
        g._classify_workers(holders, cmd)
    assert "unrecognised holder topology" in str(e.value)


def test_two_masters_are_REFUSED():
    g = _gate_module()
    with pytest.raises(RuntimeError):
        g._classify_workers({1, 2, 3}, {
            1: "python -m uvicorn api.main:app",
            2: "python -m uvicorn api.main:app",
            3: "python -c from multiprocessing.spawn import spawn_main; spawn_main()"})


def test_an_unclassified_holder_alongside_the_uvicorn_model_is_REFUSED():
    """A sidecar that ALSO holds the socket is exactly the case where guessing
    costs a missing attestation."""
    g = _gate_module()
    holders = set(TRAINER_HOLDERS) | {999}
    cmd = {**TRAINER_CMD, 999: "/usr/bin/some-sidecar --inherits-fds"}
    with pytest.raises(RuntimeError) as e:
        g._classify_workers(holders, cmd)
    assert "999" in str(e.value)


def test_no_holders_at_all_is_REFUSED():
    g = _gate_module()
    with pytest.raises(RuntimeError):
        g._classify_workers(set(), {})


def test_ancestry_is_not_consulted_any_more():
    """The rev-1 rule read /proc/<pid>/status PPid. Its removal is the fix."""
    src = GATE.read_text()
    assert "PPid" not in src, "process ancestry must not decide who serves"


def test_attested_port_comes_from_the_probe_url():
    g = _gate_module()
    assert g._port_of("http://127.0.0.1:8001/api/media-authority") == 8001
    assert g._port_of("https://example.invalid/api/media-authority") == 443


# ── the publication column list vs the replica identity (kura#50 round 3) ────
#
# Measured on PG 18.6 in the toy rehearsal (2026-09-03): a column list that omits
# the replica identity is ACCEPTED by ALTER PUBLICATION. Nothing refuses at the
# moment of the change. The damage lands at the next publisher UPDATE —
# `ERROR: cannot update table "post"` — i.e. every write on the AUTHORITY breaks,
# after the operator has been told the change succeeded. So the refusal has to
# live here, before the transition.

#: the real kura shape: 34 published columns, PK `id`, dropping the two media ones
KURA_PUBLISHED = ["id", "user_id", "creation_time", "safety", "mime-type",
                  "data", "thumbnail", "source"]
KURA_IDENTITY = ["id"]


def test_the_intended_kura_exclusion_is_allowed():
    g = _gate_module()
    proposed = g._column_list_verdict("d", KURA_IDENTITY, KURA_PUBLISHED,
                                      ["data", "thumbnail"], KURA_PUBLISHED)
    assert "id" in proposed
    assert "data" not in proposed and "thumbnail" not in proposed
    assert proposed == [c for c in KURA_PUBLISHED if c not in ("data", "thumbnail")]


def test_excluding_the_identity_column_is_REFUSED():
    g = _gate_module()
    with pytest.raises(RuntimeError) as e:
        g._column_list_verdict("d", KURA_IDENTITY, KURA_PUBLISHED, ["id"], KURA_PUBLISHED)
    msg = str(e.value)
    assert "replica identity" in msg and "cannot update table" in msg, msg


def test_excluding_a_column_that_is_not_published_is_REFUSED():
    """A typo in the exclusion list silently produces a different list."""
    g = _gate_module()
    with pytest.raises(RuntimeError) as e:
        g._column_list_verdict("d", KURA_IDENTITY, KURA_PUBLISHED,
                               ["thumbnial"], KURA_PUBLISHED)
    assert "not currently published" in str(e.value)


def test_replica_identity_full_forbids_every_exclusion():
    g = _gate_module()
    with pytest.raises(RuntimeError) as e:
        g._column_list_verdict("f", KURA_PUBLISHED, KURA_PUBLISHED,
                               ["data"], KURA_PUBLISHED)
    assert "IDENTITY FULL" in str(e.value)
    # …and with nothing excluded it is simply a no-op check
    assert g._column_list_verdict("f", KURA_PUBLISHED, KURA_PUBLISHED,
                                  [], KURA_PUBLISHED) == KURA_PUBLISHED


def test_an_empty_attnames_list_means_all_columns():
    """`attnames` is empty when the publication has no column list at all."""
    g = _gate_module()
    proposed = g._column_list_verdict("d", KURA_IDENTITY, [], ["data"], KURA_PUBLISHED)
    assert "data" not in proposed and "id" in proposed


def test_identity_index_columns_are_honoured():
    g = _gate_module()
    with pytest.raises(RuntimeError):
        g._column_list_verdict("i", ["source"], KURA_PUBLISHED, ["source"], KURA_PUBLISHED)
    assert g._column_list_verdict("i", ["source"], KURA_PUBLISHED,
                                  ["data"], KURA_PUBLISHED)


def test_the_split_topology_kinds_replaced_the_cross_node_one():
    """The combined check needed a publisher DSN and a subscriber DSN in one
    process; this fleet's pg_hba admits the subscriber for REPLICATION only, so
    it could never produce an artifact here."""
    g = _gate_module()
    assert "topology" not in g.ARTIFACT_SCHEMAS
    assert "topology-publisher" in g.ARTIFACT_SCHEMAS
    assert "topology-subscriber" in g.ARTIFACT_SCHEMAS
    assert "column-list" in g.ARTIFACT_SCHEMAS
    assert not hasattr(g, "cmd_topology")


def test_verify_artifacts_rejects_a_column_list_that_claims_no_identity(tmp_path):
    """`identity_satisfied` is a value contract, not a key that may be False."""
    g = _gate_module()
    art = g._artifact("column-list", "pub", {
        "table": "post", "identity_columns": ["id"],
        "proposed_columns": ["id", "safety"], "identity_satisfied": False})
    path = tmp_path / "cl.json"
    path.write_text(json.dumps(art))

    class A:
        require = [f"column-list={path}"]
        max_age_days = 7.0
        expect_subject_rev = ""
        expect_approval_scope = ""

    assert g.cmd_verify_artifacts(A()) == 2


# ── a split check must not be satisfiable by half of itself (round-3 fold) ───
#
# `topology-publisher` and `topology-subscriber` are one check run on two nodes,
# because this fleet's pg_hba admits the subscriber for REPLICATION only. Each
# half succeeds alone by construction — so if completeness lives in every
# caller's `--require` list, a new call site forgets it and half a topology
# proof passes. The join enforces the pair instead (primary-codex blocking finding).


def _verify(tmp_path, arts):
    g = _gate_module()
    paths = []
    for kind, payload in arts:
        art = g._artifact(kind, "subj", payload)
        f = tmp_path / f"{kind}.json"
        f.write_text(json.dumps(art))
        paths.append(f"{kind}={f}")

    class A:
        require = paths
        max_age_days = 7.0
        expect_subject_rev = ""
        expect_approval_scope = ""

    return g.cmd_verify_artifacts(A())


PUB_ART = ("topology-publisher", {"table": "post", "publications_for_table": ["kura_mesh_pub"],
                                  "columns_digest": "abc123"})
SUB_ART = ("topology-subscriber", {"non_ready_rels": 0, "total_rels": 56})


def test_half_a_topology_proof_is_refused_at_the_join(tmp_path):
    other = tmp_path / "b"
    other.mkdir()
    assert _verify(tmp_path, [PUB_ART]) == 2
    assert _verify(other, [SUB_ART]) == 2


def test_both_halves_together_verify(tmp_path):
    assert _verify(tmp_path, [PUB_ART, SUB_ART]) == 0


def test_the_corequisite_table_is_derived_not_written():
    """A hand-written pair table can be asymmetric (satisfiable from one side)
    or half-filled; deriving it from SPLIT_CHECKS makes both unrepresentable."""
    g = _gate_module()
    assert g.ARTIFACT_COREQUISITES == {
        kind: set(halves) - {kind}
        for halves in g.SPLIT_CHECKS.values() for kind in halves}
    for kind, req in g.ARTIFACT_COREQUISITES.items():
        assert kind in g.ARTIFACT_SCHEMAS
        for other in req:
            assert kind in g.ARTIFACT_COREQUISITES.get(other, set()), (
                f"{kind} requires {other} but not the other way round — an "
                "asymmetric pair is satisfiable from one side")


def test_an_unregistered_split_half_refuses_at_import(monkeypatch):
    """The follow-up hole: a FUTURE split check nobody remembers to register
    would silently go back to fail-open. A kind NAMED as a side must belong to
    a declared group, and the gate refuses to load otherwise — a test alone
    would only catch it in CI."""
    g = _gate_module()
    g.ARTIFACT_SCHEMAS["ledger-publisher"] = {"table": lambda v: True}
    try:
        with pytest.raises(RuntimeError) as e:
            g._validate_split_registration()
        assert "SPLIT_CHECKS" in str(e.value)
    finally:
        del g.ARTIFACT_SCHEMAS["ledger-publisher"]


def test_a_split_group_must_name_real_schemas_and_have_two_sides():
    g = _gate_module()
    g.SPLIT_CHECKS["bogus"] = ("topology-publisher",)
    try:
        with pytest.raises(RuntimeError):
            g._validate_split_registration()
    finally:
        del g.SPLIT_CHECKS["bogus"]

    g.SPLIT_CHECKS["ghost"] = ("ghost-publisher", "ghost-subscriber")
    g.ARTIFACT_COREQUISITES["ghost-publisher"] = {"ghost-subscriber"}
    g.ARTIFACT_COREQUISITES["ghost-subscriber"] = {"ghost-publisher"}
    try:
        with pytest.raises(RuntimeError) as e:
            g._validate_split_registration()
        assert "no artifact schema" in str(e.value)
    finally:
        del g.SPLIT_CHECKS["ghost"]
        del g.ARTIFACT_COREQUISITES["ghost-publisher"]
        del g.ARTIFACT_COREQUISITES["ghost-subscriber"]


# ── identifiers the gate cannot safely interpolate are refused, not guessed ──


def test_a_plain_table_name_is_accepted():
    g = _gate_module()
    assert g._checked_table("post") == "post"
    assert g._checked_table("tagger_head_policy") == "tagger_head_policy"


def test_schema_qualified_mixed_case_and_injection_shapes_are_REFUSED():
    g = _gate_module()
    for bad in ("public.post", "Post", '"post"', "post; drop table post",
                "post'", "", "1post", "post-1"):
        with pytest.raises(RuntimeError):
            g._checked_table(bad)


def test_a_repeated_half_is_not_a_split():
    """`("x-publisher", "x-publisher")` has len 2 but derives an EMPTY
    requirement, so the half would verify alone — the fail-open this table
    exists to prevent (primary-codex, round 4)."""
    g = _gate_module()
    g.SPLIT_CHECKS["dup"] = ("topology-publisher", "topology-publisher")
    try:
        with pytest.raises(RuntimeError) as e:
            g._validate_split_registration()
        assert "distinct" in str(e.value)
    finally:
        del g.SPLIT_CHECKS["dup"]


def test_a_half_may_belong_to_only_one_split():
    """The derivation is a dict comprehension: a half in two groups is
    overwritten by the later group and quietly loses a co-requisite."""
    g = _gate_module()
    g.ARTIFACT_SCHEMAS["ledger-subscriber"] = {"x": lambda v: True}
    g.SPLIT_CHECKS["ledger"] = ("topology-publisher", "ledger-subscriber")
    # mirror what the derivation would produce, so the "unregistered half" check
    # (which runs first) passes and the multi-group check is the one under test
    g.ARTIFACT_COREQUISITES["ledger-subscriber"] = {"topology-publisher"}
    try:
        with pytest.raises(RuntimeError) as e:
            g._validate_split_registration()
        assert "exactly one split" in str(e.value)
    finally:
        del g.SPLIT_CHECKS["ledger"]
        del g.ARTIFACT_SCHEMAS["ledger-subscriber"]
        del g.ARTIFACT_COREQUISITES["ledger-subscriber"]


def test_the_shipped_declaration_survives_its_own_validator():
    """Whatever the rules are, the table actually shipped must satisfy them."""
    g = _gate_module()
    g._validate_split_registration()
    halves = [h for group in g.SPLIT_CHECKS.values() for h in group]
    assert len(halves) == len(set(halves)), "a half is registered twice"
