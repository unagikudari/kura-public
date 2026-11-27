"""Count outstanding requests in the registry cap while preserving all history."""
from importlib import import_module

from alembic import op

revision = "0208"
down_revision = "0207"
branch_labels = None
depends_on = None
REPLICA_DDL = None

base = import_module("api.alembic.versions.0203_review_set_registry")
prior = import_module("api.alembic.versions.0207_review_set_any_purpose")

OLD = """WHERE convert_from(document,'UTF8')::jsonb->>'schema'<>'human-review-set/3')>=20"""
NEW = """WHERE convert_from(document,'UTF8')::jsonb->>'schema'<>'human-review-set/3'
            AND (convert_from(document,'UTF8')::jsonb->>'schema'='human-review-set/1'
               OR EXISTS (
                 SELECT 1 FROM public.tagger_review_set_member m
                 WHERE m.queue_sha256=tagger_review_set.queue_sha256
                   AND NOT EXISTS (
                     SELECT 1 FROM public.tagger_human_review_event e
                     WHERE e.queue_sha256=m.queue_sha256 AND e.post_id=m.post_id
                   )
               )
            ))>=20"""


def upgrade():
    op.execute("SET LOCAL lock_timeout='2s'")
    op.execute("GRANT SELECT (queue_sha256, post_id) ON public.tagger_human_review_event TO kura_reconciler")
    sql = prior.registration_sql()
    assert sql.count(OLD) == 1
    base.as_owner(op, "kura_reconciler", sql.replace(OLD, NEW))


def downgrade():
    raise RuntimeError("review_set_history_is_not_destructively_downgraded")
