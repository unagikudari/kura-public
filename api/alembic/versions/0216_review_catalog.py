"""Normalize immutable review requests and index each member's latest answer."""
from importlib import import_module

from alembic import op

from api.alembic.owner import as_owner
from api.services.dashboard_review_catalog import POPULATION

revision = "0216"
down_revision = "0215"
branch_labels = None
depends_on = None
REPLICA_DDL = None

prior = import_module("api.alembic.versions.0214_review_lane_capacity")

SET_COLUMNS = """
    ALTER TABLE public.tagger_review_set
      ADD COLUMN schema_version smallint,
      ADD COLUMN frozen_tag_name text,
      ADD COLUMN origin text,
      ADD COLUMN lane text,
      ADD COLUMN review_kind text,
      ADD COLUMN adjudication boolean,
      ADD COLUMN supersedes_queue_sha256 text REFERENCES public.tagger_review_set(queue_sha256),
      ADD COLUMN provenance_kind text,
      ADD COLUMN declared_origin text,
      ADD COLUMN artifact_sha256 text,
      ADD COLUMN snapshot_sha256 text,
      ADD COLUMN measurement_sha256 text
"""

POPULATE = """
CREATE FUNCTION public.tagger_index_review_set() RETURNS trigger
LANGUAGE plpgsql SET search_path=pg_catalog,public AS $$
DECLARE
 d jsonb := convert_from(NEW.document,'UTF8')::jsonb;
 p jsonb := d->'source_provenance';
BEGIN
 NEW.schema_version := right(d->>'schema',1)::smallint;
 NEW.frozen_tag_name := d->>'tag';
 NEW.origin := CASE
   WHEN public.tagger_is_lora_review(NEW.queue_sha256,d) THEN 'lora_findings'
   WHEN NEW.schema_version=1 THEN 'legacy'
   ELSE 'lifecycle'
 END;
 NEW.lane := CASE WHEN NEW.origin='lora_findings' THEN 'lora_findings' ELSE 'lifecycle' END;
 NEW.adjudication := public.tagger_is_adjudication(d);
 NEW.review_kind := CASE
   WHEN NEW.adjudication THEN 'adjudication'
   WHEN NEW.purpose='base_comparison' THEN 'blind_comparison'
   WHEN NEW.purpose IN ('candidate_evaluation','production_precision','fixed_evaluation') THEN 'prepublication'
   WHEN NEW.purpose='training_support' THEN 'training_support'
   WHEN NEW.purpose='historical_label_recheck' AND d->>'tag'='child' THEN 'age_check'
   WHEN NEW.origin='lora_findings' THEN 'findings'
   ELSE 'recheck'
 END;
 NEW.supersedes_queue_sha256 := p->>'supersedes_queue_sha256';
 NEW.provenance_kind := p->>'kind';
 NEW.declared_origin := p->>'origin';
 NEW.artifact_sha256 := p->>'artifact_sha256';
 NEW.snapshot_sha256 := p->>'snapshot_sha256';
 NEW.measurement_sha256 := p->>'measurement_sha256';
 RETURN NEW;
END $$;
"""

CURRENT = """
CREATE VIEW public.tagger_review_current_set AS
WITH direct AS (
 SELECT DISTINCT ON (tag_id,purpose) queue_sha256 FROM public.tagger_review_set
 WHERE schema_version=3 ORDER BY tag_id,purpose,created_at DESC,queue_sha256
)
SELECT q.queue_sha256,q.tag_id,q.purpose,q.frame_sha256,q.model_sha256,q.created_at,
       q.schema_version,q.blind,q.origin,q.lane,q.review_kind,q.adjudication,q.snapshot_sha256
FROM public.tagger_review_set q
WHERE (q.schema_version<>3 OR q.queue_sha256 IN (SELECT queue_sha256 FROM direct))
 AND NOT EXISTS (SELECT FROM public.tagger_review_set replacement
                 WHERE replacement.supersedes_queue_sha256=q.queue_sha256)
 AND NOT EXISTS (SELECT FROM public.tagger_review_set_retirement r WHERE r.queue_sha256=q.queue_sha256)
"""


# The transaction holds registration and response locks for both reads.
INVENTORY = """
SELECT jsonb_build_object(
 'sets', (SELECT count(*) FROM public.tagger_review_set),
 'requests_sha256', (SELECT encode(sha256(convert_to(coalesce(string_agg(
     queue_sha256||':'||encode(sha256(document),'hex'), E'\n' ORDER BY queue_sha256),''),'UTF8')),'hex')
     FROM public.tagger_review_set),
 'members', (SELECT count(*) FROM public.tagger_review_set_member),
 'members_sha256', (SELECT encode(sha256(convert_to(coalesce(string_agg(
     to_jsonb(m)::text, E'\n' ORDER BY queue_sha256,ordinal),''),'UTF8')),'hex')
     FROM public.tagger_review_set_member m),
 'answers', (SELECT count(*) FROM public.tagger_human_review_event),
 'answers_sha256', (SELECT encode(sha256(convert_to(coalesce(string_agg(
     encode(sha256(convert_to(to_jsonb(e)::text,'UTF8')),'hex'), E'\n' ORDER BY id),''),'UTF8')),'hex')
     FROM public.tagger_human_review_event e),
 'retired', (SELECT count(*) FROM public.tagger_review_set_retirement),
 'retired_sha256', (SELECT encode(sha256(convert_to(coalesce(string_agg(
     to_jsonb(r)::text, E'\n' ORDER BY queue_sha256),''),'UTF8')),'hex')
     FROM public.tagger_review_set_retirement r)
)
"""


def registration_sql():
    sql = prior.registration_sql()
    replacements = {
        "convert_from(document,'UTF8')::jsonb->>'schema'<>'human-review-set/3'": "schema_version<>3",
        "convert_from(document,'UTF8')::jsonb->>'schema'='human-review-set/1'": "schema_version=1",
        "NOT public.tagger_is_adjudication(convert_from(document,'UTF8')::jsonb)": "NOT adjudication",
        """public.tagger_is_lora_review(
                tagger_review_set.queue_sha256, convert_from(document,'UTF8')::jsonb
            )""": "(lane='lora_findings')",
        "FROM public.tagger_human_review_event e": "FROM public.tagger_review_member_answer e",
    }
    for before, after in replacements.items():
        assert sql.count(before) == 1, before
        sql = sql.replace(before, after)
    return sql


def upgrade():
    op.execute("SET LOCAL lock_timeout='2s'")
    op.execute("SET LOCAL statement_timeout='30s'")
    # Parent and event schema ownership differ. The event table is owned by kura.
    op.execute("SELECT pg_advisory_xact_lock(hashtextextended('tagger_review_set_registry',0))")
    op.execute("LOCK TABLE public.tagger_human_review_event IN SHARE ROW EXCLUSIVE MODE")
    before = op.get_bind().exec_driver_sql(INVENTORY).scalar_one()
    op.execute("GRANT SELECT (id, decision), REFERENCES (id) ON public.tagger_human_review_event TO kura_reconciler")
    for sql in (
        SET_COLUMNS, POPULATE,
        "ALTER TABLE public.tagger_review_set DISABLE TRIGGER tagger_review_set_immutable",
        """CREATE TRIGGER tagger_review_set_index BEFORE INSERT OR UPDATE OF document
           ON public.tagger_review_set FOR EACH ROW EXECUTE FUNCTION public.tagger_index_review_set()""",
        "UPDATE public.tagger_review_set SET document=document",
        "ALTER TABLE public.tagger_review_set ENABLE TRIGGER tagger_review_set_immutable",
        """ALTER TABLE public.tagger_review_set
           ALTER COLUMN schema_version SET NOT NULL, ALTER COLUMN frozen_tag_name SET NOT NULL,
           ALTER COLUMN origin SET NOT NULL,
           ALTER COLUMN lane SET NOT NULL, ALTER COLUMN review_kind SET NOT NULL,
           ALTER COLUMN adjudication SET NOT NULL,
           ADD COLUMN blind boolean GENERATED ALWAYS AS (schema_version=3) STORED,
           ADD CONSTRAINT review_schema_known CHECK (schema_version BETWEEN 1 AND 3),
           ADD CONSTRAINT review_origin_known CHECK (origin IN ('legacy','lifecycle','lora_findings')),
           ADD CONSTRAINT review_lane_matches_origin CHECK (
             lane=CASE WHEN origin='lora_findings' THEN 'lora_findings' ELSE 'lifecycle' END),
           ADD CONSTRAINT review_kind_known CHECK (review_kind IN (
             'adjudication','blind_comparison','prepublication','training_support','age_check','findings','recheck'))""",
        """CREATE INDEX tagger_review_catalog_direct ON public.tagger_review_set
           (tag_id,purpose,created_at DESC,queue_sha256) WHERE schema_version=3""",
        """CREATE INDEX tagger_review_catalog_predecessor ON public.tagger_review_set
           (supersedes_queue_sha256) WHERE supersedes_queue_sha256 IS NOT NULL""",
        """CREATE TABLE public.tagger_review_cohort (
           snapshot_sha256 text PRIMARY KEY CHECK (snapshot_sha256 ~ '^[0-9a-f]{64}$'),
           kind text NOT NULL CHECK (kind IN ('base_switch','independent_holdout')))""",
        """CREATE TABLE public.tagger_review_member_answer (
           queue_sha256 text NOT NULL, post_id integer NOT NULL,
           event_id bigint NOT NULL REFERENCES public.tagger_human_review_event(id),
           decision text NOT NULL CHECK (decision IN ('present','absent','uncertain')),
           PRIMARY KEY(queue_sha256,post_id), UNIQUE(event_id),
           FOREIGN KEY(queue_sha256,post_id) REFERENCES public.tagger_review_set_member(queue_sha256,post_id))""",
        """INSERT INTO public.tagger_review_member_answer
           SELECT DISTINCT ON (queue_sha256,post_id) queue_sha256,post_id,id,decision
           FROM public.tagger_human_review_event
           ORDER BY queue_sha256,post_id,id DESC""",
        """CREATE FUNCTION public.tagger_index_review_answer() RETURNS trigger
           LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,public AS $$
           BEGIN
             INSERT INTO public.tagger_review_member_answer AS current_answer VALUES(NEW.queue_sha256,NEW.post_id,NEW.id,NEW.decision)
             ON CONFLICT (queue_sha256,post_id) DO UPDATE SET event_id=EXCLUDED.event_id,decision=EXCLUDED.decision
             WHERE current_answer.event_id<EXCLUDED.event_id;
             RETURN NEW;
           END $$""",
        "REVOKE ALL ON FUNCTION public.tagger_index_review_answer() FROM PUBLIC",
        "GRANT EXECUTE ON FUNCTION public.tagger_index_review_answer() TO kura",
        CURRENT,
        "CREATE VIEW public.tagger_review_dashboard_counts WITH (security_invoker=true) AS " + POPULATION,
        "REVOKE ALL ON public.tagger_review_dashboard_counts FROM PUBLIC",
        "GRANT SELECT ON public.tagger_review_dashboard_counts TO kura",
        "REVOKE ALL ON public.tagger_review_current_set,public.tagger_review_member_answer,public.tagger_review_cohort FROM PUBLIC",
        """DO $$ BEGIN IF EXISTS (SELECT FROM pg_roles WHERE rolname='replicator') THEN
           REVOKE ALL ON public.tagger_review_current_set,public.tagger_review_member_answer,public.tagger_review_cohort FROM replicator;
           END IF; END $$""",
        "GRANT SELECT ON public.tagger_review_current_set,public.tagger_review_member_answer,public.tagger_review_cohort TO kura",
        registration_sql(),
    ):
        as_owner(op, "kura_reconciler", sql)
    op.execute("""CREATE TRIGGER tagger_review_answer_index AFTER INSERT ON public.tagger_human_review_event
                 FOR EACH ROW EXECUTE FUNCTION public.tagger_index_review_answer()""")
    after = op.get_bind().exec_driver_sql(INVENTORY).scalar_one()
    if before != after:
        raise RuntimeError("review_catalog_inventory_changed")
    mismatches = op.get_bind().exec_driver_sql("""
        WITH expected AS (
            SELECT DISTINCT ON (queue_sha256,post_id) queue_sha256,post_id,id,decision
            FROM public.tagger_human_review_event ORDER BY queue_sha256,post_id,id DESC
        )
        SELECT count(*) FROM expected e FULL JOIN public.tagger_review_member_answer a USING (queue_sha256,post_id)
        WHERE (e.id,e.decision) IS DISTINCT FROM (a.event_id,a.decision)
    """).scalar_one()
    if mismatches:
        raise RuntimeError("review_catalog_latest_answer_mismatch")
    as_owner(op, "kura_reconciler", """
        CREATE TABLE public.tagger_review_catalog_receipt (
            revision text PRIMARY KEY, inventory jsonb NOT NULL, verified_at timestamptz NOT NULL DEFAULT now())
    """)
    as_owner(op, "kura_reconciler", "GRANT SELECT ON public.tagger_review_catalog_receipt TO kura")
    as_owner(op, "kura_reconciler", "GRANT INSERT ON public.tagger_review_catalog_receipt TO kura")
    bind = op.get_bind()
    from sqlalchemy import text
    import json
    bind.execute(text("INSERT INTO public.tagger_review_catalog_receipt(revision,inventory) VALUES ('0216',CAST(:body AS jsonb))"),
                 {"body": json.dumps(after)})
    as_owner(op, "kura_reconciler", "REVOKE INSERT ON public.tagger_review_catalog_receipt FROM kura")


def downgrade():
    raise RuntimeError("review_set_history_is_not_destructively_downgraded")
