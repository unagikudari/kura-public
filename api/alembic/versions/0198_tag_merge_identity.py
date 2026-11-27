"""Canonical-only, immutable identity edges for future tag merges.

No historical merge is guessed from an alias or a nameless tag. Existing histories
remain unchanged. IDs here are historical values, deliberately not cascading FKs.
"""
from alembic import op

revision = '0198'
down_revision = '0197'
branch_labels = None
depends_on = None
REPLICA_DDL = 'SELECT 1;'

STATEMENTS = (
    """CREATE TABLE tag_merge_identity (
        source_id integer PRIMARY KEY CHECK (source_id > 0),
        target_id integer NOT NULL CHECK (target_id > 0 AND target_id <> source_id),
        source_name text NOT NULL CHECK (length(source_name) > 0),
        target_name text NOT NULL CHECK (length(target_name) > 0),
        actor_user_id integer NOT NULL CHECK (actor_user_id > 0),
        recorded_at timestamptz NOT NULL DEFAULT transaction_timestamp()
    )""",
    "CREATE INDEX tag_merge_identity_target ON tag_merge_identity(target_id)",
    """CREATE FUNCTION tag_merge_identity_immutable() RETURNS trigger
    LANGUAGE plpgsql SET search_path = pg_catalog AS $$
    BEGIN RAISE EXCEPTION 'tag_merge_identity_immutable'; END $$""",
    """CREATE TRIGGER tag_merge_identity_immutable BEFORE UPDATE OR DELETE OR TRUNCATE
    ON tag_merge_identity FOR EACH STATEMENT EXECUTE FUNCTION tag_merge_identity_immutable()""",
    "REVOKE ALL ON tag_merge_identity FROM PUBLIC",
    # a fresh install from the public core schema has no replicator role (the replica topology is private)
    """DO $$ BEGIN IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname='replicator') THEN
    REVOKE ALL ON tag_merge_identity FROM replicator; END IF; END $$""",
)


def upgrade():
    op.execute("SET LOCAL lock_timeout='2s'")
    for statement in STATEMENTS:
        op.execute(statement)


def downgrade():
    raise RuntimeError('tag_merge_identity preserves user decisions; no destructive downgrade')
