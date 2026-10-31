-- Undo stage 3 of docs/notes/tagging-work-table-plan.md: the governed functions write post again.
--
-- Run as postgres on primary, with kura-tagger-sweep.timer stopped and its service inactive:
--   sudo -u postgres psql -X -d kura < scripts/sql/tagging_work_stage3_rollback.sql
-- Same operational premise as the swap: no superuser calls the four functions meanwhile.
-- The last line says ROLLED BACK or ABORTED (nothing changed; EXECUTE granted back).
--
-- Same window as the swap (revoke, drain every kura and superuser transaction older than
-- the revoke, then one transaction), but that transaction also writes post: every post row
-- whose four columns differ from its work row is copied back from post_tagging_work, as
-- kura_reconciler (the only role the governance trigger lets write them). Only rows that
-- changed since the swap are written. There is no budget on the copy: workers see empty
-- claims and 503 completions while it runs and come back after their leases.
--
-- IF THIS IS INTERRUPTED between "revoked" and the last line, restore EXECUTE with the
-- GRANT in the header of tagging_work_stage3_swap.sql (or rerun this file: it grants on
-- every path after the revoke, and on the "already" path).

\set ON_ERROR_STOP 1
\if :{?drain_budget}
\else
\set drain_budget '7 seconds'
\endif
SET lock_timeout = '2s';

\set all_old false
\set all_new false
SELECT count(*) FILTER (WHERE md5(prosrc) IN ('840d6e4628fa07592056cf51e4e93c73', 'b38a90bb4b14dfce5cf0b6f84ead3a9a',
                                             'f3570ef96eb290c752a51fe57a0eb38d', 'dfd3f327243a0ff374e0f547bfb8a633')) = 4
         AND (SELECT md5(prosrc) FROM pg_proc WHERE oid = 'public.trg_post_tagging_work_mirror()'::regprocedure)
             = 'd7c7ab52d6d2c1e893ec3845f3e5b12b' AS all_old,
       count(*) FILTER (WHERE md5(prosrc) IN ('ad9b30ed14c91f2af9761e3495b4ddee', '49426a50bb69ceb0c41ce0ef0c735658',
                                             '03769e343c34ba756d12cf458ba670c0', '607030e834d0e9c9b7ba155a54eef624')) = 4
         AND (SELECT md5(prosrc) FROM pg_proc WHERE oid = 'public.trg_post_tagging_work_mirror()'::regprocedure)
             = '9bba27dbe5ba40cd8cae85df8cbbbc85' AS all_new
  FROM pg_proc
 WHERE oid IN ('public.tagger_governed_claim_auto_tag(integer, integer)'::regprocedure,
               'public.tagger_governed_complete_ok(integer)'::regprocedure,
               'public.tagger_governed_complete_retry(integer, text, text)'::regprocedure,
               'public.tagger_governed_requeue(integer[], bigint)'::regprocedure)
\gset
\if :all_old
GRANT EXECUTE ON FUNCTION public.tagger_governed_claim_auto_tag(integer, integer),
                          public.tagger_governed_complete_ok(integer),
                          public.tagger_governed_complete_retry(integer, text, text),
                          public.tagger_governed_requeue(integer[], bigint) TO kura;
\echo 'ROLLED BACK (already; EXECUTE granted to kura)'
\quit
\endif
\if :all_new
\else
\echo 'STOPPED: the five bodies are neither all the stage 3 ones nor all the stage 2 ones'
\quit
\endif

-- Only kura, the owner (NOLOGIN) and superusers may call them (the plan's stage 3, step 2).
-- (kura may be missing if an earlier run was interrupted after its revoke; step 4 grants it.)
SELECT bool_and(proacl::text IN ('{kura_reconciler=X/kura_reconciler,kura=X/kura_reconciler}',
                                 '{kura_reconciler=X/kura_reconciler}')) AS acl_ok,
       (SELECT count(*) FROM pg_trigger WHERE tgname IN ('post_tagging_work_mirror_ins', 'post_tagging_work_mirror_upd')
          AND tgrelid = 'public.post'::regclass) = 2 AS mirror_ok,
       (SELECT count(*) FROM pg_auth_members WHERE roleid = 'kura_reconciler'::regrole) = 0
         AND NOT (SELECT rolcanlogin FROM pg_roles WHERE rolname = 'kura_reconciler') AS owner_ok
  FROM pg_proc
 WHERE proname IN ('tagger_governed_claim_auto_tag', 'tagger_governed_complete_ok',
                   'tagger_governed_complete_retry', 'tagger_governed_requeue')
   AND pronamespace = 'public'::regnamespace
\gset
\if :acl_ok
\else
\echo 'STOPPED: EXECUTE on the governed functions is not {kura_reconciler, kura} only'
\quit
\endif
\if :mirror_ok
\else
\echo 'STOPPED: the stage 1 mirror triggers are not both on post'
\quit
\endif
\if :owner_ok
\else
\echo 'STOPPED: kura_reconciler can log in or has members'
\quit
\endif

-- ---- helpers (this session only) ---------------------------------------------------
CREATE FUNCTION pg_temp.stage3_holders(p_since timestamptz) RETURNS integer LANGUAGE sql AS $$
  SELECT count(*)::integer
    FROM pg_stat_activity a
    LEFT JOIN pg_roles r ON r.oid = a.usesysid
   WHERE a.datname = current_database() AND a.pid <> pg_backend_pid()
     -- kura, and every superuser (they bypass the revoke): any transaction of theirs
     -- that began at or before the revoke may still run, or not yet have committed, an
     -- old body. Transactions, not statements: see the header.
     AND (a.usename = 'kura' OR r.rolsuper)
     AND a.xact_start <= p_since
$$;
-- Polls in one transaction: pg_stat_clear_snapshot() makes each poll read fresh activity.
CREATE FUNCTION pg_temp.stage3_drain(p_since timestamptz, p_budget interval) RETURNS boolean LANGUAGE plpgsql AS $$
BEGIN
  LOOP
    PERFORM pg_stat_clear_snapshot();
    IF pg_temp.stage3_holders(p_since) = 0 THEN
      RETURN true;
    END IF;
    IF clock_timestamp() >= p_since + p_budget THEN
      RETURN false;
    END IF;
    PERFORM pg_sleep(0.02);
  END LOOP;
END
$$;
CREATE FUNCTION pg_temp.stage3_within(p_since timestamptz, p_budget interval) RETURNS boolean LANGUAGE plpgsql AS $$
BEGIN
  IF clock_timestamp() >= p_since + p_budget THEN
    RAISE EXCEPTION 'stage 3: window budget % spent', p_budget;
  END IF;
  RETURN true;
END
$$;

-- ---- 1. revoke ---------------------------------------------------------------------
\set drained false
\set swapped false
\set rolled_back false
-- Errors no longer stop the script from here: a REVOKE that fails (for example on its
-- lock_timeout) is reported below and changes nothing.
\set ON_ERROR_STOP 0
\set revoked false
BEGIN;
REVOKE EXECUTE ON FUNCTION public.tagger_governed_claim_auto_tag(integer, integer),
                           public.tagger_governed_complete_ok(integer),
                           public.tagger_governed_complete_retry(integer, text, text),
                           public.tagger_governed_requeue(integer[], bigint) FROM kura;
COMMIT;
SELECT NOT bool_or(has_function_privilege('kura', oid, 'EXECUTE')) AS revoked
  FROM pg_proc
 WHERE oid IN ('public.tagger_governed_claim_auto_tag(integer, integer)'::regprocedure,
               'public.tagger_governed_complete_ok(integer)'::regprocedure,
               'public.tagger_governed_complete_retry(integer, text, text)'::regprocedure,
               'public.tagger_governed_requeue(integer[], bigint)'::regprocedure)
\gset
\if :revoked
\else
-- Not revoked, or not known to be: grant (idempotent) so kura is never left without it.
GRANT EXECUTE ON FUNCTION public.tagger_governed_claim_auto_tag(integer, integer),
                          public.tagger_governed_complete_ok(integer),
                          public.tagger_governed_complete_retry(integer, text, text),
                          public.tagger_governed_requeue(integer[], bigint) TO kura;
\echo 'STOPPED: the revoke did not happen (see the error above); nothing changed'
\quit
\endif
-- From here on, nothing may stop the script before the final GRANT.
SELECT clock_timestamp() AS revoke_at \gset
\echo revoked at :revoke_at

SELECT pg_temp.stage3_drain(:'revoke_at'::timestamptz, :'drain_budget'::interval) AS drained \gset
\if :drained
\echo drained
BEGIN;
SET LOCAL lock_timeout = '5s';
-- The full mirror first, so the copy-back below lands in the work table unchanged.
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

CREATE OR REPLACE FUNCTION public.tagger_governed_claim_auto_tag(p_limit integer, p_lease_seconds integer)
 RETURNS TABLE(id integer, tagging_attempts integer, tagging_locked_until timestamp with time zone, mime_type character varying, post_type character varying)
 LANGUAGE plpgsql
 SECURITY DEFINER
 SET search_path TO 'pg_catalog', 'public'
AS $function$
        BEGIN
            IF session_user NOT IN (SELECT db_role FROM public.tagger_allowed_executor WHERE scope = 'tagging-pipeline') THEN
                RAISE EXCEPTION 'tagger_governed_claim_auto_tag: session_user % is not an allowed executor for scope tagging-pipeline',
                    session_user;
            END IF;
            RETURN QUERY
            WITH picked AS (
                SELECT p.id AS pid
                FROM public.post p
                WHERE p.tagging_status = 'pending'
                  AND p."type" IN ('image', 'animation', 'video')
                  AND p.status = 'ok'
                  AND (p.tagging_locked_until IS NULL OR p.tagging_locked_until < now())
                  AND NOT EXISTS (
                      SELECT 1 FROM public.pool_post pp
                      JOIN public.pool po ON po.id = pp.pool_id
                      WHERE pp.post_id = p.id
                        AND po.tagger_source IS NOT NULL
                        AND po.verify_status = 'pending'
                  )
                  AND NOT EXISTS (
                      SELECT 1 FROM public.pool_post pp
                      JOIN public.tagger_auto_exclude_pool ex ON ex.pool_id = pp.pool_id
                      WHERE pp.post_id = p.id AND ex.excluded
                  )
                ORDER BY p.tagging_locked_until NULLS FIRST, p.id
                LIMIT p_limit
                FOR UPDATE SKIP LOCKED
            )
            UPDATE public.post p
            SET tagging_locked_until = now() + make_interval(secs => p_lease_seconds)
            FROM picked
            WHERE p.id = picked.pid
            RETURNING p.id, p.tagging_attempts, p.tagging_locked_until, p."mime-type", p."type";
        END;
        $function$;
CREATE OR REPLACE FUNCTION public.tagger_governed_complete_ok(p_post_id integer)
 RETURNS void
 LANGUAGE plpgsql
 SECURITY DEFINER
 SET search_path TO 'pg_catalog', 'public'
AS $function$
        BEGIN
            IF session_user NOT IN (SELECT db_role FROM public.tagger_allowed_executor WHERE scope = 'tagging-pipeline') THEN
                RAISE EXCEPTION 'tagger_governed_complete_ok: session_user % is not an allowed executor for scope tagging-pipeline',
                    session_user;
            END IF;
            UPDATE public.post SET tagging_status = 'done',
                tagging_locked_until = NULL, tagging_last_error = NULL
                WHERE id = p_post_id;
        END;
        $function$;
CREATE OR REPLACE FUNCTION public.tagger_governed_complete_retry(p_post_id integer, p_status text, p_error text)
 RETURNS void
 LANGUAGE plpgsql
 SECURITY DEFINER
 SET search_path TO 'pg_catalog', 'public'
AS $function$
        BEGIN
            IF session_user NOT IN (SELECT db_role FROM public.tagger_allowed_executor WHERE scope = 'tagging-pipeline') THEN
                RAISE EXCEPTION 'tagger_governed_complete_retry: session_user % is not an allowed executor for scope tagging-pipeline',
                    session_user;
            END IF;
            UPDATE public.post SET tagging_status = p_status,
                tagging_attempts = tagging_attempts + 1,
                tagging_locked_until = NULL, tagging_last_error = p_error
                WHERE id = p_post_id;
        END;
        $function$;
CREATE OR REPLACE FUNCTION public.tagger_governed_requeue(p_post_ids integer[], p_fencing_token bigint)
 RETURNS integer
 LANGUAGE plpgsql
 SECURITY DEFINER
 SET search_path TO 'pg_catalog', 'public'
AS $function$
        DECLARE
            v_n integer;
        BEGIN
            PERFORM public.tagger_check_fencing('backlog-sweep', p_fencing_token);
            UPDATE public.post SET tagging_status = 'pending', tagging_locked_until = NULL
                WHERE id = ANY(p_post_ids) AND tagging_status = 'done';
            GET DIAGNOSTICS v_n = ROW_COUNT;
            RETURN v_n;
        END;
        $function$;

SET LOCAL ROLE kura_reconciler;
UPDATE public.post p
   SET tagging_status = w.status, tagging_locked_until = w.lease_until,
       tagging_attempts = w.attempts, tagging_last_error = w.last_error
  FROM public.post_tagging_work w
 WHERE w.post_id = p.id
   AND (p.tagging_status, p.tagging_locked_until, p.tagging_attempts, p.tagging_last_error)
       IS DISTINCT FROM (w.status, w.lease_until, w.attempts, w.last_error);
RESET ROLE;
COMMIT;
\else
\echo 'not drained within' :drain_budget
\endif

-- Always: whether the rollback committed or not, kura must end with EXECUTE (idempotent).
GRANT EXECUTE ON FUNCTION public.tagger_governed_claim_auto_tag(integer, integer),
                          public.tagger_governed_complete_ok(integer),
                          public.tagger_governed_complete_retry(integer, text, text),
                          public.tagger_governed_requeue(integer[], bigint) TO kura;
SELECT count(*) FILTER (WHERE md5(prosrc) IN ('840d6e4628fa07592056cf51e4e93c73', 'b38a90bb4b14dfce5cf0b6f84ead3a9a',
                                             'f3570ef96eb290c752a51fe57a0eb38d', 'dfd3f327243a0ff374e0f547bfb8a633')) = 4
         AND (SELECT md5(prosrc) FROM pg_proc WHERE oid = 'public.trg_post_tagging_work_mirror()'::regprocedure)
             = 'd7c7ab52d6d2c1e893ec3845f3e5b12b' AS rolled_back,
       count(*) FILTER (WHERE md5(prosrc) IN ('ad9b30ed14c91f2af9761e3495b4ddee', '49426a50bb69ceb0c41ce0ef0c735658',
                                             '03769e343c34ba756d12cf458ba670c0', '607030e834d0e9c9b7ba155a54eef624')) = 4
         AND (SELECT md5(prosrc) FROM pg_proc WHERE oid = 'public.trg_post_tagging_work_mirror()'::regprocedure)
             = '9bba27dbe5ba40cd8cae85df8cbbbc85' AS still_new
  FROM pg_proc
 WHERE oid IN ('public.tagger_governed_claim_auto_tag(integer, integer)'::regprocedure,
               'public.tagger_governed_complete_ok(integer)'::regprocedure,
               'public.tagger_governed_complete_retry(integer, text, text)'::regprocedure,
               'public.tagger_governed_requeue(integer[], bigint)'::regprocedure)
\gset
SELECT clock_timestamp() - :'revoke_at'::timestamptz AS window_length,
       bool_and(has_function_privilege('kura', oid, 'EXECUTE')) AS kura_can_execute
  FROM pg_proc
 WHERE oid IN ('public.tagger_governed_claim_auto_tag(integer, integer)'::regprocedure,
               'public.tagger_governed_complete_ok(integer)'::regprocedure,
               'public.tagger_governed_complete_retry(integer, text, text)'::regprocedure,
               'public.tagger_governed_requeue(integer[], bigint)'::regprocedure);
\if :rolled_back
\echo ROLLED BACK
\else
\echo ABORTED
\endif
