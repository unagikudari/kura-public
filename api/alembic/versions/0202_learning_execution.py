"""Canonical fenced learning execution and registration outbox."""
from alembic import op
from api.alembic.owner import as_owner
revision = "0202"
down_revision = "0201"
branch_labels = None
depends_on = None
# Both execution tables are canonical-only; replica APIs expose no observation.
REPLICA_DDL = None

TABLES = (
"""CREATE TABLE public.tagger_learning_execution (
 event_id bigint PRIMARY KEY REFERENCES public.tagger_lifecycle_event(id),
 tag_id integer NOT NULL REFERENCES public.tag(id),
 revision bigint NOT NULL CHECK(revision>0),
 state text NOT NULL CHECK(state IN ('measuring','registration_pending','retry_wait','registered','refused','settled')),
 failure_count integer NOT NULL CHECK(failure_count BETWEEN 0 AND 1000000),
 retry_after timestamptz,
 pending_payload jsonb,
 payload_sha256 text,
 reason text NOT NULL CHECK(reason IN ('started','payload_ready','child_failed','registration_unavailable','registration_failed','human_hold','registered','registration_refused','settled')),
 updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 CHECK((pending_payload IS NULL)=(payload_sha256 IS NULL)),
 CHECK(pending_payload IS NULL OR (jsonb_typeof(pending_payload)='object'
   AND octet_length(pending_payload::text)<=1048576
   AND payload_sha256=encode(sha256(convert_to(pending_payload::text,'UTF8')),'hex'))),
 CHECK((state='retry_wait')=(retry_after IS NOT NULL)),
 CHECK(state NOT IN ('registration_pending','registered') OR pending_payload IS NOT NULL)
)""",
"""CREATE TABLE public.tagger_learning_execution_event (
 event_id bigint NOT NULL REFERENCES public.tagger_learning_execution(event_id),
 revision bigint NOT NULL, state text NOT NULL, reason text NOT NULL,
 failure_count integer NOT NULL, retry_after timestamptz, payload_sha256 text,
 fencing_token bigint NOT NULL, occurred_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 PRIMARY KEY(event_id,revision)
)""",
"CREATE INDEX tagger_learning_execution_due ON public.tagger_learning_execution(retry_after) WHERE state='retry_wait'",
"REVOKE ALL ON public.tagger_learning_execution,public.tagger_learning_execution_event FROM PUBLIC,kura",
"GRANT SELECT ON public.tagger_learning_execution,public.tagger_learning_execution_event TO kura",
"""DO $$ BEGIN IF EXISTS(SELECT FROM pg_roles WHERE rolname='replicator') THEN
 REVOKE ALL ON public.tagger_learning_execution,public.tagger_learning_execution_event FROM replicator;
 END IF; END $$""",
)

FUNCTION = """
CREATE FUNCTION public.tagger_learning_step(
 p_event bigint,p_tag text,p_token bigint,p_revision bigint,p_action text,p_payload jsonb DEFAULT NULL)
RETURNS public.tagger_learning_execution LANGUAGE plpgsql SECURITY DEFINER
SET search_path=pg_catalog,public AS $$
DECLARE e public.tagger_lifecycle_event%ROWTYPE; r public.tagger_learning_execution%ROWTYPE;
 latest bigint; ns text; why text; failures integer; retry timestamptz; body jsonb;
BEGIN
 IF p_event IS NULL OR p_tag IS NULL OR p_token IS NULL OR p_revision IS NULL OR
 p_revision<0 OR p_action IS NULL THEN RAISE EXCEPTION 'learning_input_invalid'; END IF;
 PERFORM public.tagger_check_fencing('lifecycle:'||p_tag,p_token);
 IF NOT EXISTS(SELECT FROM public.tagger_lifecycle_lock
   WHERE scope='lifecycle:'||p_tag AND fencing_token=p_token AND expires_at>clock_timestamp())
 THEN RAISE EXCEPTION 'learning_lease_expired'; END IF;
 SELECT * INTO e FROM public.tagger_lifecycle_event WHERE id=p_event;
 IF NOT FOUND OR e.tag<>p_tag OR e.to_stage<>'training' THEN RAISE EXCEPTION 'learning_event_mismatch'; END IF;
 SELECT * INTO r FROM public.tagger_learning_execution WHERE event_id=p_event FOR UPDATE;
 IF COALESCE(r.revision,0)<>p_revision THEN RAISE EXCEPTION 'learning_revision_changed'; END IF;
 IF r.state IN ('registered','refused','settled') THEN RAISE EXCEPTION 'learning_terminal'; END IF;
 SELECT id INTO latest FROM public.tagger_lifecycle_event WHERE tag_id=e.tag_id ORDER BY occurred_at DESC,id DESC LIMIT 1;
 IF latest<>p_event AND p_action NOT IN ('settle','registered') THEN RAISE EXCEPTION 'learning_event_superseded'; END IF;
 IF r.retry_after>clock_timestamp() AND p_action NOT IN ('registered','settle')
 THEN RAISE EXCEPTION 'learning_backoff_active'; END IF;
 failures:=COALESCE(r.failure_count,0);body:=r.pending_payload;
 IF p_action='start' THEN
  IF p_payload IS NOT NULL OR body IS NOT NULL THEN RAISE EXCEPTION 'learning_payload_pending'; END IF;
  IF r.retry_after>clock_timestamp() THEN RAISE EXCEPTION 'learning_backoff_active'; END IF;
  ns:='measuring';why:='started';
 ELSIF p_action IN ('payload','import') THEN
  IF p_payload IS NULL OR jsonb_typeof(p_payload)<>'object' OR octet_length(p_payload::text)>1048576 OR
   (p_payload->>'tag'=p_tag AND p_payload->'lifecycle_event_id'=to_jsonb(p_event)) IS NOT TRUE
   THEN RAISE EXCEPTION 'learning_payload_invalid'; END IF;
  IF body IS NOT NULL AND body<>p_payload THEN RAISE EXCEPTION 'learning_payload_changed'; END IF;
  IF p_action='import' AND r.state IS NOT NULL THEN RAISE EXCEPTION 'learning_already_started'; END IF;
  IF p_action='payload' AND r.state IS NULL THEN RAISE EXCEPTION 'learning_not_started'; END IF;
  body:=p_payload;ns:='registration_pending';why:='payload_ready';
 ELSIF p_action IN ('child_failed','registration_unavailable','registration_failed','human_hold') THEN
  IF r.state IS NULL OR p_payload IS NOT NULL THEN RAISE EXCEPTION 'learning_not_started'; END IF;
  IF p_action LIKE 'registration_%' AND body IS NULL THEN RAISE EXCEPTION 'learning_payload_missing'; END IF;
  failures:=LEAST(failures+1,1000000);ns:='retry_wait';why:=p_action;
  retry:=clock_timestamp()+make_interval(secs=>CASE WHEN p_action='human_hold' THEN 86400
    ELSE LEAST(1800*power(2,LEAST(failures-1,6)),86400)::integer END);
 ELSIF p_action='registered' THEN
  IF body IS NULL OR NOT EXISTS(SELECT FROM public.tagger_registration_request
    WHERE lifecycle_event_id=p_event AND tag_id=e.tag_id) THEN RAISE EXCEPTION 'learning_registration_missing'; END IF;
  IF EXISTS(SELECT FROM public.tagger_registration_request
    WHERE lifecycle_event_id=p_event AND tag_id=e.tag_id
    AND source_host IS NOT DISTINCT FROM body->>'source_host'
    AND artifact_ref IS NOT DISTINCT FROM body->>'artifact_ref'
    AND artifact_sha256 IS NOT DISTINCT FROM body->>'artifact_sha256'
    AND metrics IS NOT DISTINCT FROM NULLIF(body->'metrics','null'::jsonb)
    AND gt IS NOT DISTINCT FROM NULLIF(body->'gt','null'::jsonb))
  THEN ns:='registered';why:='registered';
  ELSE ns:='refused';why:='registration_refused'; END IF;
 ELSIF p_action='refuse' THEN ns:='refused';why:='registration_refused';
 ELSIF p_action='settle' THEN
  IF latest=p_event AND NOT EXISTS(SELECT FROM public.tagger_registration_request WHERE lifecycle_event_id=p_event AND tag_id=e.tag_id)
  THEN RAISE EXCEPTION 'learning_event_still_pending'; END IF;
  ns:='settled';why:='settled';
 ELSE RAISE EXCEPTION 'learning_action_invalid'; END IF;
 IF p_action NOT IN ('payload','import') AND p_payload IS NOT NULL THEN RAISE EXCEPTION 'learning_unexpected_payload'; END IF;
 INSERT INTO public.tagger_learning_execution(event_id,tag_id,revision,state,failure_count,retry_after,pending_payload,payload_sha256,reason)
 VALUES(p_event,e.tag_id,p_revision+1,ns,failures,retry,body,
  CASE WHEN body IS NULL THEN NULL ELSE encode(sha256(convert_to(body::text,'UTF8')),'hex') END,why)
 ON CONFLICT(event_id) DO UPDATE SET revision=EXCLUDED.revision,state=EXCLUDED.state,
 failure_count=EXCLUDED.failure_count,retry_after=EXCLUDED.retry_after,pending_payload=EXCLUDED.pending_payload,
 payload_sha256=EXCLUDED.payload_sha256,reason=EXCLUDED.reason,updated_at=clock_timestamp() RETURNING * INTO r;
 INSERT INTO public.tagger_learning_execution_event(event_id,revision,state,reason,failure_count,retry_after,payload_sha256,fencing_token)
 VALUES(r.event_id,r.revision,r.state,r.reason,r.failure_count,r.retry_after,r.payload_sha256,p_token);
 RETURN r;
END $$"""

def upgrade():
    op.execute("SET LOCAL lock_timeout='2s'")
    as_owner(op, "kura", "GRANT REFERENCES ON public.tagger_lifecycle_event,public.tag TO kura_reconciler")
    for sql in (*TABLES, FUNCTION,
        "REVOKE ALL ON FUNCTION public.tagger_learning_step(bigint,text,bigint,bigint,text,jsonb) FROM PUBLIC",
        "GRANT EXECUTE ON FUNCTION public.tagger_learning_step(bigint,text,bigint,bigint,text,jsonb) TO kura"):
        as_owner(op, "kura_reconciler", sql)

def downgrade():
    raise RuntimeError("learning_execution_requires_forward_migration")
