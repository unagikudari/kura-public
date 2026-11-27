"""Every frozen review request may be re-issued as a media-bound successor."""
from importlib import import_module

from alembic import op

revision = "0207"
down_revision = "0206"
branch_labels = None
depends_on = None
REPLICA_DDL = None

base = import_module("api.alembic.versions.0203_review_set_registry")
prior = import_module("api.alembic.versions.0204_direct_candidate_review")

EDITS = (
    ("CASE WHEN explicit THEN ARRAY['historical_label_recheck','candidate_evaluation','training_support']",
     "CASE WHEN explicit THEN ARRAY['historical_label_recheck','candidate_evaluation','training_support',"
     "'fixed_evaluation','production_precision','boundary_recheck']"),
    ("WHEN 'training_support' THEN 'frozen_training_support' ELSE 'historical_human_training' END",
     "WHEN 'training_support' THEN 'frozen_training_support' WHEN 'historical_label_recheck' THEN 'historical_human_training'"
     " ELSE 'frozen_review_set' END"),
    ("   IF purpose IN ('candidate_evaluation','training_support') THEN",
     "   IF purpose<>'historical_label_recheck' THEN"),
)


def registration_sql():
    sql = prior.registration_sql()
    for old, new in EDITS:
        assert sql.count(old) == 1, old
        sql = sql.replace(old, new)
    return sql


def upgrade():
    op.execute("SET LOCAL lock_timeout='2s'")
    base.as_owner(op, 'kura_reconciler', registration_sql())


def downgrade():
    raise RuntimeError("review_set_history_is_not_destructively_downgraded")
