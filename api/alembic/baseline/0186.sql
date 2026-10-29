-- kura fresh-install baseline at revision 0186 (scripts/schema_baseline.py, generator 2).
-- Loaded by ops/install/database.py create as a superuser; never edited by hand.
--
-- PostgreSQL database dump
--



SET statement_timeout = 0;
SET lock_timeout = 0;
SET idle_in_transaction_session_timeout = 0;
SET transaction_timeout = 0;
SET client_encoding = 'UTF8';
SET standard_conforming_strings = on;
SELECT pg_catalog.set_config('search_path', '', false);
SET check_function_bodies = false;
SET xmloption = content;
SET client_min_messages = warning;
SET row_security = off;

--
-- Name: claim_storage; Type: SCHEMA; Schema: -; Owner: kura
--

CREATE SCHEMA claim_storage;


ALTER SCHEMA claim_storage OWNER TO kura;

--
-- Name: collector_next; Type: SCHEMA; Schema: -; Owner: kura
--

CREATE SCHEMA collector_next;


ALTER SCHEMA collector_next OWNER TO kura;

--
-- Name: public; Type: SCHEMA; Schema: -; Owner: kura
--

-- *not* creating schema, since initdb creates it


ALTER SCHEMA public OWNER TO kura;

--
-- Name: SCHEMA public; Type: COMMENT; Schema: -; Owner: kura
--

COMMENT ON SCHEMA public IS '';


--
-- Name: fuzzystrmatch; Type: EXTENSION; Schema: -; Owner: -
--



--
-- Name: EXTENSION fuzzystrmatch; Type: COMMENT; Schema: -; Owner: 
--



--
-- Name: pg_trgm; Type: EXTENSION; Schema: -; Owner: -
--



--
-- Name: EXTENSION pg_trgm; Type: COMMENT; Schema: -; Owner: 
--



--
-- Name: vector; Type: EXTENSION; Schema: -; Owner: -
--



--
-- Name: EXTENSION vector; Type: COMMENT; Schema: -; Owner: 
--



--
-- Name: actor_read_only(); Type: FUNCTION; Schema: claim_storage; Owner: kura
--

CREATE FUNCTION claim_storage.actor_read_only() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
BEGIN RAISE EXCEPTION 'Claimant identity is immutable; register a distinct source identity'; END $$;


ALTER FUNCTION claim_storage.actor_read_only() OWNER TO kura;

--
-- Name: legacy_write(); Type: FUNCTION; Schema: claim_storage; Owner: kura
--

CREATE FUNCTION claim_storage.legacy_write() RETURNS trigger
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'pg_catalog'
    AS $$
DECLARE actor bigint; affected integer;
BEGIN
 IF TG_RELID<>'public.post_tag_actor_claim'::regclass THEN
   RAISE EXCEPTION 'Claim compatibility writer is restricted to its public view';
 END IF;
 IF TG_OP='DELETE' THEN
   DELETE FROM claim_storage.claim c USING claim_storage.actor a
   WHERE c.post_id=OLD.post_id AND c.tag_id=OLD.tag_id AND c.actor_key=a.id AND a.actor_id=OLD.actor_id;
   GET DIAGNOSTICS affected=ROW_COUNT;
   IF affected=0 THEN RETURN NULL; END IF;
   RETURN OLD;
 END IF;
 INSERT INTO claim_storage.actor(actor_id) VALUES(NEW.actor_id) ON CONFLICT DO NOTHING;
 SELECT id INTO STRICT actor FROM claim_storage.actor WHERE actor_id=NEW.actor_id;
 IF TG_OP='INSERT' THEN
   INSERT INTO claim_storage.claim(post_id,tag_id,actor_key,claimed_at)
   VALUES(NEW.post_id,NEW.tag_id,actor,NEW.claimed_at);
 ELSE
   UPDATE claim_storage.claim c SET post_id=NEW.post_id,tag_id=NEW.tag_id,
     actor_key=actor,claimed_at=NEW.claimed_at FROM claim_storage.actor a
   WHERE c.post_id=OLD.post_id AND c.tag_id=OLD.tag_id AND c.actor_key=a.id AND a.actor_id=OLD.actor_id;
   GET DIAGNOSTICS affected=ROW_COUNT;
   IF affected=0 THEN RETURN NULL; END IF;
 END IF;
 RETURN NEW;
END $$;


ALTER FUNCTION claim_storage.legacy_write() OWNER TO kura;

--
-- Name: reject_label_rewrite(); Type: FUNCTION; Schema: collector_next; Owner: kura
--

CREATE FUNCTION collector_next.reject_label_rewrite() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
BEGIN RAISE EXCEPTION 'collector label evidence is append-only'; END;
$$;


ALTER FUNCTION collector_next.reject_label_rewrite() OWNER TO kura;

--
-- Name: attach_source_pool(); Type: FUNCTION; Schema: public; Owner: kura
--

CREATE FUNCTION public.attach_source_pool() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
DECLARE
    pr RECORD;
    nxt INT;
BEGIN
    IF NEW.source IS NULL OR NEW.source = '' THEN
        RETURN NEW;
    END IF;
    IF TG_OP = 'UPDATE' AND OLD.source IS NOT DISTINCT FROM NEW.source THEN
        RETURN NEW;
    END IF;
    FOR pr IN
        SELECT pool_id FROM pool_url WHERE url = NEW.source
    LOOP
        SELECT COALESCE(MAX(ord), -1) + 1 INTO nxt
          FROM pool_post WHERE pool_id = pr.pool_id;
        INSERT INTO pool_post (pool_id, post_id, ord)
        VALUES (pr.pool_id, NEW.id, nxt)
        ON CONFLICT (pool_id, post_id) DO NOTHING;
    END LOOP;
    RETURN NEW;
END;
$$;


ALTER FUNCTION public.attach_source_pool() OWNER TO kura;

--
-- Name: censor_annotation_dup_skip(); Type: FUNCTION; Schema: public; Owner: kura
--

CREATE FUNCTION public.censor_annotation_dup_skip() RETURNS trigger
    LANGUAGE plpgsql
    SET search_path TO 'public', 'pg_catalog'
    AS $$
BEGIN
    -- origin (app write) passes through so real UNIQUE / ON CONFLICT apply;
    -- only apply-worker ('replica') and tablesync ('local') dedup.
    IF current_setting('session_replication_role', true) = 'origin' THEN RETURN NEW; END IF;
    IF EXISTS (SELECT 1 FROM public."censor_annotation" WHERE id=NEW.id) THEN RETURN NULL; END IF;
    RETURN NEW;
END;
$$;


ALTER FUNCTION public.censor_annotation_dup_skip() OWNER TO kura;

--
-- Name: censor_pair_dup_skip(); Type: FUNCTION; Schema: public; Owner: kura
--

CREATE FUNCTION public.censor_pair_dup_skip() RETURNS trigger
    LANGUAGE plpgsql
    SET search_path TO 'public', 'pg_catalog'
    AS $$
BEGIN
    -- origin (app write) passes through so real UNIQUE / ON CONFLICT apply;
    -- only apply-worker ('replica') and tablesync ('local') dedup.
    IF current_setting('session_replication_role', true) = 'origin' THEN RETURN NEW; END IF;
    IF EXISTS (SELECT 1 FROM public."censor_pair" WHERE id=NEW.id) THEN RETURN NULL; END IF;
    RETURN NEW;
END;
$$;


ALTER FUNCTION public.censor_pair_dup_skip() OWNER TO kura;

--
-- Name: censor_pair_search_dup_skip(); Type: FUNCTION; Schema: public; Owner: kura
--

CREATE FUNCTION public.censor_pair_search_dup_skip() RETURNS trigger
    LANGUAGE plpgsql
    SET search_path TO 'public', 'pg_catalog'
    AS $$
BEGIN
    -- origin (app write) passes through so real UNIQUE / ON CONFLICT apply;
    -- only apply-worker ('replica') and tablesync ('local') dedup.
    IF current_setting('session_replication_role', true) = 'origin' THEN RETURN NEW; END IF;
    IF EXISTS (SELECT 1 FROM public."censor_pair_search" WHERE post_id=NEW.post_id) THEN RETURN NULL; END IF;
    RETURN NEW;
END;
$$;


ALTER FUNCTION public.censor_pair_search_dup_skip() OWNER TO kura;

--
-- Name: censor_pair_search_scope_dup_skip(); Type: FUNCTION; Schema: public; Owner: kura
--

CREATE FUNCTION public.censor_pair_search_scope_dup_skip() RETURNS trigger
    LANGUAGE plpgsql
    SET search_path TO 'public', 'pg_catalog'
    AS $$
BEGIN
    -- origin (app write) passes through so real UNIQUE / ON CONFLICT apply;
    -- only apply-worker ('replica') and tablesync ('local') dedup.
    IF current_setting('session_replication_role', true) = 'origin' THEN RETURN NEW; END IF;
    IF EXISTS (SELECT 1 FROM public."censor_pair_search_scope" WHERE pool_id=NEW.pool_id) THEN RETURN NULL; END IF;
    RETURN NEW;
END;
$$;


ALTER FUNCTION public.censor_pair_search_scope_dup_skip() OWNER TO kura;

--
-- Name: censor_trained_post_dup_skip(); Type: FUNCTION; Schema: public; Owner: kura
--

CREATE FUNCTION public.censor_trained_post_dup_skip() RETURNS trigger
    LANGUAGE plpgsql
    SET search_path TO 'public', 'pg_catalog'
    AS $$
BEGIN
    -- origin (app write) passes through so real UNIQUE / ON CONFLICT apply;
    -- only apply-worker ('replica') and tablesync ('local') dedup.
    IF current_setting('session_replication_role', true) = 'origin' THEN RETURN NEW; END IF;
    IF EXISTS (SELECT 1 FROM public."censor_trained_post" WHERE post_id=NEW.post_id) THEN RETURN NULL; END IF;
    RETURN NEW;
END;
$$;


ALTER FUNCTION public.censor_trained_post_dup_skip() OWNER TO kura;

--
-- Name: comment_dup_skip(); Type: FUNCTION; Schema: public; Owner: kura
--

CREATE FUNCTION public.comment_dup_skip() RETURNS trigger
    LANGUAGE plpgsql
    SET search_path TO 'public', 'pg_catalog'
    AS $$
BEGIN
    -- origin (app write) passes through so real UNIQUE / ON CONFLICT apply;
    -- only apply-worker ('replica') and tablesync ('local') dedup.
    IF current_setting('session_replication_role', true) = 'origin' THEN RETURN NEW; END IF;
    IF EXISTS (SELECT 1 FROM public."comment" WHERE id=NEW.id) THEN RETURN NULL; END IF;
    RETURN NEW;
END;
$$;


ALTER FUNCTION public.comment_dup_skip() OWNER TO kura;

--
-- Name: comment_score_dup_skip(); Type: FUNCTION; Schema: public; Owner: kura
--

CREATE FUNCTION public.comment_score_dup_skip() RETURNS trigger
    LANGUAGE plpgsql
    SET search_path TO 'public', 'pg_catalog'
    AS $$
BEGIN
    -- origin (app write) passes through so real UNIQUE / ON CONFLICT apply;
    -- only apply-worker ('replica') and tablesync ('local') dedup.
    IF current_setting('session_replication_role', true) = 'origin' THEN RETURN NEW; END IF;
    IF EXISTS (SELECT 1 FROM public."comment_score" WHERE comment_id=NEW.comment_id AND user_id=NEW.user_id) THEN RETURN NULL; END IF;
    RETURN NEW;
END;
$$;


ALTER FUNCTION public.comment_score_dup_skip() OWNER TO kura;

--
-- Name: lora_concept_dup_skip(); Type: FUNCTION; Schema: public; Owner: kura
--

CREATE FUNCTION public.lora_concept_dup_skip() RETURNS trigger
    LANGUAGE plpgsql
    SET search_path TO 'public', 'pg_catalog'
    AS $$
BEGIN
    -- origin (app write) passes through so real UNIQUE / ON CONFLICT apply;
    -- only apply-worker ('replica') and tablesync ('local') dedup.
    IF current_setting('session_replication_role', true) = 'origin' THEN RETURN NEW; END IF;
    IF EXISTS (SELECT 1 FROM public."lora_concept" WHERE id=NEW.id) THEN RETURN NULL; END IF;
    RETURN NEW;
END;
$$;


ALTER FUNCTION public.lora_concept_dup_skip() OWNER TO kura;

--
-- Name: lora_model_dup_skip(); Type: FUNCTION; Schema: public; Owner: kura
--

CREATE FUNCTION public.lora_model_dup_skip() RETURNS trigger
    LANGUAGE plpgsql
    SET search_path TO 'public', 'pg_catalog'
    AS $$
BEGIN
    -- origin (app write) passes through so real UNIQUE / ON CONFLICT apply;
    -- only apply-worker ('replica') and tablesync ('local') dedup.
    IF current_setting('session_replication_role', true) = 'origin' THEN RETURN NEW; END IF;
    IF EXISTS (SELECT 1 FROM public."lora_model" WHERE id=NEW.id) THEN RETURN NULL; END IF;
    RETURN NEW;
END;
$$;


ALTER FUNCTION public.lora_model_dup_skip() OWNER TO kura;

--
-- Name: lora_model_view_insert(); Type: FUNCTION; Schema: public; Owner: kura
--

CREATE FUNCTION public.lora_model_view_insert() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
    DECLARE aid integer;
    BEGIN
        INSERT INTO lora_asset (id, name, status, origin, kind, base_model, canonical_path,
                                comfy_ref, sha256, size_bytes, created_by, source_url,
                                preview_post_id, imported_at, created_at, updated_at, notes)
        VALUES (COALESCE(NEW.id, nextval('lora_asset_id_seq')), NEW.name,
                CASE WHEN NEW.status IN ('available', 'retired') THEN NEW.status ELSE 'pending' END,
                COALESCE(NEW.origin, 'trained'), NEW.kind, NEW.base_model, NEW.canonical_path,
                NEW.comfy_ref, NEW.sha256, NEW.size_bytes, NEW.created_by, NEW.source_url,
                NEW.preview_post_id, NEW.imported_at, COALESCE(NEW.created_at, now()),
                COALESCE(NEW.updated_at, now()), NEW.notes)
        RETURNING id INTO aid;
        IF NEW.status IN ('queued', 'training', 'failed')
           OR NEW.params IS NOT NULL OR NEW.job_spec IS NOT NULL
           OR NEW.cur_step IS NOT NULL OR NEW.final_loss IS NOT NULL THEN
            INSERT INTO lora_training_run (asset_id, state, cur_step, total_steps, cur_epoch,
                                           eta_seconds, progress_at, final_loss, params,
                                           job_spec, started_at)
            VALUES (aid,
                    CASE NEW.status WHEN 'training' THEN 'running'
                                    WHEN 'failed' THEN 'failed'
                                    WHEN 'queued' THEN 'queued'
                                    ELSE 'done' END,
                    NEW.cur_step, NEW.total_steps, NEW.cur_epoch, NEW.eta_seconds,
                    NEW.progress_at, NEW.final_loss, NEW.params, NEW.job_spec,
                    CASE WHEN NEW.status = 'training' THEN now() END);
        END IF;
        NEW.id := aid;
        RETURN NEW;
    END $$;


ALTER FUNCTION public.lora_model_view_insert() OWNER TO kura;

--
-- Name: lora_post_dup_skip(); Type: FUNCTION; Schema: public; Owner: kura
--

CREATE FUNCTION public.lora_post_dup_skip() RETURNS trigger
    LANGUAGE plpgsql
    SET search_path TO 'public', 'pg_catalog'
    AS $$
BEGIN
    -- origin (app write) passes through so real UNIQUE / ON CONFLICT apply;
    -- only apply-worker ('replica') and tablesync ('local') dedup.
    IF current_setting('session_replication_role', true) = 'origin' THEN RETURN NEW; END IF;
    IF EXISTS (SELECT 1 FROM public."lora_post" WHERE id=NEW.id) THEN RETURN NULL; END IF;
    RETURN NEW;
END;
$$;


ALTER FUNCTION public.lora_post_dup_skip() OWNER TO kura;

--
-- Name: pool_best_cover(integer); Type: FUNCTION; Schema: public; Owner: kura
--

CREATE FUNCTION public.pool_best_cover(p_pool_id integer) RETURNS integer
    LANGUAGE sql STABLE
    AS $$
    SELECT pp.post_id
    FROM pool_post pp
    JOIN post po ON po.id = pp.post_id
    WHERE pp.pool_id = p_pool_id
    ORDER BY (po.status = 'ok' AND po.thumbnail IS NOT NULL
              AND octet_length(po.thumbnail) >= 1000) DESC,
             pp.ord ASC
    LIMIT 1
$$;


ALTER FUNCTION public.pool_best_cover(p_pool_id integer) OWNER TO kura;

--
-- Name: pool_category_dup_skip(); Type: FUNCTION; Schema: public; Owner: kura
--

CREATE FUNCTION public.pool_category_dup_skip() RETURNS trigger
    LANGUAGE plpgsql
    SET search_path TO 'public', 'pg_catalog'
    AS $$
BEGIN
    -- origin (app write) passes through so real UNIQUE / ON CONFLICT apply;
    -- only apply-worker ('replica') and tablesync ('local') dedup.
    IF current_setting('session_replication_role', true) = 'origin' THEN RETURN NEW; END IF;
    IF EXISTS (SELECT 1 FROM public."pool_category" WHERE id=NEW.id) THEN RETURN NULL; END IF;
    RETURN NEW;
END;
$$;


ALTER FUNCTION public.pool_category_dup_skip() OWNER TO kura;

--
-- Name: pool_dup_skip(); Type: FUNCTION; Schema: public; Owner: kura
--

CREATE FUNCTION public.pool_dup_skip() RETURNS trigger
    LANGUAGE plpgsql
    SET search_path TO 'public', 'pg_catalog'
    AS $$
BEGIN
    -- origin (app write) passes through so real UNIQUE / ON CONFLICT apply;
    -- only apply-worker ('replica') and tablesync ('local') dedup.
    IF current_setting('session_replication_role', true) = 'origin' THEN RETURN NEW; END IF;
    IF EXISTS (SELECT 1 FROM public."pool" WHERE id=NEW.id) THEN RETURN NULL; END IF;
    RETURN NEW;
END;
$$;


ALTER FUNCTION public.pool_dup_skip() OWNER TO kura;

--
-- Name: pool_name_dup_skip(); Type: FUNCTION; Schema: public; Owner: kura
--

CREATE FUNCTION public.pool_name_dup_skip() RETURNS trigger
    LANGUAGE plpgsql
    SET search_path TO 'public', 'pg_catalog'
    AS $$
BEGIN
    -- origin (app write) passes through so real UNIQUE / ON CONFLICT apply;
    -- only apply-worker ('replica') and tablesync ('local') dedup.
    IF current_setting('session_replication_role', true) = 'origin' THEN RETURN NEW; END IF;
    IF EXISTS (SELECT 1 FROM public."pool_name" WHERE pool_name_id=NEW.pool_name_id) THEN RETURN NULL; END IF;
    RETURN NEW;
END;
$$;


ALTER FUNCTION public.pool_name_dup_skip() OWNER TO kura;

--
-- Name: pool_post_dup_skip(); Type: FUNCTION; Schema: public; Owner: kura
--

CREATE FUNCTION public.pool_post_dup_skip() RETURNS trigger
    LANGUAGE plpgsql
    SET search_path TO 'public', 'pg_catalog'
    AS $$
BEGIN
    -- origin (app write) passes through so real UNIQUE / ON CONFLICT apply;
    -- only apply-worker ('replica') and tablesync ('local') dedup.
    IF current_setting('session_replication_role', true) = 'origin' THEN RETURN NEW; END IF;
    IF EXISTS (SELECT 1 FROM public."pool_post" WHERE pool_id=NEW.pool_id AND post_id=NEW.post_id) THEN RETURN NULL; END IF;
    RETURN NEW;
END;
$$;


ALTER FUNCTION public.pool_post_dup_skip() OWNER TO kura;

--
-- Name: pool_post_sync_cover(); Type: FUNCTION; Schema: public; Owner: kura
--

CREATE FUNCTION public.pool_post_sync_cover() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
DECLARE v INT;
BEGIN
    IF TG_OP = 'INSERT' THEN
        v := pool_best_cover(NEW.pool_id);
        UPDATE pool SET cover_post_id = v, last_edit_time = NOW()
        WHERE id = NEW.pool_id AND cover_post_id IS DISTINCT FROM v;
        RETURN NEW;
    ELSIF TG_OP = 'DELETE' THEN
        v := pool_best_cover(OLD.pool_id);
        UPDATE pool SET cover_post_id = v, last_edit_time = NOW()
        WHERE id = OLD.pool_id AND cover_post_id IS DISTINCT FROM v;
        RETURN OLD;
    END IF;
    RETURN NULL;
END;
$$;


ALTER FUNCTION public.pool_post_sync_cover() OWNER TO kura;

--
-- Name: pool_state_dup_skip(); Type: FUNCTION; Schema: public; Owner: kura
--

CREATE FUNCTION public.pool_state_dup_skip() RETURNS trigger
    LANGUAGE plpgsql
    SET search_path TO 'public', 'pg_catalog'
    AS $$
BEGIN
    -- origin (app write) passes through so real UNIQUE / ON CONFLICT apply;
    -- only apply-worker ('replica') and tablesync ('local') dedup.
    IF current_setting('session_replication_role', true) = 'origin' THEN RETURN NEW; END IF;
    IF EXISTS (SELECT 1 FROM public."pool_state" WHERE pool_id=NEW.pool_id) THEN RETURN NULL; END IF;
    RETURN NEW;
END;
$$;


ALTER FUNCTION public.pool_state_dup_skip() OWNER TO kura;

--
-- Name: pool_url_dup_skip(); Type: FUNCTION; Schema: public; Owner: kura
--

CREATE FUNCTION public.pool_url_dup_skip() RETURNS trigger
    LANGUAGE plpgsql
    SET search_path TO 'public', 'pg_catalog'
    AS $$
BEGIN
    -- origin (app write) passes through so real UNIQUE / ON CONFLICT apply;
    -- only apply-worker ('replica') and tablesync ('local') dedup.
    IF current_setting('session_replication_role', true) = 'origin' THEN RETURN NEW; END IF;
    IF EXISTS (SELECT 1 FROM public."pool_url" WHERE pool_id=NEW.pool_id AND url=NEW.url) THEN RETURN NULL; END IF;
    RETURN NEW;
END;
$$;


ALTER FUNCTION public.pool_url_dup_skip() OWNER TO kura;

--
-- Name: post_censor_review_dup_skip(); Type: FUNCTION; Schema: public; Owner: kura
--

CREATE FUNCTION public.post_censor_review_dup_skip() RETURNS trigger
    LANGUAGE plpgsql
    SET search_path TO 'public', 'pg_catalog'
    AS $$
BEGIN
    -- origin (app write) passes through so real UNIQUE / ON CONFLICT apply;
    -- only apply-worker ('replica') and tablesync ('local') dedup.
    IF current_setting('session_replication_role', true) = 'origin' THEN RETURN NEW; END IF;
    IF EXISTS (SELECT 1 FROM public."post_censor_review" WHERE id=NEW.id) THEN RETURN NULL; END IF;
    RETURN NEW;
END;
$$;


ALTER FUNCTION public.post_censor_review_dup_skip() OWNER TO kura;

--
-- Name: post_dup_skip(); Type: FUNCTION; Schema: public; Owner: kura
--

CREATE FUNCTION public.post_dup_skip() RETURNS trigger
    LANGUAGE plpgsql
    SET search_path TO 'public', 'pg_catalog'
    AS $$
BEGIN
    -- origin (app write) passes through so real UNIQUE / ON CONFLICT apply;
    -- only apply-worker ('replica') and tablesync ('local') dedup.
    IF current_setting('session_replication_role', true) = 'origin' THEN RETURN NEW; END IF;
    IF EXISTS (SELECT 1 FROM public."post" WHERE id=NEW.id) THEN RETURN NULL; END IF;
    RETURN NEW;
END;
$$;


ALTER FUNCTION public.post_dup_skip() OWNER TO kura;

--
-- Name: post_favorite_dup_skip(); Type: FUNCTION; Schema: public; Owner: kura
--

CREATE FUNCTION public.post_favorite_dup_skip() RETURNS trigger
    LANGUAGE plpgsql
    SET search_path TO 'public', 'pg_catalog'
    AS $$
BEGIN
    -- origin (app write) passes through so real UNIQUE / ON CONFLICT apply;
    -- only apply-worker ('replica') and tablesync ('local') dedup.
    IF current_setting('session_replication_role', true) = 'origin' THEN RETURN NEW; END IF;
    IF EXISTS (SELECT 1 FROM public."post_favorite" WHERE post_id=NEW.post_id AND user_id=NEW.user_id) THEN RETURN NULL; END IF;
    RETURN NEW;
END;
$$;


ALTER FUNCTION public.post_favorite_dup_skip() OWNER TO kura;

--
-- Name: post_feature_dup_skip(); Type: FUNCTION; Schema: public; Owner: kura
--

CREATE FUNCTION public.post_feature_dup_skip() RETURNS trigger
    LANGUAGE plpgsql
    SET search_path TO 'public', 'pg_catalog'
    AS $$
BEGIN
    -- origin (app write) passes through so real UNIQUE / ON CONFLICT apply;
    -- only apply-worker ('replica') and tablesync ('local') dedup.
    IF current_setting('session_replication_role', true) = 'origin' THEN RETURN NEW; END IF;
    IF EXISTS (SELECT 1 FROM public."post_feature" WHERE id=NEW.id) THEN RETURN NULL; END IF;
    RETURN NEW;
END;
$$;


ALTER FUNCTION public.post_feature_dup_skip() OWNER TO kura;

--
-- Name: post_note_dup_skip(); Type: FUNCTION; Schema: public; Owner: kura
--

CREATE FUNCTION public.post_note_dup_skip() RETURNS trigger
    LANGUAGE plpgsql
    SET search_path TO 'public', 'pg_catalog'
    AS $$
BEGIN
    -- origin (app write) passes through so real UNIQUE / ON CONFLICT apply;
    -- only apply-worker ('replica') and tablesync ('local') dedup.
    IF current_setting('session_replication_role', true) = 'origin' THEN RETURN NEW; END IF;
    IF EXISTS (SELECT 1 FROM public."post_note" WHERE id=NEW.id) THEN RETURN NULL; END IF;
    RETURN NEW;
END;
$$;


ALTER FUNCTION public.post_note_dup_skip() OWNER TO kura;

--
-- Name: post_rendition_follow_legacy(); Type: FUNCTION; Schema: public; Owner: kura
--

CREATE FUNCTION public.post_rendition_follow_legacy() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
BEGIN
  IF current_setting('kura.rendition_cutover', true) = 'on' THEN
    RETURN NEW;
  END IF;
  IF NEW.data IS DISTINCT FROM OLD.data THEN
    DELETE FROM public.post_rendition WHERE post_id = NEW.id AND kind = 'view';
  END IF;
  IF NEW.thumbnail IS DISTINCT FROM OLD.thumbnail THEN
    DELETE FROM public.post_rendition WHERE post_id = NEW.id AND kind = 'thumb';
  END IF;
  RETURN NEW;
END $$;


ALTER FUNCTION public.post_rendition_follow_legacy() OWNER TO kura;

--
-- Name: post_score_dup_skip(); Type: FUNCTION; Schema: public; Owner: kura
--

CREATE FUNCTION public.post_score_dup_skip() RETURNS trigger
    LANGUAGE plpgsql
    SET search_path TO 'public', 'pg_catalog'
    AS $$
BEGIN
    -- origin (app write) passes through so real UNIQUE / ON CONFLICT apply;
    -- only apply-worker ('replica') and tablesync ('local') dedup.
    IF current_setting('session_replication_role', true) = 'origin' THEN RETURN NEW; END IF;
    IF EXISTS (SELECT 1 FROM public."post_score" WHERE post_id=NEW.post_id AND user_id=NEW.user_id) THEN RETURN NULL; END IF;
    RETURN NEW;
END;
$$;


ALTER FUNCTION public.post_score_dup_skip() OWNER TO kura;

--
-- Name: post_status_sync_pool_cover(); Type: FUNCTION; Schema: public; Owner: kura
--

CREATE FUNCTION public.post_status_sync_pool_cover() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
DECLARE r RECORD; v INT;
BEGIN
    FOR r IN SELECT pool_id FROM pool_post WHERE post_id = NEW.id LOOP
        v := pool_best_cover(r.pool_id);
        UPDATE pool SET cover_post_id = v, last_edit_time = NOW()
        WHERE id = r.pool_id AND cover_post_id IS DISTINCT FROM v;
    END LOOP;
    RETURN NEW;
END;
$$;


ALTER FUNCTION public.post_status_sync_pool_cover() OWNER TO kura;

--
-- Name: post_status_transition_guard(); Type: FUNCTION; Schema: public; Owner: kura
--

CREATE FUNCTION public.post_status_transition_guard() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        BEGIN
            IF OLD.status IN ('exact_dup','near_dup','corrupt','lost')
               AND NEW.status = 'pending' THEN
                RAISE EXCEPTION
                    'post % status % is terminal; cannot revert to pending (INV-2)',
                    OLD.id, OLD.status;
            END IF;
            RETURN NEW;
        END;
        $$;


ALTER FUNCTION public.post_status_transition_guard() OWNER TO kura;

--
-- Name: post_tag_audit_append_only(); Type: FUNCTION; Schema: public; Owner: kura
--

CREATE FUNCTION public.post_tag_audit_append_only() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        BEGIN
            RAISE EXCEPTION 'post_tag_audit is append-only (no UPDATE/DELETE)';
        END;
        $$;


ALTER FUNCTION public.post_tag_audit_append_only() OWNER TO kura;

--
-- Name: post_tag_dup_skip(); Type: FUNCTION; Schema: public; Owner: kura
--

CREATE FUNCTION public.post_tag_dup_skip() RETURNS trigger
    LANGUAGE plpgsql
    SET search_path TO 'public', 'pg_catalog'
    AS $$
BEGIN
    -- origin (app write) passes through so real UNIQUE / ON CONFLICT apply;
    -- only apply-worker ('replica') and tablesync ('local') dedup.
    IF current_setting('session_replication_role', true) = 'origin' THEN RETURN NEW; END IF;
    IF EXISTS (SELECT 1 FROM public."post_tag" WHERE post_id=NEW.post_id AND tag_id=NEW.tag_id) THEN RETURN NULL; END IF;
    RETURN NEW;
END;
$$;


ALTER FUNCTION public.post_tag_dup_skip() OWNER TO kura;

--
-- Name: post_tag_pin_guard_del(); Type: FUNCTION; Schema: public; Owner: kura
--

CREATE FUNCTION public.post_tag_pin_guard_del() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        BEGIN
            IF current_setting('kura.tag_actor', true) IS DISTINCT FROM 'human'
               -- parent post already deleted => this is the ON DELETE CASCADE
               -- from DELETE /post/{id}; never block it (see 0054/0057 docstrings)
               AND EXISTS (SELECT 1 FROM post WHERE id = OLD.post_id)
               AND EXISTS (SELECT 1 FROM post_tag_pin
                           WHERE post_id = OLD.post_id
                             AND tag_id = OLD.tag_id
                             AND state = 'present') THEN
                RETURN NULL;  -- INV-M: machine may not remove a human-added tag
            END IF;
            RETURN OLD;
        END;
        $$;


ALTER FUNCTION public.post_tag_pin_guard_del() OWNER TO kura;

--
-- Name: post_tag_pin_guard_ins(); Type: FUNCTION; Schema: public; Owner: kura
--

CREATE FUNCTION public.post_tag_pin_guard_ins() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        BEGIN
            IF current_setting('kura.tag_actor', true) IS DISTINCT FROM 'human'
               AND EXISTS (SELECT 1 FROM post_tag_pin
                           WHERE post_id = NEW.post_id
                             AND tag_id = NEW.tag_id
                             AND state = 'absent') THEN
                RETURN NULL;  -- INV-M: machine may not re-add a human-removed tag
            END IF;
            RETURN NEW;
        END;
        $$;


ALTER FUNCTION public.post_tag_pin_guard_ins() OWNER TO kura;

--
-- Name: post_variant_dup_skip(); Type: FUNCTION; Schema: public; Owner: kura
--

CREATE FUNCTION public.post_variant_dup_skip() RETURNS trigger
    LANGUAGE plpgsql
    SET search_path TO 'public', 'pg_catalog'
    AS $$
BEGIN
    -- origin (app write) passes through so real UNIQUE / ON CONFLICT apply;
    -- only apply-worker ('replica') and tablesync ('local') dedup.
    IF current_setting('session_replication_role', true) = 'origin' THEN RETURN NEW; END IF;
    IF EXISTS (SELECT 1 FROM public."post_variant" WHERE id=NEW.id) THEN RETURN NULL; END IF;
    RETURN NEW;
END;
$$;


ALTER FUNCTION public.post_variant_dup_skip() OWNER TO kura;

--
-- Name: set_updated_at(); Type: FUNCTION; Schema: public; Owner: kura
--

CREATE FUNCTION public.set_updated_at() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
BEGIN
    NEW.updated_at = now();
    RETURN NEW;
END;
$$;


ALTER FUNCTION public.set_updated_at() OWNER TO kura;

--
-- Name: sync_pool_cover_post(); Type: FUNCTION; Schema: public; Owner: kura
--

CREATE FUNCTION public.sync_pool_cover_post() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
    BEGIN
        IF TG_OP = 'INSERT' THEN
            UPDATE pool p
            SET cover_post_id = NEW.post_id
            WHERE p.id = NEW.pool_id
              AND (
                  p.cover_post_id IS NULL
                  OR NEW.ord < (
                      SELECT pp2.ord FROM pool_post pp2
                      WHERE pp2.pool_id = NEW.pool_id
                        AND pp2.post_id = p.cover_post_id
                  )
              );
            RETURN NEW;
        ELSIF TG_OP = 'DELETE' THEN
            UPDATE pool p
            SET cover_post_id = (
                SELECT pp3.post_id FROM pool_post pp3
                WHERE pp3.pool_id = OLD.pool_id
                ORDER BY pp3.ord ASC
                LIMIT 1
            )
            WHERE p.id = OLD.pool_id
              AND p.cover_post_id = OLD.post_id;
            RETURN OLD;
        END IF;
        RETURN NULL;
    END;
    $$;


ALTER FUNCTION public.sync_pool_cover_post() OWNER TO kura;

--
-- Name: sync_pool_last_edit_time(); Type: FUNCTION; Schema: public; Owner: kura
--

CREATE FUNCTION public.sync_pool_last_edit_time() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        BEGIN
            UPDATE pool
               SET last_edit_time = (now() AT TIME ZONE 'utc')
             WHERE id = COALESCE(NEW.pool_id, OLD.pool_id);
            RETURN NULL;
        END $$;


ALTER FUNCTION public.sync_pool_last_edit_time() OWNER TO kura;

--
-- Name: sync_pool_url_observed_count(); Type: FUNCTION; Schema: public; Owner: kura
--

CREATE FUNCTION public.sync_pool_url_observed_count() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
    BEGIN
        IF TG_OP = 'INSERT' THEN
            UPDATE pool_url
              SET observed_count = COALESCE(observed_count, 0) + 1,
                  expected_count = GREATEST(
                      COALESCE(expected_count, 0),
                      COALESCE(observed_count, 0) + 1
                  )
              WHERE pool_id = NEW.pool_id AND is_primary = true;
            RETURN NEW;
        ELSIF TG_OP = 'DELETE' THEN
            UPDATE pool_url
              SET observed_count = GREATEST(COALESCE(observed_count, 0) - 1, 0)
              WHERE pool_id = OLD.pool_id AND is_primary = true;
            RETURN OLD;
        ELSIF TG_OP = 'UPDATE' AND OLD.pool_id IS DISTINCT FROM NEW.pool_id THEN
            UPDATE pool_url
              SET observed_count = GREATEST(COALESCE(observed_count, 0) - 1, 0)
              WHERE pool_id = OLD.pool_id AND is_primary = true;
            UPDATE pool_url
              SET observed_count = COALESCE(observed_count, 0) + 1,
                  expected_count = GREATEST(
                      COALESCE(expected_count, 0),
                      COALESCE(observed_count, 0) + 1
                  )
              WHERE pool_id = NEW.pool_id AND is_primary = true;
            RETURN NEW;
        END IF;
        RETURN NULL;
    END;
    $$;


ALTER FUNCTION public.sync_pool_url_observed_count() OWNER TO kura;

--
-- Name: sync_post_listing(); Type: FUNCTION; Schema: public; Owner: kura
--

CREATE FUNCTION public.sync_post_listing() RETURNS trigger
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'pg_catalog'
    AS $$
BEGIN
 IF TG_RELID<>'public.post'::regclass THEN
  RAISE EXCEPTION 'Listing projection accepts only canonical post writes';
 END IF;
 IF NEW.status='ok' THEN
  INSERT INTO public.post_listing(post_id,safety,file_size) VALUES(NEW.id,NEW.safety,NEW.file_size)
  ON CONFLICT(post_id) DO UPDATE SET safety=EXCLUDED.safety,file_size=EXCLUDED.file_size
  WHERE (post_listing.safety,post_listing.file_size) IS DISTINCT FROM (EXCLUDED.safety,EXCLUDED.file_size);
 ELSE
  DELETE FROM public.post_listing WHERE post_id=NEW.id;
 END IF;
 RETURN NEW;
END $$;


ALTER FUNCTION public.sync_post_listing() OWNER TO kura;

--
-- Name: sync_post_primary_pool(); Type: FUNCTION; Schema: public; Owner: kura
--

CREATE FUNCTION public.sync_post_primary_pool() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        DECLARE
            nxt INT;
        BEGIN
            IF NEW.primary_pool_id IS NULL THEN
                RETURN NEW;
            END IF;
            IF TG_OP = 'UPDATE' AND OLD.primary_pool_id IS NOT DISTINCT FROM NEW.primary_pool_id THEN
                RETURN NEW;
            END IF;
            SELECT COALESCE(MAX(ord), -1) + 1 INTO nxt
              FROM pool_post WHERE pool_id = NEW.primary_pool_id;
            INSERT INTO pool_post (pool_id, post_id, ord)
            VALUES (NEW.primary_pool_id, NEW.id, nxt)
            ON CONFLICT (pool_id, post_id) DO NOTHING;
            RETURN NEW;
        END;
        $$;


ALTER FUNCTION public.sync_post_primary_pool() OWNER TO kura;

--
-- Name: tag_category_dup_skip(); Type: FUNCTION; Schema: public; Owner: kura
--

CREATE FUNCTION public.tag_category_dup_skip() RETURNS trigger
    LANGUAGE plpgsql
    SET search_path TO 'public', 'pg_catalog'
    AS $$
BEGIN
    -- origin (app write) passes through so real UNIQUE / ON CONFLICT apply;
    -- only apply-worker ('replica') and tablesync ('local') dedup.
    IF current_setting('session_replication_role', true) = 'origin' THEN RETURN NEW; END IF;
    IF EXISTS (SELECT 1 FROM public."tag_category" WHERE id=NEW.id) THEN RETURN NULL; END IF;
    RETURN NEW;
END;
$$;


ALTER FUNCTION public.tag_category_dup_skip() OWNER TO kura;

--
-- Name: tag_dup_skip(); Type: FUNCTION; Schema: public; Owner: kura
--

CREATE FUNCTION public.tag_dup_skip() RETURNS trigger
    LANGUAGE plpgsql
    SET search_path TO 'public', 'pg_catalog'
    AS $$
BEGIN
    -- origin (app write) passes through so real UNIQUE / ON CONFLICT apply;
    -- only apply-worker ('replica') and tablesync ('local') dedup.
    IF current_setting('session_replication_role', true) = 'origin' THEN RETURN NEW; END IF;
    IF EXISTS (SELECT 1 FROM public."tag" WHERE id=NEW.id) THEN RETURN NULL; END IF;
    RETURN NEW;
END;
$$;


ALTER FUNCTION public.tag_dup_skip() OWNER TO kura;

--
-- Name: tag_implication_dup_skip(); Type: FUNCTION; Schema: public; Owner: kura
--

CREATE FUNCTION public.tag_implication_dup_skip() RETURNS trigger
    LANGUAGE plpgsql
    SET search_path TO 'public', 'pg_catalog'
    AS $$
BEGIN
    -- origin (app write) passes through so real UNIQUE / ON CONFLICT apply;
    -- only apply-worker ('replica') and tablesync ('local') dedup.
    IF current_setting('session_replication_role', true) = 'origin' THEN RETURN NEW; END IF;
    IF EXISTS (SELECT 1 FROM public."tag_implication" WHERE parent_id=NEW.parent_id AND child_id=NEW.child_id) THEN RETURN NULL; END IF;
    RETURN NEW;
END;
$$;


ALTER FUNCTION public.tag_implication_dup_skip() OWNER TO kura;

--
-- Name: tag_name_dup_skip(); Type: FUNCTION; Schema: public; Owner: kura
--

CREATE FUNCTION public.tag_name_dup_skip() RETURNS trigger
    LANGUAGE plpgsql
    SET search_path TO 'public', 'pg_catalog'
    AS $$
BEGIN
    -- origin (app write) passes through so real UNIQUE / ON CONFLICT apply;
    -- only apply-worker ('replica') and tablesync ('local') dedup.
    IF current_setting('session_replication_role', true) = 'origin' THEN RETURN NEW; END IF;
    IF EXISTS (SELECT 1 FROM public."tag_name" WHERE tag_name_id=NEW.tag_name_id) THEN RETURN NULL; END IF;
    RETURN NEW;
END;
$$;


ALTER FUNCTION public.tag_name_dup_skip() OWNER TO kura;

--
-- Name: tag_post_count_del_stmt(); Type: FUNCTION; Schema: public; Owner: kura
--

CREATE FUNCTION public.tag_post_count_del_stmt() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
BEGIN
    UPDATE tag SET post_count = GREATEST(post_count - sub.delta, 0)
    FROM (SELECT tag_id, COUNT(*) AS delta FROM old_rows GROUP BY tag_id) sub
    WHERE tag.id = sub.tag_id;
    RETURN NULL;
END;
$$;


ALTER FUNCTION public.tag_post_count_del_stmt() OWNER TO kura;

--
-- Name: tag_post_count_ins_stmt(); Type: FUNCTION; Schema: public; Owner: kura
--

CREATE FUNCTION public.tag_post_count_ins_stmt() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
BEGIN
    UPDATE tag SET post_count = post_count + sub.delta
    FROM (SELECT tag_id, COUNT(*) AS delta FROM new_rows GROUP BY tag_id) sub
    WHERE tag.id = sub.tag_id;
    RETURN NULL;
END;
$$;


ALTER FUNCTION public.tag_post_count_ins_stmt() OWNER TO kura;

--
-- Name: tag_suggestion_dup_skip(); Type: FUNCTION; Schema: public; Owner: kura
--

CREATE FUNCTION public.tag_suggestion_dup_skip() RETURNS trigger
    LANGUAGE plpgsql
    SET search_path TO 'public', 'pg_catalog'
    AS $$
BEGIN
    -- origin (app write) passes through so real UNIQUE / ON CONFLICT apply;
    -- only apply-worker ('replica') and tablesync ('local') dedup.
    IF current_setting('session_replication_role', true) = 'origin' THEN RETURN NEW; END IF;
    IF EXISTS (SELECT 1 FROM public."tag_suggestion" WHERE parent_id=NEW.parent_id AND child_id=NEW.child_id) THEN RETURN NULL; END IF;
    RETURN NEW;
END;
$$;


ALTER FUNCTION public.tag_suggestion_dup_skip() OWNER TO kura;

--
-- Name: tagger_acquire_lease(text, integer); Type: FUNCTION; Schema: public; Owner: kura_reconciler
--

CREATE FUNCTION public.tagger_acquire_lease(p_scope text, p_lease_seconds integer DEFAULT 60) RETURNS TABLE(fencing_token bigint, expires_at timestamp with time zone)
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'pg_catalog', 'public'
    AS $$
        DECLARE
            v_token bigint;
            v_expires timestamptz;
            v_allowed boolean;
        BEGIN
            SELECT EXISTS(
                SELECT 1 FROM public.tagger_allowed_executor
                WHERE db_role = session_user AND (
                    scope = p_scope
                    OR (scope = 'lifecycle:*' AND p_scope LIKE 'lifecycle:%')
                )
            ) INTO v_allowed;
            IF NOT v_allowed THEN
                RAISE EXCEPTION 'tagger_acquire_lease: session_user % is not an allowed executor for scope %',
                    session_user, p_scope;
            END IF;

            v_token := nextval('public.tagger_fencing_seq');
            v_expires := now() + make_interval(secs => p_lease_seconds);

            INSERT INTO public.tagger_lifecycle_lock (scope, holder_agent_id, acquired_at, expires_at, fencing_token)
            VALUES (p_scope, session_user, now(), v_expires, v_token)
            ON CONFLICT (scope) DO UPDATE
                SET holder_agent_id = EXCLUDED.holder_agent_id,
                    acquired_at = EXCLUDED.acquired_at,
                    expires_at = EXCLUDED.expires_at,
                    fencing_token = EXCLUDED.fencing_token
                WHERE tagger_lifecycle_lock.expires_at < now();

            IF NOT FOUND THEN
                RAISE EXCEPTION 'tagger_acquire_lease: scope % is held by another agent and has not expired', p_scope;
            END IF;

            RETURN QUERY SELECT v_token, v_expires;
        END;
        $$;


ALTER FUNCTION public.tagger_acquire_lease(p_scope text, p_lease_seconds integer) OWNER TO kura_reconciler;

--
-- Name: tagger_bind_tag(); Type: FUNCTION; Schema: public; Owner: kura
--

CREATE FUNCTION public.tagger_bind_tag() RETURNS trigger
    LANGUAGE plpgsql
    SET search_path TO 'pg_catalog', 'public'
    AS $$
        BEGIN
            IF NEW.tag_id IS NULL THEN
                IF NEW.tag IS NULL THEN
                    RAISE EXCEPTION
                        'tagger_bind_tag: tag も tag_id も無い行は書けない';
                END IF;
                NEW.tag_id := public.tagger_tag_id_of(NEW.tag);
                IF NEW.tag_id IS NULL THEN
                    RAISE EXCEPTION
                        'tagger_bind_tag: kura に存在しないタグ % は書けない'
                        ' (タガーの行は必ず kura のタグに紐づく)', NEW.tag;
                END IF;
            END IF;
            NEW.tag := public.tagger_tag_name_of(NEW.tag_id);
            IF NEW.tag IS NULL THEN
                RAISE EXCEPTION 'tagger_bind_tag: tag_id % に名前が無い', NEW.tag_id;
            END IF;
            RETURN NEW;
        END;
        $$;


ALTER FUNCTION public.tagger_bind_tag() OWNER TO kura;

--
-- Name: tagger_check_fencing(text, bigint); Type: FUNCTION; Schema: public; Owner: kura_reconciler
--

CREATE FUNCTION public.tagger_check_fencing(p_scope text, p_fencing_token bigint) RETURNS void
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'pg_catalog', 'public'
    AS $$
        DECLARE
            v_row public.tagger_lifecycle_lock%ROWTYPE;
        BEGIN
            SELECT * INTO v_row FROM public.tagger_lifecycle_lock
                WHERE scope = p_scope FOR UPDATE;
            IF NOT FOUND THEN
                RAISE EXCEPTION 'tagger_check_fencing: no lease held for scope %', p_scope;
            END IF;
            IF v_row.holder_agent_id <> session_user
               OR v_row.fencing_token <> p_fencing_token
               OR v_row.expires_at <= now() THEN
                RAISE EXCEPTION 'tagger_check_fencing: fencing check failed for scope % (holder/token/expiry mismatch)', p_scope;
            END IF;
        END;
        $$;


ALTER FUNCTION public.tagger_check_fencing(p_scope text, p_fencing_token bigint) OWNER TO kura_reconciler;

--
-- Name: tagger_governed_add_head_candidate(text, integer, text, text); Type: FUNCTION; Schema: public; Owner: kura_reconciler
--

CREATE FUNCTION public.tagger_governed_add_head_candidate(p_tag text, p_source_pool_id integer, p_reason text, p_added_by text) RETURNS boolean
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'pg_catalog', 'public'
    AS $$
        BEGIN
            IF session_user NOT IN (SELECT db_role FROM public.tagger_allowed_executor WHERE scope = 'tagging-pipeline') THEN
                RAISE EXCEPTION 'tagger_governed_add_head_candidate: session_user % is not an allowed executor for scope tagging-pipeline',
                    session_user;
            END IF;
            IF EXISTS (
                SELECT 1 FROM public.tagger_head_candidate
                WHERE tag = p_tag AND source_pool_id IS NOT DISTINCT FROM p_source_pool_id
            ) THEN
                RETURN false;
            END IF;
            INSERT INTO public.tagger_head_candidate (tag, source_pool_id, reason, added_by)
            VALUES (p_tag, p_source_pool_id, p_reason, p_added_by);
            RETURN true;
        END;
        $$;


ALTER FUNCTION public.tagger_governed_add_head_candidate(p_tag text, p_source_pool_id integer, p_reason text, p_added_by text) OWNER TO kura_reconciler;

--
-- Name: tagger_governed_claim_auto_tag(integer, integer); Type: FUNCTION; Schema: public; Owner: kura_reconciler
--

CREATE FUNCTION public.tagger_governed_claim_auto_tag(p_limit integer, p_lease_seconds integer) RETURNS TABLE(id integer, tagging_attempts integer, tagging_locked_until timestamp with time zone, mime_type character varying, post_type character varying)
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'pg_catalog', 'public'
    AS $$
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
        $$;


ALTER FUNCTION public.tagger_governed_claim_auto_tag(p_limit integer, p_lease_seconds integer) OWNER TO kura_reconciler;

--
-- Name: tagger_governed_complete_ok(integer); Type: FUNCTION; Schema: public; Owner: kura_reconciler
--

CREATE FUNCTION public.tagger_governed_complete_ok(p_post_id integer) RETURNS void
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'pg_catalog', 'public'
    AS $$
        BEGIN
            IF session_user NOT IN (SELECT db_role FROM public.tagger_allowed_executor WHERE scope = 'tagging-pipeline') THEN
                RAISE EXCEPTION 'tagger_governed_complete_ok: session_user % is not an allowed executor for scope tagging-pipeline',
                    session_user;
            END IF;
            UPDATE public.post_tagging_work SET status = 'done',
                lease_until = NULL, last_error = NULL
                WHERE post_id = p_post_id;
        END;
        $$;


ALTER FUNCTION public.tagger_governed_complete_ok(p_post_id integer) OWNER TO kura_reconciler;

--
-- Name: tagger_governed_complete_retry(integer, text, text); Type: FUNCTION; Schema: public; Owner: kura_reconciler
--

CREATE FUNCTION public.tagger_governed_complete_retry(p_post_id integer, p_status text, p_error text) RETURNS void
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'pg_catalog', 'public'
    AS $$
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
        $$;


ALTER FUNCTION public.tagger_governed_complete_retry(p_post_id integer, p_status text, p_error text) OWNER TO kura_reconciler;

--
-- Name: tagger_governed_dismiss_head_candidate(text, text); Type: FUNCTION; Schema: public; Owner: kura_reconciler
--

CREATE FUNCTION public.tagger_governed_dismiss_head_candidate(p_tag text, p_reason text) RETURNS integer
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'pg_catalog', 'public'
    AS $$
        DECLARE v_count integer;
        BEGIN
            IF session_user NOT IN (SELECT db_role FROM public.tagger_allowed_executor WHERE scope = 'registration') THEN
                RAISE EXCEPTION 'tagger_governed_dismiss_head_candidate: session_user % is not an allowed executor for scope registration', session_user;
            END IF;
            UPDATE public.tagger_head_candidate
                SET status = 'dismissed'
                WHERE tag = p_tag AND status = 'candidate';
            GET DIAGNOSTICS v_count = ROW_COUNT;
            IF v_count > 0 THEN
                INSERT INTO public.tagger_candidate_event (tag, kind, detail)
                VALUES (p_tag, 'dismissed', p_reason);
                PERFORM public.tagger_governed_record_candidate_disposition(
                    p_tag, 'dismiss', 'triage_dismissal', p_reason,
                    'auto:vnext_triage', 'automation', NULL, NULL, NULL);
            END IF;
            RETURN v_count;
        END; $$;


ALTER FUNCTION public.tagger_governed_dismiss_head_candidate(p_tag text, p_reason text) OWNER TO kura_reconciler;

--
-- Name: tagger_governed_expire_candidate_disposition(text, text); Type: FUNCTION; Schema: public; Owner: kura_reconciler
--

CREATE FUNCTION public.tagger_governed_expire_candidate_disposition(p_tag text, p_decided_by text) RETURNS bigint
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'pg_catalog', 'public'
    AS $$
        DECLARE
            v_cur public.tagger_candidate_disposition%ROWTYPE;
            v_metric text; v_measured numeric; v_met boolean := false;
            v_seq bigint;
        BEGIN
            IF session_user NOT IN (SELECT db_role FROM public.tagger_allowed_executor WHERE scope = 'reconciler-control') THEN
                RAISE EXCEPTION 'expire_candidate_disposition: session_user % is not an allowed executor', session_user;
            END IF;
            SELECT * INTO v_cur FROM public.tagger_candidate_disposition
                WHERE tag_id = public.tagger_tag_id_of(p_tag)
                ORDER BY seq DESC LIMIT 1;
            -- Not-met / nothing-to-do returns NULL (the caller's quiet steady
            -- state); ONLY an unevaluable condition raises (P2 hard error).
            -- Callers must never branch on error prose (P3).
            IF NOT FOUND OR v_cur.disposition <> 'defer' THEN
                RETURN NULL;
            END IF;
            IF v_cur.valid_until IS NOT NULL AND v_cur.valid_until <= now() THEN
                v_met := true;
            END IF;
            IF NOT v_met AND v_cur.release_condition IS NOT NULL THEN
                IF v_cur.cond_version IS DISTINCT FROM 1 THEN
                    -- P2: an unevaluable condition is a HARD error, not a quiet
                    -- true/false — the caller surfaces it (durable diagnostic).
                    RAISE EXCEPTION 'expire_candidate_disposition: % has unevaluable cond_version %', p_tag, v_cur.cond_version;
                END IF;
                v_metric := v_cur.release_condition->>'metric';
                IF v_metric = 'gt_negative_total' THEN
                    SELECT count(*) INTO v_measured FROM public.tagger_gt_label
                        WHERE tag_id = public.tagger_tag_id_of(p_tag) AND verdict = 'negative';
                ELSIF v_metric = 'gt_positive_total' THEN
                    SELECT count(*) INTO v_measured FROM public.tagger_gt_label
                        WHERE tag_id = public.tagger_tag_id_of(p_tag) AND verdict = 'positive';
                ELSE
                    RAISE EXCEPTION 'expire_candidate_disposition: % has unevaluable metric %', p_tag, v_metric;
                END IF;
                v_met := v_measured > (v_cur.release_condition->>'value')::numeric;
            END IF;
            IF NOT v_met THEN
                RETURN NULL;
            END IF;
            -- The expiry row executes the decision's OWN declared condition —
            -- automation may write it (P7's one sanctioned path over a human row).
            INSERT INTO public.tagger_candidate_disposition
                (tag, disposition, reason_class, reason, decided_by, actor_type,
                 supersedes_seq)
            VALUES (p_tag, 'supersede', 'expiry',
                    format('defer seq %s expired: its own deadline/condition was met', v_cur.seq),
                    p_decided_by, 'automation', v_cur.seq)
            RETURNING seq INTO v_seq;
            RETURN v_seq;
        END;
        $$;


ALTER FUNCTION public.tagger_governed_expire_candidate_disposition(p_tag text, p_decided_by text) OWNER TO kura_reconciler;

--
-- Name: tagger_governed_hold_head_candidate(text, text, jsonb); Type: FUNCTION; Schema: public; Owner: kura_reconciler
--

CREATE FUNCTION public.tagger_governed_hold_head_candidate(p_tag text, p_reason_class text, p_evidence jsonb) RETURNS integer
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'pg_catalog', 'public'
    AS $$
        DECLARE v_count integer;
        BEGIN
            IF session_user NOT IN (SELECT db_role FROM public.tagger_allowed_executor WHERE scope = 'reconciler-control') THEN
                RAISE EXCEPTION 'tagger_governed_hold_head_candidate: session_user % is not an allowed executor for scope reconciler-control', session_user;
            END IF;
            IF p_reason_class NOT IN ('prevalence_ceiling', 'label_definition_conflict', 'inert_serving_path', 'vocab_absent_routing', 'insufficient_gt', 'migration_quarantine') THEN
                RAISE EXCEPTION 'tagger_governed_hold_head_candidate: unknown reason_class %', p_reason_class;
            END IF;
            UPDATE public.tagger_head_candidate
                SET status = 'held', hold_reason_class = p_reason_class,
                    hold_evidence = jsonb_build_object('history',
                        COALESCE(hold_evidence -> 'history', '[]'::jsonb) || jsonb_build_array(p_evidence))
                WHERE tag = p_tag AND status = 'candidate';
            GET DIAGNOSTICS v_count = ROW_COUNT;
            IF v_count > 0 THEN
                INSERT INTO public.tagger_candidate_event (tag, kind, hold_reason_class, detail)
                VALUES (p_tag, 'held', p_reason_class, 'candidate -> held');
            END IF;
            RETURN v_count;
        END; $$;


ALTER FUNCTION public.tagger_governed_hold_head_candidate(p_tag text, p_reason_class text, p_evidence jsonb) OWNER TO kura_reconciler;

--
-- Name: tagger_governed_promote_registration(bigint, integer); Type: FUNCTION; Schema: public; Owner: kura_reconciler
--

CREATE FUNCTION public.tagger_governed_promote_registration(p_registration_id bigint, p_patch_id integer) RETURNS void
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'pg_catalog', 'public'
    AS $$
        DECLARE
            v_tag text;
            v_status text;
            v_existing_link integer;
        BEGIN
            IF session_user NOT IN (SELECT db_role FROM public.tagger_allowed_executor WHERE scope = 'registration') THEN
                RAISE EXCEPTION 'tagger_governed_promote_registration: session_user % is not an allowed executor for scope registration',
                    session_user;
            END IF;
            SELECT tag, status, promoted_patch_id INTO v_tag, v_status, v_existing_link
                FROM public.tagger_registration_request WHERE id = p_registration_id;
            IF v_tag IS NULL THEN
                RAISE EXCEPTION 'tagger_governed_promote_registration: registration_request % not found', p_registration_id;
            END IF;
            IF v_status <> 'validated' THEN
                RAISE EXCEPTION 'tagger_governed_promote_registration: registration_request % has status % (must be validated)',
                    p_registration_id, v_status;
            END IF;
            IF v_existing_link IS NOT NULL THEN
                RAISE EXCEPTION 'tagger_governed_promote_registration: registration_request % already promoted to patch %',
                    p_registration_id, v_existing_link;
            END IF;

            UPDATE public.tagger_registration_request
                SET promoted_patch_id = p_patch_id WHERE id = p_registration_id;

            UPDATE public.tagger_head_candidate
                SET status = 'promoted', promoted_patch_id = p_patch_id
                WHERE tag = v_tag AND status = 'candidate';
        END;
        $$;


ALTER FUNCTION public.tagger_governed_promote_registration(p_registration_id bigint, p_patch_id integer) OWNER TO kura_reconciler;

--
-- Name: tagger_governed_publish_release(text, text, text, text, text, bytea, bytea, integer, integer, jsonb, text, text); Type: FUNCTION; Schema: public; Owner: kura_reconciler
--

CREATE FUNCTION public.tagger_governed_publish_release(p_commit_sha text, p_base_model_repo text, p_base_model_revision text, p_patch_set_digest text, p_model_sha256 text, p_model_data bytea, p_selected_tags_csv bytea, p_n_stock integer, p_n_out integer, p_patched jsonb, p_lockfile text, p_created_by text) RETURNS integer
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'pg_catalog', 'public'
    AS $$
        DECLARE
            v_release_id integer;
        BEGIN
            IF session_user NOT IN (SELECT db_role FROM public.tagger_allowed_executor WHERE scope = 'release-publish') THEN
                RAISE EXCEPTION 'tagger_governed_publish_release: session_user % is not an allowed executor for scope release-publish',
                    session_user;
            END IF;
            INSERT INTO public.tagger_release
                (commit_sha, base_model_repo, base_model_revision, patch_set_digest,
                 model_sha256, model_data, selected_tags_csv, n_stock, n_out,
                 patched, lockfile, created_by)
            VALUES
                (p_commit_sha, p_base_model_repo, p_base_model_revision, p_patch_set_digest,
                 p_model_sha256, p_model_data, p_selected_tags_csv, p_n_stock, p_n_out,
                 p_patched, p_lockfile, p_created_by)
            RETURNING id INTO v_release_id;

            UPDATE public.tagger_release_active
            SET release_id = v_release_id, generation = generation + 1,
                updated_at = now(), updated_by = p_created_by
            WHERE id = 1;

            RETURN v_release_id;
        END;
        $$;


ALTER FUNCTION public.tagger_governed_publish_release(p_commit_sha text, p_base_model_repo text, p_base_model_revision text, p_patch_set_digest text, p_model_sha256 text, p_model_data bytea, p_selected_tags_csv bytea, p_n_stock integer, p_n_out integer, p_patched jsonb, p_lockfile text, p_created_by text) OWNER TO kura_reconciler;

--
-- Name: tagger_governed_purge_ineligible_gt_labels(); Type: FUNCTION; Schema: public; Owner: kura_reconciler
--

CREATE FUNCTION public.tagger_governed_purge_ineligible_gt_labels() RETURNS integer
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'pg_catalog', 'public'
    AS $$
        DECLARE
            n integer;
        BEGIN
            IF session_user NOT IN (SELECT db_role FROM public.tagger_allowed_executor WHERE scope = 'gt-sync') THEN
                RAISE EXCEPTION 'tagger_governed_purge_ineligible_gt_labels: session_user % is not an allowed executor for scope gt-sync',
                    session_user;
            END IF;
            -- Exactly the write-side rule from 0113, inverted. Nothing this
            -- deletes could be re-created by the governed upsert.
            DELETE FROM public.tagger_gt_label g
             USING public.post p
             WHERE p.id = g.post_id AND p.status <> 'ok';
            GET DIAGNOSTICS n = ROW_COUNT;
            RAISE NOTICE 'tagger_governed_purge_ineligible_gt_labels: removed % label(s) on non-ok posts', n;
            RETURN n;
        END;
        $$;


ALTER FUNCTION public.tagger_governed_purge_ineligible_gt_labels() OWNER TO kura_reconciler;

--
-- Name: tagger_governed_record_candidate_disposition(text, text, text, text, text, text, timestamp with time zone, jsonb, bigint); Type: FUNCTION; Schema: public; Owner: kura_reconciler
--

CREATE FUNCTION public.tagger_governed_record_candidate_disposition(p_tag text, p_disposition text, p_reason_class text, p_reason text, p_decided_by text, p_actor_type text, p_valid_until timestamp with time zone DEFAULT NULL::timestamp with time zone, p_release_condition jsonb DEFAULT NULL::jsonb, p_supersedes_seq bigint DEFAULT NULL::bigint) RETURNS bigint
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'pg_catalog', 'public'
    AS $$
        DECLARE
            v_prev public.tagger_candidate_disposition%ROWTYPE;
            v_seq bigint;
            v_cond_version integer := NULL;
        BEGIN
            IF session_user NOT IN (SELECT db_role FROM public.tagger_allowed_executor WHERE scope = 'reconciler-control') THEN
                RAISE EXCEPTION 'record_candidate_disposition: session_user % is not an allowed executor', session_user;
            END IF;
            IF p_disposition NOT IN ('proceed', 'defer', 'dismiss', 'block', 'supersede') THEN
                RAISE EXCEPTION 'record_candidate_disposition: unknown disposition %', p_disposition;
            END IF;
            IF p_actor_type NOT IN ('human', 'policy', 'automation') THEN
                RAISE EXCEPTION 'record_candidate_disposition: unknown actor_type %', p_actor_type;
            END IF;
            -- closed DSL v1 (P2): validated HERE, at write time, by the same code
            -- family that evaluates it (expire fn below). Unknown shapes are a
            -- hard error, never a silent true/false.
            IF p_release_condition IS NOT NULL THEN
                IF p_release_condition->>'metric' NOT IN ('gt_negative_total', 'gt_positive_total')
                   OR p_release_condition->>'op' IS DISTINCT FROM 'grows_past'
                   OR (p_release_condition->>'value') IS NULL THEN
                    RAISE EXCEPTION 'record_candidate_disposition: release_condition % does not match DSL v1 (metric gt_negative_total or gt_positive_total, op grows_past, numeric value)', p_release_condition;
                END IF;
                PERFORM (p_release_condition->>'value')::numeric;  -- raises if non-numeric
                v_cond_version := 1;
            END IF;
            -- P7: automation must not supersede a standing HUMAN decision.
            -- (Expiry is not a supersede -- it goes through the expire fn, which
            -- verifies the decision's own declared condition.)
            SELECT * INTO v_prev FROM public.tagger_candidate_disposition
                WHERE tag_id = public.tagger_tag_id_of(p_tag)
                ORDER BY seq DESC LIMIT 1;
            IF FOUND AND v_prev.actor_type = 'human' AND p_actor_type <> 'human' THEN
                RAISE EXCEPTION
                    'record_candidate_disposition: a human decision (seq %) stands for %; automation may not supersede it — use tagger_governed_expire_candidate_disposition if its own deadline/condition is met',
                    v_prev.seq, p_tag;
            END IF;
            -- tag_id is bound by the BEFORE trigger (tagger_bind_tag) — passing it
            -- explicitly would bypass the existence check for the tag name.
            INSERT INTO public.tagger_candidate_disposition
                (tag, disposition, reason_class, reason, decided_by, actor_type,
                 valid_until, release_condition, cond_version, supersedes_seq)
            VALUES (p_tag, p_disposition, p_reason_class, p_reason, p_decided_by,
                    p_actor_type, p_valid_until, p_release_condition, v_cond_version,
                    p_supersedes_seq)
            RETURNING seq INTO v_seq;
            RETURN v_seq;
        END;
        $$;


ALTER FUNCTION public.tagger_governed_record_candidate_disposition(p_tag text, p_disposition text, p_reason_class text, p_reason text, p_decided_by text, p_actor_type text, p_valid_until timestamp with time zone, p_release_condition jsonb, p_supersedes_seq bigint) OWNER TO kura_reconciler;

--
-- Name: tagger_governed_record_sweep_run(integer, bigint, integer, integer, boolean, text, timestamp with time zone); Type: FUNCTION; Schema: public; Owner: kura_reconciler
--

CREATE FUNCTION public.tagger_governed_record_sweep_run(p_release_id integer, p_release_generation bigint, p_requested_ceiling integer, p_flipped_count integer, p_dry_run boolean, p_status text, p_started_at timestamp with time zone) RETURNS bigint
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'pg_catalog', 'public'
    AS $$
        DECLARE
            v_id bigint;
        BEGIN
            IF session_user NOT IN (
                SELECT db_role FROM public.tagger_allowed_executor
                WHERE scope = 'tagging-pipeline'
            ) THEN
                RAISE EXCEPTION
                    'tagger_governed_record_sweep_run: session_user % is not an allowed executor for scope tagging-pipeline',
                    session_user;
            END IF;

            INSERT INTO public.tagger_sweep_run (
                release_id, release_generation, requested_ceiling,
                flipped_count, dry_run, status, started_at, finished_at
            ) VALUES (
                p_release_id, p_release_generation, p_requested_ceiling,
                p_flipped_count, p_dry_run, p_status,
                COALESCE(p_started_at, now()), now()
            )
            RETURNING id INTO v_id;
            RETURN v_id;
        END;
        $$;


ALTER FUNCTION public.tagger_governed_record_sweep_run(p_release_id integer, p_release_generation bigint, p_requested_ceiling integer, p_flipped_count integer, p_dry_run boolean, p_status text, p_started_at timestamp with time zone) OWNER TO kura_reconciler;

--
-- Name: tagger_governed_release_head_candidate(text, text); Type: FUNCTION; Schema: public; Owner: kura_reconciler
--

CREATE FUNCTION public.tagger_governed_release_head_candidate(p_tag text, p_reason text) RETURNS integer
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'pg_catalog', 'public'
    AS $$
        DECLARE v_count integer;
        BEGIN
            IF session_user NOT IN (SELECT db_role FROM public.tagger_allowed_executor WHERE scope = 'reconciler-control') THEN
                RAISE EXCEPTION 'tagger_governed_release_head_candidate: session_user % is not an allowed executor for scope reconciler-control', session_user;
            END IF;
            -- held -> candidate; clear reason_class (mutual CHECK) but KEEP
            -- hold_evidence so its append-only history survives the cycle.
            UPDATE public.tagger_head_candidate
                SET status = 'candidate', hold_reason_class = NULL
                WHERE tag = p_tag AND status = 'held';
            GET DIAGNOSTICS v_count = ROW_COUNT;
            IF v_count > 0 THEN
                INSERT INTO public.tagger_candidate_event (tag, kind, detail)
                VALUES (p_tag, 'released', p_reason);
            END IF;
            RETURN v_count;
        END; $$;


ALTER FUNCTION public.tagger_governed_release_head_candidate(p_tag text, p_reason text) OWNER TO kura_reconciler;

--
-- Name: tagger_governed_release_pool_provenance(integer); Type: FUNCTION; Schema: public; Owner: kura_reconciler
--

CREATE FUNCTION public.tagger_governed_release_pool_provenance(p_pool_id integer) RETURNS integer
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'pg_catalog', 'public'
    AS $$
        DECLARE
            v_count integer;
        BEGIN
            IF session_user NOT IN (SELECT db_role FROM public.tagger_allowed_executor WHERE scope = 'registration') THEN
                RAISE EXCEPTION 'tagger_governed_release_pool_provenance: session_user % is not an allowed executor for scope registration',
                    session_user;
            END IF;
            UPDATE public.tagger_head_candidate
                SET source_pool_id = NULL
                WHERE source_pool_id = p_pool_id AND status IN ('promoted', 'dismissed');
            GET DIAGNOSTICS v_count = ROW_COUNT;
            RETURN v_count;
        END;
        $$;


ALTER FUNCTION public.tagger_governed_release_pool_provenance(p_pool_id integer) OWNER TO kura_reconciler;

--
-- Name: tagger_governed_reopen_headless_candidate(text, text); Type: FUNCTION; Schema: public; Owner: kura_reconciler
--

CREATE FUNCTION public.tagger_governed_reopen_headless_candidate(p_tag text, p_reason text) RETURNS integer
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'pg_catalog', 'public'
    AS $$
        DECLARE v_count integer;
        BEGIN
            IF session_user NOT IN (
                SELECT db_role FROM public.tagger_allowed_executor
                WHERE scope = 'reconciler-control') THEN
                RAISE EXCEPTION 'tagger_governed_reopen_headless_candidate: session_user % is not an allowed executor for scope reconciler-control', session_user;
            END IF;
            -- 不変条件: 配備できる head を持つタグは開き直さない (0140 view が定義)。
            IF EXISTS (SELECT 1 FROM public.tagger_deployable_head d
                       WHERE d.tag_id = public.tagger_tag_id_of(p_tag)) THEN
                RETURN 0;
            END IF;
            -- promoted -> candidate。promoted_patch_id は残す (履歴は書き換えない)。
            UPDATE public.tagger_head_candidate
                SET status = 'candidate'
                WHERE tag = p_tag AND status = 'promoted';
            GET DIAGNOSTICS v_count = ROW_COUNT;
            IF v_count > 0 THEN
                INSERT INTO public.tagger_candidate_event (tag, kind, detail)
                VALUES (p_tag, 'reopened_headless', p_reason);
            END IF;
            RETURN v_count;
        END; $$;


ALTER FUNCTION public.tagger_governed_reopen_headless_candidate(p_tag text, p_reason text) OWNER TO kura_reconciler;

--
-- Name: tagger_governed_reopen_overdetected_candidate(text, text); Type: FUNCTION; Schema: public; Owner: kura_reconciler
--

CREATE FUNCTION public.tagger_governed_reopen_overdetected_candidate(p_tag text, p_reason text) RETURNS integer
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'pg_catalog', 'public'
    AS $$
        DECLARE
            v_count integer;
            v_last_neg integer;
            v_cur_neg integer;
        BEGIN
            IF session_user NOT IN (
                SELECT db_role FROM public.tagger_allowed_executor
                WHERE scope = 'tagging-pipeline') THEN
                RAISE EXCEPTION 'tagger_governed_reopen_overdetected_candidate: session_user % is not an allowed executor for scope tagging-pipeline', session_user;
            END IF;
            -- 不変条件1: 配備中のタグだけ (policy 無しは 0137 の辺)。
            IF NOT EXISTS (SELECT 1 FROM public.tagger_head_policy hp
                           WHERE hp.tag = p_tag) THEN
                RETURN 0;
            END IF;
            -- 不変条件2: 負例 GT が最後の registration baseline を超えて成長。
            SELECT COALESCE((gt->>'negative')::int, 0) INTO v_last_neg
              FROM public.tagger_registration_request
             WHERE tag = p_tag ORDER BY requested_at DESC LIMIT 1;
            IF v_last_neg IS NULL THEN RETURN 0; END IF;
            SELECT count(*) INTO v_cur_neg FROM public.tagger_gt_label
             WHERE tag = p_tag AND verdict = 'negative';
            IF v_cur_neg <= v_last_neg THEN RETURN 0; END IF;
            -- promoted -> candidate。promoted_patch_id は残す。
            UPDATE public.tagger_head_candidate
               SET status = 'candidate'
             WHERE tag = p_tag AND status = 'promoted';
            GET DIAGNOSTICS v_count = ROW_COUNT;
            IF v_count > 0 THEN
                INSERT INTO public.tagger_candidate_event (tag, kind, detail)
                VALUES (p_tag, 'reopened_overdetection', p_reason);
            END IF;
            RETURN v_count;
        END; $$;


ALTER FUNCTION public.tagger_governed_reopen_overdetected_candidate(p_tag text, p_reason text) OWNER TO kura_reconciler;

--
-- Name: tagger_governed_requeue(integer[], bigint); Type: FUNCTION; Schema: public; Owner: kura_reconciler
--

CREATE FUNCTION public.tagger_governed_requeue(p_post_ids integer[], p_fencing_token bigint) RETURNS integer
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'pg_catalog', 'public'
    AS $$
        DECLARE
            v_n integer;
        BEGIN
            PERFORM public.tagger_check_fencing('backlog-sweep', p_fencing_token);
            UPDATE public.post_tagging_work SET status = 'pending', lease_until = NULL
                WHERE post_id = ANY(p_post_ids) AND status = 'done';
            GET DIAGNOSTICS v_n = ROW_COUNT;
            RETURN v_n;
        END;
        $$;


ALTER FUNCTION public.tagger_governed_requeue(p_post_ids integer[], p_fencing_token bigint) OWNER TO kura_reconciler;

--
-- Name: tagger_governed_retract_pin_gt(text, integer); Type: FUNCTION; Schema: public; Owner: kura_reconciler
--

CREATE FUNCTION public.tagger_governed_retract_pin_gt(p_tag text, p_post_id integer) RETURNS integer
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'pg_catalog', 'public'
    AS $$
        DECLARE
            v_count integer;
        BEGIN
            IF session_user NOT IN (SELECT db_role FROM public.tagger_allowed_executor WHERE scope = 'gt-sync') THEN
                RAISE EXCEPTION 'tagger_governed_retract_pin_gt: session_user % is not an allowed executor for scope gt-sync',
                    session_user;
            END IF;
            DELETE FROM public.tagger_gt_label
            WHERE tag = p_tag AND post_id = p_post_id
              AND source_pool_id IS NULL AND derived_from = 'human_correction';
            GET DIAGNOSTICS v_count = ROW_COUNT;
            RETURN v_count;
        END;
        $$;


ALTER FUNCTION public.tagger_governed_retract_pin_gt(p_tag text, p_post_id integer) OWNER TO kura_reconciler;

--
-- Name: tagger_governed_set_config(text, numeric, text); Type: FUNCTION; Schema: public; Owner: kura_reconciler
--

CREATE FUNCTION public.tagger_governed_set_config(p_key text, p_value numeric, p_updated_by text) RETURNS void
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'pg_catalog', 'public'
    AS $$
        BEGIN
            IF session_user NOT IN (SELECT db_role FROM public.tagger_allowed_executor WHERE scope = 'tagger-config') THEN
                RAISE EXCEPTION 'tagger_governed_set_config: session_user % is not an allowed executor for scope tagger-config',
                    session_user;
            END IF;
            IF NOT EXISTS (SELECT 1 FROM public.tagger_config WHERE key = p_key) THEN
                RAISE EXCEPTION 'tagger_governed_set_config: unknown key %, refusing to create new keys via this path', p_key;
            END IF;
            UPDATE public.tagger_config
            SET value = p_value, updated_at = now(), updated_by = p_updated_by
            WHERE key = p_key;
        END;
        $$;


ALTER FUNCTION public.tagger_governed_set_config(p_key text, p_value numeric, p_updated_by text) OWNER TO kura_reconciler;

--
-- Name: tagger_governed_set_registration_status(bigint, text, text, text); Type: FUNCTION; Schema: public; Owner: kura_reconciler
--

CREATE FUNCTION public.tagger_governed_set_registration_status(p_request_id bigint, p_status text, p_rejected_reason text, p_review_class text DEFAULT NULL::text) RETURNS void
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'pg_catalog', 'public'
    AS $$
        BEGIN
            IF session_user NOT IN (SELECT db_role FROM public.tagger_allowed_executor WHERE scope = 'registration') THEN
                RAISE EXCEPTION 'tagger_governed_set_registration_status: session_user % is not an allowed executor for scope registration',
                    session_user;
            END IF;
            IF p_status NOT IN ('validated', 'rejected') THEN
                RAISE EXCEPTION 'tagger_governed_set_registration_status: status must be validated or rejected, got %', p_status;
            END IF;
            IF p_review_class IS NOT NULL AND p_review_class NOT IN ('deployable', 'not_needed', 'unmeasurable', 'owner_decision', 'floor_rejected') THEN
                RAISE EXCEPTION 'tagger_governed_set_registration_status: unknown review_class %', p_review_class;
            END IF;
            IF p_status = 'validated' AND p_review_class IS NOT NULL AND p_review_class <> 'deployable' THEN
                RAISE EXCEPTION 'tagger_governed_set_registration_status: validated implies review_class deployable, got %', p_review_class;
            END IF;
            UPDATE public.tagger_registration_request
                SET status = p_status, rejected_reason = p_rejected_reason,
                    review_class = COALESCE(p_review_class,
                                            CASE WHEN p_status = 'validated' THEN 'deployable' END),
                    validated_at = now()
                WHERE id = p_request_id AND status = 'pending';
            IF NOT FOUND THEN
                RAISE EXCEPTION 'tagger_governed_set_registration_status: request % not found or not pending', p_request_id;
            END IF;
        END;
        $$;


ALTER FUNCTION public.tagger_governed_set_registration_status(p_request_id bigint, p_status text, p_rejected_reason text, p_review_class text) OWNER TO kura_reconciler;

--
-- Name: tagger_governed_submit_registration(text, text, text, text, jsonb, jsonb, bigint); Type: FUNCTION; Schema: public; Owner: kura_reconciler
--

CREATE FUNCTION public.tagger_governed_submit_registration(p_tag text, p_source_host text, p_artifact_ref text, p_artifact_sha256 text, p_metrics jsonb, p_gt jsonb, p_lifecycle_event_id bigint DEFAULT NULL::bigint) RETURNS bigint
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'pg_catalog', 'public'
    AS $$
        DECLARE
            v_id bigint;
            v_tag_id integer;
        BEGIN
            IF session_user NOT IN (
                SELECT db_role FROM public.tagger_allowed_executor
                WHERE scope = 'registration'
            ) THEN
                RAISE EXCEPTION
                    'tagger_governed_submit_registration: session_user % is not an allowed executor for scope registration',
                    session_user;
            END IF;

            v_tag_id := public.tagger_tag_id_of(p_tag);
            IF v_tag_id IS NULL THEN
                RAISE EXCEPTION
                    'tagger_governed_submit_registration: kura に存在しないタグ % は登録できない',
                    p_tag;
            END IF;

            INSERT INTO public.tagger_registration_request
                (tag, tag_id, source_host, artifact_ref, artifact_sha256, metrics, gt,
                 status, lifecycle_event_id)
            VALUES (p_tag, v_tag_id, p_source_host, p_artifact_ref, p_artifact_sha256,
                    p_metrics, p_gt, 'pending', p_lifecycle_event_id)
            ON CONFLICT ON CONSTRAINT tagger_registration_request_measurement_key
            DO NOTHING
            RETURNING id INTO v_id;

            IF v_id IS NULL THEN
                -- 同一測定の再配達。**tag_id で引く** (別名で来ても同じ行)。
                SELECT id INTO v_id FROM public.tagger_registration_request
                WHERE tag_id = v_tag_id
                  AND artifact_sha256 = p_artifact_sha256
                  AND lifecycle_event_id IS NOT DISTINCT FROM p_lifecycle_event_id;
            END IF;
            RETURN v_id;
        END;
        $$;


ALTER FUNCTION public.tagger_governed_submit_registration(p_tag text, p_source_host text, p_artifact_ref text, p_artifact_sha256 text, p_metrics jsonb, p_gt jsonb, p_lifecycle_event_id bigint) OWNER TO kura_reconciler;

--
-- Name: tagger_governed_sync_neg_gt_from_pins(); Type: FUNCTION; Schema: public; Owner: kura_reconciler
--

CREATE FUNCTION public.tagger_governed_sync_neg_gt_from_pins() RETURNS integer
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'pg_catalog', 'public'
    AS $$
        DECLARE v_count integer;
        BEGIN
            IF session_user NOT IN (SELECT db_role FROM public.tagger_allowed_executor
                                    WHERE scope = 'gt-sync') THEN
                RAISE EXCEPTION 'tagger_governed_sync_neg_gt_from_pins: session_user % is not an allowed executor for scope gt-sync',
                    session_user;
            END IF;
            INSERT INTO public.tagger_gt_label
                (tag, tag_id, post_id, verdict, source_pool_id, derived_from, verified_at)
            SELECT tn.name, ptp.tag_id, ptp.post_id, 'negative', NULL, 'human_correction', now()
            FROM public.post_tag_pin ptp
            JOIN public.tag_name tn ON tn.tag_id = ptp.tag_id AND tn.ord = 0
            LEFT JOIN public.post_tag pt ON pt.post_id = ptp.post_id AND pt.tag_id = ptp.tag_id
            JOIN public.post po ON po.id = ptp.post_id AND po.status = 'ok'
            -- 事前判定 (0163): 既に negative/human_correction/NULL になっている
            -- 行は INSERT まで運ばない。ON CONFLICT の衝突判定は 73,803 行ぶん
            -- 一意 index を叩くので、DO UPDATE の WHERE だけでは 688ms 残る。
            LEFT JOIN public.tagger_gt_label g
                   ON g.tag_id = ptp.tag_id AND g.post_id = ptp.post_id
            WHERE ptp.state = 'absent'
              AND NOT COALESCE(pt.locked, false)
              AND (g.tag_id IS NULL
                   OR g.verdict        IS DISTINCT FROM 'negative'
                   OR g.derived_from   IS DISTINCT FROM 'human_correction'
                   OR g.source_pool_id IS NOT NULL)
            -- Unconditional in VERDICT: an absent pin is the human's LATEST
            -- explicit decision for this (tag, post) -- it always wins as
            -- negative. But a row that ALREADY says so needs no write: the
            -- unconditional form rewrote all 73,803 rows every 14 minutes
            -- (0163). The WHERE below does not weaken the rule, it only
            -- skips writes that would not change the row.
            ON CONFLICT (tag_id, post_id) DO UPDATE SET
                verdict = 'negative', source_pool_id = EXCLUDED.source_pool_id,
                derived_from = 'human_correction', verified_at = EXCLUDED.verified_at
            WHERE tagger_gt_label.verdict        IS DISTINCT FROM EXCLUDED.verdict
               OR tagger_gt_label.derived_from   IS DISTINCT FROM EXCLUDED.derived_from
               OR tagger_gt_label.source_pool_id IS DISTINCT FROM EXCLUDED.source_pool_id;
            GET DIAGNOSTICS v_count = ROW_COUNT;
            RETURN v_count;
        END;
        $$;


ALTER FUNCTION public.tagger_governed_sync_neg_gt_from_pins() OWNER TO kura_reconciler;

--
-- Name: tagger_governed_sync_pos_gt_from_pins(); Type: FUNCTION; Schema: public; Owner: kura_reconciler
--

CREATE FUNCTION public.tagger_governed_sync_pos_gt_from_pins() RETURNS integer
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'pg_catalog', 'public'
    AS $$
        DECLARE v_count integer;
        BEGIN
            IF session_user NOT IN (SELECT db_role FROM public.tagger_allowed_executor
                                    WHERE scope = 'gt-sync') THEN
                RAISE EXCEPTION 'tagger_governed_sync_pos_gt_from_pins: session_user % is not an allowed executor for scope gt-sync',
                    session_user;
            END IF;
            INSERT INTO public.tagger_gt_label
                (tag, tag_id, post_id, verdict, source_pool_id, derived_from, verified_at)
            SELECT tn.name, ptp.tag_id, ptp.post_id, 'positive', NULL, 'human_correction', now()
            FROM public.post_tag_pin ptp
            JOIN public.tag_name tn ON tn.tag_id = ptp.tag_id AND tn.ord = 0
            -- INNER: a positive assertion only makes sense while the tag is
            -- actually on the post (pin flips to absent when a human removes
            -- it, so a divergence here means a machine bypass -- emit nothing).
            JOIN public.post_tag pt ON pt.post_id = ptp.post_id AND pt.tag_id = ptp.tag_id
            JOIN public.post po ON po.id = ptp.post_id AND po.status = 'ok'
            -- 事前判定 (0163) — neg 側と同じ理由。
            LEFT JOIN public.tagger_gt_label g
                   ON g.tag_id = ptp.tag_id AND g.post_id = ptp.post_id
            WHERE ptp.state = 'present'
              AND NOT COALESCE(pt.locked, false)
              AND (g.tag_id IS NULL
                   OR g.verdict        IS DISTINCT FROM 'positive'
                   OR g.derived_from   IS DISTINCT FROM 'human_correction'
                   OR g.source_pool_id IS NOT NULL)
            ON CONFLICT (tag_id, post_id) DO UPDATE SET
                verdict = 'positive', source_pool_id = EXCLUDED.source_pool_id,
                derived_from = 'human_correction', verified_at = EXCLUDED.verified_at
            WHERE tagger_gt_label.verdict        IS DISTINCT FROM EXCLUDED.verdict
               OR tagger_gt_label.derived_from   IS DISTINCT FROM EXCLUDED.derived_from
               OR tagger_gt_label.source_pool_id IS DISTINCT FROM EXCLUDED.source_pool_id;
            GET DIAGNOSTICS v_count = ROW_COUNT;
            RETURN v_count;
        END;
        $$;


ALTER FUNCTION public.tagger_governed_sync_pos_gt_from_pins() OWNER TO kura_reconciler;

--
-- Name: tagger_governed_upsert_gt_label(text, integer, text, integer, text); Type: FUNCTION; Schema: public; Owner: kura_reconciler
--

CREATE FUNCTION public.tagger_governed_upsert_gt_label(p_tag text, p_post_id integer, p_verdict text, p_source_pool_id integer, p_derived_from text) RETURNS void
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'pg_catalog', 'public'
    AS $$
        BEGIN
            IF session_user NOT IN (SELECT db_role FROM public.tagger_allowed_executor
                                    WHERE scope = 'gt-sync') THEN
                RAISE EXCEPTION 'tagger_governed_upsert_gt_label: session_user % is not an allowed executor for scope gt-sync',
                    session_user;
            END IF;
            IF p_verdict NOT IN ('positive', 'negative') THEN
                RAISE EXCEPTION 'tagger_governed_upsert_gt_label: verdict must be positive or negative, got %', p_verdict;
            END IF;
            IF p_derived_from NOT IN ('human_correction', 'implicit_review', 'pool_seed') THEN
                RAISE EXCEPTION 'tagger_governed_upsert_gt_label: derived_from must be human_correction, implicit_review or pool_seed, got %', p_derived_from;
            END IF;
            IF NOT EXISTS (SELECT 1 FROM public.post
                           WHERE id = p_post_id AND status = 'ok') THEN
                RAISE NOTICE 'tagger_governed_upsert_gt_label: skipping post % (post.status is not ok -- duplicate/corrupt/lost posts must not contribute ground truth)', p_post_id;
                RETURN;
            END IF;

            -- tag_id は tagger_bind_tag trigger が tag から埋める。
            INSERT INTO public.tagger_gt_label
                (tag, post_id, verdict, source_pool_id, derived_from, verified_at)
            VALUES (p_tag, p_post_id, p_verdict, p_source_pool_id, p_derived_from, now())
            ON CONFLICT (tag_id, post_id) DO UPDATE SET
                verdict = EXCLUDED.verdict, source_pool_id = EXCLUDED.source_pool_id,
                derived_from = EXCLUDED.derived_from, verified_at = EXCLUDED.verified_at;
        END;
        $$;


ALTER FUNCTION public.tagger_governed_upsert_gt_label(p_tag text, p_post_id integer, p_verdict text, p_source_pool_id integer, p_derived_from text) OWNER TO kura_reconciler;

--
-- Name: tagger_governed_upsert_head_policy(text, integer, text, text, double precision, double precision, double precision, text, text[], bigint); Type: FUNCTION; Schema: public; Owner: kura_reconciler
--

CREATE FUNCTION public.tagger_governed_upsert_head_policy(p_tag text, p_patch_version integer, p_content_sha256 text, p_operating_point text, p_threshold double precision, p_measured_recall double precision, p_measured_fpr double precision, p_apply_mode text, p_gate_tags text[], p_registration_id bigint) RETURNS void
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'pg_catalog', 'public'
    AS $$
        BEGIN
            IF session_user NOT IN (
                SELECT db_role FROM public.tagger_allowed_executor
                WHERE scope = 'registration'
            ) THEN
                RAISE EXCEPTION
                    'tagger_governed_upsert_head_policy: session_user % is not an allowed executor for scope registration',
                    session_user;
            END IF;

            INSERT INTO public.tagger_head_policy (
                tag, patch_version, content_sha256, operating_point, threshold,
                measured_recall, measured_fpr, apply_mode, gate_tags, registration_id)
            VALUES (
                p_tag, p_patch_version, p_content_sha256, p_operating_point, p_threshold,
                p_measured_recall, p_measured_fpr, p_apply_mode,
                COALESCE(p_gate_tags, '{}'), p_registration_id)
            ON CONFLICT (tag_id, patch_version) DO UPDATE SET
                content_sha256  = EXCLUDED.content_sha256,
                operating_point = EXCLUDED.operating_point,
                threshold       = EXCLUDED.threshold,
                measured_recall = EXCLUDED.measured_recall,
                measured_fpr    = EXCLUDED.measured_fpr,
                apply_mode      = EXCLUDED.apply_mode,
                gate_tags       = EXCLUDED.gate_tags,
                registration_id = EXCLUDED.registration_id;
        END;
        $$;


ALTER FUNCTION public.tagger_governed_upsert_head_policy(p_tag text, p_patch_version integer, p_content_sha256 text, p_operating_point text, p_threshold double precision, p_measured_recall double precision, p_measured_fpr double precision, p_apply_mode text, p_gate_tags text[], p_registration_id bigint) OWNER TO kura_reconciler;

--
-- Name: tagger_governed_upsert_patch(text, integer, text, text, text, text, jsonb, jsonb, text, text, text, integer, date, text, bytea); Type: FUNCTION; Schema: public; Owner: kura_reconciler
--

CREATE FUNCTION public.tagger_governed_upsert_patch(p_tag text, p_version integer, p_status text, p_patch_sha256 text, p_why text, p_why_rejected text, p_metrics jsonb, p_gt jsonb, p_head_type text, p_base_repo text, p_base_revision text, p_logit_index integer, p_created_at date, p_created_by text, p_model_data bytea DEFAULT NULL::bytea) RETURNS void
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'pg_catalog', 'public'
    AS $$
        BEGIN
            IF session_user NOT IN (SELECT db_role FROM public.tagger_allowed_executor
                                    WHERE scope = 'release-build') THEN
                RAISE EXCEPTION 'tagger_governed_upsert_patch: session_user % is not an allowed executor for scope release-build',
                    session_user;
            END IF;
            INSERT INTO public.tagger_patch
                (tag, version, status, patch_sha256, why, why_rejected, metrics, gt,
                 head_type, base_repo, base_revision, logit_index, created_at, created_by,
                 model_data, updated_at)
            VALUES
                (p_tag, p_version, p_status, p_patch_sha256, p_why, p_why_rejected, p_metrics, p_gt,
                 p_head_type, p_base_repo, p_base_revision, p_logit_index, p_created_at, p_created_by,
                 p_model_data, now())
            ON CONFLICT (tag_id, version) DO UPDATE SET
                status = EXCLUDED.status, patch_sha256 = EXCLUDED.patch_sha256, why = EXCLUDED.why,
                why_rejected = EXCLUDED.why_rejected, metrics = EXCLUDED.metrics, gt = EXCLUDED.gt,
                head_type = EXCLUDED.head_type, base_repo = EXCLUDED.base_repo,
                base_revision = EXCLUDED.base_revision, logit_index = EXCLUDED.logit_index,
                created_by = EXCLUDED.created_by, model_data = EXCLUDED.model_data,
                updated_at = now();
        END;
        $$;


ALTER FUNCTION public.tagger_governed_upsert_patch(p_tag text, p_version integer, p_status text, p_patch_sha256 text, p_why text, p_why_rejected text, p_metrics jsonb, p_gt jsonb, p_head_type text, p_base_repo text, p_base_revision text, p_logit_index integer, p_created_at date, p_created_by text, p_model_data bytea) OWNER TO kura_reconciler;

--
-- Name: tagger_head_policy_requires_head_bytes(); Type: FUNCTION; Schema: public; Owner: kura
--

CREATE FUNCTION public.tagger_head_policy_requires_head_bytes() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        DECLARE k text;
        BEGIN
            SELECT kind INTO k FROM public.tagger_artifact
             WHERE content_sha256 = NEW.content_sha256;
            IF k IS DISTINCT FROM 'content_head_bundle' THEN
                RAISE EXCEPTION
                  'tagger_head_policy(%, v%): artifact % は kind=% で head bundle ではない。'
                  ' head として load できないバイトを policy から指すことはできない'
                  ' (分類は tagger/artifact_kind.py、NULL は「未分類」で通さない)',
                  NEW.tag, NEW.patch_version, left(NEW.content_sha256, 12),
                  coalesce(k, 'NULL');
            END IF;
            RETURN NEW;
        END $$;


ALTER FUNCTION public.tagger_head_policy_requires_head_bytes() OWNER TO kura;

--
-- Name: tagger_node_state_dup_skip(); Type: FUNCTION; Schema: public; Owner: kura
--

CREATE FUNCTION public.tagger_node_state_dup_skip() RETURNS trigger
    LANGUAGE plpgsql
    SET search_path TO 'public', 'pg_catalog'
    AS $$
BEGIN
    -- origin (app write) passes through so real UNIQUE / ON CONFLICT apply;
    -- only apply-worker ('replica') and tablesync ('local') dedup.
    IF current_setting('session_replication_role', true) = 'origin' THEN RETURN NEW; END IF;
    IF EXISTS (SELECT 1 FROM public."tagger_node_state" WHERE host=NEW.host) THEN RETURN NULL; END IF;
    RETURN NEW;
END;
$$;


ALTER FUNCTION public.tagger_node_state_dup_skip() OWNER TO kura;

--
-- Name: tagger_patch_dup_skip(); Type: FUNCTION; Schema: public; Owner: kura
--

CREATE FUNCTION public.tagger_patch_dup_skip() RETURNS trigger
    LANGUAGE plpgsql
    SET search_path TO 'public', 'pg_catalog'
    AS $$
BEGIN
    -- origin (app write) passes through so real UNIQUE / ON CONFLICT apply;
    -- only apply-worker ('replica') and tablesync ('local') dedup.
    IF current_setting('session_replication_role', true) = 'origin' THEN RETURN NEW; END IF;
    IF EXISTS (SELECT 1 FROM public."tagger_patch" WHERE id=NEW.id) THEN RETURN NULL; END IF;
    RETURN NEW;
END;
$$;


ALTER FUNCTION public.tagger_patch_dup_skip() OWNER TO kura;

--
-- Name: tagger_record_lifecycle_event(text, text, text, text, bigint); Type: FUNCTION; Schema: public; Owner: kura_reconciler
--

CREATE FUNCTION public.tagger_record_lifecycle_event(p_tag text, p_from_stage text, p_to_stage text, p_reason text, p_fencing_token bigint) RETURNS bigint
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'pg_catalog', 'public'
    AS $$
        DECLARE
            v_id bigint;
        BEGIN
            PERFORM public.tagger_check_fencing('lifecycle:' || p_tag, p_fencing_token);
            INSERT INTO public.tagger_lifecycle_event
                (tag, from_stage, to_stage, reason, actor_db_role, fencing_token)
            VALUES (p_tag, p_from_stage, p_to_stage, p_reason, session_user, p_fencing_token)
            RETURNING id INTO v_id;
            RETURN v_id;
        END;
        $$;


ALTER FUNCTION public.tagger_record_lifecycle_event(p_tag text, p_from_stage text, p_to_stage text, p_reason text, p_fencing_token bigint) OWNER TO kura_reconciler;

--
-- Name: tagger_record_operation(text, text, text, bigint, text); Type: FUNCTION; Schema: public; Owner: kura_reconciler
--

CREATE FUNCTION public.tagger_record_operation(p_scope text, p_target_id text, p_operation_type text, p_fencing_token bigint, p_result text DEFAULT NULL::text) RETURNS boolean
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'pg_catalog', 'public'
    AS $$
        DECLARE
            v_key text;
        BEGIN
            PERFORM public.tagger_check_fencing(p_scope, p_fencing_token);
            v_key := encode(sha256((p_scope || ':' || p_target_id || ':' || p_operation_type)::bytea), 'hex');
            INSERT INTO public.tagger_operation
                (idempotency_key, scope, target_id, operation_type, fencing_token, requested_by, result)
            VALUES (v_key, p_scope, p_target_id, p_operation_type, p_fencing_token, session_user, p_result)
            ON CONFLICT ON CONSTRAINT tagger_operation_scope_target_id_operation_type_key DO NOTHING;
            RETURN FOUND;
        END;
        $$;


ALTER FUNCTION public.tagger_record_operation(p_scope text, p_target_id text, p_operation_type text, p_fencing_token bigint, p_result text) OWNER TO kura_reconciler;

--
-- Name: tagger_release_active_dup_skip(); Type: FUNCTION; Schema: public; Owner: kura
--

CREATE FUNCTION public.tagger_release_active_dup_skip() RETURNS trigger
    LANGUAGE plpgsql
    SET search_path TO 'public', 'pg_catalog'
    AS $$
BEGIN
    -- origin (app write) passes through so real UNIQUE / ON CONFLICT apply;
    -- only apply-worker ('replica') and tablesync ('local') dedup.
    IF current_setting('session_replication_role', true) = 'origin' THEN RETURN NEW; END IF;
    IF EXISTS (SELECT 1 FROM public."tagger_release_active" WHERE id=NEW.id) THEN RETURN NULL; END IF;
    RETURN NEW;
END;
$$;


ALTER FUNCTION public.tagger_release_active_dup_skip() OWNER TO kura;

--
-- Name: tagger_release_dup_skip(); Type: FUNCTION; Schema: public; Owner: kura
--

CREATE FUNCTION public.tagger_release_dup_skip() RETURNS trigger
    LANGUAGE plpgsql
    SET search_path TO 'public', 'pg_catalog'
    AS $$
BEGIN
    -- origin (app write) passes through so real UNIQUE / ON CONFLICT apply;
    -- only apply-worker ('replica') and tablesync ('local') dedup.
    IF current_setting('session_replication_role', true) = 'origin' THEN RETURN NEW; END IF;
    IF EXISTS (SELECT 1 FROM public."tagger_release" WHERE id=NEW.id) THEN RETURN NULL; END IF;
    RETURN NEW;
END;
$$;


ALTER FUNCTION public.tagger_release_dup_skip() OWNER TO kura;

--
-- Name: tagger_release_lease(text, bigint); Type: FUNCTION; Schema: public; Owner: kura_reconciler
--

CREATE FUNCTION public.tagger_release_lease(p_scope text, p_fencing_token bigint) RETURNS boolean
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'pg_catalog', 'public'
    AS $$
        BEGIN
            -- Fencing-matched, idempotent DELETE. The token proves prior
            -- authorized acquisition; no separate allowed_executor check is
            -- needed (and check_fencing is deliberately NOT used here -- it
            -- rejects an expired lease, but releasing an expired-but-un-
            -- reclaimed lease we still hold the token for is legitimate
            -- cleanup, not a stale write).
            DELETE FROM public.tagger_lifecycle_lock
                WHERE scope = p_scope
                  AND holder_agent_id = session_user
                  AND fencing_token = p_fencing_token;
            RETURN FOUND;  -- false == already released / reclaimed by another holder
        END;
        $$;


ALTER FUNCTION public.tagger_release_lease(p_scope text, p_fencing_token bigint) OWNER TO kura_reconciler;

--
-- Name: tagger_set_reconciler_pause(boolean, text, bigint); Type: FUNCTION; Schema: public; Owner: kura_reconciler
--

CREATE FUNCTION public.tagger_set_reconciler_pause(p_paused boolean, p_halt_reason text, p_fencing_token bigint) RETURNS void
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'pg_catalog', 'public'
    AS $$
        BEGIN
            PERFORM public.tagger_check_fencing('reconciler-control', p_fencing_token);
            UPDATE public.tagger_reconciler_state
                SET paused = p_paused, halt_reason = p_halt_reason,
                    updated_at = now(), updated_by = session_user
                WHERE id = true;
        END;
        $$;


ALTER FUNCTION public.tagger_set_reconciler_pause(p_paused boolean, p_halt_reason text, p_fencing_token bigint) OWNER TO kura_reconciler;

--
-- Name: tagger_tag_id_of(text); Type: FUNCTION; Schema: public; Owner: kura
--

CREATE FUNCTION public.tagger_tag_id_of(p_name text) RETURNS integer
    LANGUAGE sql STABLE
    SET search_path TO 'pg_catalog', 'public'
    AS $$
            SELECT tag_id FROM public.tag_name WHERE name = p_name
        $$;


ALTER FUNCTION public.tagger_tag_id_of(p_name text) OWNER TO kura;

--
-- Name: tagger_tag_name_of(integer); Type: FUNCTION; Schema: public; Owner: kura
--

CREATE FUNCTION public.tagger_tag_name_of(p_tag_id integer) RETURNS text
    LANGUAGE sql STABLE
    SET search_path TO 'pg_catalog', 'public'
    AS $$
            SELECT name FROM public.tag_name
            WHERE tag_id = p_tag_id ORDER BY ord LIMIT 1
        $$;


ALTER FUNCTION public.tagger_tag_name_of(p_tag_id integer) OWNER TO kura;

--
-- Name: tagger_upsert_heartbeat(text, text, text, bigint, integer, text, text); Type: FUNCTION; Schema: public; Owner: kura_reconciler
--

CREATE FUNCTION public.tagger_upsert_heartbeat(p_worker_id text, p_host text, p_profile text, p_generation bigint, p_release_id integer, p_provider text, p_model_sha256 text) RETURNS void
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'pg_catalog', 'public'
    AS $$
        BEGIN
            IF session_user NOT IN (SELECT db_role FROM public.tagger_allowed_executor WHERE scope = 'worker-heartbeat') THEN
                RAISE EXCEPTION 'tagger_upsert_heartbeat: session_user % is not an allowed executor for scope worker-heartbeat',
                    session_user;
            END IF;
            INSERT INTO public.tagger_worker_heartbeat
                (worker_id, host, profile, generation, release_id, provider, model_sha256, reported_at)
            VALUES (p_worker_id, p_host, p_profile, p_generation, p_release_id, p_provider, p_model_sha256, now())
            ON CONFLICT (worker_id) DO UPDATE SET
                host = EXCLUDED.host, profile = EXCLUDED.profile,
                generation = EXCLUDED.generation, release_id = EXCLUDED.release_id,
                provider = EXCLUDED.provider, model_sha256 = EXCLUDED.model_sha256,
                reported_at = EXCLUDED.reported_at;
        END;
        $$;


ALTER FUNCTION public.tagger_upsert_heartbeat(p_worker_id text, p_host text, p_profile text, p_generation bigint, p_release_id integer, p_provider text, p_model_sha256 text) OWNER TO kura_reconciler;

--
-- Name: trg_post_reset_dups_before_delete(); Type: FUNCTION; Schema: public; Owner: kura
--

CREATE FUNCTION public.trg_post_reset_dups_before_delete() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
    BEGIN
      UPDATE post SET status = 'pending', duplicate_of = NULL, data = NULL,
                      thumbnail = NULL, media_location = NULL, perceptual_distance = NULL
       WHERE duplicate_of = OLD.id;
      RETURN OLD;
    END; $$;


ALTER FUNCTION public.trg_post_reset_dups_before_delete() OWNER TO kura;

--
-- Name: trg_post_tagging_work_mirror(); Type: FUNCTION; Schema: public; Owner: kura_reconciler
--

CREATE FUNCTION public.trg_post_tagging_work_mirror() RETURNS trigger
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'pg_catalog', 'public'
    AS $$
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
$$;


ALTER FUNCTION public.trg_post_tagging_work_mirror() OWNER TO kura_reconciler;

--
-- Name: trg_tagger_governed_only(); Type: FUNCTION; Schema: public; Owner: kura
--

CREATE FUNCTION public.trg_tagger_governed_only() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        BEGIN
            IF current_user <> 'kura_reconciler' THEN
                RAISE EXCEPTION
                    'table % is governed: direct % from role % is not permitted, use a governed function',
                    TG_TABLE_NAME, TG_OP, current_user;
            END IF;
            IF TG_OP = 'DELETE' THEN
                RETURN OLD;
            END IF;
            RETURN NEW;
        END;
        $$;


ALTER FUNCTION public.trg_tagger_governed_only() OWNER TO kura;

--
-- Name: trg_tagger_lifecycle_event_edge_check(); Type: FUNCTION; Schema: public; Owner: kura
--

CREATE FUNCTION public.trg_tagger_lifecycle_event_edge_check() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        DECLARE
            edge_kind text;
            from_rank integer;
            to_rank integer;
        BEGIN
            SELECT kind INTO edge_kind FROM tagger_stage_transition
                WHERE from_stage IS NOT DISTINCT FROM NEW.from_stage AND to_stage = NEW.to_stage;
            IF edge_kind IS NULL THEN
                RAISE EXCEPTION 'tagger_lifecycle_event: % -> % is not an allowed transition edge',
                    NEW.from_stage, NEW.to_stage;
            END IF;
            IF NEW.from_stage IS NOT NULL THEN
                SELECT rank INTO from_rank FROM tagger_stage WHERE stage = NEW.from_stage;
                SELECT rank INTO to_rank FROM tagger_stage WHERE stage = NEW.to_stage;
                IF edge_kind = 'advance' AND to_rank <= from_rank THEN
                    RAISE EXCEPTION 'tagger_lifecycle_event: advance edge % -> % is not rank-increasing',
                        NEW.from_stage, NEW.to_stage;
                END IF;
            END IF;
            RETURN NEW;
        END;
        $$;


ALTER FUNCTION public.trg_tagger_lifecycle_event_edge_check() OWNER TO kura;

--
-- Name: trg_tagger_release_immutable(); Type: FUNCTION; Schema: public; Owner: kura
--

CREATE FUNCTION public.trg_tagger_release_immutable() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        BEGIN
            RAISE EXCEPTION 'tagger_release rows are immutable (id=%): UPDATE/DELETE not permitted', OLD.id;
        END;
        $$;


ALTER FUNCTION public.trg_tagger_release_immutable() OWNER TO kura;

--
-- Name: trg_tcd_append_only(); Type: FUNCTION; Schema: public; Owner: kura
--

CREATE FUNCTION public.trg_tcd_append_only() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        BEGIN
            RAISE EXCEPTION
                'tagger_candidate_disposition is append-only: % is not permitted, record a new decision row',
                TG_OP;
        END; $$;


ALTER FUNCTION public.trg_tcd_append_only() OWNER TO kura;

--
-- Name: trg_tce_append_only(); Type: FUNCTION; Schema: public; Owner: kura
--

CREATE FUNCTION public.trg_tce_append_only() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        BEGIN
            RAISE EXCEPTION
                'tagger_candidate_event is append-only: % is not permitted', TG_OP;
        END; $$;


ALTER FUNCTION public.trg_tce_append_only() OWNER TO kura;

--
-- Name: user_dup_skip(); Type: FUNCTION; Schema: public; Owner: kura
--

CREATE FUNCTION public.user_dup_skip() RETURNS trigger
    LANGUAGE plpgsql
    SET search_path TO 'public', 'pg_catalog'
    AS $$
BEGIN
    -- origin (app write) passes through so real UNIQUE / ON CONFLICT apply;
    -- only apply-worker ('replica') and tablesync ('local') dedup.
    IF current_setting('session_replication_role', true) = 'origin' THEN RETURN NEW; END IF;
    IF EXISTS (SELECT 1 FROM public."user" WHERE id=NEW.id) THEN RETURN NULL; END IF;
    RETURN NEW;
END;
$$;


ALTER FUNCTION public.user_dup_skip() OWNER TO kura;

--
-- Name: user_token_dup_skip(); Type: FUNCTION; Schema: public; Owner: kura
--

CREATE FUNCTION public.user_token_dup_skip() RETURNS trigger
    LANGUAGE plpgsql
    SET search_path TO 'public', 'pg_catalog'
    AS $$
BEGIN
    -- origin (app write) passes through so real UNIQUE / ON CONFLICT apply;
    -- only apply-worker ('replica') and tablesync ('local') dedup.
    IF current_setting('session_replication_role', true) = 'origin' THEN RETURN NEW; END IF;
    IF EXISTS (SELECT 1 FROM public."user_token" WHERE id=NEW.id) THEN RETURN NULL; END IF;
    RETURN NEW;
END;
$$;


ALTER FUNCTION public.user_token_dup_skip() OWNER TO kura;

SET default_tablespace = '';

SET default_table_access_method = heap;

--
-- Name: actor; Type: TABLE; Schema: claim_storage; Owner: kura
--

CREATE TABLE claim_storage.actor (
    id bigint NOT NULL,
    actor_id text NOT NULL
);


ALTER TABLE claim_storage.actor OWNER TO kura;

--
-- Name: TABLE actor; Type: COMMENT; Schema: claim_storage; Owner: kura
--

COMMENT ON TABLE claim_storage.actor IS 'Exact legacy source identity, not an inferred model version or human account';


--
-- Name: actor_id_seq; Type: SEQUENCE; Schema: claim_storage; Owner: kura
--

ALTER TABLE claim_storage.actor ALTER COLUMN id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME claim_storage.actor_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: claim; Type: TABLE; Schema: claim_storage; Owner: kura
--

CREATE TABLE claim_storage.claim (
    post_id integer NOT NULL,
    tag_id integer NOT NULL,
    actor_key bigint NOT NULL,
    claimed_at timestamp with time zone DEFAULT now() NOT NULL
);


ALTER TABLE claim_storage.claim OWNER TO kura;

--
-- Name: migration; Type: TABLE; Schema: claim_storage; Owner: kura
--

CREATE TABLE claim_storage.migration (
    singleton boolean DEFAULT true NOT NULL,
    phase text DEFAULT 'copying'::text NOT NULL,
    cursor_post integer DEFAULT '-2147483648'::integer NOT NULL,
    cursor_tag integer DEFAULT '-2147483648'::integer NOT NULL,
    cursor_actor text DEFAULT ''::text NOT NULL,
    copied_rows bigint DEFAULT 0 NOT NULL,
    verified_through integer DEFAULT '-2147483648'::integer NOT NULL,
    updated_at timestamp with time zone DEFAULT clock_timestamp() NOT NULL,
    CONSTRAINT migration_phase_check CHECK ((phase = ANY (ARRAY['copying'::text, 'verifying'::text, 'ready'::text, 'active'::text]))),
    CONSTRAINT migration_singleton_check CHECK (singleton)
);


ALTER TABLE claim_storage.migration OWNER TO kura;

--
-- Name: asset; Type: TABLE; Schema: collector_next; Owner: kura
--

CREATE TABLE collector_next.asset (
    id bigint NOT NULL,
    uuid uuid DEFAULT gen_random_uuid() NOT NULL,
    owner_id integer NOT NULL,
    blob_id bigint NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


ALTER TABLE collector_next.asset OWNER TO kura;

--
-- Name: asset_id_seq; Type: SEQUENCE; Schema: collector_next; Owner: kura
--

ALTER TABLE collector_next.asset ALTER COLUMN id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME collector_next.asset_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: asset_label_state; Type: TABLE; Schema: collector_next; Owner: kura
--

CREATE TABLE collector_next.asset_label_state (
    asset_id bigint NOT NULL,
    owner_id integer NOT NULL,
    revision bigint DEFAULT 0 NOT NULL,
    positive_count integer DEFAULT 0 NOT NULL,
    last_observed_at timestamp with time zone DEFAULT '1970-01-01 09:00:00+09'::timestamp with time zone NOT NULL,
    policy_id smallint DEFAULT 1 NOT NULL,
    CONSTRAINT asset_label_state_positive_count_check CHECK (((positive_count >= 0) AND (positive_count <= 128))),
    CONSTRAINT asset_label_state_revision_check CHECK ((revision >= 0))
);


ALTER TABLE collector_next.asset_label_state OWNER TO kura;

--
-- Name: blob; Type: TABLE; Schema: collector_next; Owner: kura
--

CREATE TABLE collector_next.blob (
    id bigint NOT NULL,
    sha256 bytea NOT NULL,
    byte_size bigint NOT NULL,
    CONSTRAINT blob_byte_size_check CHECK ((byte_size > 0)),
    CONSTRAINT blob_sha256_check CHECK ((octet_length(sha256) = 32))
);


ALTER TABLE collector_next.blob OWNER TO kura;

--
-- Name: blob_id_seq; Type: SEQUENCE; Schema: collector_next; Owner: kura
--

ALTER TABLE collector_next.blob ALTER COLUMN id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME collector_next.blob_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: blob_location; Type: TABLE; Schema: collector_next; Owner: kura
--

CREATE TABLE collector_next.blob_location (
    blob_id bigint NOT NULL,
    storage_key text NOT NULL
);


ALTER TABLE collector_next.blob_location OWNER TO kura;

--
-- Name: collection; Type: TABLE; Schema: collector_next; Owner: kura
--

CREATE TABLE collector_next.collection (
    id bigint NOT NULL,
    owner_id integer NOT NULL,
    title text NOT NULL,
    request_key uuid NOT NULL,
    CONSTRAINT collection_title_check CHECK (((length(title) >= 1) AND (length(title) <= 200)))
);


ALTER TABLE collector_next.collection OWNER TO kura;

--
-- Name: collection_entry; Type: TABLE; Schema: collector_next; Owner: kura
--

CREATE TABLE collector_next.collection_entry (
    id bigint NOT NULL,
    owner_id integer NOT NULL,
    collection_id bigint NOT NULL,
    asset_id bigint NOT NULL,
    "position" bigint NOT NULL,
    CONSTRAINT collection_entry_position_check CHECK (("position" > 0))
);


ALTER TABLE collector_next.collection_entry OWNER TO kura;

--
-- Name: collection_entry_id_seq; Type: SEQUENCE; Schema: collector_next; Owner: kura
--

ALTER TABLE collector_next.collection_entry ALTER COLUMN id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME collector_next.collection_entry_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: collection_id_seq; Type: SEQUENCE; Schema: collector_next; Owner: kura
--

ALTER TABLE collector_next.collection ALTER COLUMN id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME collector_next.collection_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: import_item; Type: TABLE; Schema: collector_next; Owner: kura
--

CREATE TABLE collector_next.import_item (
    owner_id integer NOT NULL,
    operation_key uuid NOT NULL,
    "position" integer NOT NULL,
    request_key uuid NOT NULL,
    original_name character varying(255) NOT NULL,
    relative_path text,
    byte_size bigint NOT NULL,
    sha256 bytea,
    collection_id bigint,
    collection_position bigint,
    state smallint DEFAULT 0 NOT NULL,
    error_code text,
    error_message character varying(500),
    CONSTRAINT import_item_byte_size_check CHECK ((byte_size >= 0)),
    CONSTRAINT import_item_check CHECK (((collection_id IS NULL) = (collection_position IS NULL))),
    CONSTRAINT import_item_check1 CHECK (((sha256 IS NOT NULL) OR (state = 2))),
    CONSTRAINT import_item_collection_position_check CHECK ((collection_position > 0)),
    CONSTRAINT import_item_position_check CHECK (("position" > 0)),
    CONSTRAINT import_item_sha256_check CHECK (((sha256 IS NULL) OR (octet_length(sha256) = 32))),
    CONSTRAINT import_item_state_check CHECK ((state = ANY (ARRAY[0, 1, 2])))
);


ALTER TABLE collector_next.import_item OWNER TO kura;

--
-- Name: import_operation; Type: TABLE; Schema: collector_next; Owner: kura
--

CREATE TABLE collector_next.import_operation (
    owner_id integer NOT NULL,
    request_key uuid NOT NULL,
    fingerprint bytea NOT NULL,
    item_count integer NOT NULL,
    registered_count integer DEFAULT 0 NOT NULL,
    rejected_count integer DEFAULT 0 NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT import_operation_check CHECK (((registered_count + rejected_count) <= item_count)),
    CONSTRAINT import_operation_fingerprint_check CHECK ((octet_length(fingerprint) = 32)),
    CONSTRAINT import_operation_item_count_check CHECK (((item_count >= 1) AND (item_count <= 10000))),
    CONSTRAINT import_operation_registered_count_check CHECK ((registered_count >= 0)),
    CONSTRAINT import_operation_rejected_count_check CHECK ((rejected_count >= 0))
);


ALTER TABLE collector_next.import_operation OWNER TO kura;

--
-- Name: label_head; Type: TABLE; Schema: collector_next; Owner: kura
--

CREATE TABLE collector_next.label_head (
    owner_id integer NOT NULL,
    asset_id bigint NOT NULL,
    tag_id bigint NOT NULL,
    observation_id bigint NOT NULL,
    present boolean NOT NULL
);


ALTER TABLE collector_next.label_head OWNER TO kura;

--
-- Name: label_observation; Type: TABLE; Schema: collector_next; Owner: kura
--

CREATE TABLE collector_next.label_observation (
    id bigint NOT NULL,
    owner_id integer NOT NULL,
    asset_id bigint NOT NULL,
    tag_id bigint NOT NULL,
    producer_id bigint NOT NULL,
    request_key uuid NOT NULL,
    present boolean NOT NULL,
    prior_present boolean NOT NULL,
    based_on_id bigint,
    observed_at timestamp with time zone NOT NULL
);


ALTER TABLE collector_next.label_observation OWNER TO kura;

--
-- Name: label_observation_id_seq; Type: SEQUENCE; Schema: collector_next; Owner: kura
--

ALTER TABLE collector_next.label_observation ALTER COLUMN id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME collector_next.label_observation_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: label_operation; Type: TABLE; Schema: collector_next; Owner: kura
--

CREATE TABLE collector_next.label_operation (
    owner_id integer NOT NULL,
    request_key uuid NOT NULL,
    fingerprint bytea NOT NULL,
    producer_id bigint NOT NULL,
    asset_count integer NOT NULL,
    tag_count integer NOT NULL,
    policy_id smallint DEFAULT 1 NOT NULL,
    created_at timestamp with time zone DEFAULT clock_timestamp() NOT NULL,
    CONSTRAINT label_operation_asset_count_check CHECK (((asset_count >= 1) AND (asset_count <= 100))),
    CONSTRAINT label_operation_fingerprint_check CHECK ((octet_length(fingerprint) = 32)),
    CONSTRAINT label_operation_tag_count_check CHECK (((tag_count >= 1) AND (tag_count <= 16)))
);


ALTER TABLE collector_next.label_operation OWNER TO kura;

--
-- Name: label_policy; Type: TABLE; Schema: collector_next; Owner: kura
--

CREATE TABLE collector_next.label_policy (
    id smallint NOT NULL,
    version text NOT NULL
);


ALTER TABLE collector_next.label_policy OWNER TO kura;

--
-- Name: label_producer; Type: TABLE; Schema: collector_next; Owner: kura
--

CREATE TABLE collector_next.label_producer (
    id bigint NOT NULL,
    owner_id integer NOT NULL,
    kind_id smallint CONSTRAINT label_producer_kind_id_not_null1 NOT NULL,
    reference text NOT NULL,
    version text NOT NULL
);


ALTER TABLE collector_next.label_producer OWNER TO kura;

--
-- Name: label_producer_id_seq; Type: SEQUENCE; Schema: collector_next; Owner: kura
--

ALTER TABLE collector_next.label_producer ALTER COLUMN id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME collector_next.label_producer_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: label_producer_kind; Type: TABLE; Schema: collector_next; Owner: kura
--

CREATE TABLE collector_next.label_producer_kind (
    id smallint NOT NULL,
    name text NOT NULL
);


ALTER TABLE collector_next.label_producer_kind OWNER TO kura;

--
-- Name: label_target; Type: TABLE; Schema: collector_next; Owner: kura
--

CREATE TABLE collector_next.label_target (
    owner_id integer NOT NULL,
    request_key uuid NOT NULL,
    asset_id bigint NOT NULL,
    before_revision bigint NOT NULL,
    after_revision bigint NOT NULL,
    CONSTRAINT label_target_before_revision_check CHECK ((before_revision >= 0)),
    CONSTRAINT label_target_check CHECK ((after_revision = (before_revision + 1)))
);


ALTER TABLE collector_next.label_target OWNER TO kura;

--
-- Name: media_current; Type: TABLE; Schema: collector_next; Owner: kura
--

CREATE TABLE collector_next.media_current (
    asset_id bigint NOT NULL,
    owner_id integer NOT NULL,
    original_name text NOT NULL,
    media_type text NOT NULL,
    width integer NOT NULL,
    height integer NOT NULL,
    CONSTRAINT media_current_height_check CHECK ((height > 0)),
    CONSTRAINT media_current_width_check CHECK ((width > 0))
);


ALTER TABLE collector_next.media_current OWNER TO kura;

--
-- Name: media_tag_current; Type: VIEW; Schema: collector_next; Owner: kura
--

CREATE VIEW collector_next.media_tag_current AS
 SELECT owner_id,
    asset_id,
    tag_id,
    observation_id
   FROM collector_next.label_head
  WHERE present;


ALTER VIEW collector_next.media_tag_current OWNER TO kura;

--
-- Name: receipt; Type: TABLE; Schema: collector_next; Owner: kura
--

CREATE TABLE collector_next.receipt (
    owner_id integer NOT NULL,
    request_key uuid NOT NULL,
    fingerprint bytea NOT NULL,
    original_name text NOT NULL,
    asset_id bigint NOT NULL,
    entry_id bigint,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    relative_path character varying(4096),
    CONSTRAINT receipt_fingerprint_check CHECK ((octet_length(fingerprint) = 32)),
    CONSTRAINT receipt_relative_path_check CHECK (((relative_path IS NULL) OR ((length((relative_path)::text) > 0) AND ("left"((relative_path)::text, 1) <> '/'::text))))
);


ALTER TABLE collector_next.receipt OWNER TO kura;

--
-- Name: schema_version; Type: TABLE; Schema: collector_next; Owner: kura
--

CREATE TABLE collector_next.schema_version (
    version integer NOT NULL,
    CONSTRAINT schema_version_version_check CHECK ((version = 5))
);


ALTER TABLE collector_next.schema_version OWNER TO kura;

--
-- Name: tag; Type: TABLE; Schema: collector_next; Owner: kura
--

CREATE TABLE collector_next.tag (
    id bigint NOT NULL,
    owner_id integer NOT NULL,
    name character varying(100) NOT NULL,
    normalized_name character varying(100) NOT NULL COLLATE pg_catalog."C",
    CONSTRAINT tag_name_check CHECK ((length((name)::text) > 0)),
    CONSTRAINT tag_normalized_name_check CHECK ((length((normalized_name)::text) > 0))
);


ALTER TABLE collector_next.tag OWNER TO kura;

--
-- Name: TABLE tag; Type: COMMENT; Schema: collector_next; Owner: kura
--

COMMENT ON TABLE collector_next.tag IS 'Asset organization labels, not semantic scene tag_projection';


--
-- Name: tag_id_seq; Type: SEQUENCE; Schema: collector_next; Owner: kura
--

ALTER TABLE collector_next.tag ALTER COLUMN id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME collector_next.tag_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: tag_usage; Type: TABLE; Schema: collector_next; Owner: kura
--

CREATE TABLE collector_next.tag_usage (
    tag_id bigint NOT NULL,
    owner_id integer NOT NULL,
    positive_count bigint DEFAULT 0 NOT NULL,
    logical_bytes bigint DEFAULT 0 NOT NULL,
    representative_asset_id bigint,
    revision bigint DEFAULT 0 NOT NULL,
    representative_policy smallint DEFAULT 1 NOT NULL,
    CONSTRAINT tag_summary_empty CHECK (((positive_count > 0) OR ((logical_bytes = 0) AND (representative_asset_id IS NULL)))),
    CONSTRAINT tag_usage_logical_bytes_check CHECK ((logical_bytes >= 0)),
    CONSTRAINT tag_usage_positive_count_check CHECK ((positive_count >= 0)),
    CONSTRAINT tag_usage_representative_policy_check CHECK ((representative_policy = 1)),
    CONSTRAINT tag_usage_revision_check CHECK ((revision >= 0))
);


ALTER TABLE collector_next.tag_usage OWNER TO kura;

--
-- Name: alembic_version; Type: TABLE; Schema: public; Owner: kura
--

CREATE TABLE public.alembic_version (
    version_num character varying(32) NOT NULL
);


ALTER TABLE public.alembic_version OWNER TO kura;

--
-- Name: censor_annotation; Type: TABLE; Schema: public; Owner: kura
--

CREATE TABLE public.censor_annotation (
    id integer NOT NULL,
    post_id integer NOT NULL,
    image_sha256 bytea,
    post_version_seen integer,
    censor_type text NOT NULL,
    shape_type text NOT NULL,
    geometry jsonb NOT NULL,
    mask bytea,
    created_by integer,
    created_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone,
    region_version integer DEFAULT 1 NOT NULL,
    client_mutation_uuid uuid,
    deleted_at timestamp with time zone,
    supersedes_id integer,
    negative boolean DEFAULT false NOT NULL
);


ALTER TABLE public.censor_annotation OWNER TO kura;

--
-- Name: censor_annotation_id_seq; Type: SEQUENCE; Schema: public; Owner: kura
--

CREATE SEQUENCE public.censor_annotation_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.censor_annotation_id_seq OWNER TO kura;

--
-- Name: censor_annotation_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: kura
--

ALTER SEQUENCE public.censor_annotation_id_seq OWNED BY public.censor_annotation.id;


--
-- Name: censor_pair; Type: TABLE; Schema: public; Owner: kura
--

CREATE TABLE public.censor_pair (
    id integer NOT NULL,
    censored_post_id integer NOT NULL,
    uncensored_post_id integer NOT NULL,
    method text,
    off_censor_pct real,
    confirmed boolean DEFAULT false NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone
);


ALTER TABLE public.censor_pair OWNER TO kura;

--
-- Name: censor_pair_id_seq; Type: SEQUENCE; Schema: public; Owner: kura
--

CREATE SEQUENCE public.censor_pair_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.censor_pair_id_seq OWNER TO kura;

--
-- Name: censor_pair_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: kura
--

ALTER SEQUENCE public.censor_pair_id_seq OWNED BY public.censor_pair.id;


--
-- Name: censor_pair_search; Type: TABLE; Schema: public; Owner: kura
--

CREATE TABLE public.censor_pair_search (
    post_id integer NOT NULL,
    status text DEFAULT 'pending'::text NOT NULL,
    attempts integer DEFAULT 0 NOT NULL,
    candidates_seen integer DEFAULT 0 NOT NULL,
    best_candidate integer,
    best_score real,
    rejected_candidates integer[] DEFAULT '{}'::integer[] NOT NULL,
    note text,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT censor_pair_search_status_check CHECK ((status = ANY (ARRAY['pending'::text, 'found'::text, 'not_found'::text, 'excluded'::text, 'review'::text])))
);


ALTER TABLE public.censor_pair_search OWNER TO kura;

--
-- Name: censor_pair_search_scope; Type: TABLE; Schema: public; Owner: kura
--

CREATE TABLE public.censor_pair_search_scope (
    pool_id integer NOT NULL,
    enabled boolean DEFAULT true NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);


ALTER TABLE public.censor_pair_search_scope OWNER TO kura;

--
-- Name: censor_trained_post; Type: TABLE; Schema: public; Owner: kura
--

CREATE TABLE public.censor_trained_post (
    post_id integer NOT NULL,
    perceptual_hash bytea,
    model text,
    split text,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


ALTER TABLE public.censor_trained_post OWNER TO kura;

--
-- Name: comment; Type: TABLE; Schema: public; Owner: kura
--

CREATE TABLE public.comment (
    id integer NOT NULL,
    user_id integer,
    post_id integer NOT NULL,
    creation_time timestamp without time zone NOT NULL,
    last_edit_time timestamp without time zone,
    text text,
    version integer NOT NULL
);


ALTER TABLE public.comment OWNER TO kura;

--
-- Name: comment_id_seq; Type: SEQUENCE; Schema: public; Owner: kura
--

CREATE SEQUENCE public.comment_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.comment_id_seq OWNER TO kura;

--
-- Name: comment_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: kura
--

ALTER SEQUENCE public.comment_id_seq OWNED BY public.comment.id;


--
-- Name: comment_score; Type: TABLE; Schema: public; Owner: kura
--

CREATE TABLE public.comment_score (
    comment_id integer NOT NULL,
    user_id integer NOT NULL,
    "time" timestamp without time zone NOT NULL,
    score integer NOT NULL
);


ALTER TABLE public.comment_score OWNER TO kura;

--
-- Name: human_login_key; Type: TABLE; Schema: public; Owner: kura
--

CREATE TABLE public.human_login_key (
    key_digest character varying(64) NOT NULL,
    user_id integer NOT NULL,
    issued_at timestamp with time zone NOT NULL,
    revoked_at timestamp with time zone,
    CONSTRAINT human_login_key_check CHECK (((revoked_at IS NULL) OR (revoked_at >= issued_at))),
    CONSTRAINT human_login_key_key_digest_check CHECK (((key_digest)::text ~ '^[0-9a-f]{64}$'::text))
);


ALTER TABLE public.human_login_key OWNER TO kura;

--
-- Name: kura_alembic_version; Type: TABLE; Schema: public; Owner: kura
--

CREATE TABLE public.kura_alembic_version (
    version_num character varying(32) NOT NULL
);


ALTER TABLE public.kura_alembic_version OWNER TO kura;

--
-- Name: lora_asset; Type: TABLE; Schema: public; Owner: kura
--

CREATE TABLE public.lora_asset (
    id integer NOT NULL,
    name text NOT NULL,
    status text DEFAULT 'pending'::text NOT NULL,
    origin text DEFAULT 'trained'::text NOT NULL,
    kind text,
    base_model text,
    canonical_path text,
    comfy_ref text,
    sha256 text,
    size_bytes bigint,
    created_by text,
    source_url text,
    preview_post_id integer,
    imported_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    notes text,
    CONSTRAINT ck_lora_asset_origin CHECK ((origin = ANY (ARRAY['trained'::text, 'imported'::text]))),
    CONSTRAINT ck_lora_asset_status CHECK ((status = ANY (ARRAY['pending'::text, 'available'::text, 'retired'::text])))
);


ALTER TABLE public.lora_asset OWNER TO kura;

--
-- Name: lora_asset_id_seq; Type: SEQUENCE; Schema: public; Owner: kura
--

CREATE SEQUENCE public.lora_asset_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.lora_asset_id_seq OWNER TO kura;

--
-- Name: lora_asset_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: kura
--

ALTER SEQUENCE public.lora_asset_id_seq OWNED BY public.lora_asset.id;


--
-- Name: lora_concept; Type: TABLE; Schema: public; Owner: kura
--

CREATE TABLE public.lora_concept (
    id integer NOT NULL,
    lora_id integer NOT NULL,
    trigger text NOT NULL,
    description text
);


ALTER TABLE public.lora_concept OWNER TO kura;

--
-- Name: TABLE lora_concept; Type: COMMENT; Schema: public; Owner: kura
--

COMMENT ON TABLE public.lora_concept IS 'Trigger words for a LoRA (multiple for multi-concept LoRAs, e.g. sekalistyle + daioostyle).';


--
-- Name: lora_concept_id_seq; Type: SEQUENCE; Schema: public; Owner: kura
--

CREATE SEQUENCE public.lora_concept_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.lora_concept_id_seq OWNER TO kura;

--
-- Name: lora_concept_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: kura
--

ALTER SEQUENCE public.lora_concept_id_seq OWNED BY public.lora_concept.id;


--
-- Name: lora_training_run; Type: TABLE; Schema: public; Owner: kura
--

CREATE TABLE public.lora_training_run (
    id integer NOT NULL,
    asset_id integer NOT NULL,
    state text DEFAULT 'queued'::text NOT NULL,
    cur_step integer,
    total_steps integer,
    cur_epoch integer,
    eta_seconds integer,
    progress_at timestamp with time zone,
    final_loss real,
    params jsonb,
    job_spec jsonb,
    exit_code integer,
    started_at timestamp with time zone,
    completed_at timestamp with time zone,
    resume_state_path text,
    resume_count integer DEFAULT 0 NOT NULL,
    backfilled boolean DEFAULT false NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_lora_training_run_state CHECK ((state = ANY (ARRAY['queued'::text, 'admitted'::text, 'running'::text, 'held'::text, 'yielded'::text, 'done'::text, 'failed'::text, 'cancelled'::text])))
);


ALTER TABLE public.lora_training_run OWNER TO kura;

--
-- Name: COLUMN lora_training_run.job_spec; Type: COMMENT; Schema: public; Owner: kura
--

COMMENT ON COLUMN public.lora_training_run.job_spec IS 'Schemaless run state (attempt/started_at/pid/container_id/mount_snapshot/log_path/output_dir/completed_at/exit_code/warnings[]/gate_event/queue{...}). Notable optional key: dataset_dir (HOST-absolute kohya dataset path) -- when the launcher passes it to /start, lora_post/lora_concept auto-populate from the dataset (api/lora_post_resolve.py); unresolved files surface as a lora_post_unresolved warning in warnings[], not a training failure.';


--
-- Name: lora_model; Type: VIEW; Schema: public; Owner: kura
--

CREATE VIEW public.lora_model AS
 SELECT a.id,
    a.name,
        CASE
            WHEN (a.status = ANY (ARRAY['available'::text, 'retired'::text])) THEN a.status
            WHEN (r.state = ANY (ARRAY['running'::text, 'held'::text, 'yielded'::text])) THEN 'training'::text
            WHEN (r.state = ANY (ARRAY['failed'::text, 'cancelled'::text])) THEN 'failed'::text
            ELSE 'queued'::text
        END AS status,
    a.origin,
    a.kind,
    a.base_model,
    a.canonical_path,
    a.comfy_ref,
    a.sha256,
    a.size_bytes,
    a.created_by,
    a.source_url,
    a.preview_post_id,
    r.params,
    r.job_spec,
    r.final_loss,
    r.cur_step,
    r.total_steps,
    r.cur_epoch,
    r.eta_seconds,
    r.progress_at,
    a.created_at,
    a.imported_at,
    GREATEST(a.updated_at, COALESCE(r.updated_at, a.updated_at)) AS updated_at,
    a.notes
   FROM (public.lora_asset a
     LEFT JOIN LATERAL ( SELECT t.id,
            t.asset_id,
            t.state,
            t.cur_step,
            t.total_steps,
            t.cur_epoch,
            t.eta_seconds,
            t.progress_at,
            t.final_loss,
            t.params,
            t.job_spec,
            t.exit_code,
            t.started_at,
            t.completed_at,
            t.resume_state_path,
            t.resume_count,
            t.backfilled,
            t.created_at,
            t.updated_at
           FROM public.lora_training_run t
          WHERE (t.asset_id = a.id)
          ORDER BY t.id DESC
         LIMIT 1) r ON (true));


ALTER VIEW public.lora_model OWNER TO kura;

--
-- Name: lora_model_base; Type: TABLE; Schema: public; Owner: kura
--

CREATE TABLE public.lora_model_base (
    id integer CONSTRAINT lora_model_id_not_null NOT NULL,
    name text CONSTRAINT lora_model_name_not_null NOT NULL,
    status text DEFAULT 'available'::text CONSTRAINT lora_model_status_not_null NOT NULL,
    origin text DEFAULT 'trained'::text CONSTRAINT lora_model_origin_not_null NOT NULL,
    kind text,
    base_model text,
    canonical_path text,
    comfy_ref text,
    sha256 text,
    size_bytes bigint,
    created_by text,
    source_url text,
    preview_post_id integer,
    params jsonb,
    job_spec jsonb,
    final_loss real,
    cur_step integer,
    total_steps integer,
    cur_epoch integer,
    eta_seconds integer,
    progress_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT now() CONSTRAINT lora_model_created_at_not_null NOT NULL,
    imported_at timestamp with time zone,
    updated_at timestamp with time zone DEFAULT now() CONSTRAINT lora_model_updated_at_not_null NOT NULL,
    notes text,
    CONSTRAINT ck_lora_model_origin CHECK ((origin = ANY (ARRAY['trained'::text, 'imported'::text]))),
    CONSTRAINT ck_lora_model_status CHECK ((status = ANY (ARRAY['available'::text, 'queued'::text, 'training'::text, 'failed'::text, 'retired'::text])))
);


ALTER TABLE public.lora_model_base OWNER TO kura;

--
-- Name: TABLE lora_model_base; Type: COMMENT; Schema: public; Owner: kura
--

COMMENT ON TABLE public.lora_model_base IS 'LoRA asset ledger: registry + training-queue + live-progress. One row per LoRA (trained here or imported). status=queued/training rows are the training queue; file home is models/loras/.';


--
-- Name: COLUMN lora_model_base.origin; Type: COMMENT; Schema: public; Owner: kura
--

COMMENT ON COLUMN public.lora_model_base.origin IS 'trained = built here from kura pools (see lora_pool); imported = fetched from elsewhere (see source_url).';


--
-- Name: COLUMN lora_model_base.canonical_path; Type: COMMENT; Schema: public; Owner: kura
--

COMMENT ON COLUMN public.lora_model_base.canonical_path IS 'physical .safetensors path (models/loras/<name>.safetensors); ledger is authoritative, reconciler keeps FS in sync.';


--
-- Name: COLUMN lora_model_base.preview_post_id; Type: COMMENT; Schema: public; Owner: kura
--

COMMENT ON COLUMN public.lora_model_base.preview_post_id IS 'kura post.id used as the LoRA thumbnail (a validation output for trained, or the imported preview).';


--
-- Name: lora_model_id_seq; Type: SEQUENCE; Schema: public; Owner: kura
--

CREATE SEQUENCE public.lora_model_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.lora_model_id_seq OWNER TO kura;

--
-- Name: lora_model_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: kura
--

ALTER SEQUENCE public.lora_model_id_seq OWNED BY public.lora_model_base.id;


--
-- Name: lora_post; Type: TABLE; Schema: public; Owner: kura
--

CREATE TABLE public.lora_post (
    id integer NOT NULL,
    lora_id integer NOT NULL,
    concept_id integer,
    post_id integer NOT NULL
);


ALTER TABLE public.lora_post OWNER TO kura;

--
-- Name: TABLE lora_post; Type: COMMENT; Schema: public; Owner: kura
--

COMMENT ON TABLE public.lora_post IS 'Authoritative LoRA training provenance: the exact kura posts (per concept) a LoRA was trained on. AUTO-POPULATED at training start when POST /loras/{id}/start receives an optional dataset_dir (HOST-absolute kohya dataset path) -- see api/lora_post_resolve.py for the resolution ladder (filename fast-path p{id}/sk_{id} -> direct md5 vs post.checksum_md5 -> ASIN/pixel backtrace vs char-bins*/). Omitting dataset_dir (still the default for most callers) leaves rows empty for that asset; scripts/lora_register_posts.py --apply is the manual backfill path for pre-existing gaps. lora_pool is a VIEW derived FROM this table (post -> pool rollup), never write it directly.';


--
-- Name: pool_post; Type: TABLE; Schema: public; Owner: kura
--

CREATE TABLE public.pool_post (
    pool_id integer NOT NULL,
    post_id integer NOT NULL,
    ord integer NOT NULL
);


ALTER TABLE public.pool_post OWNER TO kura;

--
-- Name: lora_pool; Type: VIEW; Schema: public; Owner: kura
--

CREATE VIEW public.lora_pool AS
 SELECT lp.lora_id,
    lp.concept_id,
    pp.pool_id,
    (count(*))::integer AS image_count
   FROM (public.lora_post lp
     JOIN public.pool_post pp ON ((pp.post_id = lp.post_id)))
  GROUP BY lp.lora_id, lp.concept_id, pp.pool_id;


ALTER VIEW public.lora_pool OWNER TO kura;

--
-- Name: VIEW lora_pool; Type: COMMENT; Schema: public; Owner: kura
--

COMMENT ON VIEW public.lora_pool IS 'Derived rollup of lora_post → pools (via pool_post). Powers the pool-list trained? badge + card source chips. Do not write; edit lora_post.';


--
-- Name: lora_post_id_seq; Type: SEQUENCE; Schema: public; Owner: kura
--

CREATE SEQUENCE public.lora_post_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.lora_post_id_seq OWNER TO kura;

--
-- Name: lora_post_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: kura
--

ALTER SEQUENCE public.lora_post_id_seq OWNED BY public.lora_post.id;


--
-- Name: lora_training_run_id_seq; Type: SEQUENCE; Schema: public; Owner: kura
--

CREATE SEQUENCE public.lora_training_run_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.lora_training_run_id_seq OWNER TO kura;

--
-- Name: lora_training_run_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: kura
--

ALTER SEQUENCE public.lora_training_run_id_seq OWNED BY public.lora_training_run.id;


--
-- Name: pool; Type: TABLE; Schema: public; Owner: kura
--

CREATE TABLE public.pool (
    id integer NOT NULL,
    version integer NOT NULL,
    description text,
    category_id integer NOT NULL,
    creation_time timestamp without time zone NOT NULL,
    last_edit_time timestamp without time zone,
    meta_tagging_status text DEFAULT 'pending'::text NOT NULL,
    meta_tagging_locked_until timestamp with time zone,
    meta_tagging_attempts integer DEFAULT 0 NOT NULL,
    meta_tagging_last_error text,
    cover_post_id integer,
    verify_status text,
    tagger_source text,
    CONSTRAINT pool_meta_tagging_status_chk CHECK ((meta_tagging_status = ANY (ARRAY['pending'::text, 'done'::text, 'failed'::text, 'skipped'::text])))
);


ALTER TABLE public.pool OWNER TO kura;

--
-- Name: pool_category; Type: TABLE; Schema: public; Owner: kura
--

CREATE TABLE public.pool_category (
    id integer CONSTRAINT pool_category_id_not_null1 NOT NULL,
    version integer NOT NULL,
    name character varying(32) NOT NULL,
    color character varying(32) NOT NULL,
    "default" boolean NOT NULL
);


ALTER TABLE public.pool_category OWNER TO kura;

--
-- Name: pool_category_id_seq; Type: SEQUENCE; Schema: public; Owner: kura
--

CREATE SEQUENCE public.pool_category_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.pool_category_id_seq OWNER TO kura;

--
-- Name: pool_category_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: kura
--

ALTER SEQUENCE public.pool_category_id_seq OWNED BY public.pool_category.id;


--
-- Name: pool_id_seq; Type: SEQUENCE; Schema: public; Owner: kura
--

CREATE SEQUENCE public.pool_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.pool_id_seq OWNER TO kura;

--
-- Name: pool_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: kura
--

ALTER SEQUENCE public.pool_id_seq OWNED BY public.pool.id;


--
-- Name: pool_name; Type: TABLE; Schema: public; Owner: kura
--

CREATE TABLE public.pool_name (
    pool_name_id integer NOT NULL,
    pool_id integer NOT NULL,
    name character varying(256) NOT NULL,
    ord integer NOT NULL
);


ALTER TABLE public.pool_name OWNER TO kura;

--
-- Name: pool_name_pool_name_id_seq; Type: SEQUENCE; Schema: public; Owner: kura
--

CREATE SEQUENCE public.pool_name_pool_name_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.pool_name_pool_name_id_seq OWNER TO kura;

--
-- Name: pool_name_pool_name_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: kura
--

ALTER SEQUENCE public.pool_name_pool_name_id_seq OWNED BY public.pool_name.pool_name_id;


--
-- Name: pool_state; Type: TABLE; Schema: public; Owner: kura
--

CREATE TABLE public.pool_state (
    pool_id integer NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    scope text
);


ALTER TABLE public.pool_state OWNER TO kura;

--
-- Name: pool_url; Type: TABLE; Schema: public; Owner: kura
--

CREATE TABLE public.pool_url (
    pool_id integer NOT NULL,
    url text NOT NULL,
    is_primary boolean DEFAULT false NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    expected_count integer,
    observed_count integer,
    last_observed_at timestamp with time zone
);


ALTER TABLE public.pool_url OWNER TO kura;

--
-- Name: post; Type: TABLE; Schema: public; Owner: kura
--

CREATE TABLE public.post (
    id integer NOT NULL,
    user_id integer,
    creation_time timestamp without time zone NOT NULL,
    last_edit_time timestamp without time zone,
    safety character varying(32) NOT NULL,
    type character varying(32) NOT NULL,
    checksum character varying(64) NOT NULL,
    source character varying(2048),
    file_size bigint,
    image_width integer,
    image_height integer,
    "mime-type" character varying(32) NOT NULL,
    version integer NOT NULL,
    flags character varying(32),
    checksum_md5 character varying(32),
    primary_pool_id integer,
    original_filename text,
    data bytea,
    sha256 bytea,
    perceptual_hash bytea,
    duplicate_of integer,
    status text DEFAULT 'pending'::text,
    tags text[],
    tags_search text[],
    perceptual_hash_bands integer[],
    perceptual_distance smallint,
    thumbnail bytea,
    media_location text,
    creation_at timestamp with time zone GENERATED ALWAYS AS ((creation_time AT TIME ZONE 'UTC'::text)),
    CONSTRAINT ck_post_media_location CHECK (((media_location IS NULL) OR (media_location = ANY (ARRAY['pg'::text, 'hdd'::text])))),
    CONSTRAINT ck_post_status CHECK (((status IS NULL) OR (status = ANY (ARRAY['pending'::text, 'ok'::text, 'exact_dup'::text, 'near_dup'::text, 'corrupt'::text, 'lost'::text]))))
)
WITH (autovacuum_vacuum_scale_factor='0.02', autovacuum_analyze_scale_factor='0.01');
ALTER TABLE ONLY public.post ALTER COLUMN data SET STORAGE EXTERNAL;
ALTER TABLE ONLY public.post ALTER COLUMN tags_search SET STATISTICS 10000;
ALTER TABLE ONLY public.post ALTER COLUMN thumbnail SET STORAGE EXTERNAL;


ALTER TABLE public.post OWNER TO kura;

--
-- Name: COLUMN post.data; Type: COMMENT; Schema: public; Owner: kura
--

COMMENT ON COLUMN public.post.data IS 'Web-delivery media bytes: WebP transcode (long-edge cap 1600, q82) of the original. WebP chosen over AVIF for fast decode under manga infinite-scroll. Non-NULL only for status=ok images stored in PG (media_location=pg). Videos/oversize keep data NULL (media_location=hdd); the full-fidelity original always lives on the HDD backup.';


--
-- Name: COLUMN post.sha256; Type: COMMENT; Schema: public; Owner: kura
--

COMMENT ON COLUMN public.post.sha256 IS 'SHA-256 (32 raw bytes) of the ORIGINAL SOURCE image — the sole identity key for dedup / collision. NOT the hash of post.data (a re-compressed WebP transcode). Distinct from legacy checksum (SHA-1) / checksum_md5 (MD5).';


--
-- Name: COLUMN post.perceptual_hash; Type: COMMENT; Schema: public; Owner: kura
--

COMMENT ON COLUMN public.post.perceptual_hash IS '64-bit dHash (difference hash: 9x8 grayscale, adjacent-pixel sign). Scale-invariant near-duplicate detection. NOT cryptographic, NOT a DCT pHash. Candidate lookup via perceptual_hash_bands (GIN).';


--
-- Name: COLUMN post.duplicate_of; Type: COMMENT; Schema: public; Owner: kura
--

COMMENT ON COLUMN public.post.duplicate_of IS 'When status in (exact_dup, near_dup): the kept representative post this row was deduped into. data is NULL for dup rows; the original stays recoverable from the HDD backup.';


--
-- Name: COLUMN post.status; Type: COMMENT; Schema: public; Owner: kura
--

COMMENT ON COLUMN public.post.status IS 'pg-native media lifecycle: pending|ok|exact_dup|near_dup|corrupt|lost. ok = displayable representative (data present). Distinct from the legacy szurubooru tagging_status column.';


--
-- Name: COLUMN post.tags; Type: COMMENT; Schema: public; Owner: kura
--

COMMENT ON COLUMN public.post.tags IS 'Denormalized direct tag membership (primary tag-name per post_tag row). Authoritative once post_tag is dropped in Phase 6.';


--
-- Name: COLUMN post.tags_search; Type: COMMENT; Schema: public; Owner: kura
--

COMMENT ON COLUMN public.post.tags_search IS 'tags plus implication closure; GIN-indexed for tags_search @> ARRAY[...] multi-tag search.';


--
-- Name: COLUMN post.perceptual_hash_bands; Type: COMMENT; Schema: public; Owner: kura
--

COMMENT ON COLUMN public.post.perceptual_hash_bands IS 'Position-encoded bands of perceptual_hash for multi-index hashing: each element = (band_index << 16) | band_value16 (band_index 0..3, value = one 16-bit slice of the 64-bit dHash). GIN array_ops makes bands && ARRAY[...] position-AWARE (a shared element implies same index AND value), so it yields true MIH candidates; verify with Hamming distance on perceptual_hash. Backfill MUST encode this way.';


--
-- Name: COLUMN post.perceptual_distance; Type: COMMENT; Schema: public; Owner: kura
--

COMMENT ON COLUMN public.post.perceptual_distance IS 'Hamming distance between perceptual_hash and duplicate_of at cull time (audit / threshold tuning). NULL unless status=near_dup.';


--
-- Name: COLUMN post.thumbnail; Type: COMMENT; Schema: public; Owner: kura
--

COMMENT ON COLUMN public.post.thumbnail IS 'Grid/list thumbnail: WebP (~320px long edge, q75, ~12KB). Set for every displayable (status=ok) post including video (poster frame).';


--
-- Name: COLUMN post.media_location; Type: COMMENT; Schema: public; Owner: kura
--

COMMENT ON COLUMN public.post.media_location IS 'For status=ok posts, where the full-resolution body lives: pg = in post.data (AVIF); hdd = on the HDD backup (video / oversize), post.data NULL. The serving layer reads this to choose the byte source.';


--
-- Name: COLUMN post.creation_at; Type: COMMENT; Schema: public; Owner: kura
--

COMMENT ON COLUMN public.post.creation_at IS 'creation_time (naive UTC, szurubooru 由来) を timestamptz として読む口。now() と直接比べてよい。creation_time を now() と比べると session TimeZone で 暗黙キャストされ 9 時間ずれる (2026-09-02、423 件入った直後に 0 が返った)。';


--
-- Name: post_censor_review; Type: TABLE; Schema: public; Owner: kura
--

CREATE TABLE public.post_censor_review (
    id integer NOT NULL,
    post_id integer NOT NULL,
    image_sha256 bytea,
    post_version_seen integer,
    state text DEFAULT 'partial'::text NOT NULL,
    has_no_censor boolean DEFAULT false NOT NULL,
    reviewed_by integer,
    reviewed_at timestamp with time zone,
    teacher_excluded boolean DEFAULT false NOT NULL
);


ALTER TABLE public.post_censor_review OWNER TO kura;

--
-- Name: post_censor_review_id_seq; Type: SEQUENCE; Schema: public; Owner: kura
--

CREATE SEQUENCE public.post_censor_review_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.post_censor_review_id_seq OWNER TO kura;

--
-- Name: post_censor_review_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: kura
--

ALTER SEQUENCE public.post_censor_review_id_seq OWNED BY public.post_censor_review.id;


--
-- Name: post_displayable; Type: VIEW; Schema: public; Owner: kura
--

CREATE VIEW public.post_displayable AS
 SELECT id,
    "mime-type" AS mime,
    image_width,
    image_height,
    file_size,
    tags,
    tags_search,
    sha256,
    perceptual_hash,
    media_location,
    (thumbnail IS NOT NULL) AS has_thumbnail,
    (data IS NOT NULL) AS body_in_pg
   FROM public.post
  WHERE (status = 'ok'::text);


ALTER VIEW public.post_displayable OWNER TO kura;

--
-- Name: post_favorite; Type: TABLE; Schema: public; Owner: kura
--

CREATE TABLE public.post_favorite (
    post_id integer NOT NULL,
    user_id integer NOT NULL,
    "time" timestamp without time zone NOT NULL
);


ALTER TABLE public.post_favorite OWNER TO kura;

--
-- Name: post_feature; Type: TABLE; Schema: public; Owner: kura
--

CREATE TABLE public.post_feature (
    id integer NOT NULL,
    post_id integer NOT NULL,
    user_id integer,
    "time" timestamp without time zone NOT NULL
);


ALTER TABLE public.post_feature OWNER TO kura;

--
-- Name: post_feature_id_seq; Type: SEQUENCE; Schema: public; Owner: kura
--

CREATE SEQUENCE public.post_feature_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.post_feature_id_seq OWNER TO kura;

--
-- Name: post_feature_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: kura
--

ALTER SEQUENCE public.post_feature_id_seq OWNED BY public.post_feature.id;


--
-- Name: post_id_seq; Type: SEQUENCE; Schema: public; Owner: kura
--

CREATE SEQUENCE public.post_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.post_id_seq OWNER TO kura;

--
-- Name: post_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: kura
--

ALTER SEQUENCE public.post_id_seq OWNED BY public.post.id;


--
-- Name: post_listing; Type: TABLE; Schema: public; Owner: kura
--

CREATE TABLE public.post_listing (
    post_id integer NOT NULL,
    safety character varying(32),
    file_size bigint
);


ALTER TABLE public.post_listing OWNER TO kura;

--
-- Name: post_note; Type: TABLE; Schema: public; Owner: kura
--

CREATE TABLE public.post_note (
    id integer NOT NULL,
    post_id integer NOT NULL,
    text text NOT NULL,
    polygon bytea NOT NULL
);


ALTER TABLE public.post_note OWNER TO kura;

--
-- Name: post_note_id_seq; Type: SEQUENCE; Schema: public; Owner: kura
--

CREATE SEQUENCE public.post_note_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.post_note_id_seq OWNER TO kura;

--
-- Name: post_note_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: kura
--

ALTER SEQUENCE public.post_note_id_seq OWNED BY public.post_note.id;


--
-- Name: post_rendition; Type: TABLE; Schema: public; Owner: kura
--

CREATE TABLE public.post_rendition (
    post_id integer NOT NULL,
    kind text NOT NULL,
    sha256 bytea NOT NULL,
    recipe text NOT NULL,
    attached_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT post_rendition_kind_check CHECK ((kind = ANY (ARRAY['view'::text, 'thumb'::text]))),
    CONSTRAINT post_rendition_recipe_check CHECK (((length(recipe) >= 1) AND (length(recipe) <= 64)))
);


ALTER TABLE public.post_rendition OWNER TO kura;

--
-- Name: post_score; Type: TABLE; Schema: public; Owner: kura
--

CREATE TABLE public.post_score (
    post_id integer NOT NULL,
    user_id integer NOT NULL,
    "time" timestamp without time zone NOT NULL,
    score integer NOT NULL
);


ALTER TABLE public.post_score OWNER TO kura;

--
-- Name: post_tag; Type: TABLE; Schema: public; Owner: kura
--

CREATE TABLE public.post_tag (
    post_id integer NOT NULL,
    tag_id integer NOT NULL,
    locked boolean DEFAULT false NOT NULL
)
WITH (autovacuum_vacuum_scale_factor='0.02', autovacuum_analyze_scale_factor='0.01');
ALTER TABLE ONLY public.post_tag ALTER COLUMN tag_id SET STATISTICS 1000;


ALTER TABLE public.post_tag OWNER TO kura;

--
-- Name: COLUMN post_tag.locked; Type: COMMENT; Schema: public; Owner: kura
--

COMMENT ON COLUMN public.post_tag.locked IS 'true = written by an upload-origin call site (_apply_tags or an implication it triggered) and MUST NEVER be removed by complete_auto_tag''s reap side. false = eligible for reap if the current WD14 run does not reproduce it (subject to the independent post_tag_pin human-pin guard, migration 0057, which is unrelated to this column). Pre-existing rows default false.';


--
-- Name: post_tag_actor_claim; Type: VIEW; Schema: public; Owner: kura
--

CREATE VIEW public.post_tag_actor_claim AS
 SELECT c.post_id,
    c.tag_id,
    a.actor_id,
    c.claimed_at
   FROM (claim_storage.claim c
     JOIN claim_storage.actor a ON ((a.id = c.actor_key)));


ALTER VIEW public.post_tag_actor_claim OWNER TO kura;

--
-- Name: post_tag_audit; Type: TABLE; Schema: public; Owner: kura
--

CREATE TABLE public.post_tag_audit (
    id bigint NOT NULL,
    post_id integer NOT NULL,
    actor_type text NOT NULL,
    actor_id text NOT NULL,
    source text NOT NULL,
    added_tags text[] DEFAULT '{}'::text[] NOT NULL,
    removed_tags text[] DEFAULT '{}'::text[] NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    host text,
    release_id integer,
    CONSTRAINT ck_post_tag_audit_actor_id_len CHECK ((length(actor_id) <= 500)),
    CONSTRAINT ck_post_tag_audit_actor_type CHECK ((actor_type = ANY (ARRAY['release'::text, 'human'::text, 'script'::text]))),
    CONSTRAINT ck_post_tag_audit_arrays_bounded CHECK (((cardinality(added_tags) <= 500) AND (cardinality(removed_tags) <= 500))),
    CONSTRAINT ck_post_tag_audit_source CHECK (((source = ANY (ARRAY['auto_tag'::text, 'manual'::text])) OR (source ~~ 'bulk_lock:%'::text) OR (source ~~ 'bulk_remove:%'::text)))
)
WITH (autovacuum_vacuum_scale_factor='0.02', autovacuum_analyze_scale_factor='0.01');


ALTER TABLE public.post_tag_audit OWNER TO kura;

--
-- Name: TABLE post_tag_audit; Type: COMMENT; Schema: public; Owner: kura
--

COMMENT ON TABLE public.post_tag_audit IS 'One row per tagging run (machine or human) that touched a post''s tags. added_tags/removed_tags are point-in-time tag NAMES (not FKs) -- a later tag rename does not retroactively change history. Append-only: see trg_post_tag_audit_append_only. Phase A (task 4328fb0e) only writes source=auto_tag; later phases (kura-tagger-audit-and-remediation, fact fe46c5fa) add manual/backfill/remediation/implication via their own migrations.';


--
-- Name: COLUMN post_tag_audit.host; Type: COMMENT; Schema: public; Owner: kura
--

COMMENT ON COLUMN public.post_tag_audit.host IS 'Worker-reported hostname for this run (nodec/primary/trainer/...). NULL for older workers that predate this column.';


--
-- Name: COLUMN post_tag_audit.release_id; Type: COMMENT; Schema: public; Owner: kura
--

COMMENT ON COLUMN public.post_tag_audit.release_id IS 'tagger_release.id this run''s tags came from, when the actor is a release-registry-backed worker. NULL for patch-head-only workers, human edits, or scripts with no corresponding release row.';


--
-- Name: post_tag_audit_id_seq; Type: SEQUENCE; Schema: public; Owner: kura
--

CREATE SEQUENCE public.post_tag_audit_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.post_tag_audit_id_seq OWNER TO kura;

--
-- Name: post_tag_audit_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: kura
--

ALTER SEQUENCE public.post_tag_audit_id_seq OWNED BY public.post_tag_audit.id;


--
-- Name: post_tag_pin; Type: TABLE; Schema: public; Owner: kura
--

CREATE TABLE public.post_tag_pin (
    post_id integer NOT NULL,
    tag_id integer NOT NULL,
    state text NOT NULL,
    pinned_by text NOT NULL,
    audit_id bigint,
    pinned_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_post_tag_pin_state CHECK ((state = ANY (ARRAY['present'::text, 'absent'::text])))
);


ALTER TABLE public.post_tag_pin OWNER TO kura;

--
-- Name: TABLE post_tag_pin; Type: COMMENT; Schema: public; Owner: kura
--

COMMENT ON TABLE public.post_tag_pin IS 'INV-M registry (plan fe46c5fa Step C): one row per (post,tag) a human explicitly added (state=present) or removed (state=absent) via the tag-edit API. Machine writes to post_tag are blocked against these rows by 01_post_tag_pin_guard_{ins,del} unless the transaction sets kura.tag_actor=human. audit_id is a soft pointer to the post_tag_audit row recording the same human edit (not an FK, see migration 0057 docstring). A later human edit that reverses the decision flips state in place.';


--
-- Name: post_tagging_work; Type: TABLE; Schema: public; Owner: kura_reconciler
--

CREATE TABLE public.post_tagging_work (
    post_id integer NOT NULL,
    status text NOT NULL,
    lease_until timestamp with time zone,
    attempts integer DEFAULT 0 NOT NULL,
    last_error text,
    CONSTRAINT post_tagging_work_status_check CHECK ((status = ANY (ARRAY['pending'::text, 'done'::text, 'failed'::text, 'skipped'::text])))
);


ALTER TABLE public.post_tagging_work OWNER TO kura_reconciler;

--
-- Name: post_variant; Type: TABLE; Schema: public; Owner: kura
--

CREATE TABLE public.post_variant (
    id integer NOT NULL,
    post_id integer NOT NULL,
    kind text NOT NULL,
    source_sha256 bytea,
    source_version_seen integer,
    mime_type text DEFAULT 'image/webp'::text NOT NULL,
    canvas_width integer,
    canvas_height integer,
    file_size bigint,
    variant_sha256 bytea,
    data bytea,
    thumbnail bytea,
    model text,
    params jsonb,
    review_state text DEFAULT 'unreviewed'::text NOT NULL,
    region_version integer DEFAULT 1 NOT NULL,
    created_by integer,
    created_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone
);
ALTER TABLE ONLY public.post_variant ALTER COLUMN data SET STORAGE EXTERNAL;
ALTER TABLE ONLY public.post_variant ALTER COLUMN thumbnail SET STORAGE EXTERNAL;


ALTER TABLE public.post_variant OWNER TO kura;

--
-- Name: post_variant_id_seq; Type: SEQUENCE; Schema: public; Owner: kura
--

CREATE SEQUENCE public.post_variant_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.post_variant_id_seq OWNER TO kura;

--
-- Name: post_variant_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: kura
--

ALTER SEQUENCE public.post_variant_id_seq OWNED BY public.post_variant.id;


--
-- Name: rendition_object; Type: TABLE; Schema: public; Owner: kura
--

CREATE TABLE public.rendition_object (
    sha256 bytea NOT NULL,
    mime text NOT NULL,
    byte_size bigint NOT NULL,
    width integer NOT NULL,
    height integer NOT NULL,
    CONSTRAINT rendition_object_byte_size_check CHECK ((byte_size > 0)),
    CONSTRAINT rendition_object_height_check CHECK ((height > 0)),
    CONSTRAINT rendition_object_mime_check CHECK ((mime = ANY (ARRAY['image/webp'::text, 'image/jpeg'::text, 'image/png'::text, 'image/gif'::text]))),
    CONSTRAINT rendition_object_sha256_check CHECK ((octet_length(sha256) = 32)),
    CONSTRAINT rendition_object_width_check CHECK ((width > 0))
);


ALTER TABLE public.rendition_object OWNER TO kura;

--
-- Name: snapshot_id_seq; Type: SEQUENCE; Schema: public; Owner: kura
--

CREATE SEQUENCE public.snapshot_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.snapshot_id_seq OWNER TO kura;

--
-- Name: tag; Type: TABLE; Schema: public; Owner: kura
--

CREATE TABLE public.tag (
    id integer NOT NULL,
    category_id integer NOT NULL,
    creation_time timestamp without time zone NOT NULL,
    last_edit_time timestamp without time zone,
    description text,
    version integer NOT NULL,
    post_count integer DEFAULT 0 NOT NULL
);


ALTER TABLE public.tag OWNER TO kura;

--
-- Name: tag_category; Type: TABLE; Schema: public; Owner: kura
--

CREATE TABLE public.tag_category (
    id integer CONSTRAINT tag_category_id_not_null1 NOT NULL,
    name character varying(32) NOT NULL,
    color character varying(32) NOT NULL,
    "default" boolean NOT NULL,
    version integer NOT NULL,
    "order" integer NOT NULL
);


ALTER TABLE public.tag_category OWNER TO kura;

--
-- Name: tag_category_id_seq; Type: SEQUENCE; Schema: public; Owner: kura
--

CREATE SEQUENCE public.tag_category_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.tag_category_id_seq OWNER TO kura;

--
-- Name: tag_category_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: kura
--

ALTER SEQUENCE public.tag_category_id_seq OWNED BY public.tag_category.id;


--
-- Name: tag_id_seq; Type: SEQUENCE; Schema: public; Owner: kura
--

CREATE SEQUENCE public.tag_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.tag_id_seq OWNER TO kura;

--
-- Name: tag_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: kura
--

ALTER SEQUENCE public.tag_id_seq OWNED BY public.tag.id;


--
-- Name: tag_implication; Type: TABLE; Schema: public; Owner: kura
--

CREATE TABLE public.tag_implication (
    parent_id integer NOT NULL,
    child_id integer NOT NULL
);


ALTER TABLE public.tag_implication OWNER TO kura;

--
-- Name: tag_link; Type: TABLE; Schema: public; Owner: kura
--

CREATE TABLE public.tag_link (
    id bigint NOT NULL,
    tag_id integer NOT NULL,
    platform text NOT NULL,
    url text NOT NULL,
    handle text,
    uncensored smallint,
    verified boolean DEFAULT false NOT NULL,
    note text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    pool_id integer,
    CONSTRAINT ck_tag_link_uncensored CHECK ((uncensored = ANY (ARRAY[0, 1])))
);


ALTER TABLE public.tag_link OWNER TO kura;

--
-- Name: CONSTRAINT ck_tag_link_uncensored ON tag_link; Type: COMMENT; Schema: public; Owner: kura
--

COMMENT ON CONSTRAINT ck_tag_link_uncensored ON public.tag_link IS 'NULL IS DELIBERATE. uncensored is tri-state: 1 = uncensored, 0 = censored, NULL = unknown (api/routers/tags.py:_validate_uncensored accepts all three). NULL passing this CHECK is the intent, not the NULL-hole bug that IS NOT DISTINCT FROM exists to close elsewhere. Do not tighten it: every link whose status is honestly unknown would be rejected.';


--
-- Name: tag_link_id_seq; Type: SEQUENCE; Schema: public; Owner: kura
--

CREATE SEQUENCE public.tag_link_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.tag_link_id_seq OWNER TO kura;

--
-- Name: tag_link_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: kura
--

ALTER SEQUENCE public.tag_link_id_seq OWNED BY public.tag_link.id;


--
-- Name: tag_localization; Type: TABLE; Schema: public; Owner: kura
--

CREATE TABLE public.tag_localization (
    tag_id integer NOT NULL,
    locale character varying(35) NOT NULL,
    name character varying(128) NOT NULL,
    CONSTRAINT tag_localization_locale CHECK (((locale)::text ~ '^[a-z]{2,8}(-[a-z0-9]{1,8})*$'::text)),
    CONSTRAINT tag_localization_name CHECK ((((name)::text = btrim((name)::text)) AND (length((name)::text) > 0) AND ((name)::text !~ '[[:cntrl:]]'::text)))
);


ALTER TABLE public.tag_localization OWNER TO kura;

--
-- Name: tag_name; Type: TABLE; Schema: public; Owner: kura
--

CREATE TABLE public.tag_name (
    tag_name_id integer NOT NULL,
    tag_id integer NOT NULL,
    name character varying(128) NOT NULL,
    ord integer NOT NULL
);


ALTER TABLE public.tag_name OWNER TO kura;

--
-- Name: tag_name_tag_name_id_seq; Type: SEQUENCE; Schema: public; Owner: kura
--

CREATE SEQUENCE public.tag_name_tag_name_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.tag_name_tag_name_id_seq OWNER TO kura;

--
-- Name: tag_name_tag_name_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: kura
--

ALTER SEQUENCE public.tag_name_tag_name_id_seq OWNED BY public.tag_name.tag_name_id;


--
-- Name: tag_pin_inventory; Type: TABLE; Schema: public; Owner: kura
--

CREATE TABLE public.tag_pin_inventory (
    id bigint NOT NULL,
    post_id integer NOT NULL,
    tag text NOT NULL,
    source text NOT NULL,
    evidence text,
    snapshot_at timestamp with time zone DEFAULT now() NOT NULL,
    coverage_verified boolean DEFAULT false NOT NULL,
    CONSTRAINT ck_tag_pin_inventory_source CHECK ((source = ANY (ARRAY['judged_labels'::text, 'db_update_meta'::text, 'reconcile_flag'::text])))
);


ALTER TABLE public.tag_pin_inventory OWNER TO kura;

--
-- Name: TABLE tag_pin_inventory; Type: COMMENT; Schema: public; Owner: kura
--

COMMENT ON TABLE public.tag_pin_inventory IS 'Legacy (pre-0057) provenance bootstrap: reconciles historical (post,tag) pairs found in an implemented source into human-confirmed (coverage_verified=true). A pair NOT present in this table has not been reconciled by any implemented source -- absence is not evidence either way, and must never be read as human-confirmed. coverage_verified=false is reserved for a future source that positively flags a pair as checked-but-unconfirmed (e.g. Step D''s unimplemented source (c), reconcile_flag) -- as of migration 0067, no implemented source produces that value; only source (a) (judged_labels) is implemented and it only ever writes coverage_verified=true. post_tag_pin (0057) is the separate FORWARD-LOOKING enforcement table for pins created after 2026-07-15 -- this table does not feed into it automatically; a promotion step (not yet built) would consume verified rows here into post_tag_pin explicitly.';


--
-- Name: tag_pin_inventory_id_seq; Type: SEQUENCE; Schema: public; Owner: kura
--

CREATE SEQUENCE public.tag_pin_inventory_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.tag_pin_inventory_id_seq OWNER TO kura;

--
-- Name: tag_pin_inventory_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: kura
--

ALTER SEQUENCE public.tag_pin_inventory_id_seq OWNED BY public.tag_pin_inventory.id;


--
-- Name: tag_quarantine; Type: TABLE; Schema: public; Owner: kura
--

CREATE TABLE public.tag_quarantine (
    id bigint NOT NULL,
    tag text NOT NULL,
    reason text NOT NULL,
    since timestamp with time zone DEFAULT now() NOT NULL,
    evidence_pools integer[] DEFAULT '{}'::integer[] NOT NULL,
    active boolean DEFAULT true NOT NULL,
    tag_id integer NOT NULL
);


ALTER TABLE public.tag_quarantine OWNER TO kura;

--
-- Name: TABLE tag_quarantine; Type: COMMENT; Schema: public; Owner: kura
--

COMMENT ON TABLE public.tag_quarantine IS 'Tag suppression set as DATA (rev2 decision), replacing three duplicated QUARANTINE_TAGS code constants (primary tagger/infer.py, desk kura-tagger-worker/tagger/infer.py, nodec kura_autotag_worker.py). NO WORKER READS THIS TABLE YET as of migration 0068 -- the three code constants remain the live suppression source. A future revision (plan kura-tagger-audit-and-remediation rev5, not yet drafted) will make workers fetch from here and retire the code constants; that is a live-behavior change to three production taggers and deliberately was not bundled with this additive schema migration. active=false is reserved for un-quarantining a tag once its model head is fixed, without losing the historical reason/evidence_pools row.';


--
-- Name: tag_quarantine_id_seq; Type: SEQUENCE; Schema: public; Owner: kura
--

CREATE SEQUENCE public.tag_quarantine_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.tag_quarantine_id_seq OWNER TO kura;

--
-- Name: tag_quarantine_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: kura
--

ALTER SEQUENCE public.tag_quarantine_id_seq OWNED BY public.tag_quarantine.id;


--
-- Name: tag_relation; Type: TABLE; Schema: public; Owner: kura
--

CREATE TABLE public.tag_relation (
    a_tag_id integer NOT NULL,
    b_tag_id integer NOT NULL,
    kind text NOT NULL,
    note text,
    creation_time timestamp without time zone DEFAULT now() NOT NULL,
    CONSTRAINT tag_relation_check CHECK ((a_tag_id < b_tag_id)),
    CONSTRAINT tag_relation_kind_check CHECK ((kind = ANY (ARRAY['related'::text, 'exclusive'::text])))
);


ALTER TABLE public.tag_relation OWNER TO kura;

--
-- Name: tag_suggestion; Type: TABLE; Schema: public; Owner: kura
--

CREATE TABLE public.tag_suggestion (
    parent_id integer NOT NULL,
    child_id integer NOT NULL
);


ALTER TABLE public.tag_suggestion OWNER TO kura;

--
-- Name: tagger_allowed_executor; Type: TABLE; Schema: public; Owner: kura
--

CREATE TABLE public.tagger_allowed_executor (
    db_role text NOT NULL,
    scope text NOT NULL,
    added_at timestamp with time zone DEFAULT now() NOT NULL,
    added_by text
);


ALTER TABLE public.tagger_allowed_executor OWNER TO kura;

--
-- Name: TABLE tagger_allowed_executor; Type: COMMENT; Schema: public; Owner: kura
--

COMMENT ON TABLE public.tagger_allowed_executor IS 'db_role must equal session_user (the real login role -- immune to
        SECURITY DEFINER''s identity collapse, where current_user inside such
        a function is always the function OWNER, never the caller; round4
        BLOCKING finding). scope is either a fixed name (backlog-sweep,
        reconciler-control, worker-heartbeat, release-build,
        release-publish) or lifecycle:<tag> (per-tag, seeded by
        tagger/transitions.py::ensure_tag_executor_allowed on first use by
        an already-allowed general role -- this does NOT let an arbitrary
        caller mint new scopes, since inserting the allowlist row itself
        goes through the same kura_reconciler-owned governed path in a
        later migration once release-build/publish functions exist).';


--
-- Name: tagger_artifact; Type: TABLE; Schema: public; Owner: kura
--

CREATE TABLE public.tagger_artifact (
    content_sha256 text NOT NULL,
    byte_size bigint NOT NULL,
    kind text,
    kind_checked_at timestamp with time zone,
    CONSTRAINT tagger_artifact_byte_size_check CHECK ((byte_size >= 0)),
    CONSTRAINT tagger_artifact_content_sha256_check CHECK ((content_sha256 ~ '^[0-9a-f]{64}$'::text)),
    CONSTRAINT tagger_artifact_kind_check CHECK (((kind IS NULL) OR (kind = ANY (ARRAY['content_head_bundle'::text, 'probe_safetensors'::text, 'measurement_json'::text, 'release_model'::text, 'opaque'::text])))),
    CONSTRAINT tagger_artifact_kind_needs_bytes CHECK (((kind IS NULL) OR (byte_size > 0))),
    CONSTRAINT tagger_artifact_kind_stamped CHECK (((kind IS NULL) = (kind_checked_at IS NULL)))
);


ALTER TABLE public.tagger_artifact OWNER TO kura;

--
-- Name: tagger_auto_exclude_pool; Type: TABLE; Schema: public; Owner: kura
--

CREATE TABLE public.tagger_auto_exclude_pool (
    pool_id integer NOT NULL,
    excluded boolean DEFAULT true NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_by text
);


ALTER TABLE public.tagger_auto_exclude_pool OWNER TO kura;

--
-- Name: tagger_candidate_disposition; Type: TABLE; Schema: public; Owner: kura
--

CREATE TABLE public.tagger_candidate_disposition (
    seq bigint NOT NULL,
    tag text NOT NULL,
    tag_id integer NOT NULL,
    disposition text NOT NULL,
    reason_class text NOT NULL,
    reason text NOT NULL,
    decided_by text NOT NULL,
    actor_type text NOT NULL,
    decided_at timestamp with time zone DEFAULT now() NOT NULL,
    valid_until timestamp with time zone,
    release_condition jsonb,
    cond_version integer,
    supersedes_seq bigint,
    CONSTRAINT ck_tcd_cond_versioned CHECK (((release_condition IS NULL) = (cond_version IS NULL))),
    CONSTRAINT ck_tcd_defer_needs_expiry CHECK (((disposition <> 'defer'::text) OR (valid_until IS NOT NULL) OR (release_condition IS NOT NULL))),
    CONSTRAINT tagger_candidate_disposition_actor_type_check CHECK ((actor_type = ANY (ARRAY['human'::text, 'policy'::text, 'automation'::text]))),
    CONSTRAINT tagger_candidate_disposition_decided_by_check CHECK ((length(btrim(decided_by)) > 0)),
    CONSTRAINT tagger_candidate_disposition_disposition_check CHECK ((disposition = ANY (ARRAY['proceed'::text, 'defer'::text, 'dismiss'::text, 'block'::text, 'supersede'::text]))),
    CONSTRAINT tagger_candidate_disposition_reason_check CHECK ((length(btrim(reason)) > 0)),
    CONSTRAINT tagger_candidate_disposition_reason_class_check CHECK ((length(btrim(reason_class)) > 0))
);


ALTER TABLE public.tagger_candidate_disposition OWNER TO kura;

--
-- Name: TABLE tagger_candidate_disposition; Type: COMMENT; Schema: public; Owner: kura
--

COMMENT ON TABLE public.tagger_candidate_disposition IS 'issue #55 P1 disposition ledger (append-only; current = per-tag latest seq). defer=not-now (expiry required), dismiss=needs a new decision row to overturn, block=machine-diagnosed structural obstacle, supersede=void the prior row. Writes only via tagger_governed_record/expire_candidate_disposition.';


--
-- Name: tagger_candidate_disposition_current; Type: VIEW; Schema: public; Owner: kura
--

CREATE VIEW public.tagger_candidate_disposition_current AS
 SELECT DISTINCT ON (tag_id) seq,
    tag,
    tag_id,
    disposition,
    reason_class,
    reason,
    decided_by,
    actor_type,
    decided_at,
    valid_until,
    release_condition,
    cond_version,
    supersedes_seq
   FROM public.tagger_candidate_disposition
  ORDER BY tag_id, seq DESC;


ALTER VIEW public.tagger_candidate_disposition_current OWNER TO kura;

--
-- Name: tagger_candidate_disposition_seq_seq; Type: SEQUENCE; Schema: public; Owner: kura
--

CREATE SEQUENCE public.tagger_candidate_disposition_seq_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.tagger_candidate_disposition_seq_seq OWNER TO kura;

--
-- Name: tagger_candidate_disposition_seq_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: kura
--

ALTER SEQUENCE public.tagger_candidate_disposition_seq_seq OWNED BY public.tagger_candidate_disposition.seq;


--
-- Name: tagger_candidate_event; Type: TABLE; Schema: public; Owner: kura
--

CREATE TABLE public.tagger_candidate_event (
    seq bigint NOT NULL,
    tag text NOT NULL,
    tag_id integer NOT NULL,
    kind text NOT NULL,
    hold_reason_class text,
    detail text NOT NULL,
    occurred_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_tce_held_has_class CHECK (((kind = 'held'::text) = (hold_reason_class IS NOT NULL))),
    CONSTRAINT tagger_candidate_event_detail_check CHECK ((length(btrim(detail)) > 0)),
    CONSTRAINT tagger_candidate_event_kind_check CHECK ((kind = ANY (ARRAY['held'::text, 'released'::text, 'dismissed'::text, 'reopened_headless'::text, 'reopened_overdetection'::text])))
);


ALTER TABLE public.tagger_candidate_event OWNER TO kura;

--
-- Name: TABLE tagger_candidate_event; Type: COMMENT; Schema: public; Owner: kura
--

COMMENT ON TABLE public.tagger_candidate_event IS 'candidate 行の status 遷移の append-only 台帳 (issue #55 の T)。0155 以前の遷移履歴は head_candidate.reason 内の [released:…] 等の凍結 legacy テキストにのみ残る (時刻情報が無く event 化しない)';


--
-- Name: tagger_candidate_event_seq_seq; Type: SEQUENCE; Schema: public; Owner: kura
--

CREATE SEQUENCE public.tagger_candidate_event_seq_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.tagger_candidate_event_seq_seq OWNER TO kura;

--
-- Name: tagger_candidate_event_seq_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: kura
--

ALTER SEQUENCE public.tagger_candidate_event_seq_seq OWNED BY public.tagger_candidate_event.seq;


--
-- Name: tagger_config; Type: TABLE; Schema: public; Owner: kura
--

CREATE TABLE public.tagger_config (
    key text NOT NULL,
    value numeric NOT NULL,
    description text,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_by text
);


ALTER TABLE public.tagger_config OWNER TO kura;

--
-- Name: tagger_head_policy; Type: TABLE; Schema: public; Owner: kura
--

CREATE TABLE public.tagger_head_policy (
    tag text NOT NULL,
    patch_version integer NOT NULL,
    content_sha256 text NOT NULL,
    operating_point text NOT NULL,
    threshold double precision NOT NULL,
    measured_recall double precision NOT NULL,
    measured_fpr double precision NOT NULL,
    apply_mode text NOT NULL,
    gate_tags text[] DEFAULT '{}'::text[] NOT NULL,
    registration_id bigint NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    tag_id integer NOT NULL,
    CONSTRAINT tagger_head_policy_apply_mode_check CHECK ((apply_mode = ANY (ARRAY['add_only'::text, 'replace'::text]))),
    CONSTRAINT tagger_head_policy_measured_fpr_check CHECK (((measured_fpr >= (0.0)::double precision) AND (measured_fpr <= (1.0)::double precision))),
    CONSTRAINT tagger_head_policy_measured_recall_check CHECK (((measured_recall >= (0.0)::double precision) AND (measured_recall <= (1.0)::double precision))),
    CONSTRAINT tagger_head_policy_threshold_check CHECK (((threshold > (0.0)::double precision) AND (threshold <= (1.0)::double precision)))
);


ALTER TABLE public.tagger_head_policy OWNER TO kura;

--
-- Name: tagger_patch; Type: TABLE; Schema: public; Owner: kura
--

CREATE TABLE public.tagger_patch (
    id integer NOT NULL,
    tag text NOT NULL,
    version integer NOT NULL,
    status text NOT NULL,
    patch_sha256 text NOT NULL,
    why text,
    why_rejected text,
    metrics jsonb,
    base_repo text,
    base_revision text,
    logit_index integer,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    created_by text,
    gt jsonb,
    head_type text DEFAULT 'wd14_logit_patch'::text NOT NULL,
    first_release_id integer,
    model_data bytea,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    tag_id integer NOT NULL,
    CONSTRAINT ck_tagger_patch_head_type CHECK ((head_type = ANY (ARRAY['wd14_logit_patch'::text, 'content_head'::text, 'anatomy_multihead'::text]))),
    CONSTRAINT ck_tagger_patch_status CHECK ((status = ANY (ARRAY['approved'::text, 'rejected'::text, 'superseded'::text])))
);


ALTER TABLE public.tagger_patch OWNER TO kura;

--
-- Name: TABLE tagger_patch; Type: COMMENT; Schema: public; Owner: kura
--

COMMENT ON TABLE public.tagger_patch IS 'Mirrors tagger/patches/<tag>/v<N>.json (git repo is SoT for why/why_rejected prose); one row per patch, approved+rejected both kept (INV-Q).';


--
-- Name: COLUMN tagger_patch.gt; Type: COMMENT; Schema: public; Owner: kura
--

COMMENT ON COLUMN public.tagger_patch.gt IS 'ground truth {pools,n_train,n_val}. metrics へ畳んでいた band-aid を 0070 で専用列へ昇格。';


--
-- Name: COLUMN tagger_patch.head_type; Type: COMMENT; Schema: public; Owner: kura
--

COMMENT ON COLUMN public.tagger_patch.head_type IS 'wd14_logit_patch | content_head (chastity_cage/belt 等 WD14語彙外) | anatomy_multihead (skin-state)。';


--
-- Name: COLUMN tagger_patch.first_release_id; Type: COMMENT; Schema: public; Owner: kura
--

COMMENT ON COLUMN public.tagger_patch.first_release_id IS 'The tagger_release this patch was first baked into (wd14_logit_patch
        heads only; content_head patches are distributed out-of-band and stay
        NULL here by design, see head-artifact distribution, Step 19).';


--
-- Name: COLUMN tagger_patch.model_data; Type: COMMENT; Schema: public; Owner: kura
--

COMMENT ON COLUMN public.tagger_patch.model_data IS 'Content-head checkpoint bytes (head_type=content_head rows only --
        wd14_logit_patch rows have no standalone model file and stay NULL
        here). Populated opportunistically by sync_patch_registry.py when
        the local checkpoint exists on primary. No fetch client consumes this
        yet (Step 19 follow-up) -- storage only, for now.';


--
-- Name: COLUMN tagger_patch.updated_at; Type: COMMENT; Schema: public; Owner: kura
--

COMMENT ON COLUMN public.tagger_patch.updated_at IS 'Refreshed on every governed upsert (tagger_governed_upsert_patch),
        unlike created_at which is set once at first INSERT and never
        touched again. This is the real "last touched" signal for the
        dashboard; created_at answers a different question ("when did
        this tag/version first exist").';


--
-- Name: tagger_current_head_policy; Type: VIEW; Schema: public; Owner: kura
--

CREATE VIEW public.tagger_current_head_policy AS
 SELECT DISTINCT ON (p.tag_id) p.tag_id,
    p.tag,
    p.patch_version,
    p.content_sha256,
    p.operating_point,
    p.threshold,
    p.measured_recall,
    p.measured_fpr,
    p.apply_mode,
    p.gate_tags,
    p.registration_id,
    p.created_at,
    a.kind AS artifact_kind
   FROM ((public.tagger_head_policy p
     JOIN public.tagger_patch t ON (((t.tag_id = p.tag_id) AND (t.version = p.patch_version))))
     LEFT JOIN public.tagger_artifact a ON ((a.content_sha256 = p.content_sha256)))
  WHERE (t.status = 'approved'::text)
  ORDER BY p.tag_id, p.patch_version DESC;


ALTER VIEW public.tagger_current_head_policy OWNER TO kura;

--
-- Name: tagger_release; Type: TABLE; Schema: public; Owner: kura
--

CREATE TABLE public.tagger_release (
    id integer NOT NULL,
    commit_sha text NOT NULL,
    base_model_repo text NOT NULL,
    base_model_revision text NOT NULL,
    patch_set_digest text NOT NULL,
    model_sha256 text NOT NULL,
    model_data bytea NOT NULL,
    selected_tags_csv bytea NOT NULL,
    n_stock integer NOT NULL,
    n_out integer NOT NULL,
    patched jsonb NOT NULL,
    lockfile text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    created_by text,
    signed_digest text
);


ALTER TABLE public.tagger_release OWNER TO kura;

--
-- Name: TABLE tagger_release; Type: COMMENT; Schema: public; Owner: kura
--

COMMENT ON TABLE public.tagger_release IS 'One row per built+freeze-validated wd14 merge. Immutable; identified by (commit_sha, patch_set_digest, model_sha256). model_data/selected_tags_csv are the pulled BYTEA payload.';


--
-- Name: COLUMN tagger_release.signed_digest; Type: COMMENT; Schema: public; Owner: kura
--

COMMENT ON COLUMN public.tagger_release.signed_digest IS 'sha256 of (commit_sha, patch_set_digest, model_sha256) concatenated,
        computed at publish time -- a lightweight integrity signature so a
        release row cannot be silently row-swapped without the digest
        visibly no longer matching. NULL for releases published before this
        column existed (id=2); backfilled opportunistically, never enforced
        retroactively.';


--
-- Name: tagger_release_active; Type: TABLE; Schema: public; Owner: kura
--

CREATE TABLE public.tagger_release_active (
    id integer NOT NULL,
    release_id integer,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_by text,
    generation bigint DEFAULT 1 NOT NULL,
    CONSTRAINT tagger_release_active_id_check CHECK ((id = 1))
);


ALTER TABLE public.tagger_release_active OWNER TO kura;

--
-- Name: TABLE tagger_release_active; Type: COMMENT; Schema: public; Owner: kura
--

COMMENT ON TABLE public.tagger_release_active IS 'Singleton (id=1) CAS pointer to the current tagger_release. Only ever updated in the same transaction as the release insert it names -- no partial release is ever active (INV-R/atomicity).';


--
-- Name: COLUMN tagger_release_active.generation; Type: COMMENT; Schema: public; Owner: kura
--

COMMENT ON COLUMN public.tagger_release_active.generation IS 'Optimistic-lock counter, incremented every time the active pointer
        flips. A sweep/claim resolves (release_id, generation) ONCE at the
        start of its run and re-checks generation before committing -- if it
        changed mid-run, the operation aborts and re-plans instead of acting
        on a target that is no longer active (round3-adjacent TOCTOU gap).';


--
-- Name: tagger_deployable_head; Type: VIEW; Schema: public; Owner: kura
--

CREATE VIEW public.tagger_deployable_head AS
 SELECT c.tag_id,
    c.tag,
    'content_head'::text AS lane
   FROM public.tagger_current_head_policy c
  WHERE (c.artifact_kind = 'content_head_bundle'::text)
UNION
 SELECT DISTINCT t.tag_id,
    t.tag,
    'wd14_logit_patch'::text AS lane
   FROM ((public.tagger_patch t
     JOIN public.tagger_release_active ra ON (true))
     JOIN public.tagger_release r ON ((r.id = ra.release_id)))
  WHERE ((t.status = 'approved'::text) AND (t.head_type = 'wd14_logit_patch'::text) AND (r.patched ? t.tag));


ALTER VIEW public.tagger_deployable_head OWNER TO kura;

--
-- Name: tagger_expected_worker; Type: TABLE; Schema: public; Owner: kura
--

CREATE TABLE public.tagger_expected_worker (
    host text NOT NULL,
    description text NOT NULL,
    added_at timestamp with time zone DEFAULT now() NOT NULL,
    added_by text,
    profile text,
    expected_provider text,
    opportunistic boolean DEFAULT false NOT NULL
);


ALTER TABLE public.tagger_expected_worker OWNER TO kura;

--
-- Name: TABLE tagger_expected_worker; Type: COMMENT; Schema: public; Owner: kura
--

COMMENT ON TABLE public.tagger_expected_worker IS 'Explicit, manually-maintained registry of hosts that are supposed to run a tagger worker continuously. Joined against tagger_node_state in the dashboard to positively flag a registered-but-never-reported or registered-but-stale host, instead of silently omitting it -- this is the direct fix for the 2026-06-20..2026-07-20 incident where primary''s kura-tagger.service was fully built but never enabled and nobody noticed for a month. Membership is deliberately manual, not auto-discovered from node_state reports (auto-discovery cannot flag a host that has NEVER reported, which is exactly the failure mode this table exists to catch).';


--
-- Name: COLUMN tagger_expected_worker.profile; Type: COMMENT; Schema: public; Owner: kura
--

COMMENT ON COLUMN public.tagger_expected_worker.profile IS 'Expected worker profile for this host (e.g. full, wd14,
        training-only, canonical). Checked against
        tagger_worker_heartbeat.profile at claim time (api/routers/
        tagging.py::claim) -- a worker reporting a profile that does not
        match its own host''s expected row is refused the claim (409).
        NULL until Step 21 backfills each fleet host explicitly.';


--
-- Name: COLUMN tagger_expected_worker.expected_provider; Type: COMMENT; Schema: public; Owner: kura
--

COMMENT ON COLUMN public.tagger_expected_worker.expected_provider IS '期待する onnxruntime execution provider (例 CPUExecutionProvider / CUDAExecutionProvider / ROCMExecutionProvider)。tagger_fleet.degraded_reason は この値と実報告の不一致だけを degraded にする。NULL = 期待未設定 = provider に ついて意見を持たない (そのホストの provider 退行は検出されない — 恒久的な 状態にしないこと)。migration 0120';


--
-- Name: COLUMN tagger_expected_worker.opportunistic; Type: COMMENT; Schema: public; Owner: kura
--

COMMENT ON COLUMN public.tagger_expected_worker.opportunistic IS 'true = no uptime SLA (e.g. a laptop that comes and goes -- see CLAUDE.md node-role table). health.py excludes a merely-stale (no degraded_reason) opportunistic host from paging; a genuine degraded_reason (provider mismatch etc) still pages. migration 0126';


--
-- Name: tagger_fencing_seq; Type: SEQUENCE; Schema: public; Owner: kura
--

CREATE SEQUENCE public.tagger_fencing_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.tagger_fencing_seq OWNER TO kura;

--
-- Name: tagger_node_state; Type: TABLE; Schema: public; Owner: kura
--

CREATE TABLE public.tagger_node_state (
    host text NOT NULL,
    release_id integer,
    verified boolean DEFAULT false NOT NULL,
    detail text,
    reported_at timestamp with time zone DEFAULT now() NOT NULL
);


ALTER TABLE public.tagger_node_state OWNER TO kura;

--
-- Name: TABLE tagger_node_state; Type: COMMENT; Schema: public; Owner: kura
--

COMMENT ON TABLE public.tagger_node_state IS 'Per-host last-reported release + verification result. Plain SELECT reveals fleet drift (replaces the ad-hoc ssh audit that caught the 2026-07-14 incident).';


--
-- Name: tagger_worker_heartbeat; Type: TABLE; Schema: public; Owner: kura
--

CREATE TABLE public.tagger_worker_heartbeat (
    worker_id text NOT NULL,
    host text NOT NULL,
    profile text NOT NULL,
    generation bigint NOT NULL,
    release_id integer,
    provider text NOT NULL,
    model_sha256 text,
    reported_at timestamp with time zone DEFAULT now() NOT NULL
);


ALTER TABLE public.tagger_worker_heartbeat OWNER TO kura;

--
-- Name: TABLE tagger_worker_heartbeat; Type: COMMENT; Schema: public; Owner: kura
--

COMMENT ON TABLE public.tagger_worker_heartbeat IS 'What a worker actually reports about itself (profile/generation/
        release/provider/model hash), checked at claim time against the
        active release and the expected profile for its host
        (tagger_expected_worker) -- an incompatible worker is refused the
        claim (409), not silently allowed to tag with a stale/wrong model.
        This is the direct fix for nodec/desk silently running CPU while
        reporting GPU capability was assumed: it now has to report the
        provider it actually initialized, not what its unit name claims.';


--
-- Name: tagger_fleet; Type: VIEW; Schema: public; Owner: kura
--

CREATE VIEW public.tagger_fleet AS
 SELECT ew.host,
    ew.description AS expected_description,
    hb.worker_id,
    hb.profile AS reported_profile,
    hb.generation AS reported_generation,
    hb.release_id AS reported_release_id,
    hb.provider AS reported_provider,
    hb.model_sha256 AS reported_model_sha256,
    hb.reported_at AS heartbeat_at,
    ns.release_id AS node_state_release_id,
    ns.verified AS node_state_verified,
    ns.reported_at AS node_state_at,
    ( SELECT tagger_release_active.release_id
           FROM public.tagger_release_active
          WHERE (tagger_release_active.id = 1)) AS active_release_id,
        CASE
            WHEN (hb.worker_id IS NULL) THEN 'missing'::text
            WHEN (hb.reported_at < (now() - '01:00:00'::interval)) THEN 'stale'::text
            WHEN (hb.release_id IS DISTINCT FROM ( SELECT tagger_release_active.release_id
               FROM public.tagger_release_active
              WHERE (tagger_release_active.id = 1))) THEN 'off-release'::text
            ELSE 'active'::text
        END AS liveness,
        CASE
            WHEN (hb.worker_id IS NULL) THEN NULL::text
            WHEN (ew.expected_provider IS NULL) THEN NULL::text
            WHEN (hb.provider IS DISTINCT FROM ew.expected_provider) THEN (((('provider mismatch: expected '::text || ew.expected_provider) || ' but reporting '::text) || COALESCE(hb.provider, '(none)'::text)) || ' -- accelerator regression or an out-of-date expectation'::text)
            ELSE NULL::text
        END AS degraded_reason,
    ew.expected_provider,
    ew.opportunistic
   FROM ((public.tagger_expected_worker ew
     LEFT JOIN public.tagger_worker_heartbeat hb ON (((hb.host = ew.host) AND (hb.worker_id !~~ '%smoketest%'::text))))
     LEFT JOIN public.tagger_node_state ns ON ((ns.host = ew.host)));


ALTER VIEW public.tagger_fleet OWNER TO kura;

--
-- Name: VIEW tagger_fleet; Type: COMMENT; Schema: public; Owner: kura
--

COMMENT ON VIEW public.tagger_fleet IS 'Read-only projection over tagger_expected_worker (role SoT, 0071/0074)
        joined against tagger_worker_heartbeat (0077, what a worker actually
        reports about itself) and tagger_node_state (0052, legacy last-beat).
        degraded_reason flags e.g. a full-profile worker reporting a CPU
        provider -- the exact silent-regression pattern this session found on
        nodec and desk 15 days after their GPU deployment plan completed with
        nobody re-checking. Heartbeats whose worker_id contains "smoketest"
        are excluded (0119): they are test fixtures, never real workers, and
        their frozen reported_at otherwise pollutes liveness=stale forever.';


--
-- Name: tagger_gt_label; Type: TABLE; Schema: public; Owner: kura
--

CREATE TABLE public.tagger_gt_label (
    id bigint NOT NULL,
    tag text NOT NULL,
    post_id integer NOT NULL,
    verdict text NOT NULL,
    source_pool_id integer,
    derived_from text NOT NULL,
    verified_at timestamp with time zone DEFAULT now() NOT NULL,
    tag_id integer NOT NULL,
    CONSTRAINT tagger_gt_label_derived_from_check CHECK ((derived_from = ANY (ARRAY['human_correction'::text, 'implicit_review'::text, 'pool_seed'::text]))),
    CONSTRAINT tagger_gt_label_verdict_check CHECK ((verdict = ANY (ARRAY['positive'::text, 'negative'::text])))
);


ALTER TABLE public.tagger_gt_label OWNER TO kura;

--
-- Name: TABLE tagger_gt_label; Type: COMMENT; Schema: public; Owner: kura
--

COMMENT ON TABLE public.tagger_gt_label IS 'Derived training labels -- NOT hand-entered. See tagger/gt_sync.py.
        human_correction rows come from post_tag_audit (actor_type=human)
        add/remove events, the strongest signal. implicit_review rows come
        from posts in a verify_status=verified pool that were never
        touched for that tag -- the human saw them and did not object, so
        the current tag state (present/absent) is taken as correct. This
        table is a read model for training data selection; post_tag_audit
        remains the canonical record of what actually happened.';


--
-- Name: tagger_gt_label_id_seq; Type: SEQUENCE; Schema: public; Owner: kura
--

CREATE SEQUENCE public.tagger_gt_label_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.tagger_gt_label_id_seq OWNER TO kura;

--
-- Name: tagger_gt_label_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: kura
--

ALTER SEQUENCE public.tagger_gt_label_id_seq OWNED BY public.tagger_gt_label.id;


--
-- Name: tagger_head_candidate; Type: TABLE; Schema: public; Owner: kura
--

CREATE TABLE public.tagger_head_candidate (
    id bigint NOT NULL,
    tag text NOT NULL,
    source_pool_id integer,
    reason text NOT NULL,
    status text DEFAULT 'candidate'::text NOT NULL,
    added_at timestamp with time zone DEFAULT now() NOT NULL,
    added_by text,
    promoted_patch_id integer,
    hold_reason_class text,
    hold_evidence jsonb,
    tag_id integer NOT NULL,
    CONSTRAINT tagger_head_candidate_held_mutual_check CHECK (((status = 'held'::text) = (hold_reason_class IS NOT NULL))),
    CONSTRAINT tagger_head_candidate_hold_reason_class_check CHECK (((hold_reason_class IS NULL) OR (hold_reason_class = ANY (ARRAY['prevalence_ceiling'::text, 'label_definition_conflict'::text, 'inert_serving_path'::text, 'vocab_absent_routing'::text, 'insufficient_gt'::text, 'migration_quarantine'::text])))),
    CONSTRAINT tagger_head_candidate_status_check CHECK ((status = ANY (ARRAY['candidate'::text, 'promoted'::text, 'dismissed'::text, 'held'::text])))
);


ALTER TABLE public.tagger_head_candidate OWNER TO kura;

--
-- Name: TABLE tagger_head_candidate; Type: COMMENT; Schema: public; Owner: kura
--

COMMENT ON TABLE public.tagger_head_candidate IS 'Concepts flagged during verify-pool review as worth a dedicated tagger head (wd14_logit_patch or content_head), tracked so the idea is not lost between the review and someone actually building it. Populated automatically by PATCH /pools/{id}/verify-status when the caller includes head_candidate in the body alongside status=verified -- not a separate manual step. status promoted once a matching row lands in tagger_patch; dismissed if judged not worth it.';


--
-- Name: COLUMN tagger_head_candidate.reason; Type: COMMENT; Schema: public; Owner: kura
--

COMMENT ON COLUMN public.tagger_head_candidate.reason IS '初回検出の理由 (検出時に一度だけ書かれる)。0155 で遷移の追記を廃止 — 遷移は tagger_candidate_event が正。[dismissed:…]/[released:…]/[reopened…:…] を含む行は 0155 以前の凍結 legacy';


--
-- Name: COLUMN tagger_head_candidate.promoted_patch_id; Type: COMMENT; Schema: public; Owner: kura
--

COMMENT ON COLUMN public.tagger_head_candidate.promoted_patch_id IS 'Set once this candidate has an actual tagger_patch row (status
        promotes from candidate to promoted, 0075). NULLABLE: pre-existing
        candidates predate this link and match by tag-name string only until
        opportunistically backfilled -- never enforced retroactively.';


--
-- Name: COLUMN tagger_head_candidate.hold_reason_class; Type: COMMENT; Schema: public; Owner: kura
--

COMMENT ON COLUMN public.tagger_head_candidate.hold_reason_class IS '3-axis blocking reason (INV-E / INV-D / data); NOT NULL iff status=held';


--
-- Name: COLUMN tagger_head_candidate.hold_evidence; Type: COMMENT; Schema: public; Owner: kura
--

COMMENT ON COLUMN public.tagger_head_candidate.hold_evidence IS 'append-only {"history":[{measured_at,pi,fpr,p_deploy,...}]}; the arithmetic behind the hold';


--
-- Name: tagger_head_candidate_id_seq; Type: SEQUENCE; Schema: public; Owner: kura
--

CREATE SEQUENCE public.tagger_head_candidate_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.tagger_head_candidate_id_seq OWNER TO kura;

--
-- Name: tagger_head_candidate_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: kura
--

ALTER SEQUENCE public.tagger_head_candidate_id_seq OWNED BY public.tagger_head_candidate.id;


--
-- Name: tagger_lifecycle_event; Type: TABLE; Schema: public; Owner: kura
--

CREATE TABLE public.tagger_lifecycle_event (
    id bigint NOT NULL,
    tag text NOT NULL,
    from_stage text,
    to_stage text NOT NULL,
    reason text NOT NULL,
    actor_db_role text NOT NULL,
    fencing_token bigint,
    occurred_at timestamp with time zone DEFAULT now() NOT NULL,
    tag_id integer NOT NULL
);


ALTER TABLE public.tagger_lifecycle_event OWNER TO kura;

--
-- Name: TABLE tagger_lifecycle_event; Type: COMMENT; Schema: public; Owner: kura
--

COMMENT ON TABLE public.tagger_lifecycle_event IS 'Append-only, canonical: the CURRENT stage of a tag/head concept is
        "to_stage of the latest row for this tag", nothing else. tagger_lifecycle
        (0078) is a read-only VIEW derived from this table -- it is a
        projection, never a second source of truth. Insert-only: no UPDATE/
        DELETE grant exists on this table for any role except during
        migration; production writes go through 0079''s governed functions.';


--
-- Name: tagger_stage; Type: TABLE; Schema: public; Owner: kura
--

CREATE TABLE public.tagger_stage (
    stage text NOT NULL,
    rank integer NOT NULL
);


ALTER TABLE public.tagger_stage OWNER TO kura;

--
-- Name: tagger_lifecycle; Type: VIEW; Schema: public; Owner: kura
--

CREATE VIEW public.tagger_lifecycle AS
 WITH latest_event AS (
         SELECT DISTINCT ON (tagger_lifecycle_event.tag_id) tagger_lifecycle_event.tag_id,
            tagger_lifecycle_event.tag,
            tagger_lifecycle_event.to_stage,
            tagger_lifecycle_event.actor_db_role,
            tagger_lifecycle_event.occurred_at,
            tagger_lifecycle_event.reason
           FROM public.tagger_lifecycle_event
          ORDER BY tagger_lifecycle_event.tag_id, tagger_lifecycle_event.occurred_at DESC, tagger_lifecycle_event.id DESC
        ), latest_candidate AS (
         SELECT DISTINCT ON (tagger_head_candidate.tag_id) tagger_head_candidate.tag_id,
            tagger_head_candidate.id AS candidate_id,
            tagger_head_candidate.status AS candidate_status,
            tagger_head_candidate.promoted_patch_id
           FROM public.tagger_head_candidate
          ORDER BY tagger_head_candidate.tag_id, tagger_head_candidate.added_at DESC, tagger_head_candidate.id DESC
        ), latest_patch AS (
         SELECT DISTINCT ON (tagger_patch.tag_id) tagger_patch.tag_id,
            tagger_patch.id AS patch_id,
            tagger_patch.version AS patch_version,
            tagger_patch.status AS patch_status,
            tagger_patch.head_type,
            tagger_patch.first_release_id,
            tagger_patch.created_at AS patch_created_at
           FROM public.tagger_patch
          ORDER BY tagger_patch.tag_id, tagger_patch.version DESC
        )
 SELECT public.tagger_tag_name_of(COALESCE(le.tag_id, lc.tag_id, lp.tag_id)) AS tag,
    le.to_stage AS lifecycle_stage,
    le.occurred_at AS lifecycle_updated_at,
    lc.candidate_id,
    lc.candidate_status,
    lp.patch_id,
    lp.patch_version,
    lp.patch_status,
    lp.head_type,
    lp.first_release_id,
    ((lp.first_release_id IS NOT NULL) AND (lp.first_release_id = ( SELECT tagger_release_active.release_id
           FROM public.tagger_release_active
          WHERE (tagger_release_active.id = 1)))) AS on_active_release,
        CASE
            WHEN ((lp.patch_status = 'approved'::text) AND (le.to_stage IS NULL)) THEN 'patch approved but no lifecycle_event recorded -- possible bypass of governed path'::text
            WHEN ((lp.patch_status = 'approved'::text) AND (NOT (EXISTS ( SELECT 1
               FROM public.tagger_lifecycle_event e
              WHERE ((e.tag_id = lp.tag_id) AND (e.to_stage = 'approved'::text)))))) THEN 'patch approved but no lifecycle_event ever reached approved -- possible bypass of governed path'::text
            WHEN ((lp.patch_status = 'approved'::text) AND (( SELECT tagger_stage.rank
               FROM public.tagger_stage
              WHERE (tagger_stage.stage = le.to_stage)) < ( SELECT tagger_stage.rank
               FROM public.tagger_stage
              WHERE (tagger_stage.stage = 'approved'::text))) AND (le.occurred_at < lp.patch_created_at)) THEN 'patch approved but latest lifecycle_event predates the approved patch -- stale ledger or bypass'::text
            WHEN ((lc.candidate_status = 'promoted'::text) AND (lc.promoted_patch_id IS NULL)) THEN 'candidate marked promoted but no promoted_patch_id link -- inconsistent'::text
            ELSE NULL::text
        END AS consistency,
    COALESCE(le.tag_id, lc.tag_id, lp.tag_id) AS tag_id
   FROM ((latest_event le
     FULL JOIN latest_candidate lc ON ((lc.tag_id = le.tag_id)))
     FULL JOIN latest_patch lp ON ((lp.tag_id = COALESCE(le.tag_id, lc.tag_id))));


ALTER VIEW public.tagger_lifecycle OWNER TO kura;

--
-- Name: VIEW tagger_lifecycle; Type: COMMENT; Schema: public; Owner: kura
--

COMMENT ON VIEW public.tagger_lifecycle IS 'Read-only projection, NOT canonical (canonical = tagger_lifecycle_event
        + tagger_head_candidate + tagger_patch). consistency IS NULL means no
        known contradiction; a non-NULL value surfaces a state that should be
        impossible under the governed-function-only write path (0079) and
        needs human/reconciler attention, never silently resolved by this
        view picking a side. Since 0119, an approved tag whose LATEST event is
        candidate/training is NOT a violation when an approved event exists
        and the latest event postdates the approved patch: that is the normal
        living cycle of regression re-candidacy (user directive 2026-07-20,
        health.py check_regressions). Violations are limited to a truly
        unrecorded approval (no approved event / no event at all) or a ledger
        frozen before the approved patch existed.';


--
-- Name: tagger_lifecycle_event_id_seq; Type: SEQUENCE; Schema: public; Owner: kura
--

CREATE SEQUENCE public.tagger_lifecycle_event_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.tagger_lifecycle_event_id_seq OWNER TO kura;

--
-- Name: tagger_lifecycle_event_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: kura
--

ALTER SEQUENCE public.tagger_lifecycle_event_id_seq OWNED BY public.tagger_lifecycle_event.id;


--
-- Name: tagger_lifecycle_lock; Type: TABLE; Schema: public; Owner: kura
--

CREATE TABLE public.tagger_lifecycle_lock (
    scope text NOT NULL,
    holder_agent_id text NOT NULL,
    acquired_at timestamp with time zone DEFAULT now() NOT NULL,
    expires_at timestamp with time zone NOT NULL,
    fencing_token bigint NOT NULL
);


ALTER TABLE public.tagger_lifecycle_lock OWNER TO kura;

--
-- Name: TABLE tagger_lifecycle_lock; Type: COMMENT; Schema: public; Owner: kura
--

COMMENT ON TABLE public.tagger_lifecycle_lock IS 'Scope-exclusive lease (scope examples: backlog-sweep, release-build,
        release-publish, patch-registration:<tag>). fencing_token is issued
        ONLY via nextval(tagger_fencing_seq) by the acquire path -- a caller
        never generates, specifies, or replays one; it can only present the
        exact token it was handed. A mutating governed function (0079) locks
        this row FOR UPDATE and requires scope+holder_agent_id+fencing_token
        to match EXACTLY (not "not older than") and expires_at > now() before
        proceeding. A crashed holder is reclaimed once expires_at passes --
        the next acquirer gets a strictly higher token from the sequence.
        This is the direct structural fix for the 2026-07-20 incident where
        primary-cc-primary and primary-cc-2 independently mutated the same backlog
        scope with no shared lease at all.';


--
-- Name: tagger_node_head; Type: TABLE; Schema: public; Owner: kura
--

CREATE TABLE public.tagger_node_head (
    host text NOT NULL,
    tag_id integer NOT NULL,
    tag text NOT NULL,
    content_sha256 text NOT NULL,
    loaded boolean NOT NULL,
    detail text NOT NULL,
    reported_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT tagger_node_head_detail_check CHECK ((length(btrim(detail)) > 0))
);


ALTER TABLE public.tagger_node_head OWNER TO kura;

--
-- Name: tagger_node_reap_authority; Type: VIEW; Schema: public; Owner: kura
--

CREATE VIEW public.tagger_node_reap_authority AS
 SELECT n.host,
    p.tag_id,
    p.tag,
    p.content_sha256
   FROM ((public.tagger_head_policy p
     JOIN public.tagger_patch t ON (((t.tag_id = p.tag_id) AND (t.version = p.patch_version))))
     JOIN public.tagger_node_head n ON (((n.tag_id = p.tag_id) AND (n.content_sha256 = p.content_sha256))))
  WHERE ((t.status = 'approved'::text) AND (p.apply_mode = 'replace'::text) AND n.loaded);


ALTER VIEW public.tagger_node_reap_authority OWNER TO kura;

--
-- Name: tagger_operation; Type: TABLE; Schema: public; Owner: kura
--

CREATE TABLE public.tagger_operation (
    idempotency_key text NOT NULL,
    scope text NOT NULL,
    target_id text NOT NULL,
    operation_type text NOT NULL,
    fencing_token bigint NOT NULL,
    requested_by text NOT NULL,
    result text,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


ALTER TABLE public.tagger_operation OWNER TO kura;

--
-- Name: TABLE tagger_operation; Type: COMMENT; Schema: public; Owner: kura
--

COMMENT ON TABLE public.tagger_operation IS 'idempotency_key is computed SERVER-SIDE by the governed function
        performing the operation (from scope+target_id+operation_type),
        never trusted from the caller -- callers do not need to agree on how
        to derive it. The composite UNIQUE(scope, target_id, operation_type)
        is what actually blocks a double-injection: even if two callers
        somehow produced two different idempotency_key values for what is
        logically the same operation, this constraint still rejects the
        second one.';


--
-- Name: tagger_patch_id_seq; Type: SEQUENCE; Schema: public; Owner: kura
--

CREATE SEQUENCE public.tagger_patch_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.tagger_patch_id_seq OWNER TO kura;

--
-- Name: tagger_patch_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: kura
--

ALTER SEQUENCE public.tagger_patch_id_seq OWNED BY public.tagger_patch.id;


--
-- Name: tagger_producer_threshold; Type: TABLE; Schema: public; Owner: kura
--

CREATE TABLE public.tagger_producer_threshold (
    producer text NOT NULL,
    category smallint NOT NULL,
    threshold double precision NOT NULL,
    evidence text NOT NULL,
    CONSTRAINT tagger_producer_threshold_evidence_check CHECK ((length(btrim(evidence)) >= 10)),
    CONSTRAINT tagger_producer_threshold_threshold_check CHECK (((threshold > (0.0)::double precision) AND (threshold <= (1.0)::double precision)))
);


ALTER TABLE public.tagger_producer_threshold OWNER TO kura;

--
-- Name: tagger_reconciler_state; Type: TABLE; Schema: public; Owner: kura
--

CREATE TABLE public.tagger_reconciler_state (
    id boolean DEFAULT true NOT NULL,
    paused boolean DEFAULT false NOT NULL,
    halt_reason text,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_by text,
    CONSTRAINT tagger_reconciler_state_id_check CHECK (id)
);


ALTER TABLE public.tagger_reconciler_state OWNER TO kura;

--
-- Name: TABLE tagger_reconciler_state; Type: COMMENT; Schema: public; Owner: kura
--

COMMENT ON TABLE public.tagger_reconciler_state IS 'Singleton (CHECK(id) forces exactly one row). paused=true stops
        every automatic transition the reconciler drives; the health timer
        sets it (with halt_reason) on a detected breach, a human clears it.
        This is the manual override the fail-closed design still preserves.';


--
-- Name: tagger_registration_request; Type: TABLE; Schema: public; Owner: kura
--

CREATE TABLE public.tagger_registration_request (
    id bigint NOT NULL,
    tag text NOT NULL,
    source_host text NOT NULL,
    artifact_ref text NOT NULL,
    artifact_sha256 text NOT NULL,
    metrics jsonb,
    gt jsonb,
    status text DEFAULT 'pending'::text NOT NULL,
    rejected_reason text,
    requested_at timestamp with time zone DEFAULT now() NOT NULL,
    validated_at timestamp with time zone,
    promoted_patch_id integer,
    lifecycle_event_id bigint,
    tag_id integer NOT NULL,
    review_class text,
    CONSTRAINT ck_trr_review_class CHECK (((review_class IS NULL) OR (review_class = ANY (ARRAY['deployable'::text, 'not_needed'::text, 'unmeasurable'::text, 'owner_decision'::text, 'floor_rejected'::text])))),
    CONSTRAINT tagger_registration_request_status_check CHECK ((status = ANY (ARRAY['pending'::text, 'validated'::text, 'rejected'::text])))
);


ALTER TABLE public.tagger_registration_request OWNER TO kura;

--
-- Name: TABLE tagger_registration_request; Type: COMMENT; Schema: public; Owner: kura
--

COMMENT ON TABLE public.tagger_registration_request IS 'The ONLY thing a training node (trainer) may write directly --
        canonical-writer invariant means trainer is a read-only replica of
        kura proper and must never INSERT into tagger_patch itself. A
        privileged primary-side service validates (schema, content-hash,
        provenance, dedup, compat) and only then creates the tagger_patch
        candidate row, recording it back here via promoted_patch_id.
        Registration (this table) and active-promotion (tagger_patch status
        flip) are two separate transitions, never combined into one.';


--
-- Name: COLUMN tagger_registration_request.review_class; Type: COMMENT; Schema: public; Owner: kura
--

COMMENT ON COLUMN public.tagger_registration_request.review_class IS 'auto_review の判定 5 分類 (issue #55: status 2 値への潰し込みの解消)。NULL = 未判定 (pending)。unmeasurable = 測れなかった (GT 不足 — not now)、not_needed = 測った上で不要、owner_decision = 人間の決裁待ち、floor_rejected = 実際の基準未達/根拠欠落。rejected_reason の前置 prose は rendering に降格 — 機械はこの列を読む';


--
-- Name: tagger_registration_request_id_seq; Type: SEQUENCE; Schema: public; Owner: kura
--

CREATE SEQUENCE public.tagger_registration_request_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.tagger_registration_request_id_seq OWNER TO kura;

--
-- Name: tagger_registration_request_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: kura
--

ALTER SEQUENCE public.tagger_registration_request_id_seq OWNED BY public.tagger_registration_request.id;


--
-- Name: tagger_release_admissibility; Type: TABLE; Schema: public; Owner: kura
--

CREATE TABLE public.tagger_release_admissibility (
    seq bigint NOT NULL,
    release_digest text NOT NULL,
    node text,
    admissible boolean NOT NULL,
    reason text NOT NULL,
    decided_at timestamp with time zone DEFAULT now() NOT NULL,
    decided_by text NOT NULL
);


ALTER TABLE public.tagger_release_admissibility OWNER TO kura;

--
-- Name: tagger_release_admissibility_seq_seq; Type: SEQUENCE; Schema: public; Owner: kura
--

CREATE SEQUENCE public.tagger_release_admissibility_seq_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.tagger_release_admissibility_seq_seq OWNER TO kura;

--
-- Name: tagger_release_admissibility_seq_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: kura
--

ALTER SEQUENCE public.tagger_release_admissibility_seq_seq OWNED BY public.tagger_release_admissibility.seq;


--
-- Name: tagger_release_component; Type: TABLE; Schema: public; Owner: kura
--

CREATE TABLE public.tagger_release_component (
    release_digest text NOT NULL,
    role text NOT NULL,
    ordinal integer NOT NULL,
    content_sha256 text NOT NULL,
    deriver_sha256 text NOT NULL,
    params jsonb DEFAULT '{}'::jsonb NOT NULL,
    CONSTRAINT tagger_release_component_deriver_sha256_check CHECK ((deriver_sha256 ~ '^[0-9a-f]{64}$'::text))
);


ALTER TABLE public.tagger_release_component OWNER TO kura;

--
-- Name: tagger_release_id_seq; Type: SEQUENCE; Schema: public; Owner: kura
--

CREATE SEQUENCE public.tagger_release_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.tagger_release_id_seq OWNER TO kura;

--
-- Name: tagger_release_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: kura
--

ALTER SEQUENCE public.tagger_release_id_seq OWNED BY public.tagger_release.id;


--
-- Name: tagger_release_manifest; Type: TABLE; Schema: public; Owner: kura
--

CREATE TABLE public.tagger_release_manifest (
    release_digest text NOT NULL,
    provenance jsonb NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    created_by text NOT NULL,
    release_id integer,
    CONSTRAINT tagger_release_manifest_release_digest_check CHECK ((release_digest ~ '^[0-9a-f]{64}$'::text))
);


ALTER TABLE public.tagger_release_manifest OWNER TO kura;

--
-- Name: tagger_stage_transition; Type: TABLE; Schema: public; Owner: kura
--

CREATE TABLE public.tagger_stage_transition (
    id bigint NOT NULL,
    from_stage text,
    to_stage text NOT NULL,
    kind text NOT NULL,
    CONSTRAINT tagger_stage_transition_kind_check CHECK ((kind = ANY (ARRAY['advance'::text, 'rollback'::text])))
);


ALTER TABLE public.tagger_stage_transition OWNER TO kura;

--
-- Name: TABLE tagger_stage_transition; Type: COMMENT; Schema: public; Owner: kura
--

COMMENT ON TABLE public.tagger_stage_transition IS 'The allowed-edge graph for tagger_lifecycle_event. advance rows
        must have to_stage.rank > from_stage.rank (enforced by the
        trg_tagger_lifecycle_event_edge trigger below, not just convention);
        rollback rows are the only way to move to a lower rank, and each one
        is an explicit, reviewed edge -- a bare rank decrease is otherwise
        structurally unexpressable.';


--
-- Name: tagger_stage_transition_id_seq; Type: SEQUENCE; Schema: public; Owner: kura
--

CREATE SEQUENCE public.tagger_stage_transition_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.tagger_stage_transition_id_seq OWNER TO kura;

--
-- Name: tagger_stage_transition_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: kura
--

ALTER SEQUENCE public.tagger_stage_transition_id_seq OWNED BY public.tagger_stage_transition.id;


--
-- Name: tagger_sweep_run; Type: TABLE; Schema: public; Owner: kura
--

CREATE TABLE public.tagger_sweep_run (
    id bigint NOT NULL,
    release_id integer NOT NULL,
    release_generation bigint NOT NULL,
    requested_ceiling integer NOT NULL,
    flipped_count integer DEFAULT 0 NOT NULL,
    dry_run boolean DEFAULT false NOT NULL,
    status text DEFAULT 'running'::text NOT NULL,
    started_at timestamp with time zone DEFAULT now() NOT NULL,
    finished_at timestamp with time zone,
    CONSTRAINT tagger_sweep_run_status_check CHECK ((status = ANY (ARRAY['running'::text, 'completed'::text, 'aborted_toctou'::text, 'failed'::text])))
);


ALTER TABLE public.tagger_sweep_run OWNER TO kura;

--
-- Name: TABLE tagger_sweep_run; Type: COMMENT; Schema: public; Owner: kura
--

COMMENT ON TABLE public.tagger_sweep_run IS 'One row per backlog-sweep invocation, pinned to the exact
        (release_id, generation) it targeted at start. A sweep whose
        generation no longer matches tagger_release_active.generation by
        commit time aborts as aborted_toctou rather than flipping posts
        against a target that is no longer the active release.';


--
-- Name: tagger_sweep_run_id_seq; Type: SEQUENCE; Schema: public; Owner: kura
--

CREATE SEQUENCE public.tagger_sweep_run_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.tagger_sweep_run_id_seq OWNER TO kura;

--
-- Name: tagger_sweep_run_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: kura
--

ALTER SEQUENCE public.tagger_sweep_run_id_seq OWNED BY public.tagger_sweep_run.id;


--
-- Name: tagger_tag_condition; Type: TABLE; Schema: public; Owner: kura
--

CREATE TABLE public.tagger_tag_condition (
    name text NOT NULL,
    all_tags text[] DEFAULT '{}'::text[] NOT NULL,
    any_tags text[] DEFAULT '{}'::text[] NOT NULL,
    none_tags text[] DEFAULT '{}'::text[] NOT NULL,
    evidence text NOT NULL,
    CONSTRAINT tagger_tag_condition_evidence_check CHECK ((length(btrim(evidence)) >= 10))
);


ALTER TABLE public.tagger_tag_condition OWNER TO kura;

--
-- Name: tagger_tag_rule; Type: TABLE; Schema: public; Owner: kura
--

CREATE TABLE public.tagger_tag_rule (
    producer text NOT NULL,
    tag_id integer NOT NULL,
    tag text NOT NULL,
    threshold double precision NOT NULL,
    remove_below double precision NOT NULL,
    may_add boolean NOT NULL,
    may_remove boolean NOT NULL,
    condition text DEFAULT 'always'::text NOT NULL,
    evidence text NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT tagger_tag_rule_check CHECK ((remove_below <= threshold)),
    CONSTRAINT tagger_tag_rule_check1 CHECK ((may_add OR may_remove)),
    CONSTRAINT tagger_tag_rule_evidence_check CHECK ((length(btrim(evidence)) >= 10)),
    CONSTRAINT tagger_tag_rule_remove_below_check CHECK ((remove_below > (0.0)::double precision)),
    CONSTRAINT tagger_tag_rule_threshold_check CHECK (((threshold > (0.0)::double precision) AND (threshold <= (1.0)::double precision)))
);


ALTER TABLE public.tagger_tag_rule OWNER TO kura;

--
-- Name: tagger_tag_application; Type: VIEW; Schema: public; Owner: kura
--

CREATE VIEW public.tagger_tag_application AS
 SELECT r.producer,
    r.tag_id,
    r.tag,
    r.threshold,
    r.remove_below,
    r.may_add,
    r.may_remove,
    c.all_tags,
    c.any_tags,
    c.none_tags
   FROM (public.tagger_tag_rule r
     JOIN public.tagger_tag_condition c ON ((c.name = r.condition)))
UNION ALL
 SELECT 'head'::text AS producer,
    p.tag_id,
    p.tag,
    p.threshold,
    p.threshold AS remove_below,
    true AS may_add,
    (p.apply_mode = 'replace'::text) AS may_remove,
    p.gate_tags AS all_tags,
    '{}'::text[] AS any_tags,
    '{}'::text[] AS none_tags
   FROM (public.tagger_head_policy p
     JOIN ( SELECT tagger_head_policy.tag_id,
            max(tagger_head_policy.patch_version) AS v
           FROM public.tagger_head_policy
          GROUP BY tagger_head_policy.tag_id) latest ON (((latest.tag_id = p.tag_id) AND (latest.v = p.patch_version))));


ALTER VIEW public.tagger_tag_application OWNER TO kura;

--
-- Name: tagger_tag_exclusion; Type: TABLE; Schema: public; Owner: kura
--

CREATE TABLE public.tagger_tag_exclusion (
    tag_a_id integer NOT NULL,
    tag_b_id integer NOT NULL,
    condition text DEFAULT 'always'::text NOT NULL,
    evidence text NOT NULL,
    CONSTRAINT tagger_tag_exclusion_check CHECK ((tag_a_id < tag_b_id)),
    CONSTRAINT tagger_tag_exclusion_evidence_check CHECK ((length(btrim(evidence)) >= 10))
);


ALTER TABLE public.tagger_tag_exclusion OWNER TO kura;

--
-- Name: tagger_tag_state; Type: VIEW; Schema: public; Owner: kura
--

CREATE VIEW public.tagger_tag_state AS
 SELECT t.id AS tag_id,
    public.tagger_tag_name_of(t.id) AS tag,
    tc.name AS category,
    t.post_count,
    lc.lifecycle_stage,
    lc.patch_version,
    lc.patch_status,
    lc.head_type,
    lc.on_active_release,
    hp.threshold,
    hp.apply_mode,
    hp.gate_tags,
    hp.measured_recall,
    hp.operating_point,
    ((r.metrics -> 'classification'::text) ->> 'verdict'::text) AS verdict,
    (((r.metrics -> 'classification'::text) ->> 'lift'::text))::double precision AS lift,
    (((r.metrics -> 'classification'::text) ->> 'net_fp_removed_deploy'::text))::double precision AS net_fp_removed,
    ((r.gt ->> 'positive'::text))::integer AS gt_positive,
    ((r.gt ->> 'negative'::text))::integer AS gt_negative,
    COALESCE(q.active, false) AS quarantined
   FROM (((((public.tag t
     JOIN public.tag_category tc ON ((tc.id = t.category_id)))
     LEFT JOIN public.tagger_lifecycle lc ON ((lc.tag_id = t.id)))
     LEFT JOIN public.tagger_head_policy hp ON (((hp.tag_id = t.id) AND (hp.patch_version = lc.patch_version))))
     LEFT JOIN LATERAL ( SELECT rr.metrics,
            rr.gt
           FROM public.tagger_registration_request rr
          WHERE (rr.tag_id = t.id)
          ORDER BY rr.id DESC
         LIMIT 1) r ON (true))
     LEFT JOIN public.tag_quarantine q ON ((q.tag_id = t.id)));


ALTER VIEW public.tagger_tag_state OWNER TO kura;

--
-- Name: user; Type: TABLE; Schema: public; Owner: kura
--

CREATE TABLE public."user" (
    id integer NOT NULL,
    name character varying(50) NOT NULL,
    password_hash character varying(128),
    password_salt character varying(32),
    email character varying(64),
    rank character varying(32) NOT NULL,
    creation_time timestamp without time zone NOT NULL,
    last_login_time timestamp without time zone,
    avatar_style character varying(32) NOT NULL,
    version integer NOT NULL,
    password_revision smallint NOT NULL,
    broker_trust_tier character varying(32)
);


ALTER TABLE public."user" OWNER TO kura;

--
-- Name: user_id_seq; Type: SEQUENCE; Schema: public; Owner: kura
--

CREATE SEQUENCE public.user_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.user_id_seq OWNER TO kura;

--
-- Name: user_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: kura
--

ALTER SEQUENCE public.user_id_seq OWNED BY public."user".id;


--
-- Name: user_token; Type: TABLE; Schema: public; Owner: kura
--

CREATE TABLE public.user_token (
    id integer NOT NULL,
    user_id integer NOT NULL,
    token character varying(36) NOT NULL,
    note character varying(128),
    enabled boolean NOT NULL,
    expiration_time timestamp without time zone,
    creation_time timestamp without time zone NOT NULL,
    last_edit_time timestamp without time zone,
    last_usage_time timestamp without time zone,
    version integer NOT NULL
);


ALTER TABLE public.user_token OWNER TO kura;

--
-- Name: user_token_id_seq; Type: SEQUENCE; Schema: public; Owner: kura
--

CREATE SEQUENCE public.user_token_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.user_token_id_seq OWNER TO kura;

--
-- Name: user_token_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: kura
--

ALTER SEQUENCE public.user_token_id_seq OWNED BY public.user_token.id;


--
-- Name: censor_annotation id; Type: DEFAULT; Schema: public; Owner: kura
--

ALTER TABLE ONLY public.censor_annotation ALTER COLUMN id SET DEFAULT nextval('public.censor_annotation_id_seq'::regclass);


--
-- Name: censor_pair id; Type: DEFAULT; Schema: public; Owner: kura
--

ALTER TABLE ONLY public.censor_pair ALTER COLUMN id SET DEFAULT nextval('public.censor_pair_id_seq'::regclass);


--
-- Name: comment id; Type: DEFAULT; Schema: public; Owner: kura
--

ALTER TABLE ONLY public.comment ALTER COLUMN id SET DEFAULT nextval('public.comment_id_seq'::regclass);


--
-- Name: lora_asset id; Type: DEFAULT; Schema: public; Owner: kura
--

ALTER TABLE ONLY public.lora_asset ALTER COLUMN id SET DEFAULT nextval('public.lora_asset_id_seq'::regclass);


--
-- Name: lora_concept id; Type: DEFAULT; Schema: public; Owner: kura
--

ALTER TABLE ONLY public.lora_concept ALTER COLUMN id SET DEFAULT nextval('public.lora_concept_id_seq'::regclass);


--
-- Name: lora_model status; Type: DEFAULT; Schema: public; Owner: kura
--

ALTER TABLE ONLY public.lora_model ALTER COLUMN status SET DEFAULT 'available'::text;


--
-- Name: lora_model origin; Type: DEFAULT; Schema: public; Owner: kura
--

ALTER TABLE ONLY public.lora_model ALTER COLUMN origin SET DEFAULT 'trained'::text;


--
-- Name: lora_model_base id; Type: DEFAULT; Schema: public; Owner: kura
--

ALTER TABLE ONLY public.lora_model_base ALTER COLUMN id SET DEFAULT nextval('public.lora_model_id_seq'::regclass);


--
-- Name: lora_post id; Type: DEFAULT; Schema: public; Owner: kura
--

ALTER TABLE ONLY public.lora_post ALTER COLUMN id SET DEFAULT nextval('public.lora_post_id_seq'::regclass);


--
-- Name: lora_training_run id; Type: DEFAULT; Schema: public; Owner: kura
--

ALTER TABLE ONLY public.lora_training_run ALTER COLUMN id SET DEFAULT nextval('public.lora_training_run_id_seq'::regclass);


--
-- Name: pool id; Type: DEFAULT; Schema: public; Owner: kura
--

ALTER TABLE ONLY public.pool ALTER COLUMN id SET DEFAULT nextval('public.pool_id_seq'::regclass);


--
-- Name: pool_category id; Type: DEFAULT; Schema: public; Owner: kura
--

ALTER TABLE ONLY public.pool_category ALTER COLUMN id SET DEFAULT nextval('public.pool_category_id_seq'::regclass);


--
-- Name: pool_name pool_name_id; Type: DEFAULT; Schema: public; Owner: kura
--

ALTER TABLE ONLY public.pool_name ALTER COLUMN pool_name_id SET DEFAULT nextval('public.pool_name_pool_name_id_seq'::regclass);


--
-- Name: post id; Type: DEFAULT; Schema: public; Owner: kura
--

ALTER TABLE ONLY public.post ALTER COLUMN id SET DEFAULT nextval('public.post_id_seq'::regclass);


--
-- Name: post_censor_review id; Type: DEFAULT; Schema: public; Owner: kura
--

ALTER TABLE ONLY public.post_censor_review ALTER COLUMN id SET DEFAULT nextval('public.post_censor_review_id_seq'::regclass);


--
-- Name: post_feature id; Type: DEFAULT; Schema: public; Owner: kura
--

ALTER TABLE ONLY public.post_feature ALTER COLUMN id SET DEFAULT nextval('public.post_feature_id_seq'::regclass);


--
-- Name: post_note id; Type: DEFAULT; Schema: public; Owner: kura
--

ALTER TABLE ONLY public.post_note ALTER COLUMN id SET DEFAULT nextval('public.post_note_id_seq'::regclass);


--
-- Name: post_tag_actor_claim claimed_at; Type: DEFAULT; Schema: public; Owner: kura
--

ALTER TABLE ONLY public.post_tag_actor_claim ALTER COLUMN claimed_at SET DEFAULT now();


--
-- Name: post_tag_audit id; Type: DEFAULT; Schema: public; Owner: kura
--

ALTER TABLE ONLY public.post_tag_audit ALTER COLUMN id SET DEFAULT nextval('public.post_tag_audit_id_seq'::regclass);


--
-- Name: post_variant id; Type: DEFAULT; Schema: public; Owner: kura
--

ALTER TABLE ONLY public.post_variant ALTER COLUMN id SET DEFAULT nextval('public.post_variant_id_seq'::regclass);


--
-- Name: tag id; Type: DEFAULT; Schema: public; Owner: kura
--

ALTER TABLE ONLY public.tag ALTER COLUMN id SET DEFAULT nextval('public.tag_id_seq'::regclass);


--
-- Name: tag_category id; Type: DEFAULT; Schema: public; Owner: kura
--

ALTER TABLE ONLY public.tag_category ALTER COLUMN id SET DEFAULT nextval('public.tag_category_id_seq'::regclass);


--
-- Name: tag_link id; Type: DEFAULT; Schema: public; Owner: kura
--

ALTER TABLE ONLY public.tag_link ALTER COLUMN id SET DEFAULT nextval('public.tag_link_id_seq'::regclass);


--
-- Name: tag_name tag_name_id; Type: DEFAULT; Schema: public; Owner: kura
--

ALTER TABLE ONLY public.tag_name ALTER COLUMN tag_name_id SET DEFAULT nextval('public.tag_name_tag_name_id_seq'::regclass);


--
-- Name: tag_pin_inventory id; Type: DEFAULT; Schema: public; Owner: kura
--

ALTER TABLE ONLY public.tag_pin_inventory ALTER COLUMN id SET DEFAULT nextval('public.tag_pin_inventory_id_seq'::regclass);


--
-- Name: tag_quarantine id; Type: DEFAULT; Schema: public; Owner: kura
--

ALTER TABLE ONLY public.tag_quarantine ALTER COLUMN id SET DEFAULT nextval('public.tag_quarantine_id_seq'::regclass);


--
-- Name: tagger_candidate_disposition seq; Type: DEFAULT; Schema: public; Owner: kura
--

ALTER TABLE ONLY public.tagger_candidate_disposition ALTER COLUMN seq SET DEFAULT nextval('public.tagger_candidate_disposition_seq_seq'::regclass);


--
-- Name: tagger_candidate_event seq; Type: DEFAULT; Schema: public; Owner: kura
--

ALTER TABLE ONLY public.tagger_candidate_event ALTER COLUMN seq SET DEFAULT nextval('public.tagger_candidate_event_seq_seq'::regclass);


--
-- Name: tagger_gt_label id; Type: DEFAULT; Schema: public; Owner: kura
--

ALTER TABLE ONLY public.tagger_gt_label ALTER COLUMN id SET DEFAULT nextval('public.tagger_gt_label_id_seq'::regclass);


--
-- Name: tagger_head_candidate id; Type: DEFAULT; Schema: public; Owner: kura
--

ALTER TABLE ONLY public.tagger_head_candidate ALTER COLUMN id SET DEFAULT nextval('public.tagger_head_candidate_id_seq'::regclass);


--
-- Name: tagger_lifecycle_event id; Type: DEFAULT; Schema: public; Owner: kura
--

ALTER TABLE ONLY public.tagger_lifecycle_event ALTER COLUMN id SET DEFAULT nextval('public.tagger_lifecycle_event_id_seq'::regclass);


--
-- Name: tagger_patch id; Type: DEFAULT; Schema: public; Owner: kura
--

ALTER TABLE ONLY public.tagger_patch ALTER COLUMN id SET DEFAULT nextval('public.tagger_patch_id_seq'::regclass);


--
-- Name: tagger_registration_request id; Type: DEFAULT; Schema: public; Owner: kura
--

ALTER TABLE ONLY public.tagger_registration_request ALTER COLUMN id SET DEFAULT nextval('public.tagger_registration_request_id_seq'::regclass);


--
-- Name: tagger_release id; Type: DEFAULT; Schema: public; Owner: kura
--

ALTER TABLE ONLY public.tagger_release ALTER COLUMN id SET DEFAULT nextval('public.tagger_release_id_seq'::regclass);


--
-- Name: tagger_release_admissibility seq; Type: DEFAULT; Schema: public; Owner: kura
--

ALTER TABLE ONLY public.tagger_release_admissibility ALTER COLUMN seq SET DEFAULT nextval('public.tagger_release_admissibility_seq_seq'::regclass);


--
-- Name: tagger_stage_transition id; Type: DEFAULT; Schema: public; Owner: kura
--

ALTER TABLE ONLY public.tagger_stage_transition ALTER COLUMN id SET DEFAULT nextval('public.tagger_stage_transition_id_seq'::regclass);


--
-- Name: tagger_sweep_run id; Type: DEFAULT; Schema: public; Owner: kura
--

ALTER TABLE ONLY public.tagger_sweep_run ALTER COLUMN id SET DEFAULT nextval('public.tagger_sweep_run_id_seq'::regclass);


--
-- Name: user id; Type: DEFAULT; Schema: public; Owner: kura
--

ALTER TABLE ONLY public."user" ALTER COLUMN id SET DEFAULT nextval('public.user_id_seq'::regclass);


--
-- Name: user_token id; Type: DEFAULT; Schema: public; Owner: kura
--

ALTER TABLE ONLY public.user_token ALTER COLUMN id SET DEFAULT nextval('public.user_token_id_seq'::regclass);


--
-- Name: actor actor_actor_id_key; Type: CONSTRAINT; Schema: claim_storage; Owner: kura
--

ALTER TABLE ONLY claim_storage.actor
    ADD CONSTRAINT actor_actor_id_key UNIQUE (actor_id);


--
-- Name: actor actor_pkey; Type: CONSTRAINT; Schema: claim_storage; Owner: kura
--

ALTER TABLE ONLY claim_storage.actor
    ADD CONSTRAINT actor_pkey PRIMARY KEY (id);


--
-- Name: claim claim_pkey; Type: CONSTRAINT; Schema: claim_storage; Owner: kura
--

ALTER TABLE ONLY claim_storage.claim
    ADD CONSTRAINT claim_pkey PRIMARY KEY (post_id, tag_id, actor_key);


--
-- Name: migration migration_pkey; Type: CONSTRAINT; Schema: claim_storage; Owner: kura
--

ALTER TABLE ONLY claim_storage.migration
    ADD CONSTRAINT migration_pkey PRIMARY KEY (singleton);


--
-- Name: asset_label_state asset_label_state_pkey; Type: CONSTRAINT; Schema: collector_next; Owner: kura
--

ALTER TABLE ONLY collector_next.asset_label_state
    ADD CONSTRAINT asset_label_state_pkey PRIMARY KEY (asset_id);


--
-- Name: asset asset_owner_id_blob_id_key; Type: CONSTRAINT; Schema: collector_next; Owner: kura
--

ALTER TABLE ONLY collector_next.asset
    ADD CONSTRAINT asset_owner_id_blob_id_key UNIQUE (owner_id, blob_id);


--
-- Name: asset asset_owner_id_id_key; Type: CONSTRAINT; Schema: collector_next; Owner: kura
--

ALTER TABLE ONLY collector_next.asset
    ADD CONSTRAINT asset_owner_id_id_key UNIQUE (owner_id, id);


--
-- Name: asset asset_pkey; Type: CONSTRAINT; Schema: collector_next; Owner: kura
--

ALTER TABLE ONLY collector_next.asset
    ADD CONSTRAINT asset_pkey PRIMARY KEY (id);


--
-- Name: asset asset_uuid_key; Type: CONSTRAINT; Schema: collector_next; Owner: kura
--

ALTER TABLE ONLY collector_next.asset
    ADD CONSTRAINT asset_uuid_key UNIQUE (uuid);


--
-- Name: blob_location blob_location_pkey; Type: CONSTRAINT; Schema: collector_next; Owner: kura
--

ALTER TABLE ONLY collector_next.blob_location
    ADD CONSTRAINT blob_location_pkey PRIMARY KEY (blob_id);


--
-- Name: blob_location blob_location_storage_key_key; Type: CONSTRAINT; Schema: collector_next; Owner: kura
--

ALTER TABLE ONLY collector_next.blob_location
    ADD CONSTRAINT blob_location_storage_key_key UNIQUE (storage_key);


--
-- Name: blob blob_pkey; Type: CONSTRAINT; Schema: collector_next; Owner: kura
--

ALTER TABLE ONLY collector_next.blob
    ADD CONSTRAINT blob_pkey PRIMARY KEY (id);


--
-- Name: blob blob_sha256_key; Type: CONSTRAINT; Schema: collector_next; Owner: kura
--

ALTER TABLE ONLY collector_next.blob
    ADD CONSTRAINT blob_sha256_key UNIQUE (sha256);


--
-- Name: collection_entry collection_entry_collection_id_position_key; Type: CONSTRAINT; Schema: collector_next; Owner: kura
--

ALTER TABLE ONLY collector_next.collection_entry
    ADD CONSTRAINT collection_entry_collection_id_position_key UNIQUE (collection_id, "position");


--
-- Name: collection_entry collection_entry_pkey; Type: CONSTRAINT; Schema: collector_next; Owner: kura
--

ALTER TABLE ONLY collector_next.collection_entry
    ADD CONSTRAINT collection_entry_pkey PRIMARY KEY (id);


--
-- Name: collection collection_owner_id_id_key; Type: CONSTRAINT; Schema: collector_next; Owner: kura
--

ALTER TABLE ONLY collector_next.collection
    ADD CONSTRAINT collection_owner_id_id_key UNIQUE (owner_id, id);


--
-- Name: collection collection_owner_id_request_key_key; Type: CONSTRAINT; Schema: collector_next; Owner: kura
--

ALTER TABLE ONLY collector_next.collection
    ADD CONSTRAINT collection_owner_id_request_key_key UNIQUE (owner_id, request_key);


--
-- Name: collection collection_pkey; Type: CONSTRAINT; Schema: collector_next; Owner: kura
--

ALTER TABLE ONLY collector_next.collection
    ADD CONSTRAINT collection_pkey PRIMARY KEY (id);


--
-- Name: import_item import_item_collection_id_collection_position_key; Type: CONSTRAINT; Schema: collector_next; Owner: kura
--

ALTER TABLE ONLY collector_next.import_item
    ADD CONSTRAINT import_item_collection_id_collection_position_key UNIQUE (collection_id, collection_position);


--
-- Name: import_item import_item_owner_id_request_key_key; Type: CONSTRAINT; Schema: collector_next; Owner: kura
--

ALTER TABLE ONLY collector_next.import_item
    ADD CONSTRAINT import_item_owner_id_request_key_key UNIQUE (owner_id, request_key);


--
-- Name: import_item import_item_pkey; Type: CONSTRAINT; Schema: collector_next; Owner: kura
--

ALTER TABLE ONLY collector_next.import_item
    ADD CONSTRAINT import_item_pkey PRIMARY KEY (owner_id, operation_key, "position");


--
-- Name: import_operation import_operation_pkey; Type: CONSTRAINT; Schema: collector_next; Owner: kura
--

ALTER TABLE ONLY collector_next.import_operation
    ADD CONSTRAINT import_operation_pkey PRIMARY KEY (owner_id, request_key);


--
-- Name: label_head label_head_pkey; Type: CONSTRAINT; Schema: collector_next; Owner: kura
--

ALTER TABLE ONLY collector_next.label_head
    ADD CONSTRAINT label_head_pkey PRIMARY KEY (asset_id, tag_id);


--
-- Name: label_observation label_observation_pkey; Type: CONSTRAINT; Schema: collector_next; Owner: kura
--

ALTER TABLE ONLY collector_next.label_observation
    ADD CONSTRAINT label_observation_pkey PRIMARY KEY (asset_id, tag_id, id);


--
-- Name: label_operation label_operation_pkey; Type: CONSTRAINT; Schema: collector_next; Owner: kura
--

ALTER TABLE ONLY collector_next.label_operation
    ADD CONSTRAINT label_operation_pkey PRIMARY KEY (owner_id, request_key);


--
-- Name: label_policy label_policy_pkey; Type: CONSTRAINT; Schema: collector_next; Owner: kura
--

ALTER TABLE ONLY collector_next.label_policy
    ADD CONSTRAINT label_policy_pkey PRIMARY KEY (id);


--
-- Name: label_policy label_policy_version_key; Type: CONSTRAINT; Schema: collector_next; Owner: kura
--

ALTER TABLE ONLY collector_next.label_policy
    ADD CONSTRAINT label_policy_version_key UNIQUE (version);


--
-- Name: label_producer_kind label_producer_kind_name_key; Type: CONSTRAINT; Schema: collector_next; Owner: kura
--

ALTER TABLE ONLY collector_next.label_producer_kind
    ADD CONSTRAINT label_producer_kind_name_key UNIQUE (name);


--
-- Name: label_producer_kind label_producer_kind_pkey; Type: CONSTRAINT; Schema: collector_next; Owner: kura
--

ALTER TABLE ONLY collector_next.label_producer_kind
    ADD CONSTRAINT label_producer_kind_pkey PRIMARY KEY (id);


--
-- Name: label_producer label_producer_owner_id_id_key; Type: CONSTRAINT; Schema: collector_next; Owner: kura
--

ALTER TABLE ONLY collector_next.label_producer
    ADD CONSTRAINT label_producer_owner_id_id_key UNIQUE (owner_id, id);


--
-- Name: label_producer label_producer_owner_id_kind_id_reference_version_key; Type: CONSTRAINT; Schema: collector_next; Owner: kura
--

ALTER TABLE ONLY collector_next.label_producer
    ADD CONSTRAINT label_producer_owner_id_kind_id_reference_version_key UNIQUE (owner_id, kind_id, reference, version);


--
-- Name: label_producer label_producer_pkey; Type: CONSTRAINT; Schema: collector_next; Owner: kura
--

ALTER TABLE ONLY collector_next.label_producer
    ADD CONSTRAINT label_producer_pkey PRIMARY KEY (id);


--
-- Name: label_target label_target_pkey; Type: CONSTRAINT; Schema: collector_next; Owner: kura
--

ALTER TABLE ONLY collector_next.label_target
    ADD CONSTRAINT label_target_pkey PRIMARY KEY (owner_id, request_key, asset_id);


--
-- Name: media_current media_current_pkey; Type: CONSTRAINT; Schema: collector_next; Owner: kura
--

ALTER TABLE ONLY collector_next.media_current
    ADD CONSTRAINT media_current_pkey PRIMARY KEY (asset_id);


--
-- Name: receipt receipt_pkey; Type: CONSTRAINT; Schema: collector_next; Owner: kura
--

ALTER TABLE ONLY collector_next.receipt
    ADD CONSTRAINT receipt_pkey PRIMARY KEY (owner_id, request_key);


--
-- Name: schema_version schema_version_pkey; Type: CONSTRAINT; Schema: collector_next; Owner: kura
--

ALTER TABLE ONLY collector_next.schema_version
    ADD CONSTRAINT schema_version_pkey PRIMARY KEY (version);


--
-- Name: tag tag_owner_id_id_key; Type: CONSTRAINT; Schema: collector_next; Owner: kura
--

ALTER TABLE ONLY collector_next.tag
    ADD CONSTRAINT tag_owner_id_id_key UNIQUE (owner_id, id);


--
-- Name: tag tag_owner_id_normalized_name_key; Type: CONSTRAINT; Schema: collector_next; Owner: kura
--

ALTER TABLE ONLY collector_next.tag
    ADD CONSTRAINT tag_owner_id_normalized_name_key UNIQUE (owner_id, normalized_name);


--
-- Name: tag tag_pkey; Type: CONSTRAINT; Schema: collector_next; Owner: kura
--

ALTER TABLE ONLY collector_next.tag
    ADD CONSTRAINT tag_pkey PRIMARY KEY (id);


--
-- Name: tag_usage tag_usage_pkey; Type: CONSTRAINT; Schema: collector_next; Owner: kura
--

ALTER TABLE ONLY collector_next.tag_usage
    ADD CONSTRAINT tag_usage_pkey PRIMARY KEY (tag_id);


--
-- Name: alembic_version alembic_version_pkc; Type: CONSTRAINT; Schema: public; Owner: kura
--

ALTER TABLE ONLY public.alembic_version
    ADD CONSTRAINT alembic_version_pkc PRIMARY KEY (version_num);


--
-- Name: censor_annotation censor_annotation_pkey; Type: CONSTRAINT; Schema: public; Owner: kura
--

ALTER TABLE ONLY public.censor_annotation
    ADD CONSTRAINT censor_annotation_pkey PRIMARY KEY (id);


--
-- Name: censor_pair censor_pair_censored_post_id_key; Type: CONSTRAINT; Schema: public; Owner: kura
--

ALTER TABLE ONLY public.censor_pair
    ADD CONSTRAINT censor_pair_censored_post_id_key UNIQUE (censored_post_id);


--
-- Name: censor_pair censor_pair_pkey; Type: CONSTRAINT; Schema: public; Owner: kura
--

ALTER TABLE ONLY public.censor_pair
    ADD CONSTRAINT censor_pair_pkey PRIMARY KEY (id);


--
-- Name: censor_pair_search censor_pair_search_pkey; Type: CONSTRAINT; Schema: public; Owner: kura
--

ALTER TABLE ONLY public.censor_pair_search
    ADD CONSTRAINT censor_pair_search_pkey PRIMARY KEY (post_id);


--
-- Name: censor_pair_search_scope censor_pair_search_scope_pkey; Type: CONSTRAINT; Schema: public; Owner: kura
--

ALTER TABLE ONLY public.censor_pair_search_scope
    ADD CONSTRAINT censor_pair_search_scope_pkey PRIMARY KEY (pool_id);


--
-- Name: censor_trained_post censor_trained_post_pkey; Type: CONSTRAINT; Schema: public; Owner: kura
--

ALTER TABLE ONLY public.censor_trained_post
    ADD CONSTRAINT censor_trained_post_pkey PRIMARY KEY (post_id);


--
-- Name: comment comment_pkey; Type: CONSTRAINT; Schema: public; Owner: kura
--

ALTER TABLE ONLY public.comment
    ADD CONSTRAINT comment_pkey PRIMARY KEY (id);


--
-- Name: comment_score comment_score_pkey; Type: CONSTRAINT; Schema: public; Owner: kura
--

ALTER TABLE ONLY public.comment_score
    ADD CONSTRAINT comment_score_pkey PRIMARY KEY (comment_id, user_id);


--
-- Name: human_login_key human_login_key_pkey; Type: CONSTRAINT; Schema: public; Owner: kura
--

ALTER TABLE ONLY public.human_login_key
    ADD CONSTRAINT human_login_key_pkey PRIMARY KEY (key_digest);


--
-- Name: kura_alembic_version kura_alembic_version_pkc; Type: CONSTRAINT; Schema: public; Owner: kura
--

ALTER TABLE ONLY public.kura_alembic_version
    ADD CONSTRAINT kura_alembic_version_pkc PRIMARY KEY (version_num);


--
-- Name: lora_asset lora_asset_name_key; Type: CONSTRAINT; Schema: public; Owner: kura
--

ALTER TABLE ONLY public.lora_asset
    ADD CONSTRAINT lora_asset_name_key UNIQUE (name);


--
-- Name: lora_asset lora_asset_pkey; Type: CONSTRAINT; Schema: public; Owner: kura
--

ALTER TABLE ONLY public.lora_asset
    ADD CONSTRAINT lora_asset_pkey PRIMARY KEY (id);


--
-- Name: lora_concept lora_concept_pkey; Type: CONSTRAINT; Schema: public; Owner: kura
--

ALTER TABLE ONLY public.lora_concept
    ADD CONSTRAINT lora_concept_pkey PRIMARY KEY (id);


--
-- Name: lora_model_base lora_model_name_key; Type: CONSTRAINT; Schema: public; Owner: kura
--

ALTER TABLE ONLY public.lora_model_base
    ADD CONSTRAINT lora_model_name_key UNIQUE (name);


--
-- Name: lora_model_base lora_model_pkey; Type: CONSTRAINT; Schema: public; Owner: kura
--

ALTER TABLE ONLY public.lora_model_base
    ADD CONSTRAINT lora_model_pkey PRIMARY KEY (id);


--
-- Name: lora_post lora_post_pkey; Type: CONSTRAINT; Schema: public; Owner: kura
--

ALTER TABLE ONLY public.lora_post
    ADD CONSTRAINT lora_post_pkey PRIMARY KEY (id);


--
-- Name: lora_training_run lora_training_run_pkey; Type: CONSTRAINT; Schema: public; Owner: kura
--

ALTER TABLE ONLY public.lora_training_run
    ADD CONSTRAINT lora_training_run_pkey PRIMARY KEY (id);


--
-- Name: pool_category pool_category_pkey; Type: CONSTRAINT; Schema: public; Owner: kura
--

ALTER TABLE ONLY public.pool_category
    ADD CONSTRAINT pool_category_pkey PRIMARY KEY (id);


--
-- Name: pool_name pool_name_name_key; Type: CONSTRAINT; Schema: public; Owner: kura
--

ALTER TABLE ONLY public.pool_name
    ADD CONSTRAINT pool_name_name_key UNIQUE (name);


--
-- Name: pool_name pool_name_pkey; Type: CONSTRAINT; Schema: public; Owner: kura
--

ALTER TABLE ONLY public.pool_name
    ADD CONSTRAINT pool_name_pkey PRIMARY KEY (pool_name_id);


--
-- Name: pool pool_pkey; Type: CONSTRAINT; Schema: public; Owner: kura
--

ALTER TABLE ONLY public.pool
    ADD CONSTRAINT pool_pkey PRIMARY KEY (id);


--
-- Name: pool_post pool_post_pkey; Type: CONSTRAINT; Schema: public; Owner: kura
--

ALTER TABLE ONLY public.pool_post
    ADD CONSTRAINT pool_post_pkey PRIMARY KEY (pool_id, post_id);


--
-- Name: pool_state pool_state_pkey; Type: CONSTRAINT; Schema: public; Owner: kura
--

ALTER TABLE ONLY public.pool_state
    ADD CONSTRAINT pool_state_pkey PRIMARY KEY (pool_id);


--
-- Name: pool_url pool_url_pkey; Type: CONSTRAINT; Schema: public; Owner: kura
--

ALTER TABLE ONLY public.pool_url
    ADD CONSTRAINT pool_url_pkey PRIMARY KEY (pool_id, url);


--
-- Name: pool_url pool_url_url_unique; Type: CONSTRAINT; Schema: public; Owner: kura
--

ALTER TABLE ONLY public.pool_url
    ADD CONSTRAINT pool_url_url_unique UNIQUE (url);


--
-- Name: post_censor_review post_censor_review_pkey; Type: CONSTRAINT; Schema: public; Owner: kura
--

ALTER TABLE ONLY public.post_censor_review
    ADD CONSTRAINT post_censor_review_pkey PRIMARY KEY (id);


--
-- Name: post_favorite post_favorite_pkey; Type: CONSTRAINT; Schema: public; Owner: kura
--

ALTER TABLE ONLY public.post_favorite
    ADD CONSTRAINT post_favorite_pkey PRIMARY KEY (post_id, user_id);


--
-- Name: post_feature post_feature_pkey; Type: CONSTRAINT; Schema: public; Owner: kura
--

ALTER TABLE ONLY public.post_feature
    ADD CONSTRAINT post_feature_pkey PRIMARY KEY (id);


--
-- Name: post_listing post_listing_pkey; Type: CONSTRAINT; Schema: public; Owner: kura
--

ALTER TABLE ONLY public.post_listing
    ADD CONSTRAINT post_listing_pkey PRIMARY KEY (post_id) INCLUDE (file_size);


--
-- Name: post_note post_note_pkey; Type: CONSTRAINT; Schema: public; Owner: kura
--

ALTER TABLE ONLY public.post_note
    ADD CONSTRAINT post_note_pkey PRIMARY KEY (id);


--
-- Name: post post_pkey; Type: CONSTRAINT; Schema: public; Owner: kura
--

ALTER TABLE ONLY public.post
    ADD CONSTRAINT post_pkey PRIMARY KEY (id);


--
-- Name: post_rendition post_rendition_pkey; Type: CONSTRAINT; Schema: public; Owner: kura
--

ALTER TABLE ONLY public.post_rendition
    ADD CONSTRAINT post_rendition_pkey PRIMARY KEY (post_id, kind);


--
-- Name: post_score post_score_pkey; Type: CONSTRAINT; Schema: public; Owner: kura
--

ALTER TABLE ONLY public.post_score
    ADD CONSTRAINT post_score_pkey PRIMARY KEY (post_id, user_id);


--
-- Name: post_tag_audit post_tag_audit_pkey; Type: CONSTRAINT; Schema: public; Owner: kura
--

ALTER TABLE ONLY public.post_tag_audit
    ADD CONSTRAINT post_tag_audit_pkey PRIMARY KEY (id);


--
-- Name: post_tag_pin post_tag_pin_pkey; Type: CONSTRAINT; Schema: public; Owner: kura
--

ALTER TABLE ONLY public.post_tag_pin
    ADD CONSTRAINT post_tag_pin_pkey PRIMARY KEY (post_id, tag_id);


--
-- Name: post_tag post_tag_pkey; Type: CONSTRAINT; Schema: public; Owner: kura
--

ALTER TABLE ONLY public.post_tag
    ADD CONSTRAINT post_tag_pkey PRIMARY KEY (post_id, tag_id);


--
-- Name: post_tagging_work post_tagging_work_pkey; Type: CONSTRAINT; Schema: public; Owner: kura_reconciler
--

ALTER TABLE ONLY public.post_tagging_work
    ADD CONSTRAINT post_tagging_work_pkey PRIMARY KEY (post_id);


--
-- Name: post_variant post_variant_pkey; Type: CONSTRAINT; Schema: public; Owner: kura
--

ALTER TABLE ONLY public.post_variant
    ADD CONSTRAINT post_variant_pkey PRIMARY KEY (id);


--
-- Name: rendition_object rendition_object_pkey; Type: CONSTRAINT; Schema: public; Owner: kura
--

ALTER TABLE ONLY public.rendition_object
    ADD CONSTRAINT rendition_object_pkey PRIMARY KEY (sha256);


--
-- Name: tag_category tag_category_pkey; Type: CONSTRAINT; Schema: public; Owner: kura
--

ALTER TABLE ONLY public.tag_category
    ADD CONSTRAINT tag_category_pkey PRIMARY KEY (id);


--
-- Name: tag_implication tag_implication_pkey; Type: CONSTRAINT; Schema: public; Owner: kura
--

ALTER TABLE ONLY public.tag_implication
    ADD CONSTRAINT tag_implication_pkey PRIMARY KEY (parent_id, child_id);


--
-- Name: tag_link tag_link_pkey; Type: CONSTRAINT; Schema: public; Owner: kura
--

ALTER TABLE ONLY public.tag_link
    ADD CONSTRAINT tag_link_pkey PRIMARY KEY (id);


--
-- Name: tag_localization tag_localization_pkey; Type: CONSTRAINT; Schema: public; Owner: kura
--

ALTER TABLE ONLY public.tag_localization
    ADD CONSTRAINT tag_localization_pkey PRIMARY KEY (tag_id, locale);


--
-- Name: tag_name tag_name_name_key; Type: CONSTRAINT; Schema: public; Owner: kura
--

ALTER TABLE ONLY public.tag_name
    ADD CONSTRAINT tag_name_name_key UNIQUE (name);


--
-- Name: tag_name tag_name_pkey; Type: CONSTRAINT; Schema: public; Owner: kura
--

ALTER TABLE ONLY public.tag_name
    ADD CONSTRAINT tag_name_pkey PRIMARY KEY (tag_name_id);


--
-- Name: tag_pin_inventory tag_pin_inventory_pkey; Type: CONSTRAINT; Schema: public; Owner: kura
--

ALTER TABLE ONLY public.tag_pin_inventory
    ADD CONSTRAINT tag_pin_inventory_pkey PRIMARY KEY (id);


--
-- Name: tag tag_pkey; Type: CONSTRAINT; Schema: public; Owner: kura
--

ALTER TABLE ONLY public.tag
    ADD CONSTRAINT tag_pkey PRIMARY KEY (id);


--
-- Name: tag_quarantine tag_quarantine_pkey; Type: CONSTRAINT; Schema: public; Owner: kura
--

ALTER TABLE ONLY public.tag_quarantine
    ADD CONSTRAINT tag_quarantine_pkey PRIMARY KEY (id);


--
-- Name: tag_quarantine tag_quarantine_tag_id_key; Type: CONSTRAINT; Schema: public; Owner: kura
--

ALTER TABLE ONLY public.tag_quarantine
    ADD CONSTRAINT tag_quarantine_tag_id_key UNIQUE (tag_id);


--
-- Name: tag_relation tag_relation_pkey; Type: CONSTRAINT; Schema: public; Owner: kura
--

ALTER TABLE ONLY public.tag_relation
    ADD CONSTRAINT tag_relation_pkey PRIMARY KEY (a_tag_id, b_tag_id);


--
-- Name: tag_suggestion tag_suggestion_pkey; Type: CONSTRAINT; Schema: public; Owner: kura
--

ALTER TABLE ONLY public.tag_suggestion
    ADD CONSTRAINT tag_suggestion_pkey PRIMARY KEY (parent_id, child_id);


--
-- Name: tagger_allowed_executor tagger_allowed_executor_pkey; Type: CONSTRAINT; Schema: public; Owner: kura
--

ALTER TABLE ONLY public.tagger_allowed_executor
    ADD CONSTRAINT tagger_allowed_executor_pkey PRIMARY KEY (db_role, scope);


--
-- Name: tagger_artifact tagger_artifact_pkey; Type: CONSTRAINT; Schema: public; Owner: kura
--

ALTER TABLE ONLY public.tagger_artifact
    ADD CONSTRAINT tagger_artifact_pkey PRIMARY KEY (content_sha256);


--
-- Name: tagger_auto_exclude_pool tagger_auto_exclude_pool_pkey; Type: CONSTRAINT; Schema: public; Owner: kura
--

ALTER TABLE ONLY public.tagger_auto_exclude_pool
    ADD CONSTRAINT tagger_auto_exclude_pool_pkey PRIMARY KEY (pool_id);


--
-- Name: tagger_candidate_disposition tagger_candidate_disposition_pkey; Type: CONSTRAINT; Schema: public; Owner: kura
--

ALTER TABLE ONLY public.tagger_candidate_disposition
    ADD CONSTRAINT tagger_candidate_disposition_pkey PRIMARY KEY (seq);


--
-- Name: tagger_candidate_event tagger_candidate_event_pkey; Type: CONSTRAINT; Schema: public; Owner: kura
--

ALTER TABLE ONLY public.tagger_candidate_event
    ADD CONSTRAINT tagger_candidate_event_pkey PRIMARY KEY (seq);


--
-- Name: tagger_config tagger_config_pkey; Type: CONSTRAINT; Schema: public; Owner: kura
--

ALTER TABLE ONLY public.tagger_config
    ADD CONSTRAINT tagger_config_pkey PRIMARY KEY (key);


--
-- Name: tagger_expected_worker tagger_expected_worker_pkey; Type: CONSTRAINT; Schema: public; Owner: kura
--

ALTER TABLE ONLY public.tagger_expected_worker
    ADD CONSTRAINT tagger_expected_worker_pkey PRIMARY KEY (host);


--
-- Name: tagger_gt_label tagger_gt_label_pkey; Type: CONSTRAINT; Schema: public; Owner: kura
--

ALTER TABLE ONLY public.tagger_gt_label
    ADD CONSTRAINT tagger_gt_label_pkey PRIMARY KEY (id);


--
-- Name: tagger_gt_label tagger_gt_label_tag_id_post_id_key; Type: CONSTRAINT; Schema: public; Owner: kura
--

ALTER TABLE ONLY public.tagger_gt_label
    ADD CONSTRAINT tagger_gt_label_tag_id_post_id_key UNIQUE (tag_id, post_id);


--
-- Name: tagger_head_candidate tagger_head_candidate_pkey; Type: CONSTRAINT; Schema: public; Owner: kura
--

ALTER TABLE ONLY public.tagger_head_candidate
    ADD CONSTRAINT tagger_head_candidate_pkey PRIMARY KEY (id);


--
-- Name: tagger_head_policy tagger_head_policy_pkey; Type: CONSTRAINT; Schema: public; Owner: kura
--

ALTER TABLE ONLY public.tagger_head_policy
    ADD CONSTRAINT tagger_head_policy_pkey PRIMARY KEY (tag_id, patch_version);


--
-- Name: tagger_lifecycle_event tagger_lifecycle_event_pkey; Type: CONSTRAINT; Schema: public; Owner: kura
--

ALTER TABLE ONLY public.tagger_lifecycle_event
    ADD CONSTRAINT tagger_lifecycle_event_pkey PRIMARY KEY (id);


--
-- Name: tagger_lifecycle_lock tagger_lifecycle_lock_pkey; Type: CONSTRAINT; Schema: public; Owner: kura
--

ALTER TABLE ONLY public.tagger_lifecycle_lock
    ADD CONSTRAINT tagger_lifecycle_lock_pkey PRIMARY KEY (scope);


--
-- Name: tagger_node_head tagger_node_head_pkey; Type: CONSTRAINT; Schema: public; Owner: kura
--

ALTER TABLE ONLY public.tagger_node_head
    ADD CONSTRAINT tagger_node_head_pkey PRIMARY KEY (host, tag_id);


--
-- Name: tagger_node_state tagger_node_state_pkey; Type: CONSTRAINT; Schema: public; Owner: kura
--

ALTER TABLE ONLY public.tagger_node_state
    ADD CONSTRAINT tagger_node_state_pkey PRIMARY KEY (host);


--
-- Name: tagger_operation tagger_operation_pkey; Type: CONSTRAINT; Schema: public; Owner: kura
--

ALTER TABLE ONLY public.tagger_operation
    ADD CONSTRAINT tagger_operation_pkey PRIMARY KEY (idempotency_key);


--
-- Name: tagger_operation tagger_operation_scope_target_id_operation_type_key; Type: CONSTRAINT; Schema: public; Owner: kura
--

ALTER TABLE ONLY public.tagger_operation
    ADD CONSTRAINT tagger_operation_scope_target_id_operation_type_key UNIQUE (scope, target_id, operation_type);


--
-- Name: tagger_patch tagger_patch_pkey; Type: CONSTRAINT; Schema: public; Owner: kura
--

ALTER TABLE ONLY public.tagger_patch
    ADD CONSTRAINT tagger_patch_pkey PRIMARY KEY (id);


--
-- Name: tagger_producer_threshold tagger_producer_threshold_pkey; Type: CONSTRAINT; Schema: public; Owner: kura
--

ALTER TABLE ONLY public.tagger_producer_threshold
    ADD CONSTRAINT tagger_producer_threshold_pkey PRIMARY KEY (producer, category);


--
-- Name: tagger_reconciler_state tagger_reconciler_state_pkey; Type: CONSTRAINT; Schema: public; Owner: kura
--

ALTER TABLE ONLY public.tagger_reconciler_state
    ADD CONSTRAINT tagger_reconciler_state_pkey PRIMARY KEY (id);


--
-- Name: tagger_registration_request tagger_registration_request_measurement_key; Type: CONSTRAINT; Schema: public; Owner: kura
--

ALTER TABLE ONLY public.tagger_registration_request
    ADD CONSTRAINT tagger_registration_request_measurement_key UNIQUE (tag_id, artifact_sha256, lifecycle_event_id);


--
-- Name: tagger_registration_request tagger_registration_request_pkey; Type: CONSTRAINT; Schema: public; Owner: kura
--

ALTER TABLE ONLY public.tagger_registration_request
    ADD CONSTRAINT tagger_registration_request_pkey PRIMARY KEY (id);


--
-- Name: tagger_release_active tagger_release_active_pkey; Type: CONSTRAINT; Schema: public; Owner: kura
--

ALTER TABLE ONLY public.tagger_release_active
    ADD CONSTRAINT tagger_release_active_pkey PRIMARY KEY (id);


--
-- Name: tagger_release_admissibility tagger_release_admissibility_pkey; Type: CONSTRAINT; Schema: public; Owner: kura
--

ALTER TABLE ONLY public.tagger_release_admissibility
    ADD CONSTRAINT tagger_release_admissibility_pkey PRIMARY KEY (seq);


--
-- Name: tagger_release_component tagger_release_component_pkey; Type: CONSTRAINT; Schema: public; Owner: kura
--

ALTER TABLE ONLY public.tagger_release_component
    ADD CONSTRAINT tagger_release_component_pkey PRIMARY KEY (release_digest, role, ordinal);


--
-- Name: tagger_release_manifest tagger_release_manifest_pkey; Type: CONSTRAINT; Schema: public; Owner: kura
--

ALTER TABLE ONLY public.tagger_release_manifest
    ADD CONSTRAINT tagger_release_manifest_pkey PRIMARY KEY (release_digest);


--
-- Name: tagger_release tagger_release_pkey; Type: CONSTRAINT; Schema: public; Owner: kura
--

ALTER TABLE ONLY public.tagger_release
    ADD CONSTRAINT tagger_release_pkey PRIMARY KEY (id);


--
-- Name: tagger_stage tagger_stage_pkey; Type: CONSTRAINT; Schema: public; Owner: kura
--

ALTER TABLE ONLY public.tagger_stage
    ADD CONSTRAINT tagger_stage_pkey PRIMARY KEY (stage);


--
-- Name: tagger_stage tagger_stage_rank_key; Type: CONSTRAINT; Schema: public; Owner: kura
--

ALTER TABLE ONLY public.tagger_stage
    ADD CONSTRAINT tagger_stage_rank_key UNIQUE (rank);


--
-- Name: tagger_stage_transition tagger_stage_transition_from_stage_to_stage_key; Type: CONSTRAINT; Schema: public; Owner: kura
--

ALTER TABLE ONLY public.tagger_stage_transition
    ADD CONSTRAINT tagger_stage_transition_from_stage_to_stage_key UNIQUE (from_stage, to_stage);


--
-- Name: tagger_stage_transition tagger_stage_transition_pkey; Type: CONSTRAINT; Schema: public; Owner: kura
--

ALTER TABLE ONLY public.tagger_stage_transition
    ADD CONSTRAINT tagger_stage_transition_pkey PRIMARY KEY (id);


--
-- Name: tagger_sweep_run tagger_sweep_run_pkey; Type: CONSTRAINT; Schema: public; Owner: kura
--

ALTER TABLE ONLY public.tagger_sweep_run
    ADD CONSTRAINT tagger_sweep_run_pkey PRIMARY KEY (id);


--
-- Name: tagger_tag_condition tagger_tag_condition_pkey; Type: CONSTRAINT; Schema: public; Owner: kura
--

ALTER TABLE ONLY public.tagger_tag_condition
    ADD CONSTRAINT tagger_tag_condition_pkey PRIMARY KEY (name);


--
-- Name: tagger_tag_exclusion tagger_tag_exclusion_pkey; Type: CONSTRAINT; Schema: public; Owner: kura
--

ALTER TABLE ONLY public.tagger_tag_exclusion
    ADD CONSTRAINT tagger_tag_exclusion_pkey PRIMARY KEY (tag_a_id, tag_b_id);


--
-- Name: tagger_tag_rule tagger_tag_rule_pkey; Type: CONSTRAINT; Schema: public; Owner: kura
--

ALTER TABLE ONLY public.tagger_tag_rule
    ADD CONSTRAINT tagger_tag_rule_pkey PRIMARY KEY (producer, tag_id);


--
-- Name: tagger_worker_heartbeat tagger_worker_heartbeat_pkey; Type: CONSTRAINT; Schema: public; Owner: kura
--

ALTER TABLE ONLY public.tagger_worker_heartbeat
    ADD CONSTRAINT tagger_worker_heartbeat_pkey PRIMARY KEY (worker_id);


--
-- Name: censor_annotation uq_censor_annotation_mutation; Type: CONSTRAINT; Schema: public; Owner: kura
--

ALTER TABLE ONLY public.censor_annotation
    ADD CONSTRAINT uq_censor_annotation_mutation UNIQUE (client_mutation_uuid);


--
-- Name: post_censor_review uq_post_censor_review_post; Type: CONSTRAINT; Schema: public; Owner: kura
--

ALTER TABLE ONLY public.post_censor_review
    ADD CONSTRAINT uq_post_censor_review_post UNIQUE (post_id);


--
-- Name: post_variant uq_post_variant_post_kind; Type: CONSTRAINT; Schema: public; Owner: kura
--

ALTER TABLE ONLY public.post_variant
    ADD CONSTRAINT uq_post_variant_post_kind UNIQUE (post_id, kind);


--
-- Name: tag_link uq_tag_link_tag_url; Type: CONSTRAINT; Schema: public; Owner: kura
--

ALTER TABLE ONLY public.tag_link
    ADD CONSTRAINT uq_tag_link_tag_url UNIQUE (tag_id, url);


--
-- Name: tag_pin_inventory uq_tag_pin_inventory_post_tag_source; Type: CONSTRAINT; Schema: public; Owner: kura
--

ALTER TABLE ONLY public.tag_pin_inventory
    ADD CONSTRAINT uq_tag_pin_inventory_post_tag_source UNIQUE (post_id, tag, source);


--
-- Name: user uq_user_name; Type: CONSTRAINT; Schema: public; Owner: kura
--

ALTER TABLE ONLY public."user"
    ADD CONSTRAINT uq_user_name UNIQUE (name);


--
-- Name: user user_pkey; Type: CONSTRAINT; Schema: public; Owner: kura
--

ALTER TABLE ONLY public."user"
    ADD CONSTRAINT user_pkey PRIMARY KEY (id);


--
-- Name: user_token user_token_pkey; Type: CONSTRAINT; Schema: public; Owner: kura
--

ALTER TABLE ONLY public.user_token
    ADD CONSTRAINT user_token_pkey PRIMARY KEY (id);


--
-- Name: lora_post ux_lora_post; Type: CONSTRAINT; Schema: public; Owner: kura
--

ALTER TABLE ONLY public.lora_post
    ADD CONSTRAINT ux_lora_post UNIQUE (lora_id, post_id);


--
-- Name: tagger_patch ux_tagger_patch_tag_id_version; Type: CONSTRAINT; Schema: public; Owner: kura
--

ALTER TABLE ONLY public.tagger_patch
    ADD CONSTRAINT ux_tagger_patch_tag_id_version UNIQUE (tag_id, version);


--
-- Name: tagger_release ux_tagger_release_identity; Type: CONSTRAINT; Schema: public; Owner: kura
--

ALTER TABLE ONLY public.tagger_release
    ADD CONSTRAINT ux_tagger_release_identity UNIQUE (commit_sha, patch_set_digest, model_sha256);


--
-- Name: claim_by_actor; Type: INDEX; Schema: claim_storage; Owner: kura
--

CREATE INDEX claim_by_actor ON claim_storage.claim USING btree (post_id, actor_key);


--
-- Name: entry_asset_collection; Type: INDEX; Schema: collector_next; Owner: kura
--

CREATE INDEX entry_asset_collection ON collector_next.collection_entry USING btree (asset_id, collection_id, "position");


--
-- Name: import_operation_owner_recent; Type: INDEX; Schema: collector_next; Owner: kura
--

CREATE INDEX import_operation_owner_recent ON collector_next.import_operation USING btree (owner_id, created_at DESC, request_key DESC);


--
-- Name: label_history_asset; Type: INDEX; Schema: collector_next; Owner: kura
--

CREATE INDEX label_history_asset ON collector_next.label_observation USING btree (owner_id, asset_id, id DESC);


--
-- Name: label_history_operation; Type: INDEX; Schema: collector_next; Owner: kura
--

CREATE INDEX label_history_operation ON collector_next.label_observation USING btree (owner_id, request_key);


--
-- Name: label_positive_asset; Type: INDEX; Schema: collector_next; Owner: kura
--

CREATE INDEX label_positive_asset ON collector_next.label_head USING btree (owner_id, asset_id, tag_id) WHERE present;


--
-- Name: label_positive_search; Type: INDEX; Schema: collector_next; Owner: kura
--

CREATE INDEX label_positive_search ON collector_next.label_head USING btree (owner_id, tag_id, asset_id DESC) WHERE present;


--
-- Name: media_image_cursor; Type: INDEX; Schema: collector_next; Owner: kura
--

CREATE INDEX media_image_cursor ON collector_next.media_current USING btree (owner_id, asset_id DESC) WHERE (media_type ~~ 'image/%'::text);


--
-- Name: media_owner_cursor; Type: INDEX; Schema: collector_next; Owner: kura
--

CREATE INDEX media_owner_cursor ON collector_next.media_current USING btree (owner_id, asset_id DESC);


--
-- Name: media_video_cursor; Type: INDEX; Schema: collector_next; Owner: kura
--

CREATE INDEX media_video_cursor ON collector_next.media_current USING btree (owner_id, asset_id DESC) WHERE (media_type ~~ 'video/%'::text);


--
-- Name: receipt_entry_once; Type: INDEX; Schema: collector_next; Owner: kura
--

CREATE UNIQUE INDEX receipt_entry_once ON collector_next.receipt USING btree (entry_id) WHERE (entry_id IS NOT NULL);


--
-- Name: tag_owner_cursor; Type: INDEX; Schema: collector_next; Owner: kura
--

CREATE INDEX tag_owner_cursor ON collector_next.tag USING btree (owner_id, id DESC);


--
-- Name: censor_pair_confirmed_idx; Type: INDEX; Schema: public; Owner: kura
--

CREATE INDEX censor_pair_confirmed_idx ON public.censor_pair USING btree (confirmed);


--
-- Name: censor_pair_unc_idx; Type: INDEX; Schema: public; Owner: kura
--

CREATE INDEX censor_pair_unc_idx ON public.censor_pair USING btree (uncensored_post_id);


--
-- Name: cps_status_idx; Type: INDEX; Schema: public; Owner: kura
--

CREATE INDEX cps_status_idx ON public.censor_pair_search USING btree (status);


--
-- Name: human_login_key_user; Type: INDEX; Schema: public; Owner: kura
--

CREATE INDEX human_login_key_user ON public.human_login_key USING btree (user_id);


--
-- Name: ix_censor_annotation_live; Type: INDEX; Schema: public; Owner: kura
--

CREATE INDEX ix_censor_annotation_live ON public.censor_annotation USING btree (post_id) WHERE (deleted_at IS NULL);


--
-- Name: ix_censor_annotation_post_id; Type: INDEX; Schema: public; Owner: kura
--

CREATE INDEX ix_censor_annotation_post_id ON public.censor_annotation USING btree (post_id);


--
-- Name: ix_comment_post_id; Type: INDEX; Schema: public; Owner: kura
--

CREATE INDEX ix_comment_post_id ON public.comment USING btree (post_id);


--
-- Name: ix_comment_score_user_id; Type: INDEX; Schema: public; Owner: kura
--

CREATE INDEX ix_comment_score_user_id ON public.comment_score USING btree (user_id);


--
-- Name: ix_comment_user_id; Type: INDEX; Schema: public; Owner: kura
--

CREATE INDEX ix_comment_user_id ON public.comment USING btree (user_id);


--
-- Name: ix_lora_asset_status; Type: INDEX; Schema: public; Owner: kura
--

CREATE INDEX ix_lora_asset_status ON public.lora_asset USING btree (status);


--
-- Name: ix_lora_concept_lora; Type: INDEX; Schema: public; Owner: kura
--

CREATE INDEX ix_lora_concept_lora ON public.lora_concept USING btree (lora_id);


--
-- Name: ix_lora_model_status; Type: INDEX; Schema: public; Owner: kura
--

CREATE INDEX ix_lora_model_status ON public.lora_model_base USING btree (status);


--
-- Name: ix_lora_post_concept; Type: INDEX; Schema: public; Owner: kura
--

CREATE INDEX ix_lora_post_concept ON public.lora_post USING btree (concept_id);


--
-- Name: ix_lora_post_lora; Type: INDEX; Schema: public; Owner: kura
--

CREATE INDEX ix_lora_post_lora ON public.lora_post USING btree (lora_id);


--
-- Name: ix_lora_post_post; Type: INDEX; Schema: public; Owner: kura
--

CREATE INDEX ix_lora_post_post ON public.lora_post USING btree (post_id);


--
-- Name: ix_lora_training_run_asset; Type: INDEX; Schema: public; Owner: kura
--

CREATE INDEX ix_lora_training_run_asset ON public.lora_training_run USING btree (asset_id);


--
-- Name: ix_lora_training_run_state; Type: INDEX; Schema: public; Owner: kura
--

CREATE INDEX ix_lora_training_run_state ON public.lora_training_run USING btree (state);


--
-- Name: ix_pool_cover_post_id; Type: INDEX; Schema: public; Owner: kura
--

CREATE INDEX ix_pool_cover_post_id ON public.pool USING btree (cover_post_id) WHERE (cover_post_id IS NOT NULL);


--
-- Name: ix_pool_name_ord; Type: INDEX; Schema: public; Owner: kura
--

CREATE INDEX ix_pool_name_ord ON public.pool_name USING btree (ord);


--
-- Name: ix_pool_post_ord; Type: INDEX; Schema: public; Owner: kura
--

CREATE INDEX ix_pool_post_ord ON public.pool_post USING btree (ord);


--
-- Name: ix_pool_post_pool_ord; Type: INDEX; Schema: public; Owner: kura
--

CREATE INDEX ix_pool_post_pool_ord ON public.pool_post USING btree (pool_id, ord);


--
-- Name: ix_pool_post_post_id; Type: INDEX; Schema: public; Owner: kura
--

CREATE INDEX ix_pool_post_post_id ON public.pool_post USING btree (post_id);


--
-- Name: ix_pool_url_pool_id; Type: INDEX; Schema: public; Owner: kura
--

CREATE INDEX ix_pool_url_pool_id ON public.pool_url USING btree (pool_id);


--
-- Name: ix_pool_verify_status; Type: INDEX; Schema: public; Owner: kura
--

CREATE INDEX ix_pool_verify_status ON public.pool USING btree (verify_status) WHERE (verify_status IS NOT NULL);


--
-- Name: ix_post_censor_review_post_id; Type: INDEX; Schema: public; Owner: kura
--

CREATE INDEX ix_post_censor_review_post_id ON public.post_censor_review USING btree (post_id);


--
-- Name: ix_post_checksum_md5; Type: INDEX; Schema: public; Owner: kura
--

CREATE INDEX ix_post_checksum_md5 ON public.post USING btree (checksum_md5);


--
-- Name: ix_post_creation_at_utc; Type: INDEX; Schema: public; Owner: kura
--

CREATE INDEX ix_post_creation_at_utc ON public.post USING btree ((creation_time AT TIME ZONE 'UTC'::text));


--
-- Name: ix_post_creation_time; Type: INDEX; Schema: public; Owner: kura
--

CREATE INDEX ix_post_creation_time ON public.post USING btree (creation_time);


--
-- Name: ix_post_duplicate_of; Type: INDEX; Schema: public; Owner: kura
--

CREATE INDEX ix_post_duplicate_of ON public.post USING btree (duplicate_of) WHERE (duplicate_of IS NOT NULL);


--
-- Name: ix_post_favorite_post_id; Type: INDEX; Schema: public; Owner: kura
--

CREATE INDEX ix_post_favorite_post_id ON public.post_favorite USING btree (post_id);


--
-- Name: ix_post_favorite_user_id; Type: INDEX; Schema: public; Owner: kura
--

CREATE INDEX ix_post_favorite_user_id ON public.post_favorite USING btree (user_id);


--
-- Name: ix_post_feature_post_id; Type: INDEX; Schema: public; Owner: kura
--

CREATE INDEX ix_post_feature_post_id ON public.post_feature USING btree (post_id);


--
-- Name: ix_post_feature_user_id; Type: INDEX; Schema: public; Owner: kura
--

CREATE INDEX ix_post_feature_user_id ON public.post_feature USING btree (user_id);


--
-- Name: ix_post_listing_safe; Type: INDEX; Schema: public; Owner: kura
--

CREATE INDEX ix_post_listing_safe ON public.post_listing USING btree (post_id) INCLUDE (file_size) WHERE ((safety)::text = 'safe'::text);


--
-- Name: ix_post_needs_fingerprint; Type: INDEX; Schema: public; Owner: kura
--

CREATE INDEX ix_post_needs_fingerprint ON public.post USING btree (id) WHERE ((perceptual_hash IS NULL) AND ((status IS NULL) OR (status <> ALL (ARRAY['ok'::text, 'exact_dup'::text, 'near_dup'::text, 'corrupt'::text, 'lost'::text]))) AND (lower((type)::text) = ANY (ARRAY['image'::text, 'animation'::text])));


--
-- Name: ix_post_not_ok_not_terminal; Type: INDEX; Schema: public; Owner: kura
--

CREATE INDEX ix_post_not_ok_not_terminal ON public.post USING btree (creation_time) WHERE ((status IS DISTINCT FROM 'ok'::text) AND ((status IS NULL) OR (status <> ALL (ARRAY['exact_dup'::text, 'near_dup'::text, 'corrupt'::text, 'lost'::text]))));


--
-- Name: ix_post_not_ok_not_terminal_id; Type: INDEX; Schema: public; Owner: kura
--

CREATE INDEX ix_post_not_ok_not_terminal_id ON public.post USING btree (id) WHERE ((status IS DISTINCT FROM 'ok'::text) AND ((status IS NULL) OR (status <> ALL (ARRAY['exact_dup'::text, 'near_dup'::text, 'corrupt'::text, 'lost'::text]))));


--
-- Name: ix_post_note_post_id; Type: INDEX; Schema: public; Owner: kura
--

CREATE INDEX ix_post_note_post_id ON public.post_note USING btree (post_id);


--
-- Name: ix_post_ok_edited_order; Type: INDEX; Schema: public; Owner: kura
--

CREATE INDEX ix_post_ok_edited_order ON public.post USING btree (COALESCE(last_edit_time, creation_time) DESC, id DESC) WHERE (status = 'ok'::text);


--
-- Name: ix_post_ok_file_size; Type: INDEX; Schema: public; Owner: kura
--

CREATE INDEX ix_post_ok_file_size ON public.post USING btree (id) INCLUDE (file_size) WHERE (status = 'ok'::text);


--
-- Name: ix_post_perceptual_hash_bands_gin; Type: INDEX; Schema: public; Owner: kura
--

CREATE INDEX ix_post_perceptual_hash_bands_gin ON public.post USING gin (perceptual_hash_bands);


--
-- Name: ix_post_primary_pool; Type: INDEX; Schema: public; Owner: kura
--

CREATE INDEX ix_post_primary_pool ON public.post USING btree (primary_pool_id) WHERE (primary_pool_id IS NOT NULL);


--
-- Name: ix_post_rendition_sha256; Type: INDEX; Schema: public; Owner: kura
--

CREATE INDEX ix_post_rendition_sha256 ON public.post_rendition USING btree (sha256);


--
-- Name: ix_post_safe_ok_file_size; Type: INDEX; Schema: public; Owner: kura
--

CREATE INDEX ix_post_safe_ok_file_size ON public.post USING btree (id) INCLUDE (file_size) WHERE ((status = 'ok'::text) AND ((safety)::text = 'safe'::text));


--
-- Name: ix_post_score_post_id; Type: INDEX; Schema: public; Owner: kura
--

CREATE INDEX ix_post_score_post_id ON public.post_score USING btree (post_id);


--
-- Name: ix_post_score_user_id; Type: INDEX; Schema: public; Owner: kura
--

CREATE INDEX ix_post_score_user_id ON public.post_score USING btree (user_id);


--
-- Name: ix_post_sha256; Type: INDEX; Schema: public; Owner: kura
--

CREATE INDEX ix_post_sha256 ON public.post USING btree (sha256);


--
-- Name: ix_post_source; Type: INDEX; Schema: public; Owner: kura
--

CREATE INDEX ix_post_source ON public.post USING btree (source) WHERE ((source IS NOT NULL) AND ((source)::text <> ''::text));


--
-- Name: ix_post_source_trgm; Type: INDEX; Schema: public; Owner: kura
--

CREATE INDEX ix_post_source_trgm ON public.post USING gin (source public.gin_trgm_ops) WHERE (status = 'ok'::text);


--
-- Name: ix_post_status; Type: INDEX; Schema: public; Owner: kura
--

CREATE INDEX ix_post_status ON public.post USING btree (status) WHERE (status IS NOT NULL);


--
-- Name: ix_post_status_null; Type: INDEX; Schema: public; Owner: kura
--

CREATE INDEX ix_post_status_null ON public.post USING btree (id) WHERE (status IS NULL);


--
-- Name: ix_post_tag_audit_created_at; Type: INDEX; Schema: public; Owner: kura
--

CREATE INDEX ix_post_tag_audit_created_at ON public.post_tag_audit USING btree (created_at DESC);


--
-- Name: ix_post_tag_audit_post_id; Type: INDEX; Schema: public; Owner: kura
--

CREATE INDEX ix_post_tag_audit_post_id ON public.post_tag_audit USING btree (post_id, created_at DESC, id DESC);


--
-- Name: ix_post_tag_audit_release_id; Type: INDEX; Schema: public; Owner: kura
--

CREATE INDEX ix_post_tag_audit_release_id ON public.post_tag_audit USING btree (release_id) WHERE (release_id IS NOT NULL);


--
-- Name: ix_post_tag_audit_release_post; Type: INDEX; Schema: public; Owner: kura
--

CREATE INDEX ix_post_tag_audit_release_post ON public.post_tag_audit USING btree (release_id, post_id) WHERE (release_id IS NOT NULL);


--
-- Name: ix_post_tag_audit_release_post_created; Type: INDEX; Schema: public; Owner: kura
--

CREATE INDEX ix_post_tag_audit_release_post_created ON public.post_tag_audit USING btree (release_id, post_id, created_at) WHERE (release_id IS NOT NULL);


--
-- Name: ix_post_tag_pin_tag_id; Type: INDEX; Schema: public; Owner: kura
--

CREATE INDEX ix_post_tag_pin_tag_id ON public.post_tag_pin USING btree (tag_id);


--
-- Name: ix_post_tag_tag_post; Type: INDEX; Schema: public; Owner: kura
--

CREATE INDEX ix_post_tag_tag_post ON public.post_tag USING btree (tag_id, post_id);


--
-- Name: ix_post_user_id; Type: INDEX; Schema: public; Owner: kura
--

CREATE INDEX ix_post_user_id ON public.post USING btree (user_id);


--
-- Name: ix_post_variant_post_id; Type: INDEX; Schema: public; Owner: kura
--

CREATE INDEX ix_post_variant_post_id ON public.post_variant USING btree (post_id);


--
-- Name: ix_tag_category_id; Type: INDEX; Schema: public; Owner: kura
--

CREATE INDEX ix_tag_category_id ON public.tag USING btree (category_id);


--
-- Name: ix_tag_implication_child_id; Type: INDEX; Schema: public; Owner: kura
--

CREATE INDEX ix_tag_implication_child_id ON public.tag_implication USING btree (child_id);


--
-- Name: ix_tag_implication_parent_id; Type: INDEX; Schema: public; Owner: kura
--

CREATE INDEX ix_tag_implication_parent_id ON public.tag_implication USING btree (parent_id);


--
-- Name: ix_tag_link_platform; Type: INDEX; Schema: public; Owner: kura
--

CREATE INDEX ix_tag_link_platform ON public.tag_link USING btree (platform);


--
-- Name: ix_tag_link_pool_id; Type: INDEX; Schema: public; Owner: kura
--

CREATE INDEX ix_tag_link_pool_id ON public.tag_link USING btree (pool_id);


--
-- Name: ix_tag_link_tag; Type: INDEX; Schema: public; Owner: kura
--

CREATE INDEX ix_tag_link_tag ON public.tag_link USING btree (tag_id);


--
-- Name: ix_tag_localization_name_trgm; Type: INDEX; Schema: public; Owner: kura
--

CREATE INDEX ix_tag_localization_name_trgm ON public.tag_localization USING gin (name public.gin_trgm_ops);


--
-- Name: ix_tag_name_lower; Type: INDEX; Schema: public; Owner: kura
--

CREATE INDEX ix_tag_name_lower ON public.tag_name USING btree (lower((name)::text));


--
-- Name: ix_tag_name_ord; Type: INDEX; Schema: public; Owner: kura
--

CREATE INDEX ix_tag_name_ord ON public.tag_name USING btree (ord);


--
-- Name: ix_tag_name_tag_id; Type: INDEX; Schema: public; Owner: kura
--

CREATE INDEX ix_tag_name_tag_id ON public.tag_name USING btree (tag_id);


--
-- Name: ix_tag_name_trgm; Type: INDEX; Schema: public; Owner: kura
--

CREATE INDEX ix_tag_name_trgm ON public.tag_name USING gin (name public.gin_trgm_ops);


--
-- Name: ix_tag_pin_inventory_coverage; Type: INDEX; Schema: public; Owner: kura
--

CREATE INDEX ix_tag_pin_inventory_coverage ON public.tag_pin_inventory USING btree (coverage_verified) WHERE (NOT coverage_verified);


--
-- Name: ix_tag_pin_inventory_post_id; Type: INDEX; Schema: public; Owner: kura
--

CREATE INDEX ix_tag_pin_inventory_post_id ON public.tag_pin_inventory USING btree (post_id);


--
-- Name: ix_tag_relation_b; Type: INDEX; Schema: public; Owner: kura
--

CREATE INDEX ix_tag_relation_b ON public.tag_relation USING btree (b_tag_id);


--
-- Name: ix_tag_suggestion_child_id; Type: INDEX; Schema: public; Owner: kura
--

CREATE INDEX ix_tag_suggestion_child_id ON public.tag_suggestion USING btree (child_id);


--
-- Name: ix_tag_suggestion_parent_id; Type: INDEX; Schema: public; Owner: kura
--

CREATE INDEX ix_tag_suggestion_parent_id ON public.tag_suggestion USING btree (parent_id);


--
-- Name: ix_tagger_gt_label_post_id; Type: INDEX; Schema: public; Owner: kura
--

CREATE INDEX ix_tagger_gt_label_post_id ON public.tagger_gt_label USING btree (post_id) WHERE (post_id IS NOT NULL);


--
-- Name: ix_tagger_gt_label_tag; Type: INDEX; Schema: public; Owner: kura
--

CREATE INDEX ix_tagger_gt_label_tag ON public.tagger_gt_label USING btree (tag);


--
-- Name: ix_tagger_head_candidate_status; Type: INDEX; Schema: public; Owner: kura
--

CREATE INDEX ix_tagger_head_candidate_status ON public.tagger_head_candidate USING btree (status, added_at DESC);


--
-- Name: ix_tagger_lifecycle_event_tag; Type: INDEX; Schema: public; Owner: kura
--

CREATE INDEX ix_tagger_lifecycle_event_tag ON public.tagger_lifecycle_event USING btree (tag, occurred_at DESC);


--
-- Name: ix_tagger_node_state_release; Type: INDEX; Schema: public; Owner: kura
--

CREATE INDEX ix_tagger_node_state_release ON public.tagger_node_state USING btree (release_id);


--
-- Name: ix_tagger_patch_tag; Type: INDEX; Schema: public; Owner: kura
--

CREATE INDEX ix_tagger_patch_tag ON public.tagger_patch USING btree (tag);


--
-- Name: ix_tcd_fold; Type: INDEX; Schema: public; Owner: kura
--

CREATE INDEX ix_tcd_fold ON public.tagger_candidate_disposition USING btree (tag_id, seq DESC);


--
-- Name: ix_tce_tag; Type: INDEX; Schema: public; Owner: kura
--

CREATE INDEX ix_tce_tag ON public.tagger_candidate_event USING btree (tag_id, seq DESC);


--
-- Name: ix_user_token_user_id; Type: INDEX; Schema: public; Owner: kura
--

CREATE INDEX ix_user_token_user_id ON public.user_token USING btree (user_id);


--
-- Name: pool_meta_tagging_pending_idx; Type: INDEX; Schema: public; Owner: kura
--

CREATE INDEX pool_meta_tagging_pending_idx ON public.pool USING btree (meta_tagging_locked_until NULLS FIRST) WHERE (meta_tagging_status = 'pending'::text);


--
-- Name: post_tagging_work_claim; Type: INDEX; Schema: public; Owner: kura_reconciler
--

CREATE INDEX post_tagging_work_claim ON public.post_tagging_work USING btree (lease_until NULLS FIRST, post_id) WHERE (status = 'pending'::text);


--
-- Name: post_tagging_work_done; Type: INDEX; Schema: public; Owner: kura_reconciler
--

CREATE INDEX post_tagging_work_done ON public.post_tagging_work USING btree (post_id) WHERE (status = 'done'::text);


--
-- Name: tag_quarantine_tag_id_idx; Type: INDEX; Schema: public; Owner: kura
--

CREATE INDEX tag_quarantine_tag_id_idx ON public.tag_quarantine USING btree (tag_id);


--
-- Name: tagger_gt_label_tag_id_idx; Type: INDEX; Schema: public; Owner: kura
--

CREATE INDEX tagger_gt_label_tag_id_idx ON public.tagger_gt_label USING btree (tag_id);


--
-- Name: tagger_head_candidate_tag_id_idx; Type: INDEX; Schema: public; Owner: kura
--

CREATE INDEX tagger_head_candidate_tag_id_idx ON public.tagger_head_candidate USING btree (tag_id);


--
-- Name: tagger_head_policy_artifact_idx; Type: INDEX; Schema: public; Owner: kura
--

CREATE INDEX tagger_head_policy_artifact_idx ON public.tagger_head_policy USING btree (content_sha256);


--
-- Name: tagger_head_policy_tag_id_idx; Type: INDEX; Schema: public; Owner: kura
--

CREATE INDEX tagger_head_policy_tag_id_idx ON public.tagger_head_policy USING btree (tag_id);


--
-- Name: tagger_head_policy_tag_idx; Type: INDEX; Schema: public; Owner: kura
--

CREATE INDEX tagger_head_policy_tag_idx ON public.tagger_head_policy USING btree (tag);


--
-- Name: tagger_lifecycle_event_tag_id_idx; Type: INDEX; Schema: public; Owner: kura
--

CREATE INDEX tagger_lifecycle_event_tag_id_idx ON public.tagger_lifecycle_event USING btree (tag_id);


--
-- Name: tagger_node_head_tag_idx; Type: INDEX; Schema: public; Owner: kura
--

CREATE INDEX tagger_node_head_tag_idx ON public.tagger_node_head USING btree (tag_id, loaded);


--
-- Name: tagger_patch_tag_id_idx; Type: INDEX; Schema: public; Owner: kura
--

CREATE INDEX tagger_patch_tag_id_idx ON public.tagger_patch USING btree (tag_id);


--
-- Name: tagger_registration_request_tag_id_idx; Type: INDEX; Schema: public; Owner: kura
--

CREATE INDEX tagger_registration_request_tag_id_idx ON public.tagger_registration_request USING btree (tag_id);


--
-- Name: tagger_release_admissibility_fold_idx; Type: INDEX; Schema: public; Owner: kura
--

CREATE INDEX tagger_release_admissibility_fold_idx ON public.tagger_release_admissibility USING btree (release_digest, node, seq DESC);


--
-- Name: tagger_release_component_artifact_idx; Type: INDEX; Schema: public; Owner: kura
--

CREATE INDEX tagger_release_component_artifact_idx ON public.tagger_release_component USING btree (content_sha256);


--
-- Name: tagger_release_manifest_release_id_key; Type: INDEX; Schema: public; Owner: kura
--

CREATE UNIQUE INDEX tagger_release_manifest_release_id_key ON public.tagger_release_manifest USING btree (release_id) WHERE (release_id IS NOT NULL);


--
-- Name: tagger_release_model_sha256_idx; Type: INDEX; Schema: public; Owner: kura
--

CREATE INDEX tagger_release_model_sha256_idx ON public.tagger_release USING btree (model_sha256);


--
-- Name: tagger_tag_rule_tag_idx; Type: INDEX; Schema: public; Owner: kura
--

CREATE INDEX tagger_tag_rule_tag_idx ON public.tagger_tag_rule USING btree (tag_id);


--
-- Name: uq_lora_asset_sha256; Type: INDEX; Schema: public; Owner: kura
--

CREATE UNIQUE INDEX uq_lora_asset_sha256 ON public.lora_asset USING btree (sha256) WHERE (sha256 IS NOT NULL);


--
-- Name: uq_tag_link_platform_handle; Type: INDEX; Schema: public; Owner: kura
--

CREATE UNIQUE INDEX uq_tag_link_platform_handle ON public.tag_link USING btree (platform, handle) WHERE (handle IS NOT NULL);


--
-- Name: INDEX uq_tag_link_platform_handle; Type: COMMENT; Schema: public; Owner: kura
--

COMMENT ON INDEX public.uq_tag_link_platform_handle IS 'One artist tag per (platform, handle). Attribution is derived from this table (see upload_post), so a second claim on the same handle would make the derivation ambiguous rather than merely redundant.';


--
-- Name: ux_pool_url_primary; Type: INDEX; Schema: public; Owner: kura
--

CREATE UNIQUE INDEX ux_pool_url_primary ON public.pool_url USING btree (pool_id) WHERE (is_primary = true);


--
-- Name: ux_pool_url_primary_canonical; Type: INDEX; Schema: public; Owner: kura
--

CREATE UNIQUE INDEX ux_pool_url_primary_canonical ON public.pool_url USING btree (regexp_replace(regexp_replace(url, '[?&]p=[0-9]+'::text, ''::text, 'g'::text), '/+$'::text, ''::text)) WHERE (is_primary AND (url IS NOT NULL) AND (url !~ '^manual:pool/'::text) AND (url !~ '^legacy:'::text));


--
-- Name: ux_post_sha256_ok; Type: INDEX; Schema: public; Owner: kura
--

CREATE UNIQUE INDEX ux_post_sha256_ok ON public.post USING btree (sha256) WHERE (status = 'ok'::text);


--
-- Name: actor claim_actor_immutable; Type: TRIGGER; Schema: claim_storage; Owner: kura
--

CREATE TRIGGER claim_actor_immutable BEFORE DELETE OR UPDATE OR TRUNCATE ON claim_storage.actor FOR EACH STATEMENT EXECUTE FUNCTION claim_storage.actor_read_only();


--
-- Name: label_observation immutable_rows; Type: TRIGGER; Schema: collector_next; Owner: kura
--

CREATE TRIGGER immutable_rows BEFORE DELETE OR UPDATE ON collector_next.label_observation FOR EACH ROW EXECUTE FUNCTION collector_next.reject_label_rewrite();


--
-- Name: label_operation immutable_rows; Type: TRIGGER; Schema: collector_next; Owner: kura
--

CREATE TRIGGER immutable_rows BEFORE DELETE OR UPDATE ON collector_next.label_operation FOR EACH ROW EXECUTE FUNCTION collector_next.reject_label_rewrite();


--
-- Name: label_policy immutable_rows; Type: TRIGGER; Schema: collector_next; Owner: kura
--

CREATE TRIGGER immutable_rows BEFORE DELETE OR UPDATE ON collector_next.label_policy FOR EACH ROW EXECUTE FUNCTION collector_next.reject_label_rewrite();


--
-- Name: label_producer immutable_rows; Type: TRIGGER; Schema: collector_next; Owner: kura
--

CREATE TRIGGER immutable_rows BEFORE DELETE OR UPDATE ON collector_next.label_producer FOR EACH ROW EXECUTE FUNCTION collector_next.reject_label_rewrite();


--
-- Name: label_producer_kind immutable_rows; Type: TRIGGER; Schema: collector_next; Owner: kura
--

CREATE TRIGGER immutable_rows BEFORE DELETE OR UPDATE ON collector_next.label_producer_kind FOR EACH ROW EXECUTE FUNCTION collector_next.reject_label_rewrite();


--
-- Name: label_target immutable_rows; Type: TRIGGER; Schema: collector_next; Owner: kura
--

CREATE TRIGGER immutable_rows BEFORE DELETE OR UPDATE ON collector_next.label_target FOR EACH ROW EXECUTE FUNCTION collector_next.reject_label_rewrite();


--
-- Name: label_observation immutable_truncate; Type: TRIGGER; Schema: collector_next; Owner: kura
--

CREATE TRIGGER immutable_truncate BEFORE TRUNCATE ON collector_next.label_observation FOR EACH STATEMENT EXECUTE FUNCTION collector_next.reject_label_rewrite();


--
-- Name: label_operation immutable_truncate; Type: TRIGGER; Schema: collector_next; Owner: kura
--

CREATE TRIGGER immutable_truncate BEFORE TRUNCATE ON collector_next.label_operation FOR EACH STATEMENT EXECUTE FUNCTION collector_next.reject_label_rewrite();


--
-- Name: label_policy immutable_truncate; Type: TRIGGER; Schema: collector_next; Owner: kura
--

CREATE TRIGGER immutable_truncate BEFORE TRUNCATE ON collector_next.label_policy FOR EACH STATEMENT EXECUTE FUNCTION collector_next.reject_label_rewrite();


--
-- Name: label_producer immutable_truncate; Type: TRIGGER; Schema: collector_next; Owner: kura
--

CREATE TRIGGER immutable_truncate BEFORE TRUNCATE ON collector_next.label_producer FOR EACH STATEMENT EXECUTE FUNCTION collector_next.reject_label_rewrite();


--
-- Name: label_producer_kind immutable_truncate; Type: TRIGGER; Schema: collector_next; Owner: kura
--

CREATE TRIGGER immutable_truncate BEFORE TRUNCATE ON collector_next.label_producer_kind FOR EACH STATEMENT EXECUTE FUNCTION collector_next.reject_label_rewrite();


--
-- Name: label_target immutable_truncate; Type: TRIGGER; Schema: collector_next; Owner: kura
--

CREATE TRIGGER immutable_truncate BEFORE TRUNCATE ON collector_next.label_target FOR EACH STATEMENT EXECUTE FUNCTION collector_next.reject_label_rewrite();


--
-- Name: censor_annotation 00_censor_annotation_dup_skip; Type: TRIGGER; Schema: public; Owner: kura
--

CREATE TRIGGER "00_censor_annotation_dup_skip" BEFORE INSERT ON public.censor_annotation FOR EACH ROW EXECUTE FUNCTION public.censor_annotation_dup_skip();

ALTER TABLE public.censor_annotation ENABLE ALWAYS TRIGGER "00_censor_annotation_dup_skip";


--
-- Name: censor_pair 00_censor_pair_dup_skip; Type: TRIGGER; Schema: public; Owner: kura
--

CREATE TRIGGER "00_censor_pair_dup_skip" BEFORE INSERT ON public.censor_pair FOR EACH ROW EXECUTE FUNCTION public.censor_pair_dup_skip();

ALTER TABLE public.censor_pair ENABLE ALWAYS TRIGGER "00_censor_pair_dup_skip";


--
-- Name: censor_pair_search 00_censor_pair_search_dup_skip; Type: TRIGGER; Schema: public; Owner: kura
--

CREATE TRIGGER "00_censor_pair_search_dup_skip" BEFORE INSERT ON public.censor_pair_search FOR EACH ROW EXECUTE FUNCTION public.censor_pair_search_dup_skip();

ALTER TABLE public.censor_pair_search ENABLE ALWAYS TRIGGER "00_censor_pair_search_dup_skip";


--
-- Name: censor_pair_search_scope 00_censor_pair_search_scope_dup_skip; Type: TRIGGER; Schema: public; Owner: kura
--

CREATE TRIGGER "00_censor_pair_search_scope_dup_skip" BEFORE INSERT ON public.censor_pair_search_scope FOR EACH ROW EXECUTE FUNCTION public.censor_pair_search_scope_dup_skip();

ALTER TABLE public.censor_pair_search_scope ENABLE ALWAYS TRIGGER "00_censor_pair_search_scope_dup_skip";


--
-- Name: censor_trained_post 00_censor_trained_post_dup_skip; Type: TRIGGER; Schema: public; Owner: kura
--

CREATE TRIGGER "00_censor_trained_post_dup_skip" BEFORE INSERT ON public.censor_trained_post FOR EACH ROW EXECUTE FUNCTION public.censor_trained_post_dup_skip();

ALTER TABLE public.censor_trained_post ENABLE ALWAYS TRIGGER "00_censor_trained_post_dup_skip";


--
-- Name: comment 00_comment_dup_skip; Type: TRIGGER; Schema: public; Owner: kura
--

CREATE TRIGGER "00_comment_dup_skip" BEFORE INSERT ON public.comment FOR EACH ROW EXECUTE FUNCTION public.comment_dup_skip();

ALTER TABLE public.comment ENABLE ALWAYS TRIGGER "00_comment_dup_skip";


--
-- Name: comment_score 00_comment_score_dup_skip; Type: TRIGGER; Schema: public; Owner: kura
--

CREATE TRIGGER "00_comment_score_dup_skip" BEFORE INSERT ON public.comment_score FOR EACH ROW EXECUTE FUNCTION public.comment_score_dup_skip();

ALTER TABLE public.comment_score ENABLE ALWAYS TRIGGER "00_comment_score_dup_skip";


--
-- Name: lora_concept 00_lora_concept_dup_skip; Type: TRIGGER; Schema: public; Owner: kura
--

CREATE TRIGGER "00_lora_concept_dup_skip" BEFORE INSERT ON public.lora_concept FOR EACH ROW EXECUTE FUNCTION public.lora_concept_dup_skip();

ALTER TABLE public.lora_concept ENABLE ALWAYS TRIGGER "00_lora_concept_dup_skip";


--
-- Name: lora_model_base 00_lora_model_dup_skip; Type: TRIGGER; Schema: public; Owner: kura
--

CREATE TRIGGER "00_lora_model_dup_skip" BEFORE INSERT ON public.lora_model_base FOR EACH ROW EXECUTE FUNCTION public.lora_model_dup_skip();

ALTER TABLE public.lora_model_base ENABLE ALWAYS TRIGGER "00_lora_model_dup_skip";


--
-- Name: lora_post 00_lora_post_dup_skip; Type: TRIGGER; Schema: public; Owner: kura
--

CREATE TRIGGER "00_lora_post_dup_skip" BEFORE INSERT ON public.lora_post FOR EACH ROW EXECUTE FUNCTION public.lora_post_dup_skip();

ALTER TABLE public.lora_post ENABLE ALWAYS TRIGGER "00_lora_post_dup_skip";


--
-- Name: pool_category 00_pool_category_dup_skip; Type: TRIGGER; Schema: public; Owner: kura
--

CREATE TRIGGER "00_pool_category_dup_skip" BEFORE INSERT ON public.pool_category FOR EACH ROW EXECUTE FUNCTION public.pool_category_dup_skip();

ALTER TABLE public.pool_category ENABLE ALWAYS TRIGGER "00_pool_category_dup_skip";


--
-- Name: pool 00_pool_dup_skip; Type: TRIGGER; Schema: public; Owner: kura
--

CREATE TRIGGER "00_pool_dup_skip" BEFORE INSERT ON public.pool FOR EACH ROW EXECUTE FUNCTION public.pool_dup_skip();

ALTER TABLE public.pool ENABLE ALWAYS TRIGGER "00_pool_dup_skip";


--
-- Name: pool_name 00_pool_name_dup_skip; Type: TRIGGER; Schema: public; Owner: kura
--

CREATE TRIGGER "00_pool_name_dup_skip" BEFORE INSERT ON public.pool_name FOR EACH ROW EXECUTE FUNCTION public.pool_name_dup_skip();

ALTER TABLE public.pool_name ENABLE ALWAYS TRIGGER "00_pool_name_dup_skip";


--
-- Name: pool_post 00_pool_post_dup_skip; Type: TRIGGER; Schema: public; Owner: kura
--

CREATE TRIGGER "00_pool_post_dup_skip" BEFORE INSERT ON public.pool_post FOR EACH ROW EXECUTE FUNCTION public.pool_post_dup_skip();

ALTER TABLE public.pool_post ENABLE ALWAYS TRIGGER "00_pool_post_dup_skip";


--
-- Name: pool_state 00_pool_state_dup_skip; Type: TRIGGER; Schema: public; Owner: kura
--

CREATE TRIGGER "00_pool_state_dup_skip" BEFORE INSERT ON public.pool_state FOR EACH ROW EXECUTE FUNCTION public.pool_state_dup_skip();

ALTER TABLE public.pool_state ENABLE ALWAYS TRIGGER "00_pool_state_dup_skip";


--
-- Name: pool_url 00_pool_url_dup_skip; Type: TRIGGER; Schema: public; Owner: kura
--

CREATE TRIGGER "00_pool_url_dup_skip" BEFORE INSERT ON public.pool_url FOR EACH ROW EXECUTE FUNCTION public.pool_url_dup_skip();

ALTER TABLE public.pool_url ENABLE ALWAYS TRIGGER "00_pool_url_dup_skip";


--
-- Name: post_censor_review 00_post_censor_review_dup_skip; Type: TRIGGER; Schema: public; Owner: kura
--

CREATE TRIGGER "00_post_censor_review_dup_skip" BEFORE INSERT ON public.post_censor_review FOR EACH ROW EXECUTE FUNCTION public.post_censor_review_dup_skip();

ALTER TABLE public.post_censor_review ENABLE ALWAYS TRIGGER "00_post_censor_review_dup_skip";


--
-- Name: post 00_post_dup_skip; Type: TRIGGER; Schema: public; Owner: kura
--

CREATE TRIGGER "00_post_dup_skip" BEFORE INSERT ON public.post FOR EACH ROW EXECUTE FUNCTION public.post_dup_skip();

ALTER TABLE public.post ENABLE ALWAYS TRIGGER "00_post_dup_skip";


--
-- Name: post_favorite 00_post_favorite_dup_skip; Type: TRIGGER; Schema: public; Owner: kura
--

CREATE TRIGGER "00_post_favorite_dup_skip" BEFORE INSERT ON public.post_favorite FOR EACH ROW EXECUTE FUNCTION public.post_favorite_dup_skip();

ALTER TABLE public.post_favorite ENABLE ALWAYS TRIGGER "00_post_favorite_dup_skip";


--
-- Name: post_feature 00_post_feature_dup_skip; Type: TRIGGER; Schema: public; Owner: kura
--

CREATE TRIGGER "00_post_feature_dup_skip" BEFORE INSERT ON public.post_feature FOR EACH ROW EXECUTE FUNCTION public.post_feature_dup_skip();

ALTER TABLE public.post_feature ENABLE ALWAYS TRIGGER "00_post_feature_dup_skip";


--
-- Name: post_note 00_post_note_dup_skip; Type: TRIGGER; Schema: public; Owner: kura
--

CREATE TRIGGER "00_post_note_dup_skip" BEFORE INSERT ON public.post_note FOR EACH ROW EXECUTE FUNCTION public.post_note_dup_skip();

ALTER TABLE public.post_note ENABLE ALWAYS TRIGGER "00_post_note_dup_skip";


--
-- Name: post_score 00_post_score_dup_skip; Type: TRIGGER; Schema: public; Owner: kura
--

CREATE TRIGGER "00_post_score_dup_skip" BEFORE INSERT ON public.post_score FOR EACH ROW EXECUTE FUNCTION public.post_score_dup_skip();

ALTER TABLE public.post_score ENABLE ALWAYS TRIGGER "00_post_score_dup_skip";


--
-- Name: post_tag 00_post_tag_dup_skip; Type: TRIGGER; Schema: public; Owner: kura
--

CREATE TRIGGER "00_post_tag_dup_skip" BEFORE INSERT ON public.post_tag FOR EACH ROW EXECUTE FUNCTION public.post_tag_dup_skip();

ALTER TABLE public.post_tag ENABLE ALWAYS TRIGGER "00_post_tag_dup_skip";


--
-- Name: post_variant 00_post_variant_dup_skip; Type: TRIGGER; Schema: public; Owner: kura
--

CREATE TRIGGER "00_post_variant_dup_skip" BEFORE INSERT ON public.post_variant FOR EACH ROW EXECUTE FUNCTION public.post_variant_dup_skip();

ALTER TABLE public.post_variant ENABLE ALWAYS TRIGGER "00_post_variant_dup_skip";


--
-- Name: tag_category 00_tag_category_dup_skip; Type: TRIGGER; Schema: public; Owner: kura
--

CREATE TRIGGER "00_tag_category_dup_skip" BEFORE INSERT ON public.tag_category FOR EACH ROW EXECUTE FUNCTION public.tag_category_dup_skip();

ALTER TABLE public.tag_category ENABLE ALWAYS TRIGGER "00_tag_category_dup_skip";


--
-- Name: tag 00_tag_dup_skip; Type: TRIGGER; Schema: public; Owner: kura
--

CREATE TRIGGER "00_tag_dup_skip" BEFORE INSERT ON public.tag FOR EACH ROW EXECUTE FUNCTION public.tag_dup_skip();

ALTER TABLE public.tag ENABLE ALWAYS TRIGGER "00_tag_dup_skip";


--
-- Name: tag_implication 00_tag_implication_dup_skip; Type: TRIGGER; Schema: public; Owner: kura
--

CREATE TRIGGER "00_tag_implication_dup_skip" BEFORE INSERT ON public.tag_implication FOR EACH ROW EXECUTE FUNCTION public.tag_implication_dup_skip();

ALTER TABLE public.tag_implication ENABLE ALWAYS TRIGGER "00_tag_implication_dup_skip";


--
-- Name: tag_name 00_tag_name_dup_skip; Type: TRIGGER; Schema: public; Owner: kura
--

CREATE TRIGGER "00_tag_name_dup_skip" BEFORE INSERT ON public.tag_name FOR EACH ROW EXECUTE FUNCTION public.tag_name_dup_skip();

ALTER TABLE public.tag_name ENABLE ALWAYS TRIGGER "00_tag_name_dup_skip";


--
-- Name: tag_suggestion 00_tag_suggestion_dup_skip; Type: TRIGGER; Schema: public; Owner: kura
--

CREATE TRIGGER "00_tag_suggestion_dup_skip" BEFORE INSERT ON public.tag_suggestion FOR EACH ROW EXECUTE FUNCTION public.tag_suggestion_dup_skip();

ALTER TABLE public.tag_suggestion ENABLE ALWAYS TRIGGER "00_tag_suggestion_dup_skip";


--
-- Name: tagger_node_state 00_tagger_node_state_dup_skip; Type: TRIGGER; Schema: public; Owner: kura
--

CREATE TRIGGER "00_tagger_node_state_dup_skip" BEFORE INSERT ON public.tagger_node_state FOR EACH ROW EXECUTE FUNCTION public.tagger_node_state_dup_skip();

ALTER TABLE public.tagger_node_state ENABLE ALWAYS TRIGGER "00_tagger_node_state_dup_skip";


--
-- Name: tagger_patch 00_tagger_patch_dup_skip; Type: TRIGGER; Schema: public; Owner: kura
--

CREATE TRIGGER "00_tagger_patch_dup_skip" BEFORE INSERT ON public.tagger_patch FOR EACH ROW EXECUTE FUNCTION public.tagger_patch_dup_skip();

ALTER TABLE public.tagger_patch ENABLE ALWAYS TRIGGER "00_tagger_patch_dup_skip";


--
-- Name: tagger_release_active 00_tagger_release_active_dup_skip; Type: TRIGGER; Schema: public; Owner: kura
--

CREATE TRIGGER "00_tagger_release_active_dup_skip" BEFORE INSERT ON public.tagger_release_active FOR EACH ROW EXECUTE FUNCTION public.tagger_release_active_dup_skip();

ALTER TABLE public.tagger_release_active ENABLE ALWAYS TRIGGER "00_tagger_release_active_dup_skip";


--
-- Name: tagger_release 00_tagger_release_dup_skip; Type: TRIGGER; Schema: public; Owner: kura
--

CREATE TRIGGER "00_tagger_release_dup_skip" BEFORE INSERT ON public.tagger_release FOR EACH ROW EXECUTE FUNCTION public.tagger_release_dup_skip();

ALTER TABLE public.tagger_release ENABLE ALWAYS TRIGGER "00_tagger_release_dup_skip";


--
-- Name: user 00_user_dup_skip; Type: TRIGGER; Schema: public; Owner: kura
--

CREATE TRIGGER "00_user_dup_skip" BEFORE INSERT ON public."user" FOR EACH ROW EXECUTE FUNCTION public.user_dup_skip();

ALTER TABLE public."user" ENABLE ALWAYS TRIGGER "00_user_dup_skip";


--
-- Name: user_token 00_user_token_dup_skip; Type: TRIGGER; Schema: public; Owner: kura
--

CREATE TRIGGER "00_user_token_dup_skip" BEFORE INSERT ON public.user_token FOR EACH ROW EXECUTE FUNCTION public.user_token_dup_skip();

ALTER TABLE public.user_token ENABLE ALWAYS TRIGGER "00_user_token_dup_skip";


--
-- Name: post_tag 01_post_tag_pin_guard_del; Type: TRIGGER; Schema: public; Owner: kura
--

CREATE TRIGGER "01_post_tag_pin_guard_del" BEFORE DELETE ON public.post_tag FOR EACH ROW EXECUTE FUNCTION public.post_tag_pin_guard_del();


--
-- Name: post_tag 01_post_tag_pin_guard_ins; Type: TRIGGER; Schema: public; Owner: kura
--

CREATE TRIGGER "01_post_tag_pin_guard_ins" BEFORE INSERT ON public.post_tag FOR EACH ROW EXECUTE FUNCTION public.post_tag_pin_guard_ins();


--
-- Name: post_tag_actor_claim claim_legacy_write; Type: TRIGGER; Schema: public; Owner: kura
--

CREATE TRIGGER claim_legacy_write INSTEAD OF INSERT OR DELETE OR UPDATE ON public.post_tag_actor_claim FOR EACH ROW EXECUTE FUNCTION claim_storage.legacy_write();


--
-- Name: lora_model lora_model_view_insert; Type: TRIGGER; Schema: public; Owner: kura
--

CREATE TRIGGER lora_model_view_insert INSTEAD OF INSERT ON public.lora_model FOR EACH ROW EXECUTE FUNCTION public.lora_model_view_insert();


--
-- Name: pool_post pool_post_sync_cover_trg; Type: TRIGGER; Schema: public; Owner: kura
--

CREATE TRIGGER pool_post_sync_cover_trg AFTER INSERT OR DELETE ON public.pool_post FOR EACH ROW EXECUTE FUNCTION public.sync_pool_cover_post();


--
-- Name: pool_post pool_post_sync_last_edit_trg; Type: TRIGGER; Schema: public; Owner: kura
--

CREATE TRIGGER pool_post_sync_last_edit_trg AFTER INSERT OR DELETE OR UPDATE OF pool_id ON public.pool_post FOR EACH ROW EXECUTE FUNCTION public.sync_pool_last_edit_time();


--
-- Name: pool_post pool_post_sync_pool_url_observed_count_trg; Type: TRIGGER; Schema: public; Owner: kura
--

CREATE TRIGGER pool_post_sync_pool_url_observed_count_trg AFTER INSERT OR DELETE OR UPDATE OF pool_id ON public.pool_post FOR EACH ROW EXECUTE FUNCTION public.sync_pool_url_observed_count();


--
-- Name: post post_attach_source_pool_trg; Type: TRIGGER; Schema: public; Owner: kura
--

CREATE TRIGGER post_attach_source_pool_trg AFTER INSERT OR UPDATE OF source ON public.post FOR EACH ROW EXECUTE FUNCTION public.attach_source_pool();


--
-- Name: post post_listing_insert; Type: TRIGGER; Schema: public; Owner: kura
--

CREATE TRIGGER post_listing_insert AFTER INSERT ON public.post FOR EACH ROW EXECUTE FUNCTION public.sync_post_listing();


--
-- Name: post post_listing_update; Type: TRIGGER; Schema: public; Owner: kura
--

CREATE TRIGGER post_listing_update AFTER UPDATE OF id, status, safety, file_size ON public.post FOR EACH ROW WHEN (((old.id IS DISTINCT FROM new.id) OR (old.status IS DISTINCT FROM new.status) OR ((old.safety)::text IS DISTINCT FROM (new.safety)::text) OR (old.file_size IS DISTINCT FROM new.file_size))) EXECUTE FUNCTION public.sync_post_listing();


--
-- Name: post post_rendition_follow_legacy; Type: TRIGGER; Schema: public; Owner: kura
--

CREATE TRIGGER post_rendition_follow_legacy AFTER UPDATE OF data, thumbnail ON public.post FOR EACH ROW EXECUTE FUNCTION public.post_rendition_follow_legacy();


--
-- Name: post post_reset_dups_before_delete; Type: TRIGGER; Schema: public; Owner: kura
--

CREATE TRIGGER post_reset_dups_before_delete BEFORE DELETE ON public.post FOR EACH ROW EXECUTE FUNCTION public.trg_post_reset_dups_before_delete();


--
-- Name: post post_status_sync_pool_cover_trg; Type: TRIGGER; Schema: public; Owner: kura
--

CREATE TRIGGER post_status_sync_pool_cover_trg AFTER UPDATE OF status ON public.post FOR EACH ROW WHEN ((old.status IS DISTINCT FROM new.status)) EXECUTE FUNCTION public.post_status_sync_pool_cover();


--
-- Name: post post_status_transition_guard_trg; Type: TRIGGER; Schema: public; Owner: kura
--

CREATE TRIGGER post_status_transition_guard_trg BEFORE UPDATE OF status ON public.post FOR EACH ROW WHEN ((old.status IS DISTINCT FROM new.status)) EXECUTE FUNCTION public.post_status_transition_guard();


--
-- Name: post post_sync_primary_pool_trg; Type: TRIGGER; Schema: public; Owner: kura
--

CREATE TRIGGER post_sync_primary_pool_trg AFTER INSERT OR UPDATE OF primary_pool_id ON public.post FOR EACH ROW EXECUTE FUNCTION public.sync_post_primary_pool();


--
-- Name: post post_tagging_work_mirror_ins; Type: TRIGGER; Schema: public; Owner: kura
--

CREATE TRIGGER post_tagging_work_mirror_ins AFTER INSERT ON public.post FOR EACH ROW EXECUTE FUNCTION public.trg_post_tagging_work_mirror();


--
-- Name: tag_quarantine tag_quarantine_bind_tag; Type: TRIGGER; Schema: public; Owner: kura
--

CREATE TRIGGER tag_quarantine_bind_tag BEFORE INSERT OR UPDATE ON public.tag_quarantine FOR EACH ROW EXECUTE FUNCTION public.tagger_bind_tag();


--
-- Name: tagger_candidate_disposition tagger_candidate_disposition_bind_tag; Type: TRIGGER; Schema: public; Owner: kura
--

CREATE TRIGGER tagger_candidate_disposition_bind_tag BEFORE INSERT ON public.tagger_candidate_disposition FOR EACH ROW EXECUTE FUNCTION public.tagger_bind_tag();


--
-- Name: tagger_candidate_event tagger_candidate_event_bind_tag; Type: TRIGGER; Schema: public; Owner: kura
--

CREATE TRIGGER tagger_candidate_event_bind_tag BEFORE INSERT ON public.tagger_candidate_event FOR EACH ROW EXECUTE FUNCTION public.tagger_bind_tag();


--
-- Name: tagger_gt_label tagger_gt_label_bind_tag; Type: TRIGGER; Schema: public; Owner: kura
--

CREATE TRIGGER tagger_gt_label_bind_tag BEFORE INSERT OR UPDATE ON public.tagger_gt_label FOR EACH ROW EXECUTE FUNCTION public.tagger_bind_tag();


--
-- Name: tagger_head_candidate tagger_head_candidate_bind_tag; Type: TRIGGER; Schema: public; Owner: kura
--

CREATE TRIGGER tagger_head_candidate_bind_tag BEFORE INSERT OR UPDATE ON public.tagger_head_candidate FOR EACH ROW EXECUTE FUNCTION public.tagger_bind_tag();


--
-- Name: tagger_head_policy tagger_head_policy_bind_tag; Type: TRIGGER; Schema: public; Owner: kura
--

CREATE TRIGGER tagger_head_policy_bind_tag BEFORE INSERT OR UPDATE ON public.tagger_head_policy FOR EACH ROW EXECUTE FUNCTION public.tagger_bind_tag();


--
-- Name: tagger_head_policy tagger_head_policy_head_bytes; Type: TRIGGER; Schema: public; Owner: kura
--

CREATE TRIGGER tagger_head_policy_head_bytes BEFORE INSERT OR UPDATE OF content_sha256 ON public.tagger_head_policy FOR EACH ROW EXECUTE FUNCTION public.tagger_head_policy_requires_head_bytes();


--
-- Name: tagger_lifecycle_event tagger_lifecycle_event_bind_tag; Type: TRIGGER; Schema: public; Owner: kura
--

CREATE TRIGGER tagger_lifecycle_event_bind_tag BEFORE INSERT OR UPDATE ON public.tagger_lifecycle_event FOR EACH ROW EXECUTE FUNCTION public.tagger_bind_tag();


--
-- Name: tagger_node_head tagger_node_head_bind_tag; Type: TRIGGER; Schema: public; Owner: kura
--

CREATE TRIGGER tagger_node_head_bind_tag BEFORE INSERT OR UPDATE ON public.tagger_node_head FOR EACH ROW EXECUTE FUNCTION public.tagger_bind_tag();


--
-- Name: tagger_patch tagger_patch_bind_tag; Type: TRIGGER; Schema: public; Owner: kura
--

CREATE TRIGGER tagger_patch_bind_tag BEFORE INSERT OR UPDATE ON public.tagger_patch FOR EACH ROW EXECUTE FUNCTION public.tagger_bind_tag();


--
-- Name: tagger_registration_request tagger_registration_request_bind_tag; Type: TRIGGER; Schema: public; Owner: kura
--

CREATE TRIGGER tagger_registration_request_bind_tag BEFORE INSERT OR UPDATE ON public.tagger_registration_request FOR EACH ROW EXECUTE FUNCTION public.tagger_bind_tag();


--
-- Name: tagger_tag_rule tagger_tag_rule_bind_tag; Type: TRIGGER; Schema: public; Owner: kura
--

CREATE TRIGGER tagger_tag_rule_bind_tag BEFORE INSERT OR UPDATE ON public.tagger_tag_rule FOR EACH ROW EXECUTE FUNCTION public.tagger_bind_tag();


--
-- Name: post_tag_audit trg_post_tag_audit_append_only; Type: TRIGGER; Schema: public; Owner: kura
--

CREATE TRIGGER trg_post_tag_audit_append_only BEFORE UPDATE ON public.post_tag_audit FOR EACH ROW EXECUTE FUNCTION public.post_tag_audit_append_only();


--
-- Name: post_tag trg_tag_post_count_del_stmt; Type: TRIGGER; Schema: public; Owner: kura
--

CREATE TRIGGER trg_tag_post_count_del_stmt AFTER DELETE ON public.post_tag REFERENCING OLD TABLE AS old_rows FOR EACH STATEMENT EXECUTE FUNCTION public.tag_post_count_del_stmt();


--
-- Name: post_tag trg_tag_post_count_ins_stmt; Type: TRIGGER; Schema: public; Owner: kura
--

CREATE TRIGGER trg_tag_post_count_ins_stmt AFTER INSERT ON public.post_tag REFERENCING NEW TABLE AS new_rows FOR EACH STATEMENT EXECUTE FUNCTION public.tag_post_count_ins_stmt();


--
-- Name: tagger_config trg_tagger_config_governed; Type: TRIGGER; Schema: public; Owner: kura
--

CREATE TRIGGER trg_tagger_config_governed BEFORE INSERT OR DELETE OR UPDATE ON public.tagger_config FOR EACH ROW EXECUTE FUNCTION public.trg_tagger_governed_only();


--
-- Name: tagger_gt_label trg_tagger_gt_label_governed; Type: TRIGGER; Schema: public; Owner: kura
--

CREATE TRIGGER trg_tagger_gt_label_governed BEFORE INSERT OR DELETE OR UPDATE ON public.tagger_gt_label FOR EACH ROW EXECUTE FUNCTION public.trg_tagger_governed_only();


--
-- Name: tagger_head_candidate trg_tagger_head_candidate_governed_only; Type: TRIGGER; Schema: public; Owner: kura
--

CREATE TRIGGER trg_tagger_head_candidate_governed_only BEFORE INSERT OR DELETE OR UPDATE ON public.tagger_head_candidate FOR EACH ROW EXECUTE FUNCTION public.trg_tagger_governed_only();


--
-- Name: tagger_lifecycle_event trg_tagger_lifecycle_event_edge; Type: TRIGGER; Schema: public; Owner: kura
--

CREATE TRIGGER trg_tagger_lifecycle_event_edge BEFORE INSERT ON public.tagger_lifecycle_event FOR EACH ROW EXECUTE FUNCTION public.trg_tagger_lifecycle_event_edge_check();


--
-- Name: tagger_lifecycle_event trg_tagger_lifecycle_event_governed_only; Type: TRIGGER; Schema: public; Owner: kura
--

CREATE TRIGGER trg_tagger_lifecycle_event_governed_only BEFORE INSERT OR DELETE OR UPDATE ON public.tagger_lifecycle_event FOR EACH ROW EXECUTE FUNCTION public.trg_tagger_governed_only();


--
-- Name: tagger_lifecycle_lock trg_tagger_lifecycle_lock_governed_only; Type: TRIGGER; Schema: public; Owner: kura
--

CREATE TRIGGER trg_tagger_lifecycle_lock_governed_only BEFORE INSERT OR DELETE OR UPDATE ON public.tagger_lifecycle_lock FOR EACH ROW EXECUTE FUNCTION public.trg_tagger_governed_only();


--
-- Name: tagger_operation trg_tagger_operation_governed_only; Type: TRIGGER; Schema: public; Owner: kura
--

CREATE TRIGGER trg_tagger_operation_governed_only BEFORE INSERT OR DELETE OR UPDATE ON public.tagger_operation FOR EACH ROW EXECUTE FUNCTION public.trg_tagger_governed_only();


--
-- Name: tagger_patch trg_tagger_patch_governed_only; Type: TRIGGER; Schema: public; Owner: kura
--

CREATE TRIGGER trg_tagger_patch_governed_only BEFORE INSERT OR DELETE OR UPDATE ON public.tagger_patch FOR EACH ROW EXECUTE FUNCTION public.trg_tagger_governed_only();


--
-- Name: tagger_reconciler_state trg_tagger_reconciler_state_governed_only; Type: TRIGGER; Schema: public; Owner: kura
--

CREATE TRIGGER trg_tagger_reconciler_state_governed_only BEFORE INSERT OR DELETE OR UPDATE ON public.tagger_reconciler_state FOR EACH ROW EXECUTE FUNCTION public.trg_tagger_governed_only();


--
-- Name: tagger_registration_request trg_tagger_registration_request_governed_only; Type: TRIGGER; Schema: public; Owner: kura
--

CREATE TRIGGER trg_tagger_registration_request_governed_only BEFORE INSERT OR DELETE OR UPDATE ON public.tagger_registration_request FOR EACH ROW EXECUTE FUNCTION public.trg_tagger_governed_only();


--
-- Name: tagger_release_active trg_tagger_release_active_governed_only; Type: TRIGGER; Schema: public; Owner: kura
--

CREATE TRIGGER trg_tagger_release_active_governed_only BEFORE INSERT OR DELETE OR UPDATE ON public.tagger_release_active FOR EACH ROW EXECUTE FUNCTION public.trg_tagger_governed_only();


--
-- Name: tagger_release trg_tagger_release_governed_insert; Type: TRIGGER; Schema: public; Owner: kura
--

CREATE TRIGGER trg_tagger_release_governed_insert BEFORE INSERT ON public.tagger_release FOR EACH ROW EXECUTE FUNCTION public.trg_tagger_governed_only();


--
-- Name: tagger_release trg_tagger_release_no_delete; Type: TRIGGER; Schema: public; Owner: kura
--

CREATE TRIGGER trg_tagger_release_no_delete BEFORE DELETE ON public.tagger_release FOR EACH ROW EXECUTE FUNCTION public.trg_tagger_release_immutable();


--
-- Name: tagger_release trg_tagger_release_no_update; Type: TRIGGER; Schema: public; Owner: kura
--

CREATE TRIGGER trg_tagger_release_no_update BEFORE UPDATE ON public.tagger_release FOR EACH ROW EXECUTE FUNCTION public.trg_tagger_release_immutable();


--
-- Name: tagger_sweep_run trg_tagger_sweep_run_governed_only; Type: TRIGGER; Schema: public; Owner: kura
--

CREATE TRIGGER trg_tagger_sweep_run_governed_only BEFORE INSERT OR DELETE OR UPDATE ON public.tagger_sweep_run FOR EACH ROW EXECUTE FUNCTION public.trg_tagger_governed_only();


--
-- Name: tagger_worker_heartbeat trg_tagger_worker_heartbeat_governed_only; Type: TRIGGER; Schema: public; Owner: kura
--

CREATE TRIGGER trg_tagger_worker_heartbeat_governed_only BEFORE INSERT OR DELETE OR UPDATE ON public.tagger_worker_heartbeat FOR EACH ROW EXECUTE FUNCTION public.trg_tagger_governed_only();


--
-- Name: tagger_candidate_disposition trg_tcd_append_only; Type: TRIGGER; Schema: public; Owner: kura
--

CREATE TRIGGER trg_tcd_append_only BEFORE DELETE OR UPDATE ON public.tagger_candidate_disposition FOR EACH ROW EXECUTE FUNCTION public.trg_tcd_append_only();


--
-- Name: tagger_candidate_disposition trg_tcd_governed_only; Type: TRIGGER; Schema: public; Owner: kura
--

CREATE TRIGGER trg_tcd_governed_only BEFORE INSERT OR DELETE OR UPDATE ON public.tagger_candidate_disposition FOR EACH ROW EXECUTE FUNCTION public.trg_tagger_governed_only();


--
-- Name: tagger_candidate_event trg_tce_append_only; Type: TRIGGER; Schema: public; Owner: kura
--

CREATE TRIGGER trg_tce_append_only BEFORE DELETE OR UPDATE ON public.tagger_candidate_event FOR EACH ROW EXECUTE FUNCTION public.trg_tce_append_only();


--
-- Name: tagger_candidate_event trg_tce_governed_only; Type: TRIGGER; Schema: public; Owner: kura
--

CREATE TRIGGER trg_tce_governed_only BEFORE INSERT OR DELETE OR UPDATE ON public.tagger_candidate_event FOR EACH ROW EXECUTE FUNCTION public.trg_tagger_governed_only();


--
-- Name: claim claim_actor_key_fkey; Type: FK CONSTRAINT; Schema: claim_storage; Owner: kura
--

ALTER TABLE ONLY claim_storage.claim
    ADD CONSTRAINT claim_actor_key_fkey FOREIGN KEY (actor_key) REFERENCES claim_storage.actor(id);


--
-- Name: claim claim_post_id_fkey; Type: FK CONSTRAINT; Schema: claim_storage; Owner: kura
--

ALTER TABLE ONLY claim_storage.claim
    ADD CONSTRAINT claim_post_id_fkey FOREIGN KEY (post_id) REFERENCES public.post(id) ON DELETE CASCADE;


--
-- Name: claim claim_tag_id_fkey; Type: FK CONSTRAINT; Schema: claim_storage; Owner: kura
--

ALTER TABLE ONLY claim_storage.claim
    ADD CONSTRAINT claim_tag_id_fkey FOREIGN KEY (tag_id) REFERENCES public.tag(id);


--
-- Name: asset asset_blob_id_fkey; Type: FK CONSTRAINT; Schema: collector_next; Owner: kura
--

ALTER TABLE ONLY collector_next.asset
    ADD CONSTRAINT asset_blob_id_fkey FOREIGN KEY (blob_id) REFERENCES collector_next.blob(id);


--
-- Name: asset_label_state asset_label_state_owner_id_asset_id_fkey; Type: FK CONSTRAINT; Schema: collector_next; Owner: kura
--

ALTER TABLE ONLY collector_next.asset_label_state
    ADD CONSTRAINT asset_label_state_owner_id_asset_id_fkey FOREIGN KEY (owner_id, asset_id) REFERENCES collector_next.asset(owner_id, id);


--
-- Name: asset_label_state asset_label_state_policy_id_fkey; Type: FK CONSTRAINT; Schema: collector_next; Owner: kura
--

ALTER TABLE ONLY collector_next.asset_label_state
    ADD CONSTRAINT asset_label_state_policy_id_fkey FOREIGN KEY (policy_id) REFERENCES collector_next.label_policy(id);


--
-- Name: blob_location blob_location_blob_id_fkey; Type: FK CONSTRAINT; Schema: collector_next; Owner: kura
--

ALTER TABLE ONLY collector_next.blob_location
    ADD CONSTRAINT blob_location_blob_id_fkey FOREIGN KEY (blob_id) REFERENCES collector_next.blob(id);


--
-- Name: collection_entry collection_entry_owner_id_asset_id_fkey; Type: FK CONSTRAINT; Schema: collector_next; Owner: kura
--

ALTER TABLE ONLY collector_next.collection_entry
    ADD CONSTRAINT collection_entry_owner_id_asset_id_fkey FOREIGN KEY (owner_id, asset_id) REFERENCES collector_next.asset(owner_id, id);


--
-- Name: collection_entry collection_entry_owner_id_collection_id_fkey; Type: FK CONSTRAINT; Schema: collector_next; Owner: kura
--

ALTER TABLE ONLY collector_next.collection_entry
    ADD CONSTRAINT collection_entry_owner_id_collection_id_fkey FOREIGN KEY (owner_id, collection_id) REFERENCES collector_next.collection(owner_id, id);


--
-- Name: import_item import_item_owner_id_collection_id_fkey; Type: FK CONSTRAINT; Schema: collector_next; Owner: kura
--

ALTER TABLE ONLY collector_next.import_item
    ADD CONSTRAINT import_item_owner_id_collection_id_fkey FOREIGN KEY (owner_id, collection_id) REFERENCES collector_next.collection(owner_id, id);


--
-- Name: import_item import_item_owner_id_operation_key_fkey; Type: FK CONSTRAINT; Schema: collector_next; Owner: kura
--

ALTER TABLE ONLY collector_next.import_item
    ADD CONSTRAINT import_item_owner_id_operation_key_fkey FOREIGN KEY (owner_id, operation_key) REFERENCES collector_next.import_operation(owner_id, request_key);


--
-- Name: label_head label_head_asset_id_tag_id_observation_id_fkey; Type: FK CONSTRAINT; Schema: collector_next; Owner: kura
--

ALTER TABLE ONLY collector_next.label_head
    ADD CONSTRAINT label_head_asset_id_tag_id_observation_id_fkey FOREIGN KEY (asset_id, tag_id, observation_id) REFERENCES collector_next.label_observation(asset_id, tag_id, id);


--
-- Name: label_head label_head_owner_id_asset_id_fkey; Type: FK CONSTRAINT; Schema: collector_next; Owner: kura
--

ALTER TABLE ONLY collector_next.label_head
    ADD CONSTRAINT label_head_owner_id_asset_id_fkey FOREIGN KEY (owner_id, asset_id) REFERENCES collector_next.asset(owner_id, id);


--
-- Name: label_observation label_observation_asset_id_tag_id_based_on_id_fkey; Type: FK CONSTRAINT; Schema: collector_next; Owner: kura
--

ALTER TABLE ONLY collector_next.label_observation
    ADD CONSTRAINT label_observation_asset_id_tag_id_based_on_id_fkey FOREIGN KEY (asset_id, tag_id, based_on_id) REFERENCES collector_next.label_observation(asset_id, tag_id, id);


--
-- Name: label_observation label_observation_owner_id_asset_id_fkey; Type: FK CONSTRAINT; Schema: collector_next; Owner: kura
--

ALTER TABLE ONLY collector_next.label_observation
    ADD CONSTRAINT label_observation_owner_id_asset_id_fkey FOREIGN KEY (owner_id, asset_id) REFERENCES collector_next.asset(owner_id, id);


--
-- Name: label_observation label_observation_owner_id_producer_id_fkey; Type: FK CONSTRAINT; Schema: collector_next; Owner: kura
--

ALTER TABLE ONLY collector_next.label_observation
    ADD CONSTRAINT label_observation_owner_id_producer_id_fkey FOREIGN KEY (owner_id, producer_id) REFERENCES collector_next.label_producer(owner_id, id);


--
-- Name: label_observation label_observation_owner_id_request_key_fkey; Type: FK CONSTRAINT; Schema: collector_next; Owner: kura
--

ALTER TABLE ONLY collector_next.label_observation
    ADD CONSTRAINT label_observation_owner_id_request_key_fkey FOREIGN KEY (owner_id, request_key) REFERENCES collector_next.label_operation(owner_id, request_key);


--
-- Name: label_observation label_observation_owner_id_tag_id_fkey; Type: FK CONSTRAINT; Schema: collector_next; Owner: kura
--

ALTER TABLE ONLY collector_next.label_observation
    ADD CONSTRAINT label_observation_owner_id_tag_id_fkey FOREIGN KEY (owner_id, tag_id) REFERENCES collector_next.tag(owner_id, id);


--
-- Name: label_operation label_operation_owner_id_producer_id_fkey; Type: FK CONSTRAINT; Schema: collector_next; Owner: kura
--

ALTER TABLE ONLY collector_next.label_operation
    ADD CONSTRAINT label_operation_owner_id_producer_id_fkey FOREIGN KEY (owner_id, producer_id) REFERENCES collector_next.label_producer(owner_id, id);


--
-- Name: label_operation label_operation_policy_id_fkey; Type: FK CONSTRAINT; Schema: collector_next; Owner: kura
--

ALTER TABLE ONLY collector_next.label_operation
    ADD CONSTRAINT label_operation_policy_id_fkey FOREIGN KEY (policy_id) REFERENCES collector_next.label_policy(id);


--
-- Name: label_producer label_producer_kind_id_fkey; Type: FK CONSTRAINT; Schema: collector_next; Owner: kura
--

ALTER TABLE ONLY collector_next.label_producer
    ADD CONSTRAINT label_producer_kind_id_fkey FOREIGN KEY (kind_id) REFERENCES collector_next.label_producer_kind(id);


--
-- Name: label_target label_target_owner_id_asset_id_fkey; Type: FK CONSTRAINT; Schema: collector_next; Owner: kura
--

ALTER TABLE ONLY collector_next.label_target
    ADD CONSTRAINT label_target_owner_id_asset_id_fkey FOREIGN KEY (owner_id, asset_id) REFERENCES collector_next.asset(owner_id, id);


--
-- Name: label_target label_target_owner_id_request_key_fkey; Type: FK CONSTRAINT; Schema: collector_next; Owner: kura
--

ALTER TABLE ONLY collector_next.label_target
    ADD CONSTRAINT label_target_owner_id_request_key_fkey FOREIGN KEY (owner_id, request_key) REFERENCES collector_next.label_operation(owner_id, request_key);


--
-- Name: media_current media_current_asset_id_fkey; Type: FK CONSTRAINT; Schema: collector_next; Owner: kura
--

ALTER TABLE ONLY collector_next.media_current
    ADD CONSTRAINT media_current_asset_id_fkey FOREIGN KEY (asset_id) REFERENCES collector_next.asset(id);


--
-- Name: media_current media_current_owner_id_asset_id_fkey; Type: FK CONSTRAINT; Schema: collector_next; Owner: kura
--

ALTER TABLE ONLY collector_next.media_current
    ADD CONSTRAINT media_current_owner_id_asset_id_fkey FOREIGN KEY (owner_id, asset_id) REFERENCES collector_next.asset(owner_id, id);


--
-- Name: receipt receipt_entry_id_fkey; Type: FK CONSTRAINT; Schema: collector_next; Owner: kura
--

ALTER TABLE ONLY collector_next.receipt
    ADD CONSTRAINT receipt_entry_id_fkey FOREIGN KEY (entry_id) REFERENCES collector_next.collection_entry(id);


--
-- Name: receipt receipt_owner_id_asset_id_fkey; Type: FK CONSTRAINT; Schema: collector_next; Owner: kura
--

ALTER TABLE ONLY collector_next.receipt
    ADD CONSTRAINT receipt_owner_id_asset_id_fkey FOREIGN KEY (owner_id, asset_id) REFERENCES collector_next.asset(owner_id, id);


--
-- Name: tag_usage tag_representative_owner; Type: FK CONSTRAINT; Schema: collector_next; Owner: kura
--

ALTER TABLE ONLY collector_next.tag_usage
    ADD CONSTRAINT tag_representative_owner FOREIGN KEY (owner_id, representative_asset_id) REFERENCES collector_next.asset(owner_id, id);


--
-- Name: tag_usage tag_usage_owner_id_tag_id_fkey; Type: FK CONSTRAINT; Schema: collector_next; Owner: kura
--

ALTER TABLE ONLY collector_next.tag_usage
    ADD CONSTRAINT tag_usage_owner_id_tag_id_fkey FOREIGN KEY (owner_id, tag_id) REFERENCES collector_next.tag(owner_id, id);


--
-- Name: censor_annotation censor_annotation_created_by_fkey; Type: FK CONSTRAINT; Schema: public; Owner: kura
--

ALTER TABLE ONLY public.censor_annotation
    ADD CONSTRAINT censor_annotation_created_by_fkey FOREIGN KEY (created_by) REFERENCES public."user"(id) ON DELETE SET NULL;


--
-- Name: censor_annotation censor_annotation_post_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: kura
--

ALTER TABLE ONLY public.censor_annotation
    ADD CONSTRAINT censor_annotation_post_id_fkey FOREIGN KEY (post_id) REFERENCES public.post(id) ON DELETE CASCADE;


--
-- Name: censor_annotation censor_annotation_supersedes_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: kura
--

ALTER TABLE ONLY public.censor_annotation
    ADD CONSTRAINT censor_annotation_supersedes_id_fkey FOREIGN KEY (supersedes_id) REFERENCES public.censor_annotation(id) ON DELETE SET NULL;


--
-- Name: censor_pair_search censor_pair_search_post_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: kura
--

ALTER TABLE ONLY public.censor_pair_search
    ADD CONSTRAINT censor_pair_search_post_id_fkey FOREIGN KEY (post_id) REFERENCES public.post(id) ON DELETE CASCADE;


--
-- Name: censor_pair_search_scope censor_pair_search_scope_pool_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: kura
--

ALTER TABLE ONLY public.censor_pair_search_scope
    ADD CONSTRAINT censor_pair_search_scope_pool_id_fkey FOREIGN KEY (pool_id) REFERENCES public.pool(id) ON DELETE CASCADE;


--
-- Name: censor_trained_post censor_trained_post_post_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: kura
--

ALTER TABLE ONLY public.censor_trained_post
    ADD CONSTRAINT censor_trained_post_post_id_fkey FOREIGN KEY (post_id) REFERENCES public.post(id) ON DELETE CASCADE;


--
-- Name: comment comment_post_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: kura
--

ALTER TABLE ONLY public.comment
    ADD CONSTRAINT comment_post_id_fkey FOREIGN KEY (post_id) REFERENCES public.post(id);


--
-- Name: comment_score comment_score_comment_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: kura
--

ALTER TABLE ONLY public.comment_score
    ADD CONSTRAINT comment_score_comment_id_fkey FOREIGN KEY (comment_id) REFERENCES public.comment(id);


--
-- Name: comment_score comment_score_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: kura
--

ALTER TABLE ONLY public.comment_score
    ADD CONSTRAINT comment_score_user_id_fkey FOREIGN KEY (user_id) REFERENCES public."user"(id);


--
-- Name: comment comment_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: kura
--

ALTER TABLE ONLY public.comment
    ADD CONSTRAINT comment_user_id_fkey FOREIGN KEY (user_id) REFERENCES public."user"(id);


--
-- Name: human_login_key human_login_key_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: kura
--

ALTER TABLE ONLY public.human_login_key
    ADD CONSTRAINT human_login_key_user_id_fkey FOREIGN KEY (user_id) REFERENCES public."user"(id);


--
-- Name: lora_concept lora_concept_lora_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: kura
--

ALTER TABLE ONLY public.lora_concept
    ADD CONSTRAINT lora_concept_lora_id_fkey FOREIGN KEY (lora_id) REFERENCES public.lora_asset(id) ON DELETE CASCADE;


--
-- Name: lora_post lora_post_concept_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: kura
--

ALTER TABLE ONLY public.lora_post
    ADD CONSTRAINT lora_post_concept_id_fkey FOREIGN KEY (concept_id) REFERENCES public.lora_concept(id) ON DELETE SET NULL;


--
-- Name: lora_post lora_post_lora_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: kura
--

ALTER TABLE ONLY public.lora_post
    ADD CONSTRAINT lora_post_lora_id_fkey FOREIGN KEY (lora_id) REFERENCES public.lora_asset(id) ON DELETE CASCADE;


--
-- Name: lora_training_run lora_training_run_asset_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: kura
--

ALTER TABLE ONLY public.lora_training_run
    ADD CONSTRAINT lora_training_run_asset_id_fkey FOREIGN KEY (asset_id) REFERENCES public.lora_asset(id) ON DELETE CASCADE;


--
-- Name: pool pool_category_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: kura
--

ALTER TABLE ONLY public.pool
    ADD CONSTRAINT pool_category_id_fkey FOREIGN KEY (category_id) REFERENCES public.pool_category(id);


--
-- Name: pool pool_cover_post_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: kura
--

ALTER TABLE ONLY public.pool
    ADD CONSTRAINT pool_cover_post_id_fkey FOREIGN KEY (cover_post_id) REFERENCES public.post(id) ON DELETE SET NULL;


--
-- Name: pool_name pool_name_pool_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: kura
--

ALTER TABLE ONLY public.pool_name
    ADD CONSTRAINT pool_name_pool_id_fkey FOREIGN KEY (pool_id) REFERENCES public.pool(id);


--
-- Name: pool_post pool_post_pool_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: kura
--

ALTER TABLE ONLY public.pool_post
    ADD CONSTRAINT pool_post_pool_id_fkey FOREIGN KEY (pool_id) REFERENCES public.pool(id) ON DELETE CASCADE;


--
-- Name: pool_post pool_post_post_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: kura
--

ALTER TABLE ONLY public.pool_post
    ADD CONSTRAINT pool_post_post_id_fkey FOREIGN KEY (post_id) REFERENCES public.post(id) ON DELETE CASCADE;


--
-- Name: pool_state pool_state_pool_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: kura
--

ALTER TABLE ONLY public.pool_state
    ADD CONSTRAINT pool_state_pool_id_fkey FOREIGN KEY (pool_id) REFERENCES public.pool(id) ON DELETE CASCADE;


--
-- Name: pool_url pool_url_pool_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: kura
--

ALTER TABLE ONLY public.pool_url
    ADD CONSTRAINT pool_url_pool_id_fkey FOREIGN KEY (pool_id) REFERENCES public.pool(id) ON DELETE CASCADE;


--
-- Name: post_censor_review post_censor_review_post_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: kura
--

ALTER TABLE ONLY public.post_censor_review
    ADD CONSTRAINT post_censor_review_post_id_fkey FOREIGN KEY (post_id) REFERENCES public.post(id) ON DELETE CASCADE;


--
-- Name: post_censor_review post_censor_review_reviewed_by_fkey; Type: FK CONSTRAINT; Schema: public; Owner: kura
--

ALTER TABLE ONLY public.post_censor_review
    ADD CONSTRAINT post_censor_review_reviewed_by_fkey FOREIGN KEY (reviewed_by) REFERENCES public."user"(id) ON DELETE SET NULL;


--
-- Name: post post_duplicate_of_fkey; Type: FK CONSTRAINT; Schema: public; Owner: kura
--

ALTER TABLE ONLY public.post
    ADD CONSTRAINT post_duplicate_of_fkey FOREIGN KEY (duplicate_of) REFERENCES public.post(id) ON DELETE RESTRICT;


--
-- Name: post_favorite post_favorite_post_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: kura
--

ALTER TABLE ONLY public.post_favorite
    ADD CONSTRAINT post_favorite_post_id_fkey FOREIGN KEY (post_id) REFERENCES public.post(id);


--
-- Name: post_favorite post_favorite_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: kura
--

ALTER TABLE ONLY public.post_favorite
    ADD CONSTRAINT post_favorite_user_id_fkey FOREIGN KEY (user_id) REFERENCES public."user"(id);


--
-- Name: post_feature post_feature_post_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: kura
--

ALTER TABLE ONLY public.post_feature
    ADD CONSTRAINT post_feature_post_id_fkey FOREIGN KEY (post_id) REFERENCES public.post(id);


--
-- Name: post_feature post_feature_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: kura
--

ALTER TABLE ONLY public.post_feature
    ADD CONSTRAINT post_feature_user_id_fkey FOREIGN KEY (user_id) REFERENCES public."user"(id);


--
-- Name: post_listing post_listing_post_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: kura
--

ALTER TABLE ONLY public.post_listing
    ADD CONSTRAINT post_listing_post_id_fkey FOREIGN KEY (post_id) REFERENCES public.post(id) ON UPDATE CASCADE ON DELETE CASCADE;


--
-- Name: post_note post_note_post_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: kura
--

ALTER TABLE ONLY public.post_note
    ADD CONSTRAINT post_note_post_id_fkey FOREIGN KEY (post_id) REFERENCES public.post(id);


--
-- Name: post post_primary_pool_fk; Type: FK CONSTRAINT; Schema: public; Owner: kura
--

ALTER TABLE ONLY public.post
    ADD CONSTRAINT post_primary_pool_fk FOREIGN KEY (primary_pool_id) REFERENCES public.pool(id) ON DELETE SET NULL;


--
-- Name: post_rendition post_rendition_post_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: kura
--

ALTER TABLE ONLY public.post_rendition
    ADD CONSTRAINT post_rendition_post_id_fkey FOREIGN KEY (post_id) REFERENCES public.post(id) ON UPDATE CASCADE ON DELETE CASCADE;


--
-- Name: post_rendition post_rendition_sha256_fkey; Type: FK CONSTRAINT; Schema: public; Owner: kura
--

ALTER TABLE ONLY public.post_rendition
    ADD CONSTRAINT post_rendition_sha256_fkey FOREIGN KEY (sha256) REFERENCES public.rendition_object(sha256);


--
-- Name: post_score post_score_post_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: kura
--

ALTER TABLE ONLY public.post_score
    ADD CONSTRAINT post_score_post_id_fkey FOREIGN KEY (post_id) REFERENCES public.post(id);


--
-- Name: post_score post_score_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: kura
--

ALTER TABLE ONLY public.post_score
    ADD CONSTRAINT post_score_user_id_fkey FOREIGN KEY (user_id) REFERENCES public."user"(id);


--
-- Name: post_tag_audit post_tag_audit_post_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: kura
--

ALTER TABLE ONLY public.post_tag_audit
    ADD CONSTRAINT post_tag_audit_post_id_fkey FOREIGN KEY (post_id) REFERENCES public.post(id) ON DELETE CASCADE;


--
-- Name: post_tag_audit post_tag_audit_release_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: kura
--

ALTER TABLE ONLY public.post_tag_audit
    ADD CONSTRAINT post_tag_audit_release_id_fkey FOREIGN KEY (release_id) REFERENCES public.tagger_release(id);


--
-- Name: post_tag_pin post_tag_pin_post_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: kura
--

ALTER TABLE ONLY public.post_tag_pin
    ADD CONSTRAINT post_tag_pin_post_id_fkey FOREIGN KEY (post_id) REFERENCES public.post(id) ON DELETE CASCADE;


--
-- Name: post_tag_pin post_tag_pin_tag_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: kura
--

ALTER TABLE ONLY public.post_tag_pin
    ADD CONSTRAINT post_tag_pin_tag_id_fkey FOREIGN KEY (tag_id) REFERENCES public.tag(id) ON DELETE CASCADE;


--
-- Name: post_tag post_tag_post_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: kura
--

ALTER TABLE ONLY public.post_tag
    ADD CONSTRAINT post_tag_post_id_fkey FOREIGN KEY (post_id) REFERENCES public.post(id) ON DELETE CASCADE NOT VALID;


--
-- Name: post_tag post_tag_tag_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: kura
--

ALTER TABLE ONLY public.post_tag
    ADD CONSTRAINT post_tag_tag_id_fkey FOREIGN KEY (tag_id) REFERENCES public.tag(id);


--
-- Name: post_tagging_work post_tagging_work_post_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: kura_reconciler
--

ALTER TABLE ONLY public.post_tagging_work
    ADD CONSTRAINT post_tagging_work_post_id_fkey FOREIGN KEY (post_id) REFERENCES public.post(id) ON UPDATE CASCADE ON DELETE CASCADE;


--
-- Name: post post_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: kura
--

ALTER TABLE ONLY public.post
    ADD CONSTRAINT post_user_id_fkey FOREIGN KEY (user_id) REFERENCES public."user"(id) ON DELETE SET NULL;


--
-- Name: post_variant post_variant_created_by_fkey; Type: FK CONSTRAINT; Schema: public; Owner: kura
--

ALTER TABLE ONLY public.post_variant
    ADD CONSTRAINT post_variant_created_by_fkey FOREIGN KEY (created_by) REFERENCES public."user"(id) ON DELETE SET NULL;


--
-- Name: post_variant post_variant_post_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: kura
--

ALTER TABLE ONLY public.post_variant
    ADD CONSTRAINT post_variant_post_id_fkey FOREIGN KEY (post_id) REFERENCES public.post(id) ON DELETE CASCADE;


--
-- Name: tag tag_category_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: kura
--

ALTER TABLE ONLY public.tag
    ADD CONSTRAINT tag_category_id_fkey FOREIGN KEY (category_id) REFERENCES public.tag_category(id);


--
-- Name: tag_implication tag_implication_child_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: kura
--

ALTER TABLE ONLY public.tag_implication
    ADD CONSTRAINT tag_implication_child_id_fkey FOREIGN KEY (child_id) REFERENCES public.tag(id);


--
-- Name: tag_implication tag_implication_parent_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: kura
--

ALTER TABLE ONLY public.tag_implication
    ADD CONSTRAINT tag_implication_parent_id_fkey FOREIGN KEY (parent_id) REFERENCES public.tag(id);


--
-- Name: tag_link tag_link_pool_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: kura
--

ALTER TABLE ONLY public.tag_link
    ADD CONSTRAINT tag_link_pool_id_fkey FOREIGN KEY (pool_id) REFERENCES public.pool(id) ON DELETE SET NULL;


--
-- Name: tag_link tag_link_tag_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: kura
--

ALTER TABLE ONLY public.tag_link
    ADD CONSTRAINT tag_link_tag_id_fkey FOREIGN KEY (tag_id) REFERENCES public.tag(id) ON DELETE CASCADE;


--
-- Name: tag_localization tag_localization_tag_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: kura
--

ALTER TABLE ONLY public.tag_localization
    ADD CONSTRAINT tag_localization_tag_id_fkey FOREIGN KEY (tag_id) REFERENCES public.tag(id) ON DELETE CASCADE;


--
-- Name: tag_name tag_name_tag_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: kura
--

ALTER TABLE ONLY public.tag_name
    ADD CONSTRAINT tag_name_tag_id_fkey FOREIGN KEY (tag_id) REFERENCES public.tag(id);


--
-- Name: tag_pin_inventory tag_pin_inventory_post_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: kura
--

ALTER TABLE ONLY public.tag_pin_inventory
    ADD CONSTRAINT tag_pin_inventory_post_id_fkey FOREIGN KEY (post_id) REFERENCES public.post(id) ON DELETE CASCADE;


--
-- Name: tag_quarantine tag_quarantine_tag_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: kura
--

ALTER TABLE ONLY public.tag_quarantine
    ADD CONSTRAINT tag_quarantine_tag_id_fkey FOREIGN KEY (tag_id) REFERENCES public.tag(id);


--
-- Name: tag_relation tag_relation_a_tag_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: kura
--

ALTER TABLE ONLY public.tag_relation
    ADD CONSTRAINT tag_relation_a_tag_id_fkey FOREIGN KEY (a_tag_id) REFERENCES public.tag(id) ON DELETE CASCADE;


--
-- Name: tag_relation tag_relation_b_tag_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: kura
--

ALTER TABLE ONLY public.tag_relation
    ADD CONSTRAINT tag_relation_b_tag_id_fkey FOREIGN KEY (b_tag_id) REFERENCES public.tag(id) ON DELETE CASCADE;


--
-- Name: tag_suggestion tag_suggestion_child_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: kura
--

ALTER TABLE ONLY public.tag_suggestion
    ADD CONSTRAINT tag_suggestion_child_id_fkey FOREIGN KEY (child_id) REFERENCES public.tag(id);


--
-- Name: tag_suggestion tag_suggestion_parent_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: kura
--

ALTER TABLE ONLY public.tag_suggestion
    ADD CONSTRAINT tag_suggestion_parent_id_fkey FOREIGN KEY (parent_id) REFERENCES public.tag(id);


--
-- Name: tagger_auto_exclude_pool tagger_auto_exclude_pool_pool_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: kura
--

ALTER TABLE ONLY public.tagger_auto_exclude_pool
    ADD CONSTRAINT tagger_auto_exclude_pool_pool_id_fkey FOREIGN KEY (pool_id) REFERENCES public.pool(id) ON DELETE CASCADE;


--
-- Name: tagger_candidate_disposition tagger_candidate_disposition_supersedes_seq_fkey; Type: FK CONSTRAINT; Schema: public; Owner: kura
--

ALTER TABLE ONLY public.tagger_candidate_disposition
    ADD CONSTRAINT tagger_candidate_disposition_supersedes_seq_fkey FOREIGN KEY (supersedes_seq) REFERENCES public.tagger_candidate_disposition(seq);


--
-- Name: tagger_candidate_disposition tagger_candidate_disposition_tag_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: kura
--

ALTER TABLE ONLY public.tagger_candidate_disposition
    ADD CONSTRAINT tagger_candidate_disposition_tag_id_fkey FOREIGN KEY (tag_id) REFERENCES public.tag(id);


--
-- Name: tagger_candidate_event tagger_candidate_event_tag_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: kura
--

ALTER TABLE ONLY public.tagger_candidate_event
    ADD CONSTRAINT tagger_candidate_event_tag_id_fkey FOREIGN KEY (tag_id) REFERENCES public.tag(id);


--
-- Name: tagger_gt_label tagger_gt_label_post_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: kura
--

ALTER TABLE ONLY public.tagger_gt_label
    ADD CONSTRAINT tagger_gt_label_post_id_fkey FOREIGN KEY (post_id) REFERENCES public.post(id) ON DELETE CASCADE;


--
-- Name: tagger_gt_label tagger_gt_label_source_pool_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: kura
--

ALTER TABLE ONLY public.tagger_gt_label
    ADD CONSTRAINT tagger_gt_label_source_pool_id_fkey FOREIGN KEY (source_pool_id) REFERENCES public.pool(id);


--
-- Name: tagger_gt_label tagger_gt_label_tag_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: kura
--

ALTER TABLE ONLY public.tagger_gt_label
    ADD CONSTRAINT tagger_gt_label_tag_id_fkey FOREIGN KEY (tag_id) REFERENCES public.tag(id);


--
-- Name: tagger_head_candidate tagger_head_candidate_promoted_patch_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: kura
--

ALTER TABLE ONLY public.tagger_head_candidate
    ADD CONSTRAINT tagger_head_candidate_promoted_patch_id_fkey FOREIGN KEY (promoted_patch_id) REFERENCES public.tagger_patch(id);


--
-- Name: tagger_head_candidate tagger_head_candidate_source_pool_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: kura
--

ALTER TABLE ONLY public.tagger_head_candidate
    ADD CONSTRAINT tagger_head_candidate_source_pool_id_fkey FOREIGN KEY (source_pool_id) REFERENCES public.pool(id) ON DELETE SET NULL;


--
-- Name: tagger_head_candidate tagger_head_candidate_tag_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: kura
--

ALTER TABLE ONLY public.tagger_head_candidate
    ADD CONSTRAINT tagger_head_candidate_tag_id_fkey FOREIGN KEY (tag_id) REFERENCES public.tag(id);


--
-- Name: tagger_head_policy tagger_head_policy_content_sha256_fkey; Type: FK CONSTRAINT; Schema: public; Owner: kura
--

ALTER TABLE ONLY public.tagger_head_policy
    ADD CONSTRAINT tagger_head_policy_content_sha256_fkey FOREIGN KEY (content_sha256) REFERENCES public.tagger_artifact(content_sha256);


--
-- Name: tagger_head_policy tagger_head_policy_patch_fkey; Type: FK CONSTRAINT; Schema: public; Owner: kura
--

ALTER TABLE ONLY public.tagger_head_policy
    ADD CONSTRAINT tagger_head_policy_patch_fkey FOREIGN KEY (tag_id, patch_version) REFERENCES public.tagger_patch(tag_id, version) ON DELETE CASCADE;


--
-- Name: tagger_head_policy tagger_head_policy_tag_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: kura
--

ALTER TABLE ONLY public.tagger_head_policy
    ADD CONSTRAINT tagger_head_policy_tag_id_fkey FOREIGN KEY (tag_id) REFERENCES public.tag(id);


--
-- Name: tagger_lifecycle_event tagger_lifecycle_event_from_stage_fkey; Type: FK CONSTRAINT; Schema: public; Owner: kura
--

ALTER TABLE ONLY public.tagger_lifecycle_event
    ADD CONSTRAINT tagger_lifecycle_event_from_stage_fkey FOREIGN KEY (from_stage) REFERENCES public.tagger_stage(stage);


--
-- Name: tagger_lifecycle_event tagger_lifecycle_event_tag_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: kura
--

ALTER TABLE ONLY public.tagger_lifecycle_event
    ADD CONSTRAINT tagger_lifecycle_event_tag_id_fkey FOREIGN KEY (tag_id) REFERENCES public.tag(id);


--
-- Name: tagger_lifecycle_event tagger_lifecycle_event_to_stage_fkey; Type: FK CONSTRAINT; Schema: public; Owner: kura
--

ALTER TABLE ONLY public.tagger_lifecycle_event
    ADD CONSTRAINT tagger_lifecycle_event_to_stage_fkey FOREIGN KEY (to_stage) REFERENCES public.tagger_stage(stage);


--
-- Name: tagger_node_head tagger_node_head_content_sha256_fkey; Type: FK CONSTRAINT; Schema: public; Owner: kura
--

ALTER TABLE ONLY public.tagger_node_head
    ADD CONSTRAINT tagger_node_head_content_sha256_fkey FOREIGN KEY (content_sha256) REFERENCES public.tagger_artifact(content_sha256);


--
-- Name: tagger_node_head tagger_node_head_tag_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: kura
--

ALTER TABLE ONLY public.tagger_node_head
    ADD CONSTRAINT tagger_node_head_tag_id_fkey FOREIGN KEY (tag_id) REFERENCES public.tag(id);


--
-- Name: tagger_node_state tagger_node_state_release_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: kura
--

ALTER TABLE ONLY public.tagger_node_state
    ADD CONSTRAINT tagger_node_state_release_id_fkey FOREIGN KEY (release_id) REFERENCES public.tagger_release(id);


--
-- Name: tagger_patch tagger_patch_first_release_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: kura
--

ALTER TABLE ONLY public.tagger_patch
    ADD CONSTRAINT tagger_patch_first_release_id_fkey FOREIGN KEY (first_release_id) REFERENCES public.tagger_release(id);


--
-- Name: tagger_patch tagger_patch_tag_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: kura
--

ALTER TABLE ONLY public.tagger_patch
    ADD CONSTRAINT tagger_patch_tag_id_fkey FOREIGN KEY (tag_id) REFERENCES public.tag(id);


--
-- Name: tagger_registration_request tagger_registration_request_promoted_patch_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: kura
--

ALTER TABLE ONLY public.tagger_registration_request
    ADD CONSTRAINT tagger_registration_request_promoted_patch_id_fkey FOREIGN KEY (promoted_patch_id) REFERENCES public.tagger_patch(id);


--
-- Name: tagger_registration_request tagger_registration_request_tag_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: kura
--

ALTER TABLE ONLY public.tagger_registration_request
    ADD CONSTRAINT tagger_registration_request_tag_id_fkey FOREIGN KEY (tag_id) REFERENCES public.tag(id);


--
-- Name: tagger_release_active tagger_release_active_release_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: kura
--

ALTER TABLE ONLY public.tagger_release_active
    ADD CONSTRAINT tagger_release_active_release_id_fkey FOREIGN KEY (release_id) REFERENCES public.tagger_release(id);


--
-- Name: tagger_release_admissibility tagger_release_admissibility_release_digest_fkey; Type: FK CONSTRAINT; Schema: public; Owner: kura
--

ALTER TABLE ONLY public.tagger_release_admissibility
    ADD CONSTRAINT tagger_release_admissibility_release_digest_fkey FOREIGN KEY (release_digest) REFERENCES public.tagger_release_manifest(release_digest);


--
-- Name: tagger_release_component tagger_release_component_content_sha256_fkey; Type: FK CONSTRAINT; Schema: public; Owner: kura
--

ALTER TABLE ONLY public.tagger_release_component
    ADD CONSTRAINT tagger_release_component_content_sha256_fkey FOREIGN KEY (content_sha256) REFERENCES public.tagger_artifact(content_sha256);


--
-- Name: tagger_release_component tagger_release_component_release_digest_fkey; Type: FK CONSTRAINT; Schema: public; Owner: kura
--

ALTER TABLE ONLY public.tagger_release_component
    ADD CONSTRAINT tagger_release_component_release_digest_fkey FOREIGN KEY (release_digest) REFERENCES public.tagger_release_manifest(release_digest);


--
-- Name: tagger_release_manifest tagger_release_manifest_release_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: kura
--

ALTER TABLE ONLY public.tagger_release_manifest
    ADD CONSTRAINT tagger_release_manifest_release_id_fkey FOREIGN KEY (release_id) REFERENCES public.tagger_release(id);


--
-- Name: tagger_stage_transition tagger_stage_transition_from_stage_fkey; Type: FK CONSTRAINT; Schema: public; Owner: kura
--

ALTER TABLE ONLY public.tagger_stage_transition
    ADD CONSTRAINT tagger_stage_transition_from_stage_fkey FOREIGN KEY (from_stage) REFERENCES public.tagger_stage(stage);


--
-- Name: tagger_stage_transition tagger_stage_transition_to_stage_fkey; Type: FK CONSTRAINT; Schema: public; Owner: kura
--

ALTER TABLE ONLY public.tagger_stage_transition
    ADD CONSTRAINT tagger_stage_transition_to_stage_fkey FOREIGN KEY (to_stage) REFERENCES public.tagger_stage(stage);


--
-- Name: tagger_sweep_run tagger_sweep_run_release_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: kura
--

ALTER TABLE ONLY public.tagger_sweep_run
    ADD CONSTRAINT tagger_sweep_run_release_id_fkey FOREIGN KEY (release_id) REFERENCES public.tagger_release(id);


--
-- Name: tagger_tag_exclusion tagger_tag_exclusion_condition_fkey; Type: FK CONSTRAINT; Schema: public; Owner: kura
--

ALTER TABLE ONLY public.tagger_tag_exclusion
    ADD CONSTRAINT tagger_tag_exclusion_condition_fkey FOREIGN KEY (condition) REFERENCES public.tagger_tag_condition(name);


--
-- Name: tagger_tag_exclusion tagger_tag_exclusion_tag_a_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: kura
--

ALTER TABLE ONLY public.tagger_tag_exclusion
    ADD CONSTRAINT tagger_tag_exclusion_tag_a_id_fkey FOREIGN KEY (tag_a_id) REFERENCES public.tag(id);


--
-- Name: tagger_tag_exclusion tagger_tag_exclusion_tag_b_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: kura
--

ALTER TABLE ONLY public.tagger_tag_exclusion
    ADD CONSTRAINT tagger_tag_exclusion_tag_b_id_fkey FOREIGN KEY (tag_b_id) REFERENCES public.tag(id);


--
-- Name: tagger_tag_rule tagger_tag_rule_condition_fkey; Type: FK CONSTRAINT; Schema: public; Owner: kura
--

ALTER TABLE ONLY public.tagger_tag_rule
    ADD CONSTRAINT tagger_tag_rule_condition_fkey FOREIGN KEY (condition) REFERENCES public.tagger_tag_condition(name);


--
-- Name: tagger_tag_rule tagger_tag_rule_tag_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: kura
--

ALTER TABLE ONLY public.tagger_tag_rule
    ADD CONSTRAINT tagger_tag_rule_tag_id_fkey FOREIGN KEY (tag_id) REFERENCES public.tag(id);


--
-- Name: tagger_worker_heartbeat tagger_worker_heartbeat_release_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: kura
--

ALTER TABLE ONLY public.tagger_worker_heartbeat
    ADD CONSTRAINT tagger_worker_heartbeat_release_id_fkey FOREIGN KEY (release_id) REFERENCES public.tagger_release(id);


--
-- Name: user_token user_token_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: kura
--

ALTER TABLE ONLY public.user_token
    ADD CONSTRAINT user_token_user_id_fkey FOREIGN KEY (user_id) REFERENCES public."user"(id) ON DELETE CASCADE;


--
-- Name: SCHEMA public; Type: ACL; Schema: -; Owner: kura
--

GRANT ALL ON SCHEMA public TO kura_reconciler;


--
-- Name: FUNCTION gtrgm_in(cstring); Type: ACL; Schema: public; Owner: postgres
--

GRANT ALL ON FUNCTION public.gtrgm_in(cstring) TO kura;


--
-- Name: FUNCTION gtrgm_out(public.gtrgm); Type: ACL; Schema: public; Owner: postgres
--

GRANT ALL ON FUNCTION public.gtrgm_out(public.gtrgm) TO kura;


--
-- Name: FUNCTION daitch_mokotoff(text); Type: ACL; Schema: public; Owner: postgres
--

GRANT ALL ON FUNCTION public.daitch_mokotoff(text) TO kura;


--
-- Name: FUNCTION difference(text, text); Type: ACL; Schema: public; Owner: postgres
--

GRANT ALL ON FUNCTION public.difference(text, text) TO kura;


--
-- Name: FUNCTION dmetaphone(text); Type: ACL; Schema: public; Owner: postgres
--

GRANT ALL ON FUNCTION public.dmetaphone(text) TO kura;


--
-- Name: FUNCTION dmetaphone_alt(text); Type: ACL; Schema: public; Owner: postgres
--

GRANT ALL ON FUNCTION public.dmetaphone_alt(text) TO kura;


--
-- Name: FUNCTION gin_extract_query_trgm(text, internal, smallint, internal, internal, internal, internal); Type: ACL; Schema: public; Owner: postgres
--

GRANT ALL ON FUNCTION public.gin_extract_query_trgm(text, internal, smallint, internal, internal, internal, internal) TO kura;


--
-- Name: FUNCTION gin_extract_value_trgm(text, internal); Type: ACL; Schema: public; Owner: postgres
--

GRANT ALL ON FUNCTION public.gin_extract_value_trgm(text, internal) TO kura;


--
-- Name: FUNCTION gin_trgm_consistent(internal, smallint, text, integer, internal, internal, internal, internal); Type: ACL; Schema: public; Owner: postgres
--

GRANT ALL ON FUNCTION public.gin_trgm_consistent(internal, smallint, text, integer, internal, internal, internal, internal) TO kura;


--
-- Name: FUNCTION gin_trgm_triconsistent(internal, smallint, text, integer, internal, internal, internal); Type: ACL; Schema: public; Owner: postgres
--

GRANT ALL ON FUNCTION public.gin_trgm_triconsistent(internal, smallint, text, integer, internal, internal, internal) TO kura;


--
-- Name: FUNCTION gtrgm_compress(internal); Type: ACL; Schema: public; Owner: postgres
--

GRANT ALL ON FUNCTION public.gtrgm_compress(internal) TO kura;


--
-- Name: FUNCTION gtrgm_consistent(internal, text, smallint, oid, internal); Type: ACL; Schema: public; Owner: postgres
--

GRANT ALL ON FUNCTION public.gtrgm_consistent(internal, text, smallint, oid, internal) TO kura;


--
-- Name: FUNCTION gtrgm_decompress(internal); Type: ACL; Schema: public; Owner: postgres
--

GRANT ALL ON FUNCTION public.gtrgm_decompress(internal) TO kura;


--
-- Name: FUNCTION gtrgm_distance(internal, text, smallint, oid, internal); Type: ACL; Schema: public; Owner: postgres
--

GRANT ALL ON FUNCTION public.gtrgm_distance(internal, text, smallint, oid, internal) TO kura;


--
-- Name: FUNCTION gtrgm_options(internal); Type: ACL; Schema: public; Owner: postgres
--

GRANT ALL ON FUNCTION public.gtrgm_options(internal) TO kura;


--
-- Name: FUNCTION gtrgm_penalty(internal, internal, internal); Type: ACL; Schema: public; Owner: postgres
--

GRANT ALL ON FUNCTION public.gtrgm_penalty(internal, internal, internal) TO kura;


--
-- Name: FUNCTION gtrgm_picksplit(internal, internal); Type: ACL; Schema: public; Owner: postgres
--

GRANT ALL ON FUNCTION public.gtrgm_picksplit(internal, internal) TO kura;


--
-- Name: FUNCTION gtrgm_same(public.gtrgm, public.gtrgm, internal); Type: ACL; Schema: public; Owner: postgres
--

GRANT ALL ON FUNCTION public.gtrgm_same(public.gtrgm, public.gtrgm, internal) TO kura;


--
-- Name: FUNCTION gtrgm_union(internal, internal); Type: ACL; Schema: public; Owner: postgres
--

GRANT ALL ON FUNCTION public.gtrgm_union(internal, internal) TO kura;


--
-- Name: FUNCTION levenshtein(text, text); Type: ACL; Schema: public; Owner: postgres
--

GRANT ALL ON FUNCTION public.levenshtein(text, text) TO kura;


--
-- Name: FUNCTION levenshtein(text, text, integer, integer, integer); Type: ACL; Schema: public; Owner: postgres
--

GRANT ALL ON FUNCTION public.levenshtein(text, text, integer, integer, integer) TO kura;


--
-- Name: FUNCTION levenshtein_less_equal(text, text, integer); Type: ACL; Schema: public; Owner: postgres
--

GRANT ALL ON FUNCTION public.levenshtein_less_equal(text, text, integer) TO kura;


--
-- Name: FUNCTION levenshtein_less_equal(text, text, integer, integer, integer, integer); Type: ACL; Schema: public; Owner: postgres
--

GRANT ALL ON FUNCTION public.levenshtein_less_equal(text, text, integer, integer, integer, integer) TO kura;


--
-- Name: FUNCTION metaphone(text, integer); Type: ACL; Schema: public; Owner: postgres
--

GRANT ALL ON FUNCTION public.metaphone(text, integer) TO kura;


--
-- Name: FUNCTION set_limit(real); Type: ACL; Schema: public; Owner: postgres
--

GRANT ALL ON FUNCTION public.set_limit(real) TO kura;


--
-- Name: FUNCTION show_limit(); Type: ACL; Schema: public; Owner: postgres
--

GRANT ALL ON FUNCTION public.show_limit() TO kura;


--
-- Name: FUNCTION show_trgm(text); Type: ACL; Schema: public; Owner: postgres
--

GRANT ALL ON FUNCTION public.show_trgm(text) TO kura;


--
-- Name: FUNCTION similarity(text, text); Type: ACL; Schema: public; Owner: postgres
--

GRANT ALL ON FUNCTION public.similarity(text, text) TO kura;


--
-- Name: FUNCTION similarity_dist(text, text); Type: ACL; Schema: public; Owner: postgres
--

GRANT ALL ON FUNCTION public.similarity_dist(text, text) TO kura;


--
-- Name: FUNCTION similarity_op(text, text); Type: ACL; Schema: public; Owner: postgres
--

GRANT ALL ON FUNCTION public.similarity_op(text, text) TO kura;


--
-- Name: FUNCTION soundex(text); Type: ACL; Schema: public; Owner: postgres
--

GRANT ALL ON FUNCTION public.soundex(text) TO kura;


--
-- Name: FUNCTION strict_word_similarity(text, text); Type: ACL; Schema: public; Owner: postgres
--

GRANT ALL ON FUNCTION public.strict_word_similarity(text, text) TO kura;


--
-- Name: FUNCTION strict_word_similarity_commutator_op(text, text); Type: ACL; Schema: public; Owner: postgres
--

GRANT ALL ON FUNCTION public.strict_word_similarity_commutator_op(text, text) TO kura;


--
-- Name: FUNCTION strict_word_similarity_dist_commutator_op(text, text); Type: ACL; Schema: public; Owner: postgres
--

GRANT ALL ON FUNCTION public.strict_word_similarity_dist_commutator_op(text, text) TO kura;


--
-- Name: FUNCTION strict_word_similarity_dist_op(text, text); Type: ACL; Schema: public; Owner: postgres
--

GRANT ALL ON FUNCTION public.strict_word_similarity_dist_op(text, text) TO kura;


--
-- Name: FUNCTION strict_word_similarity_op(text, text); Type: ACL; Schema: public; Owner: postgres
--

GRANT ALL ON FUNCTION public.strict_word_similarity_op(text, text) TO kura;


--
-- Name: FUNCTION tagger_acquire_lease(p_scope text, p_lease_seconds integer); Type: ACL; Schema: public; Owner: kura_reconciler
--

REVOKE ALL ON FUNCTION public.tagger_acquire_lease(p_scope text, p_lease_seconds integer) FROM PUBLIC;
GRANT ALL ON FUNCTION public.tagger_acquire_lease(p_scope text, p_lease_seconds integer) TO kura;


--
-- Name: FUNCTION tagger_check_fencing(p_scope text, p_fencing_token bigint); Type: ACL; Schema: public; Owner: kura_reconciler
--

REVOKE ALL ON FUNCTION public.tagger_check_fencing(p_scope text, p_fencing_token bigint) FROM PUBLIC;


--
-- Name: FUNCTION tagger_governed_add_head_candidate(p_tag text, p_source_pool_id integer, p_reason text, p_added_by text); Type: ACL; Schema: public; Owner: kura_reconciler
--

REVOKE ALL ON FUNCTION public.tagger_governed_add_head_candidate(p_tag text, p_source_pool_id integer, p_reason text, p_added_by text) FROM PUBLIC;
GRANT ALL ON FUNCTION public.tagger_governed_add_head_candidate(p_tag text, p_source_pool_id integer, p_reason text, p_added_by text) TO kura;


--
-- Name: FUNCTION tagger_governed_claim_auto_tag(p_limit integer, p_lease_seconds integer); Type: ACL; Schema: public; Owner: kura_reconciler
--

REVOKE ALL ON FUNCTION public.tagger_governed_claim_auto_tag(p_limit integer, p_lease_seconds integer) FROM PUBLIC;
GRANT ALL ON FUNCTION public.tagger_governed_claim_auto_tag(p_limit integer, p_lease_seconds integer) TO kura;


--
-- Name: FUNCTION tagger_governed_complete_ok(p_post_id integer); Type: ACL; Schema: public; Owner: kura_reconciler
--

REVOKE ALL ON FUNCTION public.tagger_governed_complete_ok(p_post_id integer) FROM PUBLIC;
GRANT ALL ON FUNCTION public.tagger_governed_complete_ok(p_post_id integer) TO kura;


--
-- Name: FUNCTION tagger_governed_complete_retry(p_post_id integer, p_status text, p_error text); Type: ACL; Schema: public; Owner: kura_reconciler
--

REVOKE ALL ON FUNCTION public.tagger_governed_complete_retry(p_post_id integer, p_status text, p_error text) FROM PUBLIC;
GRANT ALL ON FUNCTION public.tagger_governed_complete_retry(p_post_id integer, p_status text, p_error text) TO kura;


--
-- Name: FUNCTION tagger_governed_dismiss_head_candidate(p_tag text, p_reason text); Type: ACL; Schema: public; Owner: kura_reconciler
--

REVOKE ALL ON FUNCTION public.tagger_governed_dismiss_head_candidate(p_tag text, p_reason text) FROM PUBLIC;
GRANT ALL ON FUNCTION public.tagger_governed_dismiss_head_candidate(p_tag text, p_reason text) TO kura;


--
-- Name: FUNCTION tagger_governed_expire_candidate_disposition(p_tag text, p_decided_by text); Type: ACL; Schema: public; Owner: kura_reconciler
--

REVOKE ALL ON FUNCTION public.tagger_governed_expire_candidate_disposition(p_tag text, p_decided_by text) FROM PUBLIC;
GRANT ALL ON FUNCTION public.tagger_governed_expire_candidate_disposition(p_tag text, p_decided_by text) TO kura;


--
-- Name: FUNCTION tagger_governed_hold_head_candidate(p_tag text, p_reason_class text, p_evidence jsonb); Type: ACL; Schema: public; Owner: kura_reconciler
--

REVOKE ALL ON FUNCTION public.tagger_governed_hold_head_candidate(p_tag text, p_reason_class text, p_evidence jsonb) FROM PUBLIC;
GRANT ALL ON FUNCTION public.tagger_governed_hold_head_candidate(p_tag text, p_reason_class text, p_evidence jsonb) TO kura;


--
-- Name: FUNCTION tagger_governed_promote_registration(p_registration_id bigint, p_patch_id integer); Type: ACL; Schema: public; Owner: kura_reconciler
--

REVOKE ALL ON FUNCTION public.tagger_governed_promote_registration(p_registration_id bigint, p_patch_id integer) FROM PUBLIC;
GRANT ALL ON FUNCTION public.tagger_governed_promote_registration(p_registration_id bigint, p_patch_id integer) TO kura;


--
-- Name: FUNCTION tagger_governed_publish_release(p_commit_sha text, p_base_model_repo text, p_base_model_revision text, p_patch_set_digest text, p_model_sha256 text, p_model_data bytea, p_selected_tags_csv bytea, p_n_stock integer, p_n_out integer, p_patched jsonb, p_lockfile text, p_created_by text); Type: ACL; Schema: public; Owner: kura_reconciler
--

REVOKE ALL ON FUNCTION public.tagger_governed_publish_release(p_commit_sha text, p_base_model_repo text, p_base_model_revision text, p_patch_set_digest text, p_model_sha256 text, p_model_data bytea, p_selected_tags_csv bytea, p_n_stock integer, p_n_out integer, p_patched jsonb, p_lockfile text, p_created_by text) FROM PUBLIC;
GRANT ALL ON FUNCTION public.tagger_governed_publish_release(p_commit_sha text, p_base_model_repo text, p_base_model_revision text, p_patch_set_digest text, p_model_sha256 text, p_model_data bytea, p_selected_tags_csv bytea, p_n_stock integer, p_n_out integer, p_patched jsonb, p_lockfile text, p_created_by text) TO kura;


--
-- Name: FUNCTION tagger_governed_purge_ineligible_gt_labels(); Type: ACL; Schema: public; Owner: kura_reconciler
--

REVOKE ALL ON FUNCTION public.tagger_governed_purge_ineligible_gt_labels() FROM PUBLIC;
GRANT ALL ON FUNCTION public.tagger_governed_purge_ineligible_gt_labels() TO kura;


--
-- Name: FUNCTION tagger_governed_record_candidate_disposition(p_tag text, p_disposition text, p_reason_class text, p_reason text, p_decided_by text, p_actor_type text, p_valid_until timestamp with time zone, p_release_condition jsonb, p_supersedes_seq bigint); Type: ACL; Schema: public; Owner: kura_reconciler
--

REVOKE ALL ON FUNCTION public.tagger_governed_record_candidate_disposition(p_tag text, p_disposition text, p_reason_class text, p_reason text, p_decided_by text, p_actor_type text, p_valid_until timestamp with time zone, p_release_condition jsonb, p_supersedes_seq bigint) FROM PUBLIC;
GRANT ALL ON FUNCTION public.tagger_governed_record_candidate_disposition(p_tag text, p_disposition text, p_reason_class text, p_reason text, p_decided_by text, p_actor_type text, p_valid_until timestamp with time zone, p_release_condition jsonb, p_supersedes_seq bigint) TO kura;


--
-- Name: FUNCTION tagger_governed_release_head_candidate(p_tag text, p_reason text); Type: ACL; Schema: public; Owner: kura_reconciler
--

REVOKE ALL ON FUNCTION public.tagger_governed_release_head_candidate(p_tag text, p_reason text) FROM PUBLIC;
GRANT ALL ON FUNCTION public.tagger_governed_release_head_candidate(p_tag text, p_reason text) TO kura;


--
-- Name: FUNCTION tagger_governed_release_pool_provenance(p_pool_id integer); Type: ACL; Schema: public; Owner: kura_reconciler
--

REVOKE ALL ON FUNCTION public.tagger_governed_release_pool_provenance(p_pool_id integer) FROM PUBLIC;
GRANT ALL ON FUNCTION public.tagger_governed_release_pool_provenance(p_pool_id integer) TO kura;


--
-- Name: FUNCTION tagger_governed_reopen_headless_candidate(p_tag text, p_reason text); Type: ACL; Schema: public; Owner: kura_reconciler
--

REVOKE ALL ON FUNCTION public.tagger_governed_reopen_headless_candidate(p_tag text, p_reason text) FROM PUBLIC;
GRANT ALL ON FUNCTION public.tagger_governed_reopen_headless_candidate(p_tag text, p_reason text) TO kura;


--
-- Name: FUNCTION tagger_governed_reopen_overdetected_candidate(p_tag text, p_reason text); Type: ACL; Schema: public; Owner: kura_reconciler
--

REVOKE ALL ON FUNCTION public.tagger_governed_reopen_overdetected_candidate(p_tag text, p_reason text) FROM PUBLIC;
GRANT ALL ON FUNCTION public.tagger_governed_reopen_overdetected_candidate(p_tag text, p_reason text) TO kura;


--
-- Name: FUNCTION tagger_governed_requeue(p_post_ids integer[], p_fencing_token bigint); Type: ACL; Schema: public; Owner: kura_reconciler
--

REVOKE ALL ON FUNCTION public.tagger_governed_requeue(p_post_ids integer[], p_fencing_token bigint) FROM PUBLIC;
GRANT ALL ON FUNCTION public.tagger_governed_requeue(p_post_ids integer[], p_fencing_token bigint) TO kura;


--
-- Name: FUNCTION tagger_governed_retract_pin_gt(p_tag text, p_post_id integer); Type: ACL; Schema: public; Owner: kura_reconciler
--

REVOKE ALL ON FUNCTION public.tagger_governed_retract_pin_gt(p_tag text, p_post_id integer) FROM PUBLIC;
GRANT ALL ON FUNCTION public.tagger_governed_retract_pin_gt(p_tag text, p_post_id integer) TO kura;


--
-- Name: FUNCTION tagger_governed_set_config(p_key text, p_value numeric, p_updated_by text); Type: ACL; Schema: public; Owner: kura_reconciler
--

REVOKE ALL ON FUNCTION public.tagger_governed_set_config(p_key text, p_value numeric, p_updated_by text) FROM PUBLIC;
GRANT ALL ON FUNCTION public.tagger_governed_set_config(p_key text, p_value numeric, p_updated_by text) TO kura;


--
-- Name: FUNCTION tagger_governed_set_registration_status(p_request_id bigint, p_status text, p_rejected_reason text, p_review_class text); Type: ACL; Schema: public; Owner: kura_reconciler
--

REVOKE ALL ON FUNCTION public.tagger_governed_set_registration_status(p_request_id bigint, p_status text, p_rejected_reason text, p_review_class text) FROM PUBLIC;
GRANT ALL ON FUNCTION public.tagger_governed_set_registration_status(p_request_id bigint, p_status text, p_rejected_reason text, p_review_class text) TO kura;


--
-- Name: FUNCTION tagger_governed_sync_neg_gt_from_pins(); Type: ACL; Schema: public; Owner: kura_reconciler
--

REVOKE ALL ON FUNCTION public.tagger_governed_sync_neg_gt_from_pins() FROM PUBLIC;
GRANT ALL ON FUNCTION public.tagger_governed_sync_neg_gt_from_pins() TO kura;


--
-- Name: FUNCTION tagger_governed_sync_pos_gt_from_pins(); Type: ACL; Schema: public; Owner: kura_reconciler
--

REVOKE ALL ON FUNCTION public.tagger_governed_sync_pos_gt_from_pins() FROM PUBLIC;
GRANT ALL ON FUNCTION public.tagger_governed_sync_pos_gt_from_pins() TO kura;


--
-- Name: FUNCTION tagger_governed_upsert_gt_label(p_tag text, p_post_id integer, p_verdict text, p_source_pool_id integer, p_derived_from text); Type: ACL; Schema: public; Owner: kura_reconciler
--

REVOKE ALL ON FUNCTION public.tagger_governed_upsert_gt_label(p_tag text, p_post_id integer, p_verdict text, p_source_pool_id integer, p_derived_from text) FROM PUBLIC;
GRANT ALL ON FUNCTION public.tagger_governed_upsert_gt_label(p_tag text, p_post_id integer, p_verdict text, p_source_pool_id integer, p_derived_from text) TO kura;


--
-- Name: FUNCTION tagger_governed_upsert_patch(p_tag text, p_version integer, p_status text, p_patch_sha256 text, p_why text, p_why_rejected text, p_metrics jsonb, p_gt jsonb, p_head_type text, p_base_repo text, p_base_revision text, p_logit_index integer, p_created_at date, p_created_by text, p_model_data bytea); Type: ACL; Schema: public; Owner: kura_reconciler
--

REVOKE ALL ON FUNCTION public.tagger_governed_upsert_patch(p_tag text, p_version integer, p_status text, p_patch_sha256 text, p_why text, p_why_rejected text, p_metrics jsonb, p_gt jsonb, p_head_type text, p_base_repo text, p_base_revision text, p_logit_index integer, p_created_at date, p_created_by text, p_model_data bytea) FROM PUBLIC;
GRANT ALL ON FUNCTION public.tagger_governed_upsert_patch(p_tag text, p_version integer, p_status text, p_patch_sha256 text, p_why text, p_why_rejected text, p_metrics jsonb, p_gt jsonb, p_head_type text, p_base_repo text, p_base_revision text, p_logit_index integer, p_created_at date, p_created_by text, p_model_data bytea) TO kura;


--
-- Name: FUNCTION tagger_record_lifecycle_event(p_tag text, p_from_stage text, p_to_stage text, p_reason text, p_fencing_token bigint); Type: ACL; Schema: public; Owner: kura_reconciler
--

REVOKE ALL ON FUNCTION public.tagger_record_lifecycle_event(p_tag text, p_from_stage text, p_to_stage text, p_reason text, p_fencing_token bigint) FROM PUBLIC;
GRANT ALL ON FUNCTION public.tagger_record_lifecycle_event(p_tag text, p_from_stage text, p_to_stage text, p_reason text, p_fencing_token bigint) TO kura;


--
-- Name: FUNCTION tagger_record_operation(p_scope text, p_target_id text, p_operation_type text, p_fencing_token bigint, p_result text); Type: ACL; Schema: public; Owner: kura_reconciler
--

REVOKE ALL ON FUNCTION public.tagger_record_operation(p_scope text, p_target_id text, p_operation_type text, p_fencing_token bigint, p_result text) FROM PUBLIC;
GRANT ALL ON FUNCTION public.tagger_record_operation(p_scope text, p_target_id text, p_operation_type text, p_fencing_token bigint, p_result text) TO kura;


--
-- Name: FUNCTION tagger_release_lease(p_scope text, p_fencing_token bigint); Type: ACL; Schema: public; Owner: kura_reconciler
--

REVOKE ALL ON FUNCTION public.tagger_release_lease(p_scope text, p_fencing_token bigint) FROM PUBLIC;
GRANT ALL ON FUNCTION public.tagger_release_lease(p_scope text, p_fencing_token bigint) TO kura;


--
-- Name: FUNCTION tagger_set_reconciler_pause(p_paused boolean, p_halt_reason text, p_fencing_token bigint); Type: ACL; Schema: public; Owner: kura_reconciler
--

REVOKE ALL ON FUNCTION public.tagger_set_reconciler_pause(p_paused boolean, p_halt_reason text, p_fencing_token bigint) FROM PUBLIC;
GRANT ALL ON FUNCTION public.tagger_set_reconciler_pause(p_paused boolean, p_halt_reason text, p_fencing_token bigint) TO kura;


--
-- Name: FUNCTION tagger_upsert_heartbeat(p_worker_id text, p_host text, p_profile text, p_generation bigint, p_release_id integer, p_provider text, p_model_sha256 text); Type: ACL; Schema: public; Owner: kura_reconciler
--

REVOKE ALL ON FUNCTION public.tagger_upsert_heartbeat(p_worker_id text, p_host text, p_profile text, p_generation bigint, p_release_id integer, p_provider text, p_model_sha256 text) FROM PUBLIC;
GRANT ALL ON FUNCTION public.tagger_upsert_heartbeat(p_worker_id text, p_host text, p_profile text, p_generation bigint, p_release_id integer, p_provider text, p_model_sha256 text) TO kura;


--
-- Name: FUNCTION text_soundex(text); Type: ACL; Schema: public; Owner: postgres
--

GRANT ALL ON FUNCTION public.text_soundex(text) TO kura;


--
-- Name: FUNCTION trg_post_tagging_work_mirror(); Type: ACL; Schema: public; Owner: kura_reconciler
--

REVOKE ALL ON FUNCTION public.trg_post_tagging_work_mirror() FROM PUBLIC;


--
-- Name: FUNCTION word_similarity(text, text); Type: ACL; Schema: public; Owner: postgres
--

GRANT ALL ON FUNCTION public.word_similarity(text, text) TO kura;


--
-- Name: FUNCTION word_similarity_commutator_op(text, text); Type: ACL; Schema: public; Owner: postgres
--

GRANT ALL ON FUNCTION public.word_similarity_commutator_op(text, text) TO kura;


--
-- Name: FUNCTION word_similarity_dist_commutator_op(text, text); Type: ACL; Schema: public; Owner: postgres
--

GRANT ALL ON FUNCTION public.word_similarity_dist_commutator_op(text, text) TO kura;


--
-- Name: FUNCTION word_similarity_dist_op(text, text); Type: ACL; Schema: public; Owner: postgres
--

GRANT ALL ON FUNCTION public.word_similarity_dist_op(text, text) TO kura;


--
-- Name: FUNCTION word_similarity_op(text, text); Type: ACL; Schema: public; Owner: postgres
--

GRANT ALL ON FUNCTION public.word_similarity_op(text, text) TO kura;


--
-- Name: TABLE pool_post; Type: ACL; Schema: public; Owner: kura
--

GRANT SELECT ON TABLE public.pool_post TO kura_reconciler;


--
-- Name: TABLE pool; Type: ACL; Schema: public; Owner: kura
--

GRANT SELECT ON TABLE public.pool TO kura_reconciler;


--
-- Name: TABLE post; Type: ACL; Schema: public; Owner: kura
--

GRANT SELECT ON TABLE public.post TO kura_reconciler;


--
-- Name: TABLE post_tag; Type: ACL; Schema: public; Owner: kura
--

GRANT SELECT ON TABLE public.post_tag TO kura_reconciler;


--
-- Name: TABLE post_tag_actor_claim; Type: ACL; Schema: public; Owner: kura
--

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.post_tag_actor_claim TO kura_audit;


--
-- Name: TABLE post_tag_pin; Type: ACL; Schema: public; Owner: kura
--

GRANT SELECT ON TABLE public.post_tag_pin TO kura_reconciler;


--
-- Name: TABLE post_tagging_work; Type: ACL; Schema: public; Owner: kura_reconciler
--

GRANT SELECT ON TABLE public.post_tagging_work TO kura;


--
-- Name: TABLE tag_name; Type: ACL; Schema: public; Owner: kura
--

GRANT SELECT ON TABLE public.tag_name TO kura_reconciler;


--
-- Name: TABLE tagger_allowed_executor; Type: ACL; Schema: public; Owner: kura
--

GRANT SELECT ON TABLE public.tagger_allowed_executor TO kura_reconciler;


--
-- Name: TABLE tagger_artifact; Type: ACL; Schema: public; Owner: kura
--

GRANT SELECT ON TABLE public.tagger_artifact TO kura_reconciler;


--
-- Name: TABLE tagger_auto_exclude_pool; Type: ACL; Schema: public; Owner: kura
--

GRANT SELECT ON TABLE public.tagger_auto_exclude_pool TO kura_reconciler;


--
-- Name: TABLE tagger_candidate_disposition; Type: ACL; Schema: public; Owner: kura
--

GRANT SELECT,INSERT ON TABLE public.tagger_candidate_disposition TO kura_reconciler;


--
-- Name: SEQUENCE tagger_candidate_disposition_seq_seq; Type: ACL; Schema: public; Owner: kura
--

GRANT USAGE ON SEQUENCE public.tagger_candidate_disposition_seq_seq TO kura_reconciler;


--
-- Name: TABLE tagger_candidate_event; Type: ACL; Schema: public; Owner: kura
--

GRANT SELECT,INSERT ON TABLE public.tagger_candidate_event TO kura_reconciler;


--
-- Name: SEQUENCE tagger_candidate_event_seq_seq; Type: ACL; Schema: public; Owner: kura
--

GRANT USAGE ON SEQUENCE public.tagger_candidate_event_seq_seq TO kura_reconciler;


--
-- Name: TABLE tagger_config; Type: ACL; Schema: public; Owner: kura
--

GRANT SELECT,INSERT,UPDATE ON TABLE public.tagger_config TO kura_reconciler;


--
-- Name: TABLE tagger_head_policy; Type: ACL; Schema: public; Owner: kura
--

GRANT SELECT,INSERT,UPDATE ON TABLE public.tagger_head_policy TO kura_reconciler;


--
-- Name: TABLE tagger_patch; Type: ACL; Schema: public; Owner: kura
--

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.tagger_patch TO kura_reconciler;


--
-- Name: TABLE tagger_current_head_policy; Type: ACL; Schema: public; Owner: kura
--

GRANT SELECT ON TABLE public.tagger_current_head_policy TO kura_reconciler;


--
-- Name: TABLE tagger_release; Type: ACL; Schema: public; Owner: kura
--

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.tagger_release TO kura_reconciler;


--
-- Name: TABLE tagger_release_active; Type: ACL; Schema: public; Owner: kura
--

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.tagger_release_active TO kura_reconciler;


--
-- Name: TABLE tagger_deployable_head; Type: ACL; Schema: public; Owner: kura
--

GRANT SELECT ON TABLE public.tagger_deployable_head TO kura_reconciler;


--
-- Name: SEQUENCE tagger_fencing_seq; Type: ACL; Schema: public; Owner: kura
--

GRANT SELECT,USAGE ON SEQUENCE public.tagger_fencing_seq TO kura_reconciler;


--
-- Name: TABLE tagger_worker_heartbeat; Type: ACL; Schema: public; Owner: kura
--

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.tagger_worker_heartbeat TO kura_reconciler;


--
-- Name: TABLE tagger_gt_label; Type: ACL; Schema: public; Owner: kura
--

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.tagger_gt_label TO kura_reconciler;


--
-- Name: SEQUENCE tagger_gt_label_id_seq; Type: ACL; Schema: public; Owner: kura
--

GRANT SELECT,USAGE ON SEQUENCE public.tagger_gt_label_id_seq TO kura_reconciler;


--
-- Name: TABLE tagger_head_candidate; Type: ACL; Schema: public; Owner: kura
--

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.tagger_head_candidate TO kura_reconciler;


--
-- Name: SEQUENCE tagger_head_candidate_id_seq; Type: ACL; Schema: public; Owner: kura
--

GRANT SELECT,USAGE ON SEQUENCE public.tagger_head_candidate_id_seq TO kura_reconciler;


--
-- Name: TABLE tagger_lifecycle_event; Type: ACL; Schema: public; Owner: kura
--

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.tagger_lifecycle_event TO kura_reconciler;


--
-- Name: TABLE tagger_stage; Type: ACL; Schema: public; Owner: kura
--

GRANT SELECT ON TABLE public.tagger_stage TO kura_reconciler;


--
-- Name: SEQUENCE tagger_lifecycle_event_id_seq; Type: ACL; Schema: public; Owner: kura
--

GRANT SELECT,USAGE ON SEQUENCE public.tagger_lifecycle_event_id_seq TO kura_reconciler;


--
-- Name: TABLE tagger_lifecycle_lock; Type: ACL; Schema: public; Owner: kura
--

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.tagger_lifecycle_lock TO kura_reconciler;


--
-- Name: TABLE tagger_node_head; Type: ACL; Schema: public; Owner: kura
--

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.tagger_node_head TO kura_reconciler;


--
-- Name: TABLE tagger_node_reap_authority; Type: ACL; Schema: public; Owner: kura
--

GRANT SELECT ON TABLE public.tagger_node_reap_authority TO kura_reconciler;


--
-- Name: TABLE tagger_operation; Type: ACL; Schema: public; Owner: kura
--

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.tagger_operation TO kura_reconciler;


--
-- Name: SEQUENCE tagger_patch_id_seq; Type: ACL; Schema: public; Owner: kura
--

GRANT SELECT,USAGE ON SEQUENCE public.tagger_patch_id_seq TO kura_reconciler;


--
-- Name: TABLE tagger_producer_threshold; Type: ACL; Schema: public; Owner: kura
--

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.tagger_producer_threshold TO kura_reconciler;


--
-- Name: TABLE tagger_reconciler_state; Type: ACL; Schema: public; Owner: kura
--

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.tagger_reconciler_state TO kura_reconciler;


--
-- Name: TABLE tagger_registration_request; Type: ACL; Schema: public; Owner: kura
--

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.tagger_registration_request TO kura_reconciler;


--
-- Name: SEQUENCE tagger_registration_request_id_seq; Type: ACL; Schema: public; Owner: kura
--

GRANT SELECT,USAGE ON SEQUENCE public.tagger_registration_request_id_seq TO kura_reconciler;


--
-- Name: TABLE tagger_release_admissibility; Type: ACL; Schema: public; Owner: kura
--

GRANT SELECT ON TABLE public.tagger_release_admissibility TO kura_reconciler;


--
-- Name: TABLE tagger_release_component; Type: ACL; Schema: public; Owner: kura
--

GRANT SELECT ON TABLE public.tagger_release_component TO kura_reconciler;


--
-- Name: SEQUENCE tagger_release_id_seq; Type: ACL; Schema: public; Owner: kura
--

GRANT SELECT,USAGE ON SEQUENCE public.tagger_release_id_seq TO kura_reconciler;


--
-- Name: TABLE tagger_release_manifest; Type: ACL; Schema: public; Owner: kura
--

GRANT SELECT ON TABLE public.tagger_release_manifest TO kura_reconciler;


--
-- Name: TABLE tagger_stage_transition; Type: ACL; Schema: public; Owner: kura
--

GRANT SELECT ON TABLE public.tagger_stage_transition TO kura_reconciler;


--
-- Name: TABLE tagger_sweep_run; Type: ACL; Schema: public; Owner: kura
--

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.tagger_sweep_run TO kura_reconciler;


--
-- Name: SEQUENCE tagger_sweep_run_id_seq; Type: ACL; Schema: public; Owner: kura
--

GRANT SELECT,USAGE ON SEQUENCE public.tagger_sweep_run_id_seq TO kura_reconciler;


--
-- Name: TABLE tagger_tag_condition; Type: ACL; Schema: public; Owner: kura
--

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.tagger_tag_condition TO kura_reconciler;


--
-- Name: TABLE tagger_tag_rule; Type: ACL; Schema: public; Owner: kura
--

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.tagger_tag_rule TO kura_reconciler;


--
-- Name: TABLE tagger_tag_application; Type: ACL; Schema: public; Owner: kura
--

GRANT SELECT ON TABLE public.tagger_tag_application TO kura_reconciler;


--
-- Name: TABLE tagger_tag_exclusion; Type: ACL; Schema: public; Owner: kura
--

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.tagger_tag_exclusion TO kura_reconciler;


--
-- Name: TABLE tagger_tag_state; Type: ACL; Schema: public; Owner: kura
--

GRANT SELECT ON TABLE public.tagger_tag_state TO kura_reconciler;


--
-- PostgreSQL database dump complete
--



-- reference rows
SET session_replication_role = replica;
COPY public.tag_category ("id", "name", "color", "default", "version", "order") FROM stdin;
1	general	#0288d1	f	4	13
38	artist	#c00004	f	1	1
39	copyright	#a800aa	f	1	2
40	character	#00ab2c	f	1	3
41	meta	#ee8800	f	3	14
45	breast	#c0894a	f	1	4
48	female_genitals	#cf5c8a	f	2	5
49	male_genitals	#4a7fc0	f	1	6
51	anus	#a0596a	f	1	8
52	pubic_hair	#6c8f3a	f	1	9
53	genital_accessory	#4a9f9f	f	1	10
54	pose	#8f6cc0	f	1	11
55	sex_position	#c05a5a	f	1	12
56	cum	#e0e0ff	f	1	13
57	sex_act	#c0a830	f	1	14
58	excretion	#8a6a4a	f	1	15
59	viewpoint	#00bcd4	f	1	16
60	expression	#ff80ab	f	1	17
61	clothing	#cddc39	f	1	18
62	unsorted	#8d8d8d	t	2	0
\.
SELECT pg_catalog.setval('public.tag_category_id_seq', COALESCE(max("id"), 1), max("id") IS NOT NULL) FROM public.tag_category;
COPY public.tagger_config ("key", "value", "description", "updated_at", "updated_by") FROM stdin;
eval_split_mode	1	head 評価の split 方式: 1=pooled out-of-fold (CV で全正例を1回採点、小データで安定・val_pos が holdout の約5倍), 0=legacy 20%% holdout。trainer の学習 split がこの値を読む。マジックナンバー化を避けるための変数 (user 2026-07-21)。	2026-07-21 22:32:58.746565+09	migration-0106
min_gt_labels	10	reconciler.py run_reconcile -- floor on total tagger_gt_label rows (pos+neg) before dispatching training	2026-07-20 19:56:12.01593+09	tagger-dashboard
min_gt_negative	50	reconciler.py run_reconcile -- v-next PRECISION gate: for a tag that already has an approved patch, negative-verdict tagger_gt_label rows must grow by at least this much past the last registration attempt to re-dispatch a precision (over-detection suppression) retrain. Negative analogue of min_gt_positive; set higher because FP deletions are far more abundant.	2026-07-23 05:10:45.293625+09	migration-0109
min_gt_positive	20	reconciler.py run_reconcile -- floor on tagger_gt_label positive-verdict rows before dispatching training (distinct from min_gt_labels, which only checks the total; a tag with 0 positive examples cannot train a classifier no matter how large the total is)	2026-07-21 12:28:28.129152+09	migration-0102
min_held_out_samples	20	transitions.py validate_metrics_floor (git-file patch schema, currently unwired) -- floor on n_val	2026-07-20 19:55:00.481536+09	migration-0098
min_net_fp_removed	10000	規模の floor (率だけでは 163,583 件減らす head が落ちる)	2026-08-05 04:16:09.623317+09	migration 0133
min_p_deploy	0.85	transitions.py validate_registration_metrics_floor -- floor on P_deploy = piR/(piR+(1-pi)FPR), the operating precision at the tag corpus prevalence pi. A head can clear val P/R yet deploy at low precision when pi is tiny (prevalence ceiling). Seeded at the value config.py DEFAULTS already supplied, so this row changes no behaviour -- it only makes the floor actually dashboard-editable as 0098 intended (it had no row until 0125, measured on primary 2026-08-02).	2026-08-02 20:00:31.831269+09	migration-0125
min_p_deploy_lift	10.0	tagger/vnext_arms.py v-next measurement -- BASELINE-RELATIVE deploy floor: P_deploy / pi, i.e. how many times more precise the head is than what the base tagger already ships (its own deploy precision IS pi). Exists because for an over-detected tag pi is tiny and the ABSOLUTE min_p_deploy becomes unreachable for any head however useful -- licking_testicle: pi=0.042, so 0.85 demands FPR<=0.54%% while the best honest operating point is 2.0%%, yet the available head suppresses 98%% of the false positives. Only evaluated at operating points that still clear min_recall and the support floors: lift rises as recall falls (8.8x at R=0.90, 17.2x at R=0.60), so without that guard "improve the tag" degenerates into "stop detecting it". 10.0 = an order of magnitude better than the base tagger while holding recall; for scale the absolute 0.85 floor equals 20.2x at pi=0.042 and the Step 8b reference reaches 14.1-14.3x. User directive 2026-08-02.	2026-08-02 20:00:31.831269+09	migration-0125
min_precision	0.85	transitions.py validate_metrics_floor / validate_registration_metrics_floor -- floor on precision	2026-07-20 19:55:00.481536+09	migration-0098
min_pred_pos_support	20	transitions.py validate_registration_metrics_floor -- floor on precision's denominator (tp+fp)	2026-07-21 19:27:17.88718+09	tagger-dashboard
min_recall	0.7	transitions.py validate_metrics_floor / validate_registration_metrics_floor -- floor on recall	2026-07-20 19:55:00.481536+09	migration-0098
min_val_pos_support	20	transitions.py validate_registration_metrics_floor -- floor on recall's denominator (tp+fn/val_pos)	2026-07-21 19:27:26.011567+09	tagger-dashboard
regression_accuracy_threshold	0.8	health.py check_regressions -- below this, an approved tag is re-flagged as head_candidate	2026-07-20 19:55:00.481536+09	migration-0098
regression_confidence_z	1.96	tagger/regression_check.py -- two-proportion z-test critical value used to decide whether a candidate base-finetune checkpoint's per-tag P/R drop against the teacher is a real regression or sampling noise. 1.96 = standard two-sided 95% confidence. Paired with regression_min_gt_labels (0098): below that floor a tag is reported but never allowed to block on its own.	2026-08-09 07:59:49.900007+09	migration-0144
regression_min_gt_labels	10	health.py check_regressions -- floor on tagger_gt_label rows before judging a tag for regression	2026-07-20 19:55:00.481536+09	migration-0098
\.
COPY public.tagger_stage ("stage", "rank") FROM stdin;
approved	50
built	70
canary	80
candidate	10
deployed	110
published	100
registered	60
shadow	90
trained	30
training	20
verified	40
\.
COPY public.tagger_stage_transition ("id", "from_stage", "to_stage", "kind") FROM stdin;
1	candidate	training	advance
2	training	trained	advance
3	trained	verified	advance
4	verified	approved	advance
5	approved	registered	advance
6	registered	built	advance
7	built	canary	advance
8	canary	shadow	advance
9	shadow	published	advance
10	published	deployed	advance
11	training	candidate	rollback
12	trained	candidate	rollback
13	verified	candidate	rollback
14	approved	candidate	rollback
15	registered	candidate	rollback
16	built	candidate	rollback
17	canary	candidate	rollback
18	shadow	candidate	rollback
19	published	candidate	rollback
20	deployed	candidate	rollback
21	\N	candidate	advance
22	\N	approved	advance
23	candidate	approved	advance
24	training	approved	advance
\.
SELECT pg_catalog.setval('public.tagger_stage_transition_id_seq', COALESCE(max("id"), 1), max("id") IS NOT NULL) FROM public.tagger_stage_transition;
COPY public.tagger_tag_condition ("name", "all_tags", "any_tags", "none_tags", "evidence") FROM stdin;
always	{}	{}	{}	無条件。条件を持たない規則がここを指すことで condition 列を NOT NULL に保つ
in_penis_domain	{penis}	{}	{}	この head は penis が写っている画像でだけ校正されている (horse_penis の GT: negative 162/162・positive 66/66 が penis 持ち、2026-09-03 実測)。danbooru 含意でも horse_penis⇒penis。領域外では発火させない — in_pussy_domain と同型の domain 宣言
in_pussy_domain	{pussy}	{}	{}	この head は pussy が写っている画像でだけ校正されている。領域外では発火させ ない (infer.py WD14_FIXED_GATES / head_policy.gate_tags の移送)。※ 移送前は worker が all 意味、infer が any 意味で **逆だった**。要素 1 個の 間は同値なので露見していなかった。all 意味に統一する
solo	{}	{solo,1girl,1boy}	{multiple_girls,multiple_boys,2girls,2boys,3girls,3boys}	単独被写体。infer.py _is_solo() の完全な移送 — 「solo タグがある」ではなく (solo|1girl|1boy) かつ複数系が無い。髪型・表情の排他は複数人だと両立するので この条件でだけ効かせる
\.
COPY public.tagger_allowed_executor ("db_role", "scope", "added_at", "added_by") FROM stdin;
kura	backlog-sweep	2026-07-20 15:53:01.131144+09	migration-0079
kura	gt-sync	2026-07-20 19:04:39.58314+09	migration-0096
kura	lifecycle:*	2026-07-20 18:20:10.943945+09	migration-0090
kura	reconciler-control	2026-07-20 16:15:58.262936+09	migration-0080
kura	registration	2026-07-20 17:14:02.525718+09	migration-0086
kura	release-build	2026-07-20 17:21:32.306247+09	migration-0087
kura	release-publish	2026-07-20 17:21:32.306247+09	migration-0087
kura	tagger-config	2026-07-20 19:55:00.481536+09	migration-0098
kura	tagging-pipeline	2026-07-20 16:35:15.693268+09	migration-0083
kura	worker-heartbeat	2026-07-20 16:15:58.262936+09	migration-0080
\.
COPY public.tagger_producer_threshold ("producer", "category", "threshold", "evidence") FROM stdin;
mldanbooru	0	0.7	ML-Danbooru の既定閾値 (infer.py MLDANBOORU_THRESH)
pixai	0	0.4	PixAI general タグの既定閾値 (infer.py PIXAI_THRESH_GENERAL)
pixai	4	0.8	PixAI character タグの既定閾値 (infer.py PIXAI_THRESH_CHAR)
wd14	4	0.85	WD14 character タグの既定閾値 (infer.py WD14_THRESH_CHAR)
wd14_fixed	0	0.5	wd14-fixed 修復 head の既定閾値 (infer.py WD14_FIXED_THRESH)
\.
COPY collector_next.schema_version ("version") FROM stdin;
5
\.
COPY collector_next.label_producer_kind ("id", "name") FROM stdin;
1	account
\.
COPY collector_next.label_policy ("id", "version") FROM stdin;
1	collector-labels-v1
\.

-- initial rows (not copied: see INITIAL_ROWS)
INSERT INTO public.pool_category (id, version, name, color, "default") VALUES (1, 1, 'default', 'default', true);
SELECT pg_catalog.setval(pg_catalog.pg_get_serial_sequence('public.pool_category', 'id'), 1, true);
INSERT INTO public.tagger_release_active (id, release_id) VALUES (1, NULL);
INSERT INTO public.tagger_reconciler_state (id, paused) VALUES (true, false);
INSERT INTO claim_storage.migration (singleton, phase) VALUES (true, 'active');
SET session_replication_role = DEFAULT;
INSERT INTO public.kura_alembic_version (version_num) VALUES ('0186');
