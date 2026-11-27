"""Review sets for comparing a candidate release base with the incumbent.

docs/notes/tagger-base-switch-plan.md step 3. A base comparison frame
(tagger/base_comparison.py) is registered as a direct request
(human-review-set/3) with its own purpose ``base_comparison`` and provenance
kind ``base_comparison`` so it never stands in for, or displaces, the
publication review of a dedicated candidate for the same tag, and review refill
does not count it as an open ordinary review.
"""
from importlib import import_module

from alembic import op

revision = "0212"
down_revision = "0211"
branch_labels = None
depends_on = None
REPLICA_DDL = None  # tagger_review_set is not in kura_mesh_pub

base = import_module("api.alembic.versions.0203_review_set_registry")
PURPOSES = ("'fixed_evaluation','production_precision','training_support','boundary_recheck',"
            "'candidate_evaluation','historical_label_recheck','base_comparison'")
EDITS = (
    ("CASE WHEN explicit THEN ARRAY['historical_label_recheck','candidate_evaluation','training_support',"
     "'fixed_evaluation','production_precision','boundary_recheck']",
     "CASE WHEN explicit THEN ARRAY['historical_label_recheck','candidate_evaluation','training_support',"
     "'fixed_evaluation','production_precision','boundary_recheck','base_comparison']"),
    (" THEN RAISE EXCEPTION 'review_purpose_invalid'; END IF;\n",
     " THEN RAISE EXCEPTION 'review_purpose_invalid'; END IF;\n"
     " IF purpose='base_comparison' AND d->>'schema'<>'human-review-set/3'"
     " THEN RAISE EXCEPTION 'review_purpose_invalid'; END IF;\n"),
    ("     IF (purpose='candidate_evaluation' AND jsonb_typeof(prov)='object'",
     "     IF (jsonb_typeof(prov)='object'"),
    ("       AND prov->'kind'='\"dedicated_candidate\"'::jsonb\n",
     "       AND ((purpose='candidate_evaluation' AND prov->'kind'='\"dedicated_candidate\"'::jsonb)\n"
     "         OR (purpose='base_comparison' AND prov->'kind'='\"base_comparison\"'::jsonb))\n"),
)


def registration_sql():
    """0211's function (0210 plus its active-slot edit) with base_comparison admitted."""
    prior = import_module("api.alembic.versions.0210_review_reselection")
    sql = prior.registration_sql()
    marker = "WHERE m.queue_sha256=tagger_review_set.queue_sha256"
    assert sql.count(marker) == 1
    sql = sql.replace(marker, marker + """
                   AND (tagger_review_set.purpose NOT IN ('historical_label_recheck','training_support')
                        OR EXISTS (
                          SELECT 1 FROM public.post p WHERE p.id=m.post_id AND p.status='ok'
                            AND NOT EXISTS (
                              SELECT 1 FROM public.post_tag pt
                              JOIN public.tag_name n ON n.tag_id=pt.tag_id AND n.ord=0
                              WHERE pt.post_id=p.id AND n.name='tagger-training-exclude'
                            )
                        ))
    """)
    for old, new in EDITS:
        assert sql.count(old) == 1, old
        sql = sql.replace(old, new)
    return sql


def upgrade():
    op.execute("SET LOCAL lock_timeout='2s'")
    base.as_owner(op, "kura_reconciler", f"""
        ALTER TABLE public.tagger_review_set DROP CONSTRAINT tagger_review_set_purpose_check,
          ADD CONSTRAINT tagger_review_set_purpose_check CHECK (purpose IN ({PURPOSES}))
    """)
    base.as_owner(op, "kura_reconciler", registration_sql())


def downgrade():
    raise RuntimeError("review_set_history_is_not_destructively_downgraded")
