-- Stage 4a of docs/notes/tagging-work-table-plan.md: a new post's work row takes its
-- status from post.type, no longer from post.tagging_status.
--
-- Run as postgres on primary, after stage 3 (SWAPPED). Feed it on stdin (postgres cannot
-- read under the kura user's home):
--   sudo -u postgres psql -X -d kura < scripts/sql/tagging_work_stage4a_insert_rule.sql
-- The last line says APPLIED. Rerunning after APPLIED does nothing.
--
-- Only the mirror trigger function changes. kura never calls it (it runs as the AFTER
-- INSERT trigger on post), so there is no EXECUTE window, no drain, and nothing to stop:
-- an INSERT racing this file gets the old or the new body, and both give the same row
-- while ingest still stamps tagging_status by the same rule (Post.initial_tagging_status).
-- The rule is a SQL literal, like the claim's type list; tests/test_tagging_work_stage4a.py
-- ties it to Post.TAGGABLE_TYPES. Measured on primary on 2026-10-25: every existing work row
-- already agrees with it (86 skipped, all type 'document'; no pending or done of another
-- type).
--
-- This must be in place before the code that stops stamping tagging_status runs (0184
-- checks it): without it, a document would get the column default 'pending'.
-- Undo: scripts/sql/tagging_work_stage4a_rollback.sql (back to the stage 3 body).

\set ON_ERROR_STOP 1
SET lock_timeout = '2s';

BEGIN;

-- The mirror is exactly stage 3's (md5 as 0183 checks it), or exactly the one below.
SELECT md5(prosrc) = '9bba27dbe5ba40cd8cae85df8cbbbc85' AS at_stage3,
       md5(prosrc) = '8687f413263f29c0f2c2a558ec592351' AS at_stage4a
  FROM pg_proc WHERE oid = 'public.trg_post_tagging_work_mirror()'::regprocedure
\gset
\if :at_stage4a
ROLLBACK;
\echo 'APPLIED (already)'
\quit
\endif
\if :at_stage3
\else
ROLLBACK;
\echo 'STOPPED: the mirror function is neither the stage 3 body nor the stage 4a body'
\quit
\endif

-- CREATE OR REPLACE keeps the owner (kura_reconciler), SECURITY DEFINER and the ACL.
CREATE OR REPLACE FUNCTION public.trg_post_tagging_work_mirror()
  RETURNS trigger
  LANGUAGE plpgsql
  SECURITY DEFINER
  SET search_path TO 'pg_catalog', 'public'
AS $function$
BEGIN
    IF TG_OP = 'INSERT' THEN
        INSERT INTO public.post_tagging_work AS w (post_id, status, lease_until, attempts, last_error)
        VALUES (NEW.id,
                CASE WHEN NEW.type IN ('image', 'animation', 'video') THEN 'pending' ELSE 'skipped' END,
                NULL, 0, NULL)
        ON CONFLICT (post_id) DO UPDATE
           SET status = EXCLUDED.status, lease_until = NULL, attempts = 0, last_error = NULL;
    END IF;
    RETURN NULL;
END;
$function$;

SELECT md5(prosrc) = '8687f413263f29c0f2c2a558ec592351' AND prosecdef
       AND pg_get_userbyid(proowner) = 'kura_reconciler' AS applied
  FROM pg_proc WHERE oid = 'public.trg_post_tagging_work_mirror()'::regprocedure
\gset
\if :applied
COMMIT;
\echo 'APPLIED'
\else
ROLLBACK;
\echo 'STOPPED: the new body did not come out as expected; nothing changed'
\endif
