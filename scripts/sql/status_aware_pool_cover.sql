-- status-aware pool cover selection (apply as the function owner: postgres).
--
-- The cover trigger picked the lowest-ord post regardless of status. At
-- pool_post INSERT time every post is 'pending'; when the materialize/cull pass
-- later flips a post to near_dup/corrupt it UPDATEs `post` (not pool_post), so
-- the old trigger never re-evaluated — leaving pools with covers pointing at a
-- non-displayable post (no thumbnail → blank pool-list cover).
--
-- These trigger functions are owned by `postgres`, so this MUST be applied as
-- postgres (the kura app role cannot CREATE OR REPLACE them — that is also why
-- it is not an alembic migration):
--   sudo -u postgres psql -d kura -f scripts/sql/status_aware_pool_cover.sql

-- first displayable post by ord, else first post by ord (provisional cover for a
-- brand-new all-pending pool).
CREATE OR REPLACE FUNCTION pool_best_cover(p_pool_id INT)
RETURNS INT LANGUAGE sql STABLE AS $$
    SELECT pp.post_id
    FROM pool_post pp
    JOIN post po ON po.id = pp.post_id
    WHERE pp.pool_id = p_pool_id
    ORDER BY (po.status = 'ok' AND po.thumbnail IS NOT NULL
              AND octet_length(po.thumbnail) >= 1000) DESC,
             pp.ord ASC
    LIMIT 1
$$;

CREATE OR REPLACE FUNCTION pool_post_sync_cover()
RETURNS TRIGGER LANGUAGE plpgsql AS $$
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

-- NEW: re-pick covers when a member post's status flips (the gap that left
-- culled near_dup posts sitting as pool covers).
CREATE OR REPLACE FUNCTION post_status_sync_pool_cover()
RETURNS TRIGGER LANGUAGE plpgsql AS $$
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

DROP TRIGGER IF EXISTS post_status_sync_pool_cover_trg ON post;
CREATE TRIGGER post_status_sync_pool_cover_trg
AFTER UPDATE OF status ON post
FOR EACH ROW
WHEN (OLD.status IS DISTINCT FROM NEW.status)
EXECUTE FUNCTION post_status_sync_pool_cover();

-- one-time reconcile of existing pools
UPDATE pool p SET cover_post_id = pool_best_cover(p.id)
WHERE cover_post_id IS DISTINCT FROM pool_best_cover(p.id);
