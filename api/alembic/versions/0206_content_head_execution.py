"""Record one completed inference per reported worker and head identity."""
from alembic import op
from api.alembic.owner import as_owner

revision = "0206"
down_revision = "0205"
branch_labels = None
depends_on = None
REPLICA_DDL = None


def upgrade():
    op.execute("SET LOCAL lock_timeout='2s'")
    for sql in (
        """CREATE TABLE public.tagger_content_head_execution (
            receipt_sha256 text NOT NULL CHECK(receipt_sha256 ~ '^[0-9a-f]{64}$'),
            worker_id text NOT NULL CHECK(length(worker_id) BETWEEN 1 AND 256),
            tag text NOT NULL CHECK(length(tag) BETWEEN 1 AND 256),
            artifact_sha256 text NOT NULL CHECK(artifact_sha256 ~ '^[0-9a-f]{64}$'),
            threshold double precision NOT NULL CHECK(threshold BETWEEN 0 AND 1),
            apply_mode text NOT NULL CHECK(apply_mode IN ('replace', 'add_only')),
            policy_epoch timestamptz NOT NULL,
            post_id integer NOT NULL CHECK(post_id > 0),
            completed_at timestamptz NOT NULL DEFAULT clock_timestamp(),
            PRIMARY KEY(receipt_sha256, worker_id, tag, artifact_sha256, threshold, apply_mode, policy_epoch)
        )""",
        """COMMENT ON TABLE public.tagger_content_head_execution IS
           'First successful worker-reported inference per execution receipt, head identity and publication epoch.'""",
        """CREATE TRIGGER tagger_content_head_execution_immutable
           BEFORE UPDATE OR DELETE OR TRUNCATE ON public.tagger_content_head_execution
           FOR EACH STATEMENT EXECUTE FUNCTION public.tagger_review_set_immutable()""",
        "REVOKE ALL ON public.tagger_content_head_execution FROM PUBLIC,kura",
        "GRANT SELECT, INSERT ON public.tagger_content_head_execution TO kura",
        """DO $$ BEGIN IF EXISTS (SELECT FROM pg_roles WHERE rolname='replicator') THEN
           REVOKE ALL ON public.tagger_content_head_execution FROM replicator;
           END IF; END $$""",
    ):
        as_owner(op, "kura_reconciler", sql)


def downgrade():
    raise RuntimeError("content_head_execution_history_requires_forward_migration")
