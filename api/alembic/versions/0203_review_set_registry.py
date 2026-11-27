"""Canonical immutable review sets and indexed population membership."""
from alembic import op
from api.alembic.owner import as_owner
from api.services.review_tag_contract_v1 import sql_function

revision = "0203"
down_revision = "0202"
branch_labels = None
depends_on = None
REPLICA_DDL = None

STATEMENTS = (
    """CREATE TABLE public.tagger_review_set (
        queue_sha256 text PRIMARY KEY CHECK(queue_sha256~'^[a-f0-9]{64}$'),
        document bytea NOT NULL CHECK(octet_length(document) BETWEEN 1 AND 131072),
        tag_id integer NOT NULL REFERENCES public.tag(id),
        purpose text NOT NULL CHECK(purpose IN ('fixed_evaluation','production_precision',
          'training_support','boundary_recheck','candidate_evaluation','historical_label_recheck')),
        frame_sha256 text NOT NULL CHECK(frame_sha256~'^[a-f0-9]{64}$'),
        model_sha256 text CHECK(model_sha256~'^[a-f0-9]{64}$'),
        created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
        CHECK(queue_sha256=encode(sha256(document),'hex'))
    )""",
    """COMMENT ON TABLE public.tagger_review_set IS
        'Canonical review request identity. Original bytes preserve existing response/score references. Not labels, independent evaluation proof or publication authority.'""",
    """CREATE TABLE public.tagger_review_set_member (
        queue_sha256 text NOT NULL REFERENCES public.tagger_review_set(queue_sha256),
        ordinal integer NOT NULL CHECK(ordinal BETWEEN 0 AND 499),
        post_id integer NOT NULL CHECK(post_id>0),
        display_sha256 text CHECK(display_sha256~'^[a-f0-9]{64}$'),
        PRIMARY KEY(queue_sha256,post_id), UNIQUE(queue_sha256,ordinal)
    )""",
    "CREATE INDEX tagger_review_set_member_post ON public.tagger_review_set_member(post_id,queue_sha256)",
    """CREATE FUNCTION public.tagger_review_set_immutable() RETURNS trigger
        LANGUAGE plpgsql SET search_path=pg_catalog AS $$
        BEGIN RAISE EXCEPTION 'review_set_immutable'; END $$""",
    """CREATE TRIGGER tagger_review_set_immutable BEFORE UPDATE OR DELETE OR TRUNCATE
       ON public.tagger_review_set FOR EACH STATEMENT EXECUTE FUNCTION public.tagger_review_set_immutable()""",
    """CREATE TRIGGER tagger_review_member_immutable BEFORE UPDATE OR DELETE OR TRUNCATE
       ON public.tagger_review_set_member FOR EACH STATEMENT EXECUTE FUNCTION public.tagger_review_set_immutable()""",
    """CREATE FUNCTION public.tagger_review_set_consistent() RETURNS trigger
       LANGUAGE plpgsql SET search_path=pg_catalog,public AS $$
       DECLARE q public.tagger_review_set%ROWTYPE; d jsonb; ref text;
       BEGIN
         ref:=NEW.queue_sha256;
         SELECT * INTO q FROM public.tagger_review_set WHERE queue_sha256=ref;
         d:=convert_from(q.document,'UTF8')::jsonb;
         IF (d->>'schema' IN ('human-review-set/1','human-review-set/2')
             AND d->>'purpose'=q.purpose AND d->>'frame_sha256'=q.frame_sha256
             AND (d->>'model_sha256') IS NOT DISTINCT FROM q.model_sha256
             AND jsonb_typeof(d->'post_ids')='array') IS NOT TRUE
         THEN RAISE EXCEPTION 'review_set_identity_mismatch'; END IF;
         IF NOT EXISTS(SELECT FROM public.tag_name WHERE tag_id=q.tag_id AND name=d->>'tag' AND ord=0)
         THEN RAISE EXCEPTION 'review_set_tag_mismatch'; END IF;
         IF (SELECT count(*) FROM public.tagger_review_set_member WHERE queue_sha256=ref)
              <>jsonb_array_length(d->'post_ids')
           OR EXISTS(SELECT FROM public.tagger_review_set_member m WHERE m.queue_sha256=ref
             AND (to_jsonb(m.post_id) IS DISTINCT FROM d->'post_ids'->m.ordinal
              OR m.display_sha256 IS DISTINCT FROM d->'media_sha256'->>m.post_id::text))
         THEN RAISE EXCEPTION 'review_set_population_mismatch'; END IF;
         RETURN NULL;
       END $$""",
    """CREATE CONSTRAINT TRIGGER tagger_review_set_consistent AFTER INSERT ON public.tagger_review_set
       DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION public.tagger_review_set_consistent()""",
    """CREATE CONSTRAINT TRIGGER tagger_review_member_consistent AFTER INSERT ON public.tagger_review_set_member
       DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION public.tagger_review_set_consistent()""",
    """CREATE TABLE public.tagger_review_registry_activation(
      singleton boolean PRIMARY KEY CHECK(singleton),
      inventory_sha256 text NOT NULL CHECK(inventory_sha256~'^[a-f0-9]{64}$'),
      initial_sets integer NOT NULL CHECK(initial_sets>0),
      activated_at timestamptz NOT NULL DEFAULT clock_timestamp())""",
    """CREATE TRIGGER tagger_review_activation_immutable BEFORE UPDATE OR DELETE OR TRUNCATE
      ON public.tagger_review_registry_activation FOR EACH STATEMENT EXECUTE FUNCTION public.tagger_review_set_immutable()""",
    "REVOKE ALL ON public.tagger_review_set,public.tagger_review_set_member,public.tagger_review_registry_activation FROM PUBLIC,kura",
    "GRANT SELECT ON public.tagger_review_set,public.tagger_review_set_member,public.tagger_review_registry_activation TO kura",
    """DO $$ BEGIN IF EXISTS(SELECT FROM pg_roles WHERE rolname='replicator') THEN
       REVOKE ALL ON public.tagger_review_set,public.tagger_review_set_member FROM replicator;
       REVOKE ALL ON public.tagger_review_registry_activation FROM replicator;
       END IF; END $$""",
)

REGISTER = """
CREATE FUNCTION public.tagger_register_review_set(raw bytea) RETURNS text
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,public AS $$
DECLARE d jsonb; j json; ref text; tid integer; explicit boolean; allowed text[];
 count_ids integer; url_bytes bigint; query_text text; expected jsonb; prov jsonb; old jsonb; purpose text;
BEGIN
 PERFORM pg_advisory_xact_lock(hashtextextended('tagger_review_set_registry',0));
 IF raw IS NULL OR octet_length(raw) NOT BETWEEN 1 AND 131072 THEN RAISE EXCEPTION 'review_document_invalid'; END IF;
 j:=convert_from(raw,'UTF8')::json; d:=j::jsonb; ref:=encode(sha256(raw),'hex');
 IF jsonb_typeof(d)<>'object' THEN RAISE EXCEPTION 'review_document_invalid'; END IF;
 explicit:=d->>'schema'='human-review-set/2';
 IF (d->>'schema' IN ('human-review-set/1','human-review-set/2')) IS NOT TRUE THEN RAISE EXCEPTION 'review_schema_invalid'; END IF;
 allowed:=ARRAY['schema','tag','purpose','frame_sha256','model_sha256','post_ids'];
 IF explicit THEN allowed:=allowed||ARRAY['source_provenance','media_sha256']; END IF;
 IF (SELECT count(*) FROM json_object_keys(j))<>cardinality(allowed)
   OR EXISTS(SELECT FROM jsonb_object_keys(d) k WHERE NOT k=ANY(allowed))
 THEN RAISE EXCEPTION 'review_keys_invalid'; END IF;
 purpose:=d->>'purpose';
 IF (purpose=ANY(CASE WHEN explicit THEN ARRAY['historical_label_recheck','candidate_evaluation','training_support']
   ELSE ARRAY['fixed_evaluation','production_precision','training_support','boundary_recheck','candidate_evaluation'] END)) IS NOT TRUE
 THEN RAISE EXCEPTION 'review_purpose_invalid'; END IF;
 IF (jsonb_typeof(d->'tag')='string' AND public.tagger_review_tag_valid(d->>'tag') AND jsonb_typeof(d->'frame_sha256')='string' AND d->>'frame_sha256'~'^[a-f0-9]{64}$'
   AND ((jsonb_typeof(d->'model_sha256')='string' AND d->>'model_sha256'~'^[a-f0-9]{64}$') OR
     (d->'model_sha256'='null'::jsonb AND purpose IN ('training_support','historical_label_recheck')))
   AND jsonb_typeof(d->'post_ids')='array') IS NOT TRUE
 THEN RAISE EXCEPTION 'review_identity_invalid'; END IF;
 SELECT tag_id INTO tid FROM public.tag_name WHERE name=d->>'tag' AND ord=0;
 IF tid IS NULL THEN RAISE EXCEPTION 'review_tag_unresolved'; END IF;
 count_ids:=jsonb_array_length(d->'post_ids');
 IF count_ids NOT BETWEEN 1 AND 500 OR EXISTS(SELECT FROM json_array_elements(j->'post_ids') x
    WHERE json_typeof(x)<>'number' OR x::text !~ '^[0-9]+$' OR (x::text)::numeric NOT BETWEEN 1 AND 2147483647)
   OR (SELECT count(DISTINCT x) FROM jsonb_array_elements(d->'post_ids') x)<>count_ids
 THEN RAISE EXCEPTION 'review_population_invalid'; END IF;
 IF NOT explicit THEN
   SELECT 'ids:'||string_agg(id,',' ORDER BY n)||' -locked:'||(d->>'tag')||' -removed:'||(d->>'tag')
   INTO query_text FROM jsonb_array_elements_text(d->'post_ids') WITH ORDINALITY ids(id,n);
   SELECT 4+16+sum(CASE WHEN b BETWEEN 65 AND 90 OR b BETWEEN 97 AND 122 OR b BETWEEN 48 AND 57 OR b IN (32,45,46,95,126)
                       THEN 1 ELSE 3 END)
   INTO url_bytes FROM (SELECT get_byte(v,i) b FROM
     (VALUES(convert_to(query_text,'UTF8')),(convert_to(d->>'tag','UTF8'))) values_to_quote(v),
     LATERAL generate_series(0,octet_length(v)-1) offsets(i)) quoted;
   IF url_bytes>7500 THEN RAISE EXCEPTION 'review_link_too_long'; END IF;
 END IF;
 IF explicit THEN
   prov:=d->'source_provenance';
   IF (jsonb_typeof(prov)='object' AND jsonb_typeof(d->'media_sha256')='object'
       AND jsonb_typeof(prov->'artifact_sha256')='string' AND prov->>'artifact_sha256'~'^[a-f0-9]{64}$') IS NOT TRUE THEN RAISE EXCEPTION 'review_provenance_invalid'; END IF;
   expected:=jsonb_build_object('kind',CASE purpose WHEN 'candidate_evaluation' THEN 'frozen_candidate_sample'
      WHEN 'training_support' THEN 'frozen_training_support' ELSE 'historical_human_training' END,
      'artifact_sha256',prov->'artifact_sha256');
   IF purpose IN ('candidate_evaluation','training_support') THEN
     IF (jsonb_typeof(prov->'supersedes_queue_sha256')='string' AND prov->>'supersedes_queue_sha256'~'^[a-f0-9]{64}$') IS NOT TRUE THEN RAISE EXCEPTION 'review_provenance_invalid'; END IF;
     expected:=expected||jsonb_build_object('supersedes_queue_sha256',prov->'supersedes_queue_sha256');
     IF EXISTS(SELECT FROM public.tagger_review_set
       WHERE queue_sha256<>ref AND convert_from(document,'UTF8')::jsonb->'source_provenance'->>'supersedes_queue_sha256'=prov->>'supersedes_queue_sha256')
     THEN RAISE EXCEPTION 'review_supersession_ambiguous'; END IF;
     SELECT convert_from(document,'UTF8')::jsonb INTO old FROM public.tagger_review_set
       WHERE queue_sha256=prov->>'supersedes_queue_sha256';
     IF old IS NULL OR old->>'schema'<>'human-review-set/1'
       OR (old-ARRAY['schema']) IS DISTINCT FROM (d-ARRAY['schema','source_provenance','media_sha256'])
     THEN RAISE EXCEPTION 'review_supersession_invalid'; END IF;
   END IF;
   IF prov<>expected OR (SELECT count(*) FROM json_object_keys(j->'source_provenance'))<>(SELECT count(*) FROM jsonb_object_keys(expected))
     OR (purpose='training_support' AND d->'model_sha256'<>'null'::jsonb)
   THEN RAISE EXCEPTION 'review_provenance_invalid'; END IF;
   IF (SELECT count(*) FROM json_object_keys(j->'media_sha256'))<>count_ids
     OR (SELECT count(*) FROM jsonb_object_keys(d->'media_sha256'))<>count_ids
     OR EXISTS(SELECT FROM jsonb_array_elements_text(d->'post_ids') id
       WHERE (jsonb_typeof(d->'media_sha256'->id)='string' AND d->'media_sha256'->>id~'^[a-f0-9]{64}$') IS NOT TRUE)
   THEN RAISE EXCEPTION 'review_media_invalid'; END IF;
 END IF;
 ref:=encode(sha256(raw),'hex');
 IF EXISTS(SELECT FROM public.tagger_review_set WHERE queue_sha256=ref) THEN RETURN ref; END IF;
 IF (SELECT count(*) FROM public.tagger_review_set)>=20 THEN RAISE EXCEPTION 'review_registry_capacity'; END IF;
 SET CONSTRAINTS tagger_review_set_consistent,tagger_review_member_consistent DEFERRED;
 INSERT INTO public.tagger_review_set(queue_sha256,document,tag_id,purpose,frame_sha256,model_sha256)
 VALUES(ref,raw,tid,purpose,d->>'frame_sha256',d->>'model_sha256');
 INSERT INTO public.tagger_review_set_member(queue_sha256,ordinal,post_id,display_sha256)
 SELECT ref,(n-1)::integer,id::integer,d->'media_sha256'->>id FROM jsonb_array_elements_text(d->'post_ids') WITH ORDINALITY ids(id,n);
 SET CONSTRAINTS tagger_review_set_consistent,tagger_review_member_consistent IMMEDIATE;
 RETURN ref;
END $$"""

ACTIVATE = """
CREATE FUNCTION public.tagger_activate_review_registry() RETURNS void LANGUAGE plpgsql
SECURITY DEFINER SET search_path=pg_catalog,public AS $$
DECLARE n integer; sha text;
BEGIN
 PERFORM pg_advisory_xact_lock(hashtextextended('tagger_review_set_registry',0));
 SELECT count(*),encode(sha256(convert_to(string_agg(queue_sha256,E'\n' ORDER BY queue_sha256),'UTF8')),'hex')
 INTO n,sha FROM public.tagger_review_set;
 IF n=0 THEN RAISE EXCEPTION 'review_registry_empty'; END IF;
 INSERT INTO public.tagger_review_registry_activation(singleton,inventory_sha256,initial_sets)
 VALUES(true,sha,n) ON CONFLICT DO NOTHING;
END $$"""

def upgrade():
    op.execute("SET LOCAL lock_timeout='2s'")
    as_owner(op,"kura","GRANT REFERENCES ON public.tag TO kura_reconciler")
    for statement in (*STATEMENTS,sql_function(),REGISTER,ACTIVATE,
        "REVOKE ALL ON FUNCTION public.tagger_register_review_set(bytea),public.tagger_activate_review_registry() FROM PUBLIC",
        "GRANT EXECUTE ON FUNCTION public.tagger_register_review_set(bytea),public.tagger_activate_review_registry() TO kura"):
        as_owner(op,"kura_reconciler",statement)

def downgrade():
    raise RuntimeError("review_set_history_requires_forward_migration")
