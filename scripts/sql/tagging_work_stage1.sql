-- Stage 1 of docs/notes/tagging-work-table-plan.md: the narrow work table and the mirror.
--
-- Run as postgres, on primary only (the replica gets the table in stage 2). Feed it on stdin:
-- postgres cannot read under the kura user's home (drwxr-x---), so `-f <path>` fails there.
--   sudo -u postgres psql -X -d kura -v ON_ERROR_STOP=1 < scripts/sql/tagging_work_stage1.sql
-- (-X: a psqlrc must not turn autocommit off, or every seed batch joins one transaction.)
-- Then check equality as kura, which may read both tables:
--   psql ... -f scripts/sql/tagging_work_reconcile.sql   (expect 0 | 0 | 0)
--
-- Nothing is granted to kura. Ownership goes to kura_reconciler explicitly: that role
-- cannot create in public, nor put a trigger or a foreign key on post (2026-10-24:
-- has_table_privilege('kura_reconciler','post','TRIGGER'/'REFERENCES') = false), so
-- the DDL runs as postgres and only the result belongs to it.
--
-- Idempotent: rerunning the whole file is how a failed seed batch is retried.
-- Behaviour does not change: the four governed functions still write post; the mirror
-- copies every such write into post_tagging_work in the same transaction.

\set ON_ERROR_STOP 1
SET lock_timeout = '2s';

BEGIN;

CREATE TABLE IF NOT EXISTS public.post_tagging_work (
  post_id     integer PRIMARY KEY REFERENCES public.post(id) ON DELETE CASCADE ON UPDATE CASCADE,
  status      text NOT NULL CHECK (status IN ('pending','done','failed','skipped')),
  lease_until timestamptz,
  attempts    integer NOT NULL DEFAULT 0,
  last_error  text
);
CREATE INDEX IF NOT EXISTS post_tagging_work_claim
  ON public.post_tagging_work (lease_until NULLS FIRST, post_id) WHERE status = 'pending';
CREATE INDEX IF NOT EXISTS post_tagging_work_done
  ON public.post_tagging_work (post_id) WHERE status = 'done';
ALTER TABLE public.post_tagging_work OWNER TO kura_reconciler;
-- postgres's default privileges in public give kura ALL on every table it creates
-- (pg_default_acl, 2026-10-24); the first apply left kura=arwdDxtm here. Take it back.
REVOKE ALL ON public.post_tagging_work FROM PUBLIC, kura;
GRANT SELECT ON public.post_tagging_work TO kura, replicator;

-- Copies post's four queue columns into the work table. SECURITY DEFINER as the
-- table owner, because the writer (kura for ingest, kura_reconciler inside the
-- governed functions) must not be able to write the table directly.
CREATE OR REPLACE FUNCTION public.trg_post_tagging_work_mirror()
  RETURNS trigger
  LANGUAGE plpgsql
  SECURITY DEFINER
  SET search_path TO 'pg_catalog', 'public'
AS $function$
BEGIN
    INSERT INTO public.post_tagging_work AS w (post_id, status, lease_until, attempts, last_error)
    VALUES (NEW.id, NEW.tagging_status, NEW.tagging_locked_until, NEW.tagging_attempts, NEW.tagging_last_error)
    ON CONFLICT (post_id) DO UPDATE
       SET status = EXCLUDED.status, lease_until = EXCLUDED.lease_until,
           attempts = EXCLUDED.attempts, last_error = EXCLUDED.last_error;
    RETURN NULL;
END;
$function$;
ALTER FUNCTION public.trg_post_tagging_work_mirror() OWNER TO kura_reconciler;
-- Called only as a trigger: nobody needs EXECUTE on it.
REVOKE ALL ON FUNCTION public.trg_post_tagging_work_mirror() FROM PUBLIC;

-- Created only when absent, so a rerun takes no lock on post for the triggers. A trigger
-- that exists with the wrong definition is not repaired here; 0181 refuses it by its full
-- definition, and the rollback file removes it.
DO $do$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgrelid = 'public.post'::regclass
                 AND tgname = 'post_tagging_work_mirror_ins') THEN
    CREATE TRIGGER post_tagging_work_mirror_ins
      AFTER INSERT ON public.post
      FOR EACH ROW EXECUTE FUNCTION public.trg_post_tagging_work_mirror();
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgrelid = 'public.post'::regclass
                 AND tgname = 'post_tagging_work_mirror_upd') THEN
    CREATE TRIGGER post_tagging_work_mirror_upd
      AFTER UPDATE OF tagging_status, tagging_locked_until, tagging_attempts, tagging_last_error ON public.post
      FOR EACH ROW EXECUTE FUNCTION public.trg_post_tagging_work_mirror();
  END IF;
END
$do$;

COMMIT;

-- Seed: existing rows, 10,000 ids per statement, each its own transaction so the
-- seed never holds work-table row locks for long (a mirror arriving at a row the
-- seed holds waits for the seed's commit). DO NOTHING keeps whatever the mirror
-- already wrote; a row the seed wrote first is updated by the next mirror.
--
-- FOR KEY SHARE takes the post row locks BEFORE inserting (codex review, 2026-10-24).
-- Without it the foreign-key check takes them at the END of the statement, after the
-- work rows exist, and a claim in between deadlocks with the seed: the seed holds work
-- row X, the claim holds post X FOR UPDATE and its mirror waits for work row X, and the
-- seed's FK check then waits for post X. Taken first, the locks are post -> work in both:
-- the claim skips rows the seed holds (SKIP LOCKED; FOR UPDATE conflicts with KEY SHARE),
-- and completion and requeue take FOR NO KEY UPDATE, which KEY SHARE does not block.
--
-- A batch that fails (e.g. a post deleted between the read and the insert) stops the
-- file; rerun it — the DDL above takes no lock on post the second time, and DO NOTHING
-- skips the rows already done.
SELECT format(
  'INSERT INTO public.post_tagging_work (post_id, status, lease_until, attempts, last_error) '
  'SELECT id, tagging_status, tagging_locked_until, tagging_attempts, tagging_last_error '
  'FROM public.post WHERE id > %s AND id <= %s ORDER BY id FOR KEY SHARE '
  'ON CONFLICT (post_id) DO NOTHING',
  lo, lo + 10000)
FROM generate_series(0, (SELECT coalesce(max(id), 0) FROM public.post), 10000) AS lo
\gexec

