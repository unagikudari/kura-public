CREATE VIEW public.post_tag_actor_claim AS
 SELECT c.post_id,c.tag_id,a.actor_id,c.claimed_at
 FROM claim_storage.claim c JOIN claim_storage.actor a ON a.id=c.actor_key;
-- statement-break
ALTER VIEW public.post_tag_actor_claim ALTER COLUMN claimed_at SET DEFAULT now();
-- statement-break
CREATE OR REPLACE FUNCTION claim_storage.legacy_write() RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog AS $$
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
-- statement-break
CREATE TRIGGER claim_legacy_write INSTEAD OF INSERT OR UPDATE OR DELETE ON public.post_tag_actor_claim
 FOR EACH ROW EXECUTE FUNCTION claim_storage.legacy_write();
-- statement-break
CREATE OR REPLACE FUNCTION claim_storage.archive_read_only() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN RAISE EXCEPTION 'Legacy claim archive is immutable; use public.post_tag_actor_claim'; END $$;
-- statement-break
CREATE TRIGGER claim_archive_read_only BEFORE INSERT OR UPDATE OR DELETE OR TRUNCATE ON claim_storage.legacy_claim_archive
 FOR EACH STATEMENT EXECUTE FUNCTION claim_storage.archive_read_only();
-- statement-break
CREATE TABLE claim_storage.changed_post(post_id integer PRIMARY KEY);
-- statement-break
CREATE OR REPLACE FUNCTION claim_storage.track_insert() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN INSERT INTO claim_storage.changed_post SELECT DISTINCT post_id FROM inserted ON CONFLICT DO NOTHING; RETURN NULL; END $$;
-- statement-break
CREATE OR REPLACE FUNCTION claim_storage.track_delete() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN INSERT INTO claim_storage.changed_post SELECT DISTINCT post_id FROM deleted ON CONFLICT DO NOTHING; RETURN NULL; END $$;
-- statement-break
CREATE OR REPLACE FUNCTION claim_storage.track_update() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN INSERT INTO claim_storage.changed_post SELECT post_id FROM inserted UNION SELECT post_id FROM deleted ON CONFLICT DO NOTHING; RETURN NULL; END $$;
-- statement-break
CREATE OR REPLACE FUNCTION claim_storage.block_truncate() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN RAISE EXCEPTION 'Retained claim checkpoint requires controlled reset, not TRUNCATE'; END $$;
-- statement-break
CREATE TRIGGER claim_track_insert AFTER INSERT ON claim_storage.claim REFERENCING NEW TABLE AS inserted FOR EACH STATEMENT EXECUTE FUNCTION claim_storage.track_insert();
-- statement-break
CREATE TRIGGER claim_track_delete AFTER DELETE ON claim_storage.claim REFERENCING OLD TABLE AS deleted FOR EACH STATEMENT EXECUTE FUNCTION claim_storage.track_delete();
-- statement-break
CREATE TRIGGER claim_track_update AFTER UPDATE ON claim_storage.claim REFERENCING OLD TABLE AS deleted NEW TABLE AS inserted FOR EACH STATEMENT EXECUTE FUNCTION claim_storage.track_update();
-- statement-break
CREATE TRIGGER claim_block_truncate BEFORE TRUNCATE ON claim_storage.claim FOR EACH STATEMENT EXECUTE FUNCTION claim_storage.block_truncate();
-- statement-break
REVOKE ALL ON claim_storage.changed_post FROM PUBLIC;
-- statement-break
CREATE OR REPLACE FUNCTION claim_storage.actor_read_only() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN RAISE EXCEPTION 'Claimant identity is immutable; register a distinct source identity'; END $$;
-- statement-break
DROP TRIGGER IF EXISTS claim_actor_immutable ON claim_storage.actor;
-- statement-break
CREATE TRIGGER claim_actor_immutable BEFORE UPDATE OR DELETE OR TRUNCATE ON claim_storage.actor
 FOR EACH STATEMENT EXECUTE FUNCTION claim_storage.actor_read_only();
