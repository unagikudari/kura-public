"""Collector label producers carry the library their evidence came from (#152).

Canonical-only namespace. No replica DDL; stamp replica ledger 0186->0187 first.
Adds collector_next.library (this install's identity, created on first use) and
label_producer.origin ('' = this library), and moves the producer's uniqueness to include it.
"""
from pathlib import Path
from alembic import op
import sqlalchemy as sa

revision = "0187"
down_revision = "0186"
branch_labels = None
depends_on = None
REPLICA_DDL = None


def upgrade():
    db = op.get_bind()
    if db.execute(sa.text("SELECT EXISTS(SELECT 1 FROM pg_publication_tables WHERE schemaname='collector_next')")).scalar():
        raise RuntimeError("collector_next must remain canonical-only")
    source = Path(__file__).resolve().parents[2] / "collector/migrations/0006_label_producer_origin.sql"
    for statement in source.read_text().split("-- statement-break"):
        if statement.strip():
            op.execute(sa.text(statement))


def downgrade():
    raise RuntimeError("Preserve label provenance; disable the feature instead of discarding it")
