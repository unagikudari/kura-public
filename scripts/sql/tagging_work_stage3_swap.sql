-- Stage 3 of docs/notes/tagging-work-table-plan.md: the governed functions write
-- post_tagging_work instead of post.
--
-- Run as postgres on primary, with kura-tagger-sweep.timer stopped and its service inactive
-- (the sweep calls tagger_governed_requeue straight from the DB; the API retry cannot
-- cover it). Feed it on stdin (postgres cannot read under the kura user's home):
--   sudo -u postgres psql -X -d kura < scripts/sql/tagging_work_stage3_swap.sql
-- Operational premise: no superuser calls the four functions while this runs (a superuser
-- needs no EXECUTE, so the revoke cannot stop one that starts during the window).
-- The last line says SWAPPED or ABORTED. ABORTED leaves stage 2 exactly as it was
-- (old bodies, EXECUTE granted); look at what held the drain and rerun. Rerunning after
-- SWAPPED does nothing.
--
-- IF THIS IS INTERRUPTED between "revoked" and the last line, kura may be left without
-- EXECUTE (the API then returns empty claims and 503 completions). Restore with:
--   sudo -u postgres psql -X -d kura -c "GRANT EXECUTE ON FUNCTION
--     public.tagger_governed_claim_auto_tag(integer, integer),
--     public.tagger_governed_complete_ok(integer),
--     public.tagger_governed_complete_retry(integer, text, text),
--     public.tagger_governed_requeue(integer[], bigint) TO kura"
--
-- The window (the plan's stage 3, rev4):
-- 1. REVOKE EXECUTE on the four functions from kura and commit. Every call that starts
--    afterwards fails with 42501, which the API retries (api/services/governed_window.py).
-- 2. Drain: wait until no kura transaction that began at or before the revoke is left,
--    and none of any superuser's (they bypass the revoke). Transactions, not statements:
--    a transaction that began before the revoke may still hold a cached copy of the
--    function's ACL and body, and only a new transaction is sure to reload them.
-- 3. Swap and GRANT in one transaction: the four bodies, and the mirror function loses
--    its UPDATE branch. Abort if 2 and 3 are not done DRAIN_BUDGET after the revoke
--    (default 7 seconds; the API's retry gives up at 20).
-- The UPDATE mirror trigger stays on post: dropping it needs ACCESS EXCLUSIVE on post,
-- which would queue every read of post behind the window. After the swap nothing writes
-- the four post columns, and the function ignores updates, so it fires on nothing. It is
-- dropped with the columns in stage 4.

\set ON_ERROR_STOP 1
\if :{?drain_budget}
\else
\set drain_budget '7 seconds'
\endif
SET lock_timeout = '2s';

-- ---- preconditions ----------------------------------------------------------------
-- The five bodies (four governed functions and the mirror) are exactly the ones measured on
-- primary on 2026-10-24 (stage 2), or exactly the ones below (stage 3 already done; md5 as 0183
-- checks them). Anything else stops here, before the revoke.
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
\if :all_new
-- An earlier run may have been interrupted after a revoke (here or in the rollback):
-- granting is idempotent, so make sure kura has EXECUTE.
GRANT EXECUTE ON FUNCTION public.tagger_governed_claim_auto_tag(integer, integer),
                          public.tagger_governed_complete_ok(integer),
                          public.tagger_governed_complete_retry(integer, text, text),
                          public.tagger_governed_requeue(integer[], bigint) TO kura;
\echo 'SWAPPED (already; EXECUTE granted to kura)'
\quit
\endif
\if :all_old
\else
\echo 'STOPPED: the four governed functions are neither all the stage 2 bodies nor all the stage 3 bodies'
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

-- ---- 2. drain ----------------------------------------------------------------------
SELECT pg_temp.stage3_drain(:'revoke_at'::timestamptz, :'drain_budget'::interval) AS drained \gset
\if :drained
\echo drained
-- ---- 3. swap + grant ---------------------------------------------------------------
BEGIN;
SET LOCAL lock_timeout = '1s';

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
            -- Stage 3 (docs/notes/tagging-work-table-plan.md): the queue is public.post_tagging_work.
            -- The eligibility conditions on post and the pools are unchanged; only the work
            -- row is locked and written, so a claim no longer rewrites the post row.
            RETURN QUERY
            WITH picked AS (
                SELECT w.post_id AS pid
                FROM public.post_tagging_work w
                JOIN public.post p ON p.id = w.post_id
                WHERE w.status = 'pending'
                  AND (w.lease_until IS NULL OR w.lease_until < now())
                  AND p."type" IN ('image', 'animation', 'video')
                  AND p.status = 'ok'
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
                ORDER BY w.lease_until NULLS FIRST, w.post_id
                LIMIT p_limit
                FOR UPDATE OF w SKIP LOCKED
            )
            UPDATE public.post_tagging_work w
            SET lease_until = now() + make_interval(secs => p_lease_seconds)
            FROM picked, public.post p
            WHERE w.post_id = picked.pid AND p.id = w.post_id
            RETURNING w.post_id, w.attempts, w.lease_until, p."mime-type", p."type";
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
            UPDATE public.post_tagging_work SET status = 'done',
                lease_until = NULL, last_error = NULL
                WHERE post_id = p_post_id;
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
            UPDATE public.post_tagging_work SET status = p_status,
                attempts = attempts + 1,
                lease_until = NULL, last_error = p_error
                WHERE post_id = p_post_id;
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
            UPDATE public.post_tagging_work SET status = 'pending', lease_until = NULL
                WHERE post_id = ANY(p_post_ids) AND status = 'done';
            GET DIAGNOSTICS v_n = ROW_COUNT;
            RETURN v_n;
        END;
        $function$;

-- Ingest still seeds the work row from the INSERT; updates of the frozen post columns
-- are ignored (only the stage 3 rollback writes them, after restoring the full mirror).
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

GRANT EXECUTE ON FUNCTION public.tagger_governed_claim_auto_tag(integer, integer),
                          public.tagger_governed_complete_ok(integer),
                          public.tagger_governed_complete_retry(integer, text, text),
                          public.tagger_governed_requeue(integer[], bigint) TO kura;
SELECT pg_temp.stage3_within(:'revoke_at'::timestamptz, :'drain_budget'::interval);
COMMIT;
\else
\echo 'not drained within' :drain_budget '- holders:'
SELECT a.pid, a.usename, a.state, a.xact_start, left(a.query, 80) AS query
  FROM pg_stat_activity a LEFT JOIN pg_roles r ON r.oid = a.usesysid
 WHERE a.datname = current_database() AND a.pid <> pg_backend_pid()
   AND (a.usename = 'kura' OR r.rolsuper) AND a.xact_start <= :'revoke_at'::timestamptz;
\endif

-- Always: whether the swap committed or not, kura must end with EXECUTE (idempotent).
GRANT EXECUTE ON FUNCTION public.tagger_governed_claim_auto_tag(integer, integer),
                          public.tagger_governed_complete_ok(integer),
                          public.tagger_governed_complete_retry(integer, text, text),
                          public.tagger_governed_requeue(integer[], bigint) TO kura;
SELECT count(*) FILTER (WHERE md5(prosrc) IN ('840d6e4628fa07592056cf51e4e93c73', 'b38a90bb4b14dfce5cf0b6f84ead3a9a',
                                             'f3570ef96eb290c752a51fe57a0eb38d', 'dfd3f327243a0ff374e0f547bfb8a633')) = 4
         AND (SELECT md5(prosrc) FROM pg_proc WHERE oid = 'public.trg_post_tagging_work_mirror()'::regprocedure)
             = 'd7c7ab52d6d2c1e893ec3845f3e5b12b' AS stage2_bodies,
       count(*) FILTER (WHERE md5(prosrc) IN ('ad9b30ed14c91f2af9761e3495b4ddee', '49426a50bb69ceb0c41ce0ef0c735658',
                                             '03769e343c34ba756d12cf458ba670c0', '607030e834d0e9c9b7ba155a54eef624')) = 4
         AND (SELECT md5(prosrc) FROM pg_proc WHERE oid = 'public.trg_post_tagging_work_mirror()'::regprocedure)
             = '9bba27dbe5ba40cd8cae85df8cbbbc85' AS swapped
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
\if :swapped
\echo SWAPPED
\else
\echo ABORTED
\endif
