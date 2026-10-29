"""kura#51 Phase A — the census may not certify what it did not read.

Mutant-first pins (each is a defect a live run actually produced, or one an
adversarial review found before it could):
  a zero-length window passes            -> test_a_window_that_holds_nothing_refuses
  the probe nonce mints a new shape      -> test_the_gate_probe_does_not_refuse_itself
  pgaudit's CSV quoting hides a shape    -> test_pgaudit_csv_quoting_is_undone
  a short declaration absorbs a path     -> test_a_prefix_of_a_declared_shape_is_not_classified
  two entries claim the same shape       -> test_two_declarations_of_one_shape_refuse
  a rehearsal passes as the census       -> test_a_short_window_is_a_rehearsal_not_a_census
  the middle of the window is unproven   -> test_a_bucket_without_a_heartbeat_refuses
  a consumer promotes a rehearsal        -> test_a_consumer_refuses_a_rehearsal_artifact
  an unknown statement is tolerated      -> test_an_undeclared_shape_is_unclassified
  a shape claims one exclusive caller    -> test_a_shape_names_candidate_sites_not_one_caller
  a multi-line statement is truncated    -> test_a_statement_with_newlines_is_one_record
  a line outside any record is dropped   -> test_an_orphan_continuation_refuses
  a finished record swallows a path      -> test_a_finished_record_does_not_absorb_the_next_line
  Q-E rests on call-site proximity       -> test_the_human_marker_is_set_in_the_same_transaction
"""
from __future__ import annotations

import importlib.util
import json
import pathlib

import pytest

REPO = pathlib.Path(__file__).resolve().parent.parent
GATE = REPO / "scripts" / "claim_census_gate.py"
MAP = REPO / "schema" / "claim_access_map.json"


def _gate():
    spec = importlib.util.spec_from_file_location("claim_census_gate", GATE)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


#: the shapes production actually issues, verbatim from the audit log
LIVE_STATEMENTS = {
    "reader": "SELECT c.tag_id FROM post_tag_actor_claim c WHERE c.post_id = $1 "
              "AND c.actor_id = $2 AND c.tag_id <> ALL($3) AND NOT EXISTS ( SELECT 1 "
              "FROM post_tag_actor_claim o WHERE o.post_id = $1 AND o.tag_id = c.tag_id "
              "AND o.actor_id <> $2)",
    "delete": "DELETE FROM post_tag_actor_claim WHERE post_id = $1 AND actor_id = $2",
    "insert": '"INSERT INTO post_tag_actor_claim (post_id, tag_id, actor_id) '
              'SELECT $1, x, $2 FROM unnest(($3)::int[]) AS x"',
    # pgaudit CSV-quotes anything containing a comma and DOUBLES inner quotes
    "cascade": '"DELETE FROM ONLY ""public"".""post_tag_actor_claim"" WHERE $1 '
               'OPERATOR(pg_catalog.=) ""post_id"""',
}


def test_pgaudit_csv_quoting_is_undone():
    """The FK-cascade DELETE arrives as ""public"".""post_tag…"". Left doubled, no
    declaration can ever match it and the census refuses forever."""
    g = _gate()
    shape = g.normalise(LIVE_STATEMENTS["cascade"])
    assert '""' not in shape, shape
    assert shape.startswith('DELETE FROM ONLY "public"."post_tag_actor_claim"'), shape


def test_bind_parameters_and_literals_collapse():
    g = _gate()
    a = g.normalise("SELECT 1 FROM t WHERE id = $1 AND name = 'abc'")
    b = g.normalise("SELECT 1 FROM t WHERE id = $7 AND name = 'zzz'")
    assert a == b, (a, b)


def test_the_gate_probe_does_not_refuse_itself():
    """Every run mints a fresh nonce. Without collapsing it, each run adds a NEW
    unclassifiable shape and the census refuses because of its own probe."""
    g = _gate()
    one = g.normalise("SELECT count(*) FROM post_tag_actor_claim WHERE false "
                      "/* kura-census-probe:a011fe115fe1b45c */")
    two = g.normalise("SELECT count(*) FROM post_tag_actor_claim WHERE false "
                      "/* kura-census-probe:440502a3a5ddf1fa */")
    assert one == two, (one, two)
    assert g._classify(one, g._load_map(str(MAP))) is not None


@pytest.mark.parametrize("key", sorted(LIVE_STATEMENTS))
def test_every_live_statement_shape_is_declared(key):
    """If production issues it, the map must name it — that IS the census."""
    g = _gate()
    entry = g._classify(g.normalise(LIVE_STATEMENTS[key]), g._load_map(str(MAP)))
    assert entry is not None, f"undeclared live shape: {key}"
    assert entry["question"] in ("Q-A", "Q-B", "Q-C", "Q-D", "Q-E")
    assert entry["sites"], "an entry must name at least one candidate site"


def test_a_prefix_of_a_declared_shape_is_not_classified():
    """The round-1 blocking: prefix matching had no boundary, so a SHORT
    declaration could absorb a different statement and the census would report
    `unverified_paths: []` with a real path hidden behind someone else's entry."""
    g = _gate()
    entries = g._load_map(str(MAP))
    declared = g.normalise(LIVE_STATEMENTS["delete"])
    # a DIFFERENT statement that merely starts the same way
    impostor = declared + " AND tag_id = $3"
    assert g._classify(declared, entries) is not None
    assert g._classify(impostor, entries) is None, (
        "a longer statement sharing a declared prefix must stay unclassified")


def test_two_declarations_of_one_shape_refuse(tmp_path):
    """Two paths cannot share one declaration: the census would credit whichever
    came first and hide the other."""
    g = _gate()
    dup = tmp_path / "dup.json"
    shape = "DELETE FROM post_tag_actor_claim WHERE post_id = $N AND actor_id = $N"
    dup.write_text(json.dumps({"paths": [
        {"shape": shape, "role": "writer", "question": "Q-A",
         "sites": [{"path": "a.py", "line": 1}], "note": "x"},
        {"shape": shape, "role": "writer", "question": "Q-A",
         "sites": [{"path": "b.py", "line": 2}], "note": "y"},
    ]}))
    with pytest.raises(RuntimeError) as e:
        g._load_map(str(dup))
    assert "SAME shape" in str(e.value)


def test_an_undeclared_shape_is_unclassified():
    """A new access path must surface as unverified, not be waved through."""
    g = _gate()
    shape = g.normalise("UPDATE post_tag_actor_claim SET actor_id = $1 WHERE post_id = $2")
    assert g._classify(shape, g._load_map(str(MAP))) is None


def test_the_shipped_map_is_well_formed():
    g = _gate()
    entries = g._load_map(str(MAP))          # raises on malformed or duplicate entries
    assert entries, "an empty map classifies nothing and refuses everything"
    for e in entries:
        assert e["role"] in ("reader", "writer", "gate-probe", "test")
        assert e.get("note"), f"{e['sites']}: an entry without a note is a shape nobody explained"


def test_a_window_that_holds_nothing_refuses(tmp_path):
    """`--window-days 0` once passed with 0 readers, 0 writers, 0 unverified —
    a census of silence reported as a completed census."""
    g = _gate()

    class A:
        window_days = 0
        map = str(MAP)
        dsn = settings_dsn = "postgresql://invalid"
        table = "post_tag_actor_claim"
        log_glob = [str(tmp_path / "none-*.log")]
        log_settle_s = 0
        out = str(tmp_path / "out.json")

    assert g.cmd_census(A()) == g.REFUSE
    assert not (tmp_path / "out.json").exists()


def test_a_short_window_is_a_rehearsal_not_a_census():
    """A rehearsal a downstream validator cannot distinguish from the census is a
    rehearsal that will eventually be used as one, so the KIND differs."""
    g = _gate()
    assert g.PLAN_WINDOW_DAYS == 14.0
    src = GATE.read_text()
    assert '"claim-census" if is_census else "claim-census-rehearsal"' in src, (
        "the artifact kind, not a payload flag, has to carry the distinction")


# ── continuity: the ends cannot show the middle (primary-codex, round 1) ─────────
#
# The old proof was "the instrument existed before the window" plus "it is
# observing right now". An instrument unloaded for a day and put back satisfies
# both and certifies a day it never saw. Organic traffic cannot close the gap
# either — quiet hours are real (02:00 and 05:00 measured with no rows at all),
# so an empty bucket is ambiguous. Hence a manufactured heartbeat.


def _dt(h, m=0):
    import datetime
    return datetime.datetime(2026, 9, 3, h, m)


def test_a_bucket_without_a_heartbeat_refuses():
    g = _gate()
    gaps = g._heartbeat_gaps([_dt(10, 5), _dt(12, 5)], _dt(10), _dt(13), 60)
    assert gaps, "the 11:00 bucket has no heartbeat and must be named"
    assert any("11:00" in x for x in gaps), gaps


def test_a_fully_covered_window_has_no_gaps():
    g = _gate()
    stamps = [_dt(10, 5), _dt(11, 5), _dt(12, 5)]
    assert g._heartbeat_gaps(stamps, _dt(10), _dt(13), 60) == []


def test_a_heartbeat_that_starts_mid_window_still_refuses():
    """A timer installed halfway through cannot vouch for the earlier half."""
    g = _gate()
    gaps = g._heartbeat_gaps([_dt(12, 5), _dt(13, 5)], _dt(10), _dt(14), 60)
    assert len(gaps) == 2, gaps


def test_the_heartbeat_shape_is_declared():
    """It is an audited read like any other; an undeclared one would refuse the
    census it exists to enable."""
    g = _gate()
    shape = g.normalise(f"SELECT count(*) FROM post_tag_actor_claim WHERE false "
                        f"/* {g.HEARTBEAT_MARK} */")
    assert g._classify(shape, g._load_map(str(MAP))) is not None


def test_a_consumer_refuses_a_rehearsal_artifact(tmp_path):
    """Finding 2's other half: acceptance rules belong in the gate, not in each
    consumer's head."""
    g = _gate()
    art = tmp_path / "reh.json"
    doc = {"kind": "claim-census-rehearsal", "produced_at": g._now(),
           "subject_rev": "x", "payload": {"unverified_paths": [], "meets_plan_window": False,
                                           "coverage_proven": True, "continuity_proven": True,
                                           "readers": [], "writers": [], "window_days": 0.5}}
    doc["sha256"] = g._digest(doc)
    art.write_text(json.dumps(doc))

    class A:
        artifact = str(art)
        max_age_days = 7.0

    assert g.cmd_verify(A()) == g.REFUSE


def test_a_consumer_refuses_a_census_without_a_continuity_proof(tmp_path):
    g = _gate()
    art = tmp_path / "old.json"
    doc = {"kind": "claim-census", "produced_at": g._now(), "subject_rev": "x",
           "payload": {"unverified_paths": [], "meets_plan_window": True,
                       "coverage_proven": True, "readers": [], "writers": [],
                       "window_days": 14}}          # no continuity_proven
    doc["sha256"] = g._digest(doc)
    art.write_text(json.dumps(doc))

    class A:
        artifact = str(art)
        max_age_days = 7.0

    assert g.cmd_verify(A()) == g.REFUSE


# ── attribution: what pgaudit can and cannot tell us (primary-codex design answer) ─
#
# The OBJECT record carries the statement and the database user, never the
# caller. Two call sites emitting identical SQL are indistinguishable, so a map
# entry names CANDIDATE sites and the artifact is stamped shape-scoped. The
# candidates stay honest only because they must exist and still contain the SQL.


def test_a_shape_names_candidate_sites_not_one_caller():
    g = _gate()
    for e in g._load_map(str(MAP)):
        assert isinstance(e["sites"], list) and e["sites"], e


def test_a_map_entry_with_no_site_refuses(tmp_path):
    g = _gate()
    bad = tmp_path / "nosite.json"
    bad.write_text(json.dumps({"paths": [
        {"shape": "SELECT 1", "role": "reader", "question": "Q-B", "sites": [], "note": "x"}]}))
    with pytest.raises(RuntimeError) as e:
        g._load_map(str(bad))
    assert "at least one possible issuing site" in str(e.value)


def test_EVERY_declared_site_still_contains_the_statement():
    """Candidacy is not a licence to list anything.

    The first version accepted a list as soon as ONE member checked out, so a
    stale or mistyped candidate could ride along invisibly (primary-codex blocking).
    "Possible issuing site" is a claim about EACH element: every one must exist
    and still contain the statement. Only entries carrying a `fragment` are
    checked — the FK cascade is emitted by PostgreSQL itself and the operator
    probes have no source file, so neither can pin one."""
    g = _gate()
    bad = []
    for e in g._load_map(str(MAP)):
        frag = e.get("fragment")
        if not frag:
            continue
        for site in e["sites"]:
            f = REPO / site["path"]
            if not f.is_file():
                bad.append((site["path"], "file does not exist"))
            elif frag not in f.read_text():
                bad.append((site["path"], f"no longer contains {frag[:50]!r}"))
    assert not bad, (
        f"declared candidate site(s) do not hold up: {bad} — a candidate list is "
        f"only honest if EVERY member is a plausible issuer; one real site must "
        f"not carry a stale one")


def test_a_site_path_is_a_real_file_or_a_declared_non_source_origin():
    """A typo'd path would otherwise sit in the map looking like evidence."""
    g = _gate()
    bad = []
    for e in g._load_map(str(MAP)):
        for site in e["sites"]:
            path = site["path"]
            if path.startswith("("):
                continue                      # e.g. "(operator, not a source site)"
            if not (REPO / path).is_file():
                bad.append((e["role"], path))
    assert not bad, f"site paths that are neither a repo file nor a declared origin: {bad}"


def test_a_site_must_be_structured_not_a_free_form_string(tmp_path):
    """`"file.py:474 (note)"` was parsed by splitting on ':' and ' ' — a parse
    that lenient is how a wrong entry comes to look fine."""
    g = _gate()
    bad = tmp_path / "freeform.json"
    bad.write_text(json.dumps({"paths": [
        {"shape": "SELECT 1", "role": "reader", "question": "Q-B",
         "sites": ["api/services/tagging.py:474 (note)"], "note": "x"}]}))
    with pytest.raises(RuntimeError) as e:
        g._load_map(str(bad))
    assert "non-empty 'path'" in str(e.value)


def test_the_artifact_stamps_its_attribution_scope():
    src = GATE.read_text()
    assert '"attribution": "shape-scoped"' in src, (
        "a reader must not be able to mistake shape counting for provenance")


# ── the log reader: a record is not a line (measured 2026-09-04) ─────────────
#
# One day after the census went live, new work introduced statements that span
# several physical log lines. Matching per line truncated the shape at the first
# newline, so two different multi-line statements sharing a first line would have
# normalised to ONE shape — the same "a path hides behind another declaration"
# failure the classifier was hardened against, arriving through the reader.


def _write_log(tmp_path, text):
    f = tmp_path / "postgresql-test.log"
    f.write_text(text)
    return str(f)


def test_a_statement_with_newlines_is_one_record(tmp_path):
    g = _gate()
    log = _write_log(tmp_path, (
        "2026-09-04 01:24:29.282 JST [1] kura@kura LOG:  AUDIT: OBJECT,1,1,READ,SELECT,"
        "TABLE,public.widget,\"SELECT a FROM widget\n"
        "\t        WHERE b = $1 AND NOT EXISTS (\n"
        "\t            SELECT 1 FROM widget c2 WHERE c2.b = $1)\",<not logged>\n"
        "2026-09-04 01:24:30.000 JST [2] kura@kura LOG:  AUDIT: OBJECT,2,1,READ,SELECT,"
        "TABLE,public.widget,SELECT 1 FROM widget,<not logged>\n"))
    records = list(g._iter_log_lines([log]))
    assert len(records) == 2, records
    first = records[0]
    assert "NOT EXISTS" in first, "the continuation lines were dropped"
    assert "c2.b = $1" in first, first


def test_two_statements_sharing_a_first_line_stay_distinct(tmp_path):
    """The actual hazard: truncation merges different paths into one shape."""
    g = _gate()
    head = ("2026-09-04 01:24:{s}.000 JST [1] kura@kura LOG:  AUDIT: OBJECT,1,1,READ,"
            "SELECT,TABLE,public.widget,\"SELECT a FROM widget\n")
    log = _write_log(tmp_path, (
        head.format(s="29") + "\t WHERE b = $1\",<not logged>\n"
        + head.format(s="30") + "\t WHERE c = $1\",<not logged>\n"))
    shapes = set()
    for rec in g._iter_log_lines([log]):
        m = g._LINE.match(rec)
        assert m, rec
        shapes.add(g.normalise(m.group("stmt").rsplit(",<not logged>", 1)[0]))
    assert len(shapes) == 2, f"truncation merged two different statements: {shapes}"


def test_a_record_with_no_continuation_is_unchanged(tmp_path):
    g = _gate()
    log = _write_log(tmp_path, (
        "2026-09-04 01:24:29.282 JST [1] kura@kura LOG:  AUDIT: OBJECT,1,1,READ,SELECT,"
        "TABLE,public.widget,SELECT 1 FROM widget,<not logged>\n"))
    records = list(g._iter_log_lines([log]))
    assert len(records) == 1 and records[0].endswith("<not logged>")


def test_an_orphan_continuation_refuses(tmp_path):
    """A continuation with no record open means the log is not the shape this reader
    assumes. Dropping it is fail-open — the orphan could carry a statement, and a
    folded orphan could even coincide with a declared shape (primary-codex, round 1)."""
    g = _gate()
    log = tmp_path / "postgresql-orphan.log"
    log.write_text("this line starts no record\n"
                   "2026-09-04 01:00:00.000 JST [1] u@d LOG:  AUDIT: OBJECT,1,1,READ,"
                   "SELECT,TABLE,public.widget,SELECT 1 FROM widget,<not logged>\n")
    with pytest.raises(RuntimeError) as e:
        list(g._iter_log_lines([str(log)]))
    assert "head is missing" in str(e.value)


def test_a_blank_line_is_not_an_orphan(tmp_path):
    """Blank lines are formatting, not evidence — refusing on them would make the
    gate unusable on any real log."""
    g = _gate()
    log = tmp_path / "postgresql-blank.log"
    log.write_text("\n\n2026-09-04 01:00:00.000 JST [1] u@d LOG:  AUDIT: OBJECT,1,1,"
                   "READ,SELECT,TABLE,public.widget,SELECT 1 FROM widget,<not logged>\n")
    assert len(list(g._iter_log_lines([str(log)]))) == 1


def test_the_human_marker_is_set_in_the_same_transaction():
    """Q-E may not rest on call-site proximity (primary-codex blocking, round 1).

    An entry that claims a human marker has to point at a site where the marker is set
    BEFORE the declared line, with no commit in between — otherwise the marker's
    transaction-local scope says nothing about the write the census is classifying.
    The statements themselves are never quoted here: the sites come from the map, so
    this checks the declaration rather than a copy of it."""
    g = _gate()
    checked = 0
    for e in g._load_map(str(MAP)):
        marker = e.get("human_marker")
        if not marker:
            continue
        for site in e["sites"]:
            path, line = site.get("path"), site.get("line")
            if path.startswith("(") or line is None:
                continue
            src = (REPO / path).read_text().splitlines()
            before = "\n".join(src[:line])
            assert marker in before, (
                f"{path}:{line} claims the {marker} marker but never sets it beforehand")
            after_marker = before[before.rindex(marker):]
            assert "commit(" not in after_marker, (
                f"{path}:{line} commits between setting {marker} and the write, so the "
                f"transaction-local marker no longer covers it")
            checked += 1
    assert checked, "no entry declared a human marker — the pin would be vacuous"


def test_a_finished_record_does_not_absorb_the_next_line(tmp_path):
    """trainer-codex's BLOCK, reproduced.

    A COMPLETE record followed by a timestamp-less line carrying a different
    statement: the line was appended, and the rsplit on the terminator then threw
    the appended statement away. One access path vanished with no refusal, because
    the shape that survived was a declared one. A finished record must refuse the
    line instead of eating it."""
    g = _gate()
    log = tmp_path / "postgresql-swallow.log"
    log.write_text(
        "2026-09-04 01:00:00.000 JST [1] u@d LOG:  AUDIT: OBJECT,1,1,READ,SELECT,"
        "TABLE,public.widget,SELECT a FROM widget,<not logged>\n"
        "SELECT b FROM widget\n")
    records = list(g._iter_log_lines([str(log)]))
    assert len(records) == 1, records
    assert "SELECT b" not in records[0], (
        "a finished record absorbed the next line; the rsplit on the terminator then "
        "discards it and that access path disappears without a refusal")


def test_an_unterminated_statement_still_takes_its_continuation(tmp_path):
    """The fix must not break the case it was written for."""
    g = _gate()
    log = tmp_path / "postgresql-open.log"
    log.write_text(
        "2026-09-04 01:00:00.000 JST [1] u@d LOG:  AUDIT: OBJECT,1,1,READ,SELECT,"
        "TABLE,public.widget,\"SELECT a FROM widget\n"
        "\t WHERE b = $1\",<not logged>\n")
    records = list(g._iter_log_lines([str(log)]))
    assert len(records) == 1 and "WHERE b = $1" in records[0]


def test_quote_balance_decides_whether_a_record_is_open():
    g = _gate()
    assert g._statement_is_open('... ,"SELECT a FROM widget')          # opened
    assert not g._statement_is_open('... ,"SELECT a\" ,<not logged>')  # closed
    assert not g._statement_is_open("... ,SELECT a FROM widget,<not logged>")


def test_a_one_off_operator_write_is_marked_as_such():
    """Both reviewers asked that a hand-run curation not be counted as a standing
    path. It stays role=writer (it really wrote) and carries its provenance."""
    g = _gate()
    for e in g._load_map(str(MAP)):
        operator_site = any(s_["path"].startswith("(operator") for s_ in e["sites"])
        if operator_site:
            assert e.get("provenance") == "operator-one-off", e["shape"][:60]
        elif e["role"] == "writer":
            assert e.get("provenance") == "standing", e["shape"][:60]


def test_a_client_diagnostic_in_the_log_is_skipped_not_refused(tmp_path):
    """/var/log/postgresql carries more than audit records. Refusing on every
    non-record line made the gate unusable on the real rotated set (measured: a psql
    "Is the server running on that host…" line), and an unsatisfiable refusal gets
    answered by narrowing the glob, which loses the window's head."""
    g = _gate()
    log = tmp_path / "postgresql-mixed.log"
    log.write_text(
        "2026-09-04 01:00:00.000 JST [1] u@d LOG:  AUDIT: OBJECT,1,1,READ,SELECT,"
        "TABLE,public.widget,SELECT a FROM widget,<not logged>\n"
        "2026-09-04 01:00:01.000 JST [2] u@d FATAL:  connection failed\n"
        "Is the server running on that host and accepting TCP/IP connections?\n"
        "2026-09-04 01:00:02.000 JST [3] u@d LOG:  AUDIT: OBJECT,2,1,READ,SELECT,"
        "TABLE,public.widget,SELECT c FROM widget,<not logged>\n")
    records = list(g._iter_log_lines([str(log)]))
    audit = [r for r in records if g._LINE.match(r)]
    assert len(audit) == 2, audit
    assert not any("Is the server running" in r for r in audit)
