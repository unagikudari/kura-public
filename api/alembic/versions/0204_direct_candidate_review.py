"""Allow review requests bound directly to a dedicated candidate."""
from importlib import import_module

from alembic import op

revision = "0204"
down_revision = "0203"
branch_labels = None
depends_on = None
REPLICA_DDL = None

prior = import_module("api.alembic.versions.0203_review_set_registry")
SCHEMAS = "'human-review-set/1','human-review-set/2'"
DIRECT = "d->>'schema'='human-review-set/3'"


def registration_sql():
    """The registration function as this revision defines it (0207 builds on it)."""
    registration = prior.REGISTER.replace("CREATE FUNCTION", "CREATE OR REPLACE FUNCTION", 1)
    registration = registration.replace(SCHEMAS, SCHEMAS + ",'human-review-set/3'")
    registration = registration.replace("explicit:=d->>'schema'='human-review-set/2';",
                                        f"explicit:=d->>'schema'='human-review-set/2' OR {DIRECT};")
    registration = registration.replace("   IF (jsonb_typeof(prov)", f"""
   IF {DIRECT} THEN
     IF (purpose='candidate_evaluation' AND jsonb_typeof(prov)='object'
       AND (SELECT count(*) FROM json_object_keys(j->'source_provenance'))=3
       AND (SELECT count(*) FROM jsonb_object_keys(prov))=3
       AND prov->'kind'='"dedicated_candidate"'::jsonb
       AND jsonb_typeof(prov->'measurement_sha256')='string'
       AND prov->>'measurement_sha256'~'^[a-f0-9]{{64}}$'
       AND jsonb_typeof(prov->'snapshot_sha256')='string'
       AND prov->>'snapshot_sha256'~'^[a-f0-9]{{64}}$'
       AND jsonb_typeof(d->'media_sha256')='object') IS NOT TRUE
     THEN RAISE EXCEPTION 'review_provenance_invalid'; END IF;
   ELSE
   IF (jsonb_typeof(prov)""", 1)
    registration = registration.replace("   IF (SELECT count(*) FROM json_object_keys(j->'media_sha256'))",
                                        "   END IF;\n   IF (SELECT count(*) FROM json_object_keys(j->'media_sha256'))")
    registration = registration.replace(
        "IF (SELECT count(*) FROM public.tagger_review_set)>=20",
        f"""IF NOT ({DIRECT}) AND (SELECT count(*) FROM public.tagger_review_set
          WHERE convert_from(document,'UTF8')::jsonb->>'schema'<>'human-review-set/3')>=20""",
    )
    return registration


def upgrade():
    op.execute("SET LOCAL lock_timeout='2s'")
    consistency = next(s for s in prior.STATEMENTS
                       if "CREATE FUNCTION public.tagger_review_set_consistent()" in s)
    prior.as_owner(op, 'kura_reconciler', consistency.replace("CREATE FUNCTION", "CREATE OR REPLACE FUNCTION", 1)
               .replace(SCHEMAS, SCHEMAS + ",'human-review-set/3'"))
    prior.as_owner(op, 'kura_reconciler', registration_sql())


def downgrade():
    raise RuntimeError("direct_candidate_review_history_is_not_destructively_downgraded")
