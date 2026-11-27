"""Allow a post deletion to remove its own governed training labels."""
from importlib import import_module

from alembic import op

revision = "0211"
down_revision = "0210"
branch_labels = None
depends_on = None
REPLICA_DDL = None

base = import_module("api.alembic.versions.0203_review_set_registry")


def upgrade():
    op.execute("SET LOCAL lock_timeout='2s'")
    base.as_owner(op, "kura_reconciler", """
        CREATE FUNCTION public.tagger_delete_gt_before_post_delete()
        RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,public AS $$
        BEGIN
            DELETE FROM public.tagger_gt_label WHERE post_id=OLD.id;
            RETURN OLD;
        END
        $$
    """)
    base.as_owner(op, "kura_reconciler",
                  "REVOKE ALL ON FUNCTION public.tagger_delete_gt_before_post_delete() FROM PUBLIC")
    base.as_owner(op, "kura_reconciler",
                  "GRANT EXECUTE ON FUNCTION public.tagger_delete_gt_before_post_delete() TO kura")
    op.execute("""
        CREATE TRIGGER tagger_delete_gt_before_post_delete
        BEFORE DELETE ON public.post
        FOR EACH ROW EXECUTE FUNCTION public.tagger_delete_gt_before_post_delete()
    """)
    base.as_owner(op, "kura_reconciler",
                  "REVOKE EXECUTE ON FUNCTION public.tagger_delete_gt_before_post_delete() FROM kura")
    base.as_owner(op, "kura_reconciler", """
        DO $$ BEGIN
          IF EXISTS (SELECT FROM pg_roles WHERE rolname='replicator') THEN
            REVOKE ALL ON FUNCTION public.tagger_delete_gt_before_post_delete() FROM replicator;
          END IF;
        END $$
    """)
    # Missing or opted-out ordinary members cannot hold an active slot.
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
    base.as_owner(op, "kura_reconciler", sql)


def downgrade():
    raise RuntimeError("review_set_history_is_not_destructively_downgraded")
