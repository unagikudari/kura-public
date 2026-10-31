"""Phase C's gate may not answer "yes" from a log that never saw the traffic.

2026-09-05, measured on primary canonical: `tally` read nginx's access log while
production served `/data/` from `tailscale serve ... path <media dir>`, which
never touches nginx. The log held 6,105 bytes / 22 lines, every one of them a
local `Python-urllib` script, so `first_party_last_days` was `[0]*7`,
`cond_first_party_zero` was true, and `start` was **"yes"**.

That answer was the only gate on unlinking 2,074,210 legacy names, and it was
zero-from-an-empty-instrument. #66 principle 3 — "unobserved / instrument-missing
is not PASS" — applied exactly, so the gate now has to show its work: fire one
legacy-named request at the production ingress and find it again in the log it
read, or answer `unknown`.

`cond_bounded_fallback` is put under the same precondition deliberately. It is
not the safe one: on an empty log `worst` is 0, so it goes true on elapsed time
alone — the same zero-from-nothing as `cond_first_party_zero`, just slower.
"""
import datetime as dt
import gzip
import importlib.util
import os
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "scripts" / "fs_salt_migrate.py"


@pytest.fixture
def mig(monkeypatch, tmp_path):
    """Load the script with a Phase B salt configuration, media dir isolated."""
    monkeypatch.setenv("KURA_FS_NAME_SALT", "new-salt")
    monkeypatch.setenv("KURA_FS_NAME_SALT_LEGACY", "old-salt")
    monkeypatch.setenv("KURA_FS_NAME_SALT_NEXT", "")
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    (tmp_path / "data" / "posts").mkdir(parents=True, exist_ok=True)
    for m in ("api.config", "api.services.hashing", "api.services.files"):
        sys.modules.pop(m, None)
    sys.path.insert(0, str(REPO))
    spec = importlib.util.spec_from_file_location("fs_salt_migrate_t", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _line(day: dt.date, path: str, ua: str, ref: str = "-") -> str:
    ts = day.strftime("%d/%b/%Y:12:00:00 +0900")
    return f'10.0.0.1 - - [{ts}] "GET {path} HTTP/1.1" 200 1234 "{ref}" "{ua}"\n'


def _legacy_path(mig, pid: int) -> str:
    from api.services.hashing import get_legacy_hash
    return f"/data/posts/{pid}_{get_legacy_hash(pid)}.jpg"


def _write_window(mig, log: Path, days: int, now: dt.date, extra: list[str] = ()):
    """A log that covers every day of the window, so only the canary is in question."""
    lines = []
    for i in range(days):
        d = now - dt.timedelta(days=i)
        lines.append(_line(d, "/", "Mozilla/5.0"))
    lines += list(extra)
    log.write_text("".join(lines))


NOW = dt.date(2026, 9, 20)
#: long enough ago that the instrument is not what any of these tests are about
INSTRUMENTED = dt.date(2026, 8, 20)


def test_a_log_that_never_saw_the_canary_answers_unknown(mig, tmp_path):
    """THE 2026-09-05 CASE. Full 7-day coverage, zero legacy requests, and the
    answer is still not "yes" — because the canary never came back, which is what
    "this log is not on the content path" looks like."""
    log = tmp_path / "access.log"
    _write_window(mig, log, 7, NOW)
    out = mig.tally(str(log), 7, "example.test", None, 30, 10, now=NOW,
                    canary={"ua": "kura-fs-salt-tally-canary/123", "status": 404})
    assert out["cond_first_party_zero"] is True     # the old gate said yes here
    assert out["start"] == "unknown"
    assert out["reason"] == "log does not observe the content path"


def test_the_canary_makes_a_clean_log_answer_yes(mig, tmp_path):
    """The other half: proven coverage plus genuinely no demand IS a yes."""
    log = tmp_path / "access.log"
    ua = "kura-fs-salt-tally-canary/123"
    _write_window(mig, log, 7, NOW, extra=[_line(NOW, _legacy_path(mig, 0), ua)])
    out = mig.tally(str(log), 7, "example.test", None, 30, 10, now=NOW,
                    canary={"ua": ua, "status": 404}, instrumented_since=INSTRUMENTED)
    assert out["coverage"]["canary_seen"] is True
    assert out["start"] == "yes"
    assert out["reason"] is None


def test_the_canary_is_never_counted_as_demand(mig, tmp_path):
    """The probe asks for a legacy name by construction. Counting it would make
    the gate raise its own floor every time it ran."""
    log = tmp_path / "access.log"
    ua = "kura-fs-salt-tally-canary/123"
    _write_window(mig, log, 7, NOW, extra=[
        _line(NOW, _legacy_path(mig, 0), ua),
        _line(NOW, _legacy_path(mig, 0), "kura-fs-salt-tally-canary/999"),   # an older run
    ])
    out = mig.tally(str(log), 7, "example.test", None, 30, 10, now=NOW,
                    canary={"ua": ua, "status": 404}, instrumented_since=INSTRUMENTED)
    assert out["per_day"] == {}, "canary probes must not appear as legacy demand"
    assert out["start"] == "yes"


def test_the_bounded_fallback_is_also_gated_on_coverage(mig, tmp_path):
    """cond_bounded_fallback goes true on an EMPTY log: worst == 0 and enough days
    elapsed. Leaving it outside the precondition would keep the whole hole open on
    a 30-day timer instead of a 7-day one."""
    log = tmp_path / "access.log"
    _write_window(mig, log, 7, NOW)                       # no canary line
    out = mig.tally(str(log), 7, "example.test", dt.date(2026, 8, 1), 30, 10, now=NOW,
                    canary={"ua": "kura-fs-salt-tally-canary/123", "status": 404})
    assert out["cond_bounded_fallback"] is True           # elapsed 50d, worst 0
    assert out["start"] == "unknown", "fallback must not bypass the coverage proof"


def test_a_window_the_log_does_not_span_is_unknown(mig, tmp_path):
    """Reading only the live file after a rotation silently shortens the window.
    Three days of log cannot answer a question about seven."""
    log = tmp_path / "access.log"
    ua = "kura-fs-salt-tally-canary/123"
    lines = [_line(NOW - dt.timedelta(days=i), "/", "Mozilla/5.0") for i in range(3)]
    lines.append(_line(NOW, _legacy_path(mig, 0), ua))
    log.write_text("".join(lines))
    out = mig.tally(str(log), 7, "example.test", None, 30, 10, now=NOW,
                    canary={"ua": ua, "status": 404}, instrumented_since=INSTRUMENTED)
    assert out["coverage"]["canary_seen"] is True
    assert out["start"] == "unknown"
    assert out["coverage"]["spans_window"] is False
    assert "does not cover" in out["reason"]


def test_rotations_including_gz_are_part_of_the_window(mig, tmp_path):
    """Same span, split across access.log / .1 / .2.gz — a glob reads all three,
    and the answer changes from unknown to yes purely because the window is now
    covered. This is the test that fails if the glob or the gzip branch is lost."""
    ua = "kura-fs-salt-tally-canary/123"
    live = tmp_path / "access.log"
    live.write_text("".join([_line(NOW, "/", "Mozilla/5.0"),
                             _line(NOW, _legacy_path(mig, 0), ua)]))
    (tmp_path / "access.log.1").write_text(
        "".join(_line(NOW - dt.timedelta(days=i), "/", "Mozilla/5.0") for i in (1, 2, 3)))
    with gzip.open(tmp_path / "access.log.2.gz", "wt") as fh:
        fh.write("".join(_line(NOW - dt.timedelta(days=i), "/", "Mozilla/5.0") for i in (4, 5, 6)))

    only_live = mig.tally(str(live), 7, "example.test", None, 30, 10, now=NOW,
                          canary={"ua": ua, "status": 404}, instrumented_since=INSTRUMENTED)
    assert only_live["start"] == "unknown"

    globbed = mig.tally(str(tmp_path / "access.log*"), 7, "example.test", None, 30, 10, now=NOW,
                        canary={"ua": ua, "status": 404}, instrumented_since=INSTRUMENTED)
    assert globbed["coverage"]["spans_window"] is True
    assert len(globbed["coverage"]["log_files"]) == 3
    assert globbed["start"] == "yes"


def test_real_first_party_demand_still_says_no(mig, tmp_path):
    """Coverage proven and somebody IS asking for a legacy name — the gate closes.
    A guard that only ever answers unknown/yes would pass every test above."""
    log = tmp_path / "access.log"
    ua = "kura-fs-salt-tally-canary/123"
    _write_window(mig, log, 7, NOW, extra=[
        _line(NOW, _legacy_path(mig, 0), ua),
        _line(NOW, _legacy_path(mig, 4242), "Mozilla/5.0", ref="https://example.test/post/4242"),
    ])
    out = mig.tally(str(log), 7, "example.test", None, 30, 10, now=NOW,
                    canary={"ua": ua, "status": 404}, instrumented_since=INSTRUMENTED)
    assert out["first_party_last_days"][-1] == 1
    assert out["cond_first_party_zero"] is False
    assert out["start"] == "no"


def test_an_unfired_canary_is_unknown_not_yes(mig, tmp_path):
    """No ingress given, no probe made. The absence of evidence must not read as
    evidence of absence — this is the path a caller takes by simply forgetting."""
    log = tmp_path / "access.log"
    _write_window(mig, log, 7, NOW)
    out = mig.tally(str(log), 7, "example.test", None, 30, 10, now=NOW)
    assert out["start"] == "unknown"
    assert "no canary" in out["reason"]


def test_the_canary_url_is_legacy_shaped_and_cannot_be_a_real_post(mig):
    """It must be the class of request tally counts (else coverage proves nothing)
    and must not be a real file (else the probe manufactures demand for one)."""
    from api.services.hashing import get_legacy_hash
    fired = mig.fire_canary("http://127.0.0.1:1", timeout=0.2)   # nothing listening
    assert fired["error"], "an unreachable ingress is reported, not raised"
    assert fired["ua"].startswith("kura-fs-salt-tally-canary/")
    assert f"/0_{get_legacy_hash(0)}.jpg" in fired["url"]
    assert not (Path(os.environ["DATA_DIR"]) / "posts" / f"0_{get_legacy_hash(0)}.jpg").exists()


# ── round-1 review findings (nodeb-gemini-primary, origin trainer) ──────────────


def _covered(mig, tmp_path, ua="kura-fs-salt-tally-canary/123"):
    """A log that satisfies canary + full window, so only the new clause is in play."""
    log = tmp_path / "access.log"
    _write_window(mig, log, 7, NOW, extra=[_line(NOW, _legacy_path(mig, 0), ua)])
    return log, {"ua": ua, "status": 404}


def test_the_window_must_lie_after_the_instrument_went_in(mig, tmp_path):
    """D1, the round-1 BLOCKER. `observed` is filled by ANY request, and the old
    route still put `/` hits in this same log — so on cutover day the canary
    passes, six pre-cutover days fill the window, legacy demand reads 0, and the
    gate opens on the day the instrument was installed. The 7 days have to be
    counted from the instrument, not from the calendar."""
    log, canary = _covered(mig, tmp_path)
    same_day = mig.tally(str(log), 7, "example.test", None, 30, 10, now=NOW,
                         canary=canary, instrumented_since=NOW)
    assert same_day["start"] == "unknown"
    assert "instrumented since" in same_day["reason"] and "0d < 7d" in same_day["reason"]

    day6 = mig.tally(str(log), 7, "example.test", None, 30, 10, now=NOW,
                     canary=canary, instrumented_since=NOW - dt.timedelta(days=6))
    assert day6["start"] == "unknown", "6 days is not 7"

    day7 = mig.tally(str(log), 7, "example.test", None, 30, 10, now=NOW,
                     canary=canary, instrumented_since=NOW - dt.timedelta(days=7))
    assert day7["start"] == "yes"


def test_omitting_instrumented_since_cannot_yield_yes(mig, tmp_path):
    """The flag is the evidence. Forgetting it must not be the easy path to a yes."""
    log, canary = _covered(mig, tmp_path)
    out = mig.tally(str(log), 7, "example.test", None, 30, 10, now=NOW, canary=canary)
    assert out["start"] == "unknown"
    assert "--instrumented-since" in out["reason"]


def test_an_unreachable_ingress_is_not_a_blind_log(mig, tmp_path):
    """D5. "I could not ask" and "the log did not see it" send the operator to
    opposite places — check the URL/network vs check the log pipeline."""
    log, _ = _covered(mig, tmp_path)
    out = mig.tally(str(log), 7, "example.test", None, 30, 10, now=NOW,
                    canary={"ua": "kura-fs-salt-tally-canary/x", "status": None,
                            "error": "URLError: [Errno 111] Connection refused"},
                    instrumented_since=NOW - dt.timedelta(days=30))
    assert out["start"] == "unknown"
    assert "canary probe to ingress failed" in out["reason"]
    assert "log does not observe" not in out["reason"]


def test_an_ingress_that_answers_oddly_says_so(mig, tmp_path):
    """A 502 from the ingress is not evidence about the log either."""
    log, _ = _covered(mig, tmp_path)
    out = mig.tally(str(log), 7, "example.test", None, 30, 10, now=NOW,
                    canary={"ua": "kura-fs-salt-tally-canary/x", "status": 502},
                    instrumented_since=NOW - dt.timedelta(days=30))
    assert out["start"] == "unknown" and "HTTP 502" in out["reason"]


def test_a_served_canary_means_the_probe_is_not_inert(mig, tmp_path):
    """D4. 404 is the expected answer. A body means post_id 0 resolves to a real
    file, so the probe would be asking for something that could be in demand —
    the premise that makes it harmless is gone, and the run must not be believed."""
    log, _ = _covered(mig, tmp_path)
    out = mig.tally(str(log), 7, "example.test", None, 30, 10, now=NOW,
                    canary={"ua": "kura-fs-salt-tally-canary/x", "status": 200},
                    instrumented_since=NOW - dt.timedelta(days=30))
    assert out["start"] == "unknown"
    assert "SERVED (HTTP 200)" in out["reason"] and "not inert" in out["reason"]


def test_a_quiet_day_inside_the_window_does_not_stall_the_gate(mig, tmp_path):
    """E1, the round-1 second seat's finding. An access log writes nothing on a day
    with no requests, so counting days-that-carried-traffic made a low-traffic
    window permanently short: the gate answered `unknown` forever with the
    instrument working perfectly. Coverage is the SPAN the log reaches, not the
    set of days that happened to be busy — a false unknown fails silent, which is
    why it survived the first design."""
    ua = "kura-fs-salt-tally-canary/123"
    log = tmp_path / "access.log"
    # the ends of the window are present; the middle is a genuinely quiet stretch
    log.write_text("".join([
        _line(NOW - dt.timedelta(days=6), "/", "Mozilla/5.0"),
        _line(NOW, "/", "Mozilla/5.0"),
        _line(NOW, _legacy_path(mig, 0), ua),
    ]))
    out = mig.tally(str(log), 7, "example.test", None, 30, 10, now=NOW,
                    canary={"ua": ua, "status": 404}, instrumented_since=INSTRUMENTED)
    assert out["coverage"]["spans_window"] is True
    assert out["start"] == "yes", f"a quiet day must not stall the gate: {out['reason']}"


def test_lines_dated_after_now_make_the_window_untrustworthy(mig, tmp_path):
    """E2. Unbounded, a later-dated line filled the coverage span while `fp_days`
    never scanned that day — a legacy request could sit in the log and outside the
    count at once. Every day boundary here is drawn with the same clock, so a
    disagreeing one is not a tidiness matter."""
    ua = "kura-fs-salt-tally-canary/123"
    log, canary = _covered(mig, tmp_path, ua)
    log.write_text(log.read_text() + _line(NOW + dt.timedelta(days=1),
                                           _legacy_path(mig, 77), "Mozilla/5.0",
                                           ref="https://example.test/post/77"))
    out = mig.tally(str(log), 7, "example.test", None, 30, 10, now=NOW,
                    canary=canary, instrumented_since=INSTRUMENTED)
    assert out["coverage"]["future_dated_lines"] == 1
    assert out["start"] == "unknown"
    assert "clock" in out["reason"]


def test_access_log_takes_several_paths_as_well_as_a_glob(mig, tmp_path):
    """E3. Unquoted, the shell expands the wildcard into many arguments; quoted, it
    arrives as one glob. Taking a single string made the first an argparse error
    and the naive single-file form silently short of the window."""
    ua = "kura-fs-salt-tally-canary/123"
    a = tmp_path / "access.log"
    b = tmp_path / "access.log.1"
    a.write_text(_line(NOW, "/", "Mozilla/5.0") + _line(NOW, _legacy_path(mig, 0), ua))
    b.write_text("".join(_line(NOW - dt.timedelta(days=i), "/", "Mozilla/5.0") for i in range(1, 7)))
    listed = mig.tally([str(a), str(b)], 7, "example.test", None, 30, 10, now=NOW,
                       canary={"ua": ua, "status": 404}, instrumented_since=INSTRUMENTED)
    globbed = mig.tally([str(tmp_path / "access.log*")], 7, "example.test", None, 30, 10, now=NOW,
                        canary={"ua": ua, "status": 404}, instrumented_since=INSTRUMENTED)
    assert listed["start"] == "yes" and globbed["start"] == "yes"
    assert listed["coverage"]["log_files"] == globbed["coverage"]["log_files"]


# ── checking the instrument must not move it (2026-09-06) ──────────────────────


def test_a_verification_probe_is_not_demand_but_the_same_request_from_a_real_ua_is(mig, tmp_path):
    """2026-09-06, on the live gate. Verifying the cutover, a pane fired a
    legacy-named GET with a hand-set Referer and a browser-shaped UA; tally counted
    it as first-party demand, `cond_first_party_zero` went false, and the day the
    instrument went in became a day somebody wanted a legacy name. The act of
    checking the gate moved the gate.

    Three requests, byte-identical but for the UA, so the exclusion is the only
    thing under test:

    * `kura-verify-probe/<who>` — an operator checking the instrument. Not demand.
    * `Mozilla/5.0` — somebody's browser. Demand, and the gate closes. Remove the
      exclusion and the first case becomes this one, which is what makes the leg
      above a real assertion rather than a tautology.
    * `Mozilla/5.0 (kura-verify-probe/x)` — the exclusion is a PREFIX, not a
      substring: naming it mid-UA must not buy anyone silence, or the escape hatch
      is a hiding place for exactly the traffic this gate exists to see.
    """
    canary_ua = "kura-fs-salt-tally-canary/123"
    legacy = _legacy_path(mig, 1000000)
    ref = "https://example.test/post/1000000"

    def run(ua):
        log = tmp_path / f"access-{abs(hash(ua))}.log"
        _write_window(mig, log, 7, NOW, extra=[
            _line(NOW, _legacy_path(mig, 0), canary_ua),
            _line(NOW, legacy, ua, ref=ref),
        ])
        return mig.tally(str(log), 7, "example.test", None, 30, 10, now=NOW,
                         canary={"ua": canary_ua, "status": 404},
                         instrumented_since=INSTRUMENTED)

    probe = run("kura-verify-probe/primary-cc-primary")
    assert probe["per_day"] == {}, "a verification probe is not in the tally at all"
    assert probe["first_party_last_days"][-1] == 0
    assert probe["cond_first_party_zero"] is True
    assert probe["start"] == "yes"

    real = run("Mozilla/5.0")
    assert real["per_day"][NOW.isoformat()] == {"total": 1, "bot": 0, "first_party": 1}
    assert real["cond_first_party_zero"] is False
    assert real["start"] == "no"

    disguised = run("Mozilla/5.0 (kura-verify-probe/x)")
    assert disguised["per_day"][NOW.isoformat()]["first_party"] == 1
    assert disguised["start"] == "no", "the prefix must not be matched as a substring"
