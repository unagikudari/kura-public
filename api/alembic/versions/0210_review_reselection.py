"""Retire selected review requests without rewriting judgments or members."""
from importlib import import_module

from alembic import op

revision = "0210"
down_revision = "0209"
branch_labels = None
depends_on = None
REPLICA_DDL = None

prior = import_module("api.alembic.versions.0209_adjudication_capacity")


def upgrade():
    base = prior.prior.base
    op.execute("SET LOCAL lock_timeout='2s'")
    base.as_owner(op, "kura_reconciler", """
        CREATE TABLE public.tagger_review_set_retirement (
            queue_sha256 text PRIMARY KEY REFERENCES public.tagger_review_set(queue_sha256),
            actor_id integer NOT NULL,
            retired_at timestamptz NOT NULL DEFAULT clock_timestamp()
        )
    """)
    base.as_owner(op, "kura_reconciler",
                  "GRANT SELECT, INSERT ON public.tagger_review_set_retirement TO kura")
    base.as_owner(op, "kura_reconciler", """
        DO $$ BEGIN
          IF EXISTS (SELECT FROM pg_roles WHERE rolname='replicator') THEN
            REVOKE ALL ON public.tagger_review_set_retirement FROM replicator;
          END IF;
        END $$
    """)
    base.as_owner(op, "kura_reconciler", registration_sql())


def registration_sql():
    sql = prior.registration_sql()
    start = "AND NOT public.tagger_is_adjudication(convert_from(document,'UTF8')::jsonb)"
    assert sql.count(start) == 1
    sql = sql.replace(start, start + """
            AND NOT EXISTS (
                SELECT 1 FROM public.tagger_review_set_retirement r
                WHERE r.queue_sha256=tagger_review_set.queue_sha256
            )""")
    return sql


def downgrade():
    raise RuntimeError("review_set_history_is_not_destructively_downgraded")
