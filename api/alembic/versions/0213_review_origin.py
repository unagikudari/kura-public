"""Accept explicit provenance for LoRA findings without changing review digests."""
from importlib import import_module

from alembic import op

revision = "0213"
down_revision = "0212"
branch_labels = None
depends_on = None
REPLICA_DDL = None

prior = import_module("api.alembic.versions.0212_base_comparison_review")


def registration_sql():
    sql = prior.registration_sql()
    marker = "   IF prov<>expected OR"
    assert sql.count(marker) == 1
    return sql.replace(marker, """   IF purpose='historical_label_recheck' AND prov->'origin'='"lora_findings"'::jsonb THEN
     expected:=expected||jsonb_build_object('origin','lora_findings');
   END IF;
""" + marker)


def upgrade():
    op.execute("SET LOCAL lock_timeout='2s'")
    prior.base.as_owner(op, "kura_reconciler", registration_sql())


def downgrade():
    raise RuntimeError("review_set_history_is_not_destructively_downgraded")
