"""Structured imported LoRA presets, separate from editable notes.

Replica: apply the ADD COLUMN and advance ledger before canonical deployment.
"""
from alembic import op
revision = '0191'
down_revision = '0190'
branch_labels = None
depends_on = None
REPLICA_DDL = "ALTER TABLE lora_asset ADD COLUMN import_metadata jsonb NOT NULL DEFAULT '{}'::jsonb;"


def upgrade():
    op.execute("ALTER TABLE lora_asset ADD COLUMN import_metadata jsonb NOT NULL DEFAULT '{}'::jsonb")


def downgrade():
    op.execute('ALTER TABLE lora_asset DROP COLUMN import_metadata')
