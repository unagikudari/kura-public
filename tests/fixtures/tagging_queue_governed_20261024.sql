-- The production shape the tagging-work stages build on, as of 2026-10-24 (kura DB 0180).
-- Function bodies are pg_get_functiondef() output copied verbatim from primary; the tables
-- carry only the columns those functions and the stage SQL touch.
-- Loaded by tests/test_tagging_work_stage1.py into a throwaway cluster as its superuser.

-- Roles are cluster-wide; a second database in the same test cluster reuses them.
DO $$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'kura') THEN CREATE ROLE kura LOGIN; END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'kura_reconciler') THEN CREATE ROLE kura_reconciler NOLOGIN; END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'replicator') THEN CREATE ROLE replicator LOGIN REPLICATION; END IF;
END $$;

-- primary's pg_default_acl (2026-10-24): what postgres creates in public, kura may fully use.
-- The stage SQL runs as postgres, so its tables are born with these grants.
ALTER DEFAULT PRIVILEGES FOR ROLE postgres IN SCHEMA public GRANT ALL ON TABLES TO kura;
ALTER DEFAULT PRIVILEGES FOR ROLE postgres IN SCHEMA public GRANT SELECT, UPDATE, USAGE ON SEQUENCES TO kura;

CREATE TABLE public.post (
  id integer PRIMARY KEY,
  "type" varchar(32) NOT NULL,
  status varchar(32),
  "mime-type" varchar(32) NOT NULL DEFAULT 'image/png',
  tagging_status text NOT NULL DEFAULT 'pending',
  tagging_locked_until timestamptz,
  tagging_attempts integer NOT NULL DEFAULT 0,
  tagging_last_error text
);
ALTER TABLE public.post OWNER TO kura;
GRANT SELECT ON public.post TO kura_reconciler, replicator;
GRANT UPDATE (tagging_status, tagging_locked_until, tagging_attempts, tagging_last_error) ON public.post TO kura_reconciler;

CREATE TABLE public.pool (id integer PRIMARY KEY, tagger_source text, verify_status text);
CREATE TABLE public.pool_post (pool_id integer, post_id integer);
CREATE TABLE public.tagger_auto_exclude_pool (pool_id integer, excluded boolean);
CREATE TABLE public.tagger_allowed_executor (db_role text, scope text, added_at timestamptz DEFAULT now(), added_by text);
ALTER TABLE public.pool OWNER TO kura; ALTER TABLE public.pool_post OWNER TO kura;
ALTER TABLE public.tagger_auto_exclude_pool OWNER TO kura; ALTER TABLE public.tagger_allowed_executor OWNER TO kura;
GRANT SELECT ON public.pool, public.pool_post, public.tagger_auto_exclude_pool, public.tagger_allowed_executor TO kura_reconciler;
INSERT INTO public.tagger_allowed_executor (db_role, scope) VALUES ('kura', 'tagging-pipeline');

-- Stand-in: the fencing lease is not what these tests are about.
CREATE FUNCTION public.tagger_check_fencing(p_scope text, p_fencing_token bigint) RETURNS void
  LANGUAGE plpgsql SECURITY DEFINER SET search_path TO 'pg_catalog', 'public' AS $$ BEGIN END $$;
ALTER FUNCTION public.tagger_check_fencing(text, bigint) OWNER TO kura_reconciler;


CREATE OR REPLACE FUNCTION public.trg_post_tagging_columns_governed()
 RETURNS trigger
 LANGUAGE plpgsql
AS $function$
        BEGIN
            IF current_user <> 'kura_reconciler' AND (
                NEW.tagging_status IS DISTINCT FROM OLD.tagging_status
                OR NEW.tagging_locked_until IS DISTINCT FROM OLD.tagging_locked_until
                OR NEW.tagging_attempts IS DISTINCT FROM OLD.tagging_attempts
                OR NEW.tagging_last_error IS DISTINCT FROM OLD.tagging_last_error
            ) THEN
                RAISE EXCEPTION
                    'post.tagging_* columns are governed: direct write from role % is not permitted, use a governed function (e.g. tagger_governed_requeue)',
                    current_user;
            END IF;
            RETURN NEW;
        END;
        $function$;
ALTER FUNCTION public.trg_post_tagging_columns_governed() OWNER TO kura;
CREATE TRIGGER trg_post_tagging_columns_governed BEFORE UPDATE ON public.post FOR EACH ROW EXECUTE FUNCTION trg_post_tagging_columns_governed();

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
ALTER FUNCTION public.tagger_governed_claim_auto_tag(integer, integer) OWNER TO kura_reconciler;
REVOKE ALL ON FUNCTION public.tagger_governed_claim_auto_tag(integer, integer) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION public.tagger_governed_claim_auto_tag(integer, integer) TO kura;

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
ALTER FUNCTION public.tagger_governed_complete_ok(integer) OWNER TO kura_reconciler;
REVOKE ALL ON FUNCTION public.tagger_governed_complete_ok(integer) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION public.tagger_governed_complete_ok(integer) TO kura;

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
ALTER FUNCTION public.tagger_governed_complete_retry(integer, text, text) OWNER TO kura_reconciler;
REVOKE ALL ON FUNCTION public.tagger_governed_complete_retry(integer, text, text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION public.tagger_governed_complete_retry(integer, text, text) TO kura;

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
ALTER FUNCTION public.tagger_governed_requeue(integer[], bigint) OWNER TO kura_reconciler;
REVOKE ALL ON FUNCTION public.tagger_governed_requeue(integer[], bigint) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION public.tagger_governed_requeue(integer[], bigint) TO kura;
