"""Canonical-only qualification receipt intake; no lifecycle or publish authority.

Original registrations remain immutable/terminal. Caller reports are assertions,
not human truth, independent review, fleet qualification or admission.
"""
from alembic import op

revision = '0199'
down_revision = '0198'
branch_labels = None
depends_on = None
REPLICA_DDL = None

STATEMENTS = (
    "DO $$ BEGIN IF current_user <> 'kura' THEN RAISE EXCEPTION 'qualification_migration_requires_kura'; END IF; END $$",
    """CREATE TABLE public.tagger_qualification_evidence (
        receipt_sha256 text PRIMARY KEY CHECK (receipt_sha256 ~ '^[0-9a-f]{64}$'),
        registration_id bigint NOT NULL REFERENCES public.tagger_registration_request(id),
        context_sha256 text NOT NULL CHECK (context_sha256 ~ '^[0-9a-f]{64}$'),
        context_canonical text NOT NULL CHECK (octet_length(context_canonical) <= 262144),
        context jsonb NOT NULL CHECK (jsonb_typeof(context) = 'object'),
        canonical_body text NOT NULL CHECK (octet_length(canonical_body) <= 524288),
        body jsonb NOT NULL CHECK (jsonb_typeof(body) = 'object'),
        submitted_by integer NOT NULL CHECK (submitted_by > 0),
        received_at timestamptz NOT NULL DEFAULT transaction_timestamp(),
        CHECK (context_canonical::jsonb = context),
        CHECK (context_sha256 = encode(sha256(convert_to(context_canonical,'UTF8')), 'hex')),
        CHECK (canonical_body::jsonb = body),
        CHECK (receipt_sha256 = encode(sha256(convert_to(canonical_body,'UTF8')), 'hex')),
        CHECK ((body->>'schema' = 'qualification-evidence/1'
            AND body->>'provenance' = 'caller_assertion'
            AND body->>'registration_id' = registration_id::text
            AND body->>'registration_context_sha256' = context_sha256
            AND body->>'kind' IN ('human_sample','runtime','independent_review')
            AND body->'report'->>'artifact_sha256' = context->>'artifact_sha256'
            AND body->'report'->'threshold' = context->'metrics'->'threshold') IS TRUE)
    )""",
    "CREATE INDEX tagger_qualification_evidence_registration ON public.tagger_qualification_evidence(registration_id, received_at, receipt_sha256)",
    """COMMENT ON TABLE public.tagger_qualification_evidence IS
        'Canonical-only DML-immutable caller assertions; owner can alter schema. API validates closed report shapes and authenticated submitter. Does not approve, reopen, retrain, publish or change holds. First submitter/time survive identical replay.'""",
    """CREATE FUNCTION public.tagger_qualification_evidence_guard() RETURNS trigger
    LANGUAGE plpgsql SET search_path = pg_catalog, public AS $$
    DECLARE r public.tagger_registration_request%ROWTYPE; expected jsonb;
    BEGIN
        SELECT * INTO r FROM public.tagger_registration_request WHERE id=NEW.registration_id FOR SHARE;
        IF NOT FOUND THEN RAISE EXCEPTION 'qualification_registration_missing'; END IF;
        expected := jsonb_build_object('id',r.id,'tag',r.tag,'tag_id',r.tag_id,
            'lifecycle_event_id',r.lifecycle_event_id,'artifact_sha256',r.artifact_sha256,
            'metrics',r.metrics,'gt',r.gt);
        IF NEW.context IS DISTINCT FROM expected THEN
            RAISE EXCEPTION 'qualification_context_mismatch';
        END IF;
        RETURN NEW;
    END $$""",
    """CREATE TRIGGER tagger_qualification_evidence_guard BEFORE INSERT ON public.tagger_qualification_evidence
        FOR EACH ROW EXECUTE FUNCTION public.tagger_qualification_evidence_guard()""",
    """CREATE FUNCTION public.tagger_qualification_evidence_immutable() RETURNS trigger
    LANGUAGE plpgsql SET search_path = pg_catalog AS $$
    BEGIN RAISE EXCEPTION 'qualification_evidence_immutable'; END $$""",
    """CREATE TRIGGER tagger_qualification_evidence_immutable BEFORE UPDATE OR DELETE OR TRUNCATE
        ON public.tagger_qualification_evidence FOR EACH STATEMENT
        EXECUTE FUNCTION public.tagger_qualification_evidence_immutable()""",
    "REVOKE ALL ON public.tagger_qualification_evidence FROM PUBLIC",
    """DO $$ BEGIN
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname='replicator') THEN
            REVOKE ALL ON public.tagger_qualification_evidence FROM replicator;
        END IF;
    END $$""",
)


def upgrade():
    op.execute("SET LOCAL lock_timeout='2s'")
    for statement in STATEMENTS:
        op.execute(statement)


def downgrade():
    raise RuntimeError('qualification_evidence_history_requires_forward_migration')
