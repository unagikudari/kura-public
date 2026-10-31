-- Undo stage 4a of docs/notes/tagging-work-table-plan.md: the mirror function goes back
-- to the stage 3 body, which copies the new post's tagging_* columns into its work row.
--
-- Order: code first, then this file. Put back the code that stamps post.tagging_status on
-- ingest and make sure no kura-api runs the 4a code (/api/build); only then run this as
-- postgres on primary (with the 4a code still running, the stage 3 body would give a document
-- the column default 'pending'). Feed it on stdin:
--   sudo -u postgres psql -X -d kura < scripts/sql/tagging_work_stage4a_rollback.sql
-- then set kura_alembic_version back to 0183. The last line says ROLLED BACK.

\set ON_ERROR_STOP 1
SET lock_timeout = '2s';

BEGIN;

SELECT md5(prosrc) = '8687f413263f29c0f2c2a558ec592351' AS at_stage4a,
       md5(prosrc) = '9bba27dbe5ba40cd8cae85df8cbbbc85' AS at_stage3
  FROM pg_proc WHERE oid = 'public.trg_post_tagging_work_mirror()'::regprocedure
\gset
\if :at_stage3
ROLLBACK;
\echo 'ROLLED BACK (already)'
\quit
\endif
\if :at_stage4a
\else
ROLLBACK;
\echo 'STOPPED: the mirror function is neither the stage 4a body nor the stage 3 body'
\quit
\endif

-- The stage 3 body, byte for byte (scripts/sql/tagging_work_stage3_swap.sql).
CREATE OR REPLACE FUNCTION public.trg_post_tagging_work_mirror()
  RETURNS trigger
  LANGUAGE plpgsql
  SECURITY DEFINER
  SET search_path TO 'pg_catalog', 'public'
AS $function$
BEGIN
    IF TG_OP = 'INSERT' THEN
        INSERT INTO public.post_tagging_work AS w (post_id, status, lease_until, attempts, last_error)
        VALUES (NEW.id, NEW.tagging_status, NEW.tagging_locked_until, NEW.tagging_attempts, NEW.tagging_last_error)
        ON CONFLICT (post_id) DO UPDATE
           SET status = EXCLUDED.status, lease_until = EXCLUDED.lease_until,
               attempts = EXCLUDED.attempts, last_error = EXCLUDED.last_error;
    END IF;
    RETURN NULL;
END;
$function$;

SELECT md5(prosrc) = '9bba27dbe5ba40cd8cae85df8cbbbc85' AS rolled_back
  FROM pg_proc WHERE oid = 'public.trg_post_tagging_work_mirror()'::regprocedure
\gset
\if :rolled_back
COMMIT;
\echo 'ROLLED BACK'
\else
ROLLBACK;
\echo 'STOPPED: the restored body did not come out as expected; nothing changed'
\endif
