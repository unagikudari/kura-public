"""Canonical-only original measurements, separate from operational registrations."""
from alembic import op

revision = '0201'
down_revision = '0200'
branch_labels = None
depends_on = None
REPLICA_DDL = None

STATEMENTS = (
    "DO $$ BEGIN IF current_user <> 'kura' THEN RAISE EXCEPTION 'measurement_migration_requires_kura'; END IF; END $$",
    """CREATE TABLE public.tagger_measurement_anchor (
        measurement_sha256 text PRIMARY KEY CHECK (measurement_sha256 ~ '^[0-9a-f]{64}$'),
        canonical_body text NOT NULL CHECK (octet_length(canonical_body) <= 262144),
        body jsonb NOT NULL CHECK (jsonb_typeof(body) = 'object'),
        submitted_by integer NOT NULL CHECK (submitted_by > 0),
        received_at timestamptz NOT NULL DEFAULT transaction_timestamp(),
        CHECK (canonical_body::jsonb = body),
        CHECK (measurement_sha256 = encode(sha256(convert_to(canonical_body,'UTF8')), 'hex')),
        CHECK ((body->>'schema' = 'original-measurement/1'
            AND body->>'provenance' = 'caller_assertion'
            AND body->>'measurement_scope' = 'internal_training'
            AND body->'quality_claim' = 'false'::jsonb
            AND jsonb_typeof(body->'tag_id') = 'number'
            AND body->>'tag_id' ~ '^[1-9][0-9]*$'
            AND jsonb_typeof(body->'tag') = 'string'
            AND length(body->>'tag') BETWEEN 1 AND 200
            AND body->>'artifact_sha256' ~ '^[0-9a-f]{64}$'
            AND jsonb_typeof(body->'threshold') = 'number') IS TRUE),
        CHECK (((body->>'threshold')::numeric > 0 AND (body->>'threshold')::numeric < 1) IS TRUE)
    )""",
    """CREATE TABLE public.tagger_measurement_evidence (
        receipt_sha256 text PRIMARY KEY CHECK (receipt_sha256 ~ '^[0-9a-f]{64}$'),
        measurement_sha256 text NOT NULL REFERENCES public.tagger_measurement_anchor(measurement_sha256),
        canonical_body text NOT NULL CHECK (octet_length(canonical_body) <= 524288),
        body jsonb NOT NULL CHECK (jsonb_typeof(body) = 'object'),
        submitted_by integer NOT NULL CHECK (submitted_by > 0),
        received_at timestamptz NOT NULL DEFAULT transaction_timestamp(),
        CHECK (canonical_body::jsonb = body),
        CHECK (receipt_sha256 = encode(sha256(convert_to(canonical_body,'UTF8')), 'hex')),
        CHECK ((body->>'schema' = 'measurement-qualification-evidence/1'
            AND body->>'provenance' = 'caller_assertion'
            AND body->>'measurement_sha256' = measurement_sha256
            AND body->>'kind' IN ('human_sample','runtime','independent_review')
            AND jsonb_typeof(body->'report') = 'object') IS TRUE)
    )""",
    "CREATE INDEX tagger_measurement_evidence_anchor ON public.tagger_measurement_evidence(measurement_sha256,received_at,receipt_sha256)",
    """COMMENT ON TABLE public.tagger_measurement_anchor IS
        'Canonical-only immutable original internal measurement assertions. Not registration, training event, current GT baseline, authenticated human review or admission. Tag identity is historical and never rewritten. First submitter/time survive replay.'""",
    """COMMENT ON TABLE public.tagger_measurement_evidence IS
        'Canonical-only immutable caller assertions bound to original measurement content hashes. Never changes registration, lifecycle, candidates, holds, GT, policy, release or human-review events. Not admission or publication authority.'""",
    """CREATE FUNCTION public.tagger_measurement_anchor_guard() RETURNS trigger
    LANGUAGE plpgsql SET search_path=pg_catalog,public AS $$
    DECLARE previous text; current_name text;
    BEGIN
        SELECT canonical_body INTO previous FROM public.tagger_measurement_anchor
          WHERE measurement_sha256=NEW.measurement_sha256;
        IF FOUND THEN
            IF previous IS DISTINCT FROM NEW.canonical_body THEN
                RAISE EXCEPTION 'measurement_receipt_collision';
            END IF;
            RETURN NEW;
        END IF;
        SELECT n.name INTO current_name FROM public.tag t
          JOIN public.tag_name n ON n.tag_id=t.id
          WHERE t.id=(NEW.body->>'tag_id')::bigint
          ORDER BY n.ord,n.tag_name_id LIMIT 1 FOR SHARE OF t,n;
        IF NOT FOUND OR current_name IS DISTINCT FROM NEW.body->>'tag' THEN
            RAISE EXCEPTION 'measurement_tag_identity_mismatch';
        END IF;
        RETURN NEW;
    END $$""",
    """CREATE TRIGGER tagger_measurement_anchor_guard BEFORE INSERT ON public.tagger_measurement_anchor
        FOR EACH ROW EXECUTE FUNCTION public.tagger_measurement_anchor_guard()""",
    """CREATE FUNCTION public.tagger_measurement_evidence_guard() RETURNS trigger
    LANGUAGE plpgsql SET search_path=pg_catalog,public AS $$
    DECLARE original jsonb;
    BEGIN
        SELECT body INTO original FROM public.tagger_measurement_anchor
          WHERE measurement_sha256=NEW.measurement_sha256 FOR SHARE;
        IF NOT FOUND THEN RAISE EXCEPTION 'measurement_anchor_missing'; END IF;
        IF (NEW.body->'report'->>'artifact_sha256' = original->>'artifact_sha256'
            AND NEW.body->'report'->'threshold' = original->'threshold') IS NOT TRUE THEN
            RAISE EXCEPTION 'measurement_anchor_mismatch';
        END IF;
        RETURN NEW;
    END $$""",
    """CREATE TRIGGER tagger_measurement_evidence_guard BEFORE INSERT ON public.tagger_measurement_evidence
        FOR EACH ROW EXECUTE FUNCTION public.tagger_measurement_evidence_guard()""",
    """CREATE FUNCTION public.tagger_measurement_immutable() RETURNS trigger
    LANGUAGE plpgsql SET search_path=pg_catalog AS $$
    BEGIN RAISE EXCEPTION 'measurement_history_immutable'; END $$""",
    """CREATE TRIGGER tagger_measurement_anchor_immutable BEFORE UPDATE OR DELETE OR TRUNCATE
        ON public.tagger_measurement_anchor FOR EACH STATEMENT EXECUTE FUNCTION public.tagger_measurement_immutable()""",
    """CREATE TRIGGER tagger_measurement_evidence_immutable BEFORE UPDATE OR DELETE OR TRUNCATE
        ON public.tagger_measurement_evidence FOR EACH STATEMENT EXECUTE FUNCTION public.tagger_measurement_immutable()""",
    "REVOKE ALL ON public.tagger_measurement_anchor,public.tagger_measurement_evidence FROM PUBLIC",
    """DO $$ BEGIN IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname='replicator') THEN
        REVOKE ALL ON public.tagger_measurement_anchor,public.tagger_measurement_evidence FROM replicator;
    END IF; END $$""",
)


def upgrade():
    op.execute("SET LOCAL lock_timeout='2s'")
    for statement in STATEMENTS:
        op.execute(statement)


def downgrade():
    raise RuntimeError('measurement_history_requires_forward_migration')
