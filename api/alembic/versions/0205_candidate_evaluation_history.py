"""Persist descriptive candidate evaluations on the canonical database."""
from alembic import op
from api.alembic.owner import as_owner

revision = "0205"
down_revision = "0204"
branch_labels = None
depends_on = None
REPLICA_DDL = None


def upgrade():
    op.execute("SET LOCAL lock_timeout='2s'")
    for sql in (
        """CREATE TABLE public.tagger_candidate_evaluation (
            receipt_sha256 text PRIMARY KEY CHECK(receipt_sha256 ~ '^[0-9a-f]{64}$'),
            registration_id bigint NOT NULL,
            queue_sha256 text NOT NULL REFERENCES public.tagger_review_set(queue_sha256),
            answers_sha256 text NOT NULL CHECK(answers_sha256 ~ '^[0-9a-f]{64}$'),
            document bytea NOT NULL CHECK(octet_length(document) BETWEEN 1 AND 1048576),
            recorded_at timestamptz NOT NULL DEFAULT clock_timestamp(),
            CHECK (receipt_sha256 = encode(sha256(document), 'hex'))
        )""",
        """CREATE INDEX tagger_candidate_evaluation_registration
           ON public.tagger_candidate_evaluation(registration_id, recorded_at DESC)""",
        """CREATE TRIGGER tagger_candidate_evaluation_immutable
           BEFORE UPDATE OR DELETE OR TRUNCATE ON public.tagger_candidate_evaluation
           FOR EACH STATEMENT EXECUTE FUNCTION public.tagger_review_set_immutable()""",
        "REVOKE ALL ON public.tagger_candidate_evaluation FROM PUBLIC,kura",
        "GRANT SELECT, INSERT ON public.tagger_candidate_evaluation TO kura",
        """DO $$ BEGIN IF EXISTS (SELECT FROM pg_roles WHERE rolname='replicator') THEN
           REVOKE ALL ON public.tagger_candidate_evaluation FROM replicator;
           END IF; END $$""",
    ):
        as_owner(op, "kura_reconciler", sql)


def downgrade():
    raise RuntimeError("candidate_evaluation_history_requires_forward_migration")
