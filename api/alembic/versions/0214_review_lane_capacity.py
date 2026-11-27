"""Reserve independent regular and LoRA review capacity."""
from importlib import import_module

from alembic import op

revision = "0214"
down_revision = "0213"
branch_labels = None
depends_on = None
REPLICA_DDL = None

prior = import_module("api.alembic.versions.0213_review_origin")
base = import_module("api.alembic.versions.0203_review_set_registry")


def registration_sql():
    sql = prior.registration_sql()
    marker = "AND NOT public.tagger_is_adjudication(convert_from(document,'UTF8')::jsonb)"
    assert sql.count(marker) == 1
    return sql.replace(marker, marker + """
            AND public.tagger_is_lora_review(
                tagger_review_set.queue_sha256, convert_from(document,'UTF8')::jsonb
            ) = public.tagger_is_lora_review(ref, d)
    """)


def upgrade():
    op.execute("SET LOCAL lock_timeout='2s'")
    statements = (
        """CREATE TABLE public.tagger_review_lora_origin (
            queue_sha256 text PRIMARY KEY REFERENCES public.tagger_review_set(queue_sha256)
        )""",
        "REVOKE ALL ON public.tagger_review_lora_origin FROM PUBLIC, kura",
        "GRANT SELECT ON public.tagger_review_lora_origin TO kura",
        """DO $$ BEGIN IF EXISTS(SELECT FROM pg_roles WHERE rolname='replicator') THEN
           REVOKE ALL ON public.tagger_review_lora_origin FROM replicator;
           END IF; END $$""",
        """CREATE FUNCTION public.tagger_is_lora_review(ref text, d jsonb) RETURNS boolean
        LANGUAGE sql STABLE SET search_path=pg_catalog AS $$
            SELECT coalesce(d->>'purpose'='historical_label_recheck' AND (
                coalesce(d->'source_provenance'->>'origin'='lora_findings', false)
                OR EXISTS (SELECT FROM public.tagger_review_lora_origin
                           WHERE queue_sha256=ref)
            ), false)
        $$""",
        "REVOKE ALL ON FUNCTION public.tagger_is_lora_review(text, jsonb) FROM PUBLIC",
        registration_sql(),
    )
    for statement in statements:
        base.as_owner(op, "kura_reconciler", statement)


def downgrade():
    raise RuntimeError("review_set_history_is_not_destructively_downgraded")
