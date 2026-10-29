CREATE SCHEMA claim_storage;
-- statement-break
REVOKE ALL ON SCHEMA claim_storage FROM PUBLIC;
-- statement-break
CREATE TABLE claim_storage.actor(
 id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
 actor_id text NOT NULL UNIQUE
);
-- statement-break
COMMENT ON TABLE claim_storage.actor IS 'Exact legacy source identity, not an inferred model version or human account';
-- statement-break
CREATE TABLE claim_storage.claim(
 post_id integer NOT NULL REFERENCES public.post(id) ON DELETE CASCADE,
 tag_id integer NOT NULL REFERENCES public.tag(id),
 actor_key bigint NOT NULL REFERENCES claim_storage.actor(id),
 claimed_at timestamptz NOT NULL DEFAULT now(),
 PRIMARY KEY(post_id,tag_id,actor_key)
);
-- statement-break
CREATE INDEX claim_by_actor ON claim_storage.claim(post_id,actor_key);
-- statement-break
CREATE TABLE claim_storage.migration(
 singleton boolean PRIMARY KEY DEFAULT true CHECK(singleton),
 phase text NOT NULL DEFAULT 'copying' CHECK(phase IN ('copying','verifying','ready','active')),
 cursor_post integer NOT NULL DEFAULT -2147483648,
 cursor_tag integer NOT NULL DEFAULT -2147483648,
 cursor_actor text NOT NULL DEFAULT '',
 copied_rows bigint NOT NULL DEFAULT 0,
 verified_through integer NOT NULL DEFAULT -2147483648,
 updated_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
-- statement-break
INSERT INTO claim_storage.migration(singleton) VALUES(true);
-- statement-break
CREATE FUNCTION claim_storage.mirror_insert() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
 INSERT INTO claim_storage.actor(actor_id) SELECT DISTINCT actor_id FROM inserted ON CONFLICT DO NOTHING;
 INSERT INTO claim_storage.claim(post_id,tag_id,actor_key,claimed_at)
 SELECT n.post_id,n.tag_id,a.id,n.claimed_at FROM inserted n JOIN claim_storage.actor a USING(actor_id)
 ON CONFLICT(post_id,tag_id,actor_key) DO UPDATE SET claimed_at=EXCLUDED.claimed_at
 WHERE claim_storage.claim.claimed_at IS DISTINCT FROM EXCLUDED.claimed_at;
 RETURN NULL;
END $$;
-- statement-break
CREATE FUNCTION claim_storage.mirror_delete() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
 DELETE FROM claim_storage.claim c USING deleted d,claim_storage.actor a
 WHERE a.actor_id=d.actor_id AND c.post_id=d.post_id AND c.tag_id=d.tag_id AND c.actor_key=a.id;
 RETURN NULL;
END $$;
-- statement-break
CREATE FUNCTION claim_storage.mirror_update() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
 DELETE FROM claim_storage.claim c USING deleted d,claim_storage.actor a
 WHERE a.actor_id=d.actor_id AND c.post_id=d.post_id AND c.tag_id=d.tag_id AND c.actor_key=a.id;
 INSERT INTO claim_storage.actor(actor_id) SELECT DISTINCT actor_id FROM inserted ON CONFLICT DO NOTHING;
 INSERT INTO claim_storage.claim(post_id,tag_id,actor_key,claimed_at)
 SELECT n.post_id,n.tag_id,a.id,n.claimed_at FROM inserted n JOIN claim_storage.actor a USING(actor_id)
 ON CONFLICT(post_id,tag_id,actor_key) DO UPDATE SET claimed_at=EXCLUDED.claimed_at;
 RETURN NULL;
END $$;
-- statement-break
CREATE FUNCTION claim_storage.mirror_truncate() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
 TRUNCATE claim_storage.claim;
 UPDATE claim_storage.migration SET phase='copying',cursor_post=-2147483648,cursor_tag=-2147483648,
 cursor_actor='',copied_rows=0,verified_through=-2147483648,updated_at=clock_timestamp();
 RETURN NULL;
END $$;
-- statement-break
CREATE TRIGGER claim_mirror_insert AFTER INSERT ON public.post_tag_actor_claim
 REFERENCING NEW TABLE AS inserted FOR EACH STATEMENT EXECUTE FUNCTION claim_storage.mirror_insert();
-- statement-break
CREATE TRIGGER claim_mirror_delete AFTER DELETE ON public.post_tag_actor_claim
 REFERENCING OLD TABLE AS deleted FOR EACH STATEMENT EXECUTE FUNCTION claim_storage.mirror_delete();
-- statement-break
CREATE TRIGGER claim_mirror_update AFTER UPDATE ON public.post_tag_actor_claim
 REFERENCING OLD TABLE AS deleted NEW TABLE AS inserted FOR EACH STATEMENT EXECUTE FUNCTION claim_storage.mirror_update();
-- statement-break
CREATE TRIGGER claim_mirror_truncate AFTER TRUNCATE ON public.post_tag_actor_claim
 FOR EACH STATEMENT EXECUTE FUNCTION claim_storage.mirror_truncate();
-- statement-break
REVOKE ALL ON ALL TABLES IN SCHEMA claim_storage FROM PUBLIC;
-- statement-break
REVOKE ALL ON ALL SEQUENCES IN SCHEMA claim_storage FROM PUBLIC;
-- statement-break
