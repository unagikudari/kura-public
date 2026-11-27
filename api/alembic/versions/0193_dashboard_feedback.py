"""Canonical-only authenticated dashboard feedback; no existing table changes."""
from alembic import op
revision='0193'
down_revision='0192'
branch_labels=None
depends_on=None
REPLICA_DDL = "SELECT 1;"
DDL = """CREATE TABLE dashboard_feedback (
 id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
 user_id integer NOT NULL REFERENCES public."user"(id),
 request_id uuid NOT NULL,
 criterion text NOT NULL CHECK(criterion IN ('general','situation','consistency','duplication','completeness','brevity','actionability','exploration','graphics','freshness','accessibility')),
 body text NOT NULL CHECK(length(btrim(body)) BETWEEN 1 AND 2000),
 page_revision text NOT NULL CHECK(page_revision ~ '^[0-9a-f]{40}$'),
 created_at timestamptz NOT NULL DEFAULT now(),
 deleted_at timestamptz,
 UNIQUE(user_id,request_id)
)"""
def upgrade():
 op.execute("SET LOCAL lock_timeout='2s'")
 op.execute(DDL)
 op.execute("CREATE INDEX dashboard_feedback_recent ON dashboard_feedback(id DESC) WHERE deleted_at IS NULL")
 op.execute("COMMENT ON TABLE dashboard_feedback IS 'Canonical-only signed-in user feedback on dashboard evaluations; agent reports are versioned source artifacts, not user comments.'")
 op.execute("REVOKE ALL ON dashboard_feedback FROM PUBLIC")
 # a fresh install from the public core schema has no replicator role (the replica topology is private)
 op.execute("""DO $$ BEGIN IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname='replicator') THEN
  REVOKE ALL ON dashboard_feedback FROM replicator; END IF; END $$""")
def downgrade():
 raise RuntimeError('dashboard_feedback contains user contributions; no destructive downgrade')
