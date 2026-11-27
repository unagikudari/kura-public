"""Keep image materialization separate from video original invalidation.

0188 invalidated newly attached image views when ingest filled sha256/MIME.
Restore 0177's legacy-column trigger and use a separate, video-only trigger.
Canonical-only functions/tables: no replica DDL; advance ledger 0188->0189 first.
"""
from alembic import op
revision = '0189'
down_revision = '0188'
branch_labels = None
depends_on = None
REPLICA_DDL = None


def upgrade():
    op.execute("SET LOCAL lock_timeout='2s'")
    op.execute("""CREATE OR REPLACE FUNCTION public.post_rendition_follow_legacy()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF current_setting('kura.rendition_cutover', true) = 'on' THEN RETURN NEW; END IF;
  IF NEW.data IS DISTINCT FROM OLD.data THEN
    DELETE FROM public.post_rendition WHERE post_id = NEW.id AND kind = 'view';
  END IF;
  IF NEW.thumbnail IS DISTINCT FROM OLD.thumbnail THEN
    DELETE FROM public.post_rendition WHERE post_id = NEW.id AND kind = 'thumb';
  END IF;
  RETURN NEW;
END $$""")
    op.execute("""CREATE OR REPLACE TRIGGER post_rendition_follow_legacy
        AFTER UPDATE OF data, thumbnail ON public.post
        FOR EACH ROW EXECUTE FUNCTION public.post_rendition_follow_legacy()""")
    op.execute("""CREATE FUNCTION public.post_video_view_follow_original()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF OLD.sha256 IS NOT NULL AND OLD."mime-type" LIKE 'video/%'
     AND (NEW.sha256 IS DISTINCT FROM OLD.sha256
          OR NEW."mime-type" IS DISTINCT FROM OLD."mime-type") THEN
    DELETE FROM public.post_rendition r USING public.rendition_object o
      WHERE r.post_id = NEW.id AND r.kind = 'view' AND r.sha256 = o.sha256
        AND o.mime LIKE 'video/%';
  END IF;
  RETURN NEW;
END $$""")
    op.execute("""CREATE TRIGGER post_video_view_follow_original
        AFTER UPDATE OF sha256, "mime-type" ON public.post
        FOR EACH ROW EXECUTE FUNCTION public.post_video_view_follow_original()""")


def downgrade():
    raise RuntimeError('Do not restore 0188 image-breaking invalidation; keep the corrected triggers')
