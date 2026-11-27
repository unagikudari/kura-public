"""Allow MP4 viewing copies without changing video originals.

Rendition tables and their invalidation trigger are canonical-only. No replica
DDL; advance the replica ledger 0187->0188 before canonical application.
"""
from alembic import op
revision = '0188'
down_revision = '0187'
branch_labels = None
depends_on = None
REPLICA_DDL = None


def upgrade():
    op.execute("SET LOCAL lock_timeout='2s'")
    op.execute("ALTER TABLE public.rendition_object DROP CONSTRAINT rendition_object_mime_check")
    op.execute("""ALTER TABLE public.rendition_object ADD CONSTRAINT rendition_object_mime_check
        CHECK (mime IN ('image/webp','image/jpeg','image/png','image/gif','video/mp4')) NOT VALID""")
    op.execute("""CREATE OR REPLACE FUNCTION public.post_rendition_follow_legacy()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF current_setting('kura.rendition_cutover', true) = 'on' THEN RETURN NEW; END IF;
  IF NEW.data IS DISTINCT FROM OLD.data OR NEW.sha256 IS DISTINCT FROM OLD.sha256
     OR NEW."mime-type" IS DISTINCT FROM OLD."mime-type" THEN
    DELETE FROM public.post_rendition WHERE post_id = NEW.id AND kind = 'view';
  END IF;
  IF NEW.thumbnail IS DISTINCT FROM OLD.thumbnail THEN
    DELETE FROM public.post_rendition WHERE post_id = NEW.id AND kind = 'thumb';
  END IF;
  RETURN NEW;
END $$""")
    op.execute("DROP TRIGGER post_rendition_follow_legacy ON public.post")
    op.execute("""CREATE TRIGGER post_rendition_follow_legacy
        AFTER UPDATE OF data, thumbnail, sha256, "mime-type" ON public.post
        FOR EACH ROW EXECUTE FUNCTION public.post_rendition_follow_legacy()""")


def downgrade():
    raise RuntimeError('Preserve viewing copies; detach video views before an explicit rollback')
