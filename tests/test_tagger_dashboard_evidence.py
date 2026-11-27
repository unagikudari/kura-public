"""Real SQL: corrected tags cannot be mistaken for current model errors."""
import os
import pytest
from api.tagger_core.dashboard_evidence import EVIDENCE_SQL, evidence_counts


def test_unknown_accuracy_is_not_zero():
    assert evidence_counts(None) == (0, 0, 0, 0)
    # These numbers never contain precision; model predictions are separate evidence.
    assert evidence_counts({'corrections': 202, 'positive': 15}) == (202, 0, 15, 0)


def test_human_evidence_sql():
    psycopg2 = pytest.importorskip('psycopg2')
    dsn = os.environ.get('KURA_TEST_DSN')
    if not dsn:
        pytest.skip('KURA_TEST_DSN required; only temporary tables are written')
    conn = psycopg2.connect(dsn)
    try:
        with conn.cursor() as c:
            c.execute("CREATE TEMP TABLE post(id int, status text)")
            c.execute("INSERT INTO post SELECT i, 'ok' FROM generate_series(1,10) i")
            c.execute("INSERT INTO post VALUES (11,'duplicate')")
            c.execute("CREATE TEMP TABLE tagger_gt_label(tag_id int,post_id int,verdict text,derived_from text)")
            c.execute("CREATE TEMP TABLE post_tag_pin(tag_id int,post_id int,state text)")
            c.execute("CREATE TEMP TABLE post_tag(tag_id int,post_id int)")
            c.execute("CREATE TEMP TABLE tag_name(tag_id int,name text,ord int)")
            c.execute("INSERT INTO tag_name VALUES (1,'monochrome',0),(1,'alias',1),(2,'ordinary',0)")
            c.execute("""INSERT INTO tagger_gt_label VALUES
              (1,1,'negative','human_correction'), (1,2,'positive','human_correction'),
              (1,3,'positive','implicit_review'), (1,4,'negative','implicit_review'),
              (1,5,'negative','human_correction'), (1,6,'positive','human_correction'),
              (1,9,'negative','human_correction')""")
            c.execute("""INSERT INTO post_tag_pin VALUES
              (1,1,'absent'),(1,2,'present'),(1,3,'absent'),(1,7,'present'),
              (1,8,'present'),(1,9,'present'),(2,10,'absent'),(1,11,'absent')""")
            c.execute("INSERT INTO post_tag VALUES (1,2),(1,4),(1,5),(1,8),(1,9),(2,10)")
            c.execute(EVIDENCE_SQL)
            rows = {r[0]: r[1:] for r in c.fetchall()}
            # Corrected negative 1 stays history, not violation; confirmed positive 2
            # is a positive, never an FN. Pins override stale GT in both directions.
            # Implicit review 4 is a GT negative, not a human disagreement.
            assert rows == {'monochrome': (5,4,3,1,2), 'ordinary': (0,1,1,1,0)}
    finally:
        conn.rollback()
        conn.close()
