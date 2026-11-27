"""Keep immutable adjudication history outside ordinary review capacity."""
from importlib import import_module

from alembic import op

revision = "0209"
down_revision = "0208"
branch_labels = None
depends_on = None
REPLICA_DDL = None

prior = import_module("api.alembic.versions.0208_active_review_capacity")


def upgrade():
    op.execute("SET LOCAL lock_timeout='2s'")
    prior.base.as_owner(op, "kura_reconciler", """
        CREATE FUNCTION public.tagger_is_adjudication(d jsonb) RETURNS boolean
        LANGUAGE sql IMMUTABLE PARALLEL SAFE SET search_path=pg_catalog AS $$
            SELECT coalesce(
                d->>'schema'='human-review-set/2'
                AND d->>'purpose'='historical_label_recheck'
                AND jsonb_array_length(d->'post_ids')=1
                AND d->>'frame_sha256'=encode(sha256(convert_to(
                    format('{"kind":"third_reviewer","media":"%s","post_id":%s,"tag":%s}',
                        d->'media_sha256'->>(d->'post_ids'->>0),
                        d->'post_ids'->>0, to_json(d->>'tag')),
                    'UTF8')), 'hex'), false)
        $$
    """)
    prior.base.as_owner(op, "kura_reconciler", registration_sql())


def registration_sql():
    sql = prior.prior.registration_sql().replace(prior.OLD, prior.NEW)
    incoming = "IF NOT (d->>'schema'='human-review-set/3') AND"
    existing = "WHERE convert_from(document,'UTF8')::jsonb->>'schema'<>'human-review-set/3'"
    assert sql.count(incoming) == sql.count(existing) == 1
    sql = sql.replace(incoming, incoming + " NOT public.tagger_is_adjudication(d) AND")
    sql = sql.replace(existing, existing + """
            AND NOT public.tagger_is_adjudication(convert_from(document,'UTF8')::jsonb)""")
    return sql


def downgrade():
    raise RuntimeError("review_set_history_is_not_destructively_downgraded")
