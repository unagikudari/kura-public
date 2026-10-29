-- Undo stage 1 of docs/notes/tagging-work-table-plan.md. Valid only before stage 3
-- (until then post.tagging_* is still the queue and nothing reads the work table).
--   sudo -u postgres psql -X -d kura -v ON_ERROR_STOP=1 < scripts/sql/tagging_work_stage1_rollback.sql
\set ON_ERROR_STOP 1
SET lock_timeout = '2s';
BEGIN;
DROP TRIGGER IF EXISTS post_tagging_work_mirror_upd ON public.post;
DROP TRIGGER IF EXISTS post_tagging_work_mirror_ins ON public.post;
DROP FUNCTION IF EXISTS public.trg_post_tagging_work_mirror();
DROP TABLE IF EXISTS public.post_tagging_work;
COMMIT;
