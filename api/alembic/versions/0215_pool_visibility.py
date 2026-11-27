"""Persist reversible pool visibility and replicate it with the pool."""
from alembic import op

revision = "0215"
down_revision = "0214"
branch_labels = None
depends_on = None

REPLICA_DDL = "ALTER TABLE public.pool ADD COLUMN IF NOT EXISTS hidden boolean NOT NULL DEFAULT false;"


def upgrade():
    op.execute("SET LOCAL lock_timeout='2s'")
    op.execute(REPLICA_DDL)


def downgrade():
    raise RuntimeError("pool visibility decisions must be retained")
