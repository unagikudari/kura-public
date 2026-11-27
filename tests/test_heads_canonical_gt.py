"""The standalone trainer reports actual, evidence-backed GT coverage."""
from contextlib import nullcontext

import pytest
from tests.test_character_pin_private_pg import cluster

from tagger.heads.canonical_gt import CONCEPTS, LABELS_SQL, coverage, read_inputs


class QueryFixture:
    def __init__(self, floors):
        self.floors = floors

    def cursor(self):
        return nullcontext(self)

    def execute(self, sql, params=None):
        self.sql = sql

    def fetchall(self):
        if "tagger_config" in self.sql:
            return self.floors
        assert self.sql == LABELS_SQL
        return [("phimosis", 99, "positive", 7), ("phimosis", 100, "negative", 8)]


def test_reports_both_floors_and_keeps_evidence_reference():
    samples, floors = read_inputs(QueryFixture([("min_gt_positive", 20), ("min_gt_negative", 50), ("min_precision", .85), ("min_recall", .7), ("min_val_pos_support", 20)]))
    assert set(samples) == set(CONCEPTS)
    assert samples["phimosis"] == {"positive": {99: 7}, "negative": {100: 8}}
    assert coverage(samples, floors)["phimosis"] == {
        "positive": 1, "negative": 1, "need_positive": 19, "need_negative": 49,
    }
    assert coverage(samples, floors)["half-erect"]["need_negative"] == 50


def test_missing_floor_fails_closed():
    with pytest.raises(ValueError, match="floors"):
        read_inputs(QueryFixture([("min_gt_positive", 20)]))


def test_evaluation_audits_are_excluded_in_both_readers(cluster):
    """Exercise each evaluation purpose and keep an ordinary human correction."""
    from tagger.heads.promote_verified import EVIDENCE

    sql = """
    BEGIN;
    CREATE TEMP TABLE post(id int,status text,type text,media_location text,data bytea);
    CREATE TEMP TABLE tag_name(tag_id int,name text,ord int);
    CREATE TEMP TABLE post_tag(post_id int,tag_id int);
    CREATE TEMP TABLE post_tag_pin(post_id int,tag_id int,state text,audit_id int);
    CREATE TEMP TABLE post_tag_audit(id int,post_id int,actor_type text,actor_id text,
        created_at timestamptz,added_tags text[],removed_tags text[]);
    CREATE TEMP TABLE post_rendition(post_id int,kind text,sha256 bytea);
    CREATE TEMP TABLE tagger_gt_label(post_id int,tag_id int,verdict text,derived_from text);
    CREATE TEMP TABLE tagger_human_review_event(id int,post_id int,audit_id int,actor_id int,
        actor_name text,received_at timestamptz,decision text,source_context jsonb);
    CREATE TEMP TABLE pool_post(post_id int,pool_id int);
    CREATE TEMP TABLE tagger_auto_exclude_pool(pool_id int,excluded boolean);
    CREATE TEMP TABLE pool(id int,verify_status text);

    INSERT INTO tag_name VALUES (4,'fixture',0);
    INSERT INTO post SELECT i,'ok','image','pg','bytes'::bytea FROM generate_series(1,6) i;
    INSERT INTO post_tag SELECT i,4 FROM generate_series(1,6) i;
    INSERT INTO post_tag_pin SELECT i,4,'present',i FROM generate_series(1,6) i;
    INSERT INTO post_tag_audit SELECT i,i,'human','reviewer',now(),ARRAY['fixture'],ARRAY[]::text[]
        FROM generate_series(1,6) i;
    INSERT INTO tagger_gt_label SELECT i,4,'positive','human_correction' FROM generate_series(1,6) i;
    INSERT INTO tagger_human_review_event
    SELECT i,i,i,7,'reviewer',now(),'present',
        jsonb_build_object('queue',jsonb_build_object('purpose',purpose))
    FROM unnest(ARRAY['candidate_evaluation','production_precision','fixed_evaluation','base_comparison','boundary_recheck'])
         WITH ORDINALITY AS e(purpose,i);
    """
    gt = LABELS_SQL.replace("%s", "ARRAY['fixture']")
    promo = EVIDENCE.replace("%s", "ARRAY['tagger-training-exclude']", 1)
    for _ in range(5):
        promo = promo.replace("%s", "'fixture'", 1)
    checks = [f"SELECT 'gt:' || post_id FROM ({gt}) kept;"]
    for post_id in range(1, 7):
        query = promo.replace("%s", str(post_id))
        checks.append(f"""SELECT 'promo:{post_id}:' || coalesce(kind,'blocked')
                          FROM ({query}) checked(a,b,c,d,e,kind);""")
    result = cluster(sql + "\n".join(checks) + "\nROLLBACK;")
    assert [line for line in result.splitlines() if line.startswith("gt:")] == ["gt:6"]
    assert [line for line in result.splitlines() if line.startswith("promo:")] == [
        "promo:1:blocked", "promo:2:blocked", "promo:3:blocked", "promo:4:blocked", "promo:5:blocked", "promo:6:human",
    ]
