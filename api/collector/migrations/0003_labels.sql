-- Organizational labels: user intent on assets, not inferred scene semantics.
CREATE TABLE collector_next.label_producer_kind(id smallint PRIMARY KEY, name text NOT NULL UNIQUE);
-- statement-break
INSERT INTO collector_next.label_producer_kind VALUES(1,'account');
-- statement-break
CREATE TABLE collector_next.label_policy(id smallint PRIMARY KEY, version text NOT NULL UNIQUE);
-- statement-break
INSERT INTO collector_next.label_policy VALUES(1,'collector-labels-v1');
-- statement-break
CREATE TABLE collector_next.label_producer(
 id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY, owner_id integer NOT NULL,
 kind_id smallint NOT NULL REFERENCES collector_next.label_producer_kind,
 reference text NOT NULL, version text NOT NULL,
 UNIQUE(owner_id,kind_id,reference,version), UNIQUE(owner_id,id)
);
-- statement-break
CREATE TABLE collector_next.tag(
 id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY, owner_id integer NOT NULL,
 name varchar(100) NOT NULL CHECK(length(name)>0),
 normalized_name varchar(100) COLLATE "C" NOT NULL CHECK(length(normalized_name)>0),
 UNIQUE(owner_id,normalized_name), UNIQUE(owner_id,id)
);
-- statement-break
COMMENT ON TABLE collector_next.tag IS 'Asset organization labels, not semantic scene tag_projection';
-- statement-break
CREATE TABLE collector_next.label_operation(
 owner_id integer NOT NULL, request_key uuid NOT NULL,
 fingerprint bytea NOT NULL CHECK(octet_length(fingerprint)=32),
 producer_id bigint NOT NULL, asset_count integer NOT NULL CHECK(asset_count BETWEEN 1 AND 100),
 tag_count integer NOT NULL CHECK(tag_count BETWEEN 1 AND 16),
 policy_id smallint NOT NULL DEFAULT 1 REFERENCES collector_next.label_policy,
 created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 PRIMARY KEY(owner_id,request_key),
 FOREIGN KEY(owner_id,producer_id) REFERENCES collector_next.label_producer(owner_id,id)
);
-- statement-break
CREATE TABLE collector_next.label_observation(
 id bigint GENERATED ALWAYS AS IDENTITY,
 owner_id integer NOT NULL, asset_id bigint NOT NULL, tag_id bigint NOT NULL,
 producer_id bigint NOT NULL, request_key uuid NOT NULL,
 present boolean NOT NULL, prior_present boolean NOT NULL,
 based_on_id bigint, observed_at timestamptz NOT NULL,
 PRIMARY KEY(asset_id,tag_id,id),
 FOREIGN KEY(owner_id,asset_id) REFERENCES collector_next.asset(owner_id,id),
 FOREIGN KEY(owner_id,tag_id) REFERENCES collector_next.tag(owner_id,id),
 FOREIGN KEY(owner_id,producer_id) REFERENCES collector_next.label_producer(owner_id,id),
 FOREIGN KEY(owner_id,request_key) REFERENCES collector_next.label_operation(owner_id,request_key),
 FOREIGN KEY(asset_id,tag_id,based_on_id) REFERENCES collector_next.label_observation(asset_id,tag_id,id)
);
-- statement-break
CREATE INDEX label_history_asset ON collector_next.label_observation(owner_id,asset_id,id DESC);
-- statement-break
CREATE INDEX label_history_operation ON collector_next.label_observation(owner_id,request_key);
-- statement-break
CREATE TABLE collector_next.label_head(
 owner_id integer NOT NULL, asset_id bigint NOT NULL, tag_id bigint NOT NULL,
 observation_id bigint NOT NULL, present boolean NOT NULL,
 PRIMARY KEY(asset_id,tag_id),
 FOREIGN KEY(owner_id,asset_id) REFERENCES collector_next.asset(owner_id,id),
 FOREIGN KEY(asset_id,tag_id,observation_id) REFERENCES collector_next.label_observation(asset_id,tag_id,id)
);
-- statement-break
CREATE INDEX label_positive_search ON collector_next.label_head(owner_id,tag_id,asset_id DESC) WHERE present;
-- statement-break
CREATE INDEX label_positive_asset ON collector_next.label_head(owner_id,asset_id,tag_id) WHERE present;
-- statement-break
CREATE VIEW collector_next.media_tag_current AS
 SELECT owner_id,asset_id,tag_id,observation_id FROM collector_next.label_head WHERE present;
-- statement-break
CREATE TABLE collector_next.asset_label_state(
 asset_id bigint PRIMARY KEY, owner_id integer NOT NULL,
 revision bigint NOT NULL DEFAULT 0 CHECK(revision>=0),
 positive_count integer NOT NULL DEFAULT 0 CHECK(positive_count BETWEEN 0 AND 128),
 last_observed_at timestamptz NOT NULL DEFAULT 'epoch',
 policy_id smallint NOT NULL DEFAULT 1 REFERENCES collector_next.label_policy,
 FOREIGN KEY(owner_id,asset_id) REFERENCES collector_next.asset(owner_id,id)
);
-- statement-break
CREATE TABLE collector_next.label_target(
 owner_id integer NOT NULL, request_key uuid NOT NULL, asset_id bigint NOT NULL,
 before_revision bigint NOT NULL CHECK(before_revision>=0),
 after_revision bigint NOT NULL CHECK(after_revision=before_revision+1),
 PRIMARY KEY(owner_id,request_key,asset_id),
 FOREIGN KEY(owner_id,request_key) REFERENCES collector_next.label_operation(owner_id,request_key),
 FOREIGN KEY(owner_id,asset_id) REFERENCES collector_next.asset(owner_id,id)
);
-- statement-break
CREATE TABLE collector_next.tag_usage(
 tag_id bigint PRIMARY KEY, owner_id integer NOT NULL,
 positive_count bigint NOT NULL DEFAULT 0 CHECK(positive_count>=0),
 FOREIGN KEY(owner_id,tag_id) REFERENCES collector_next.tag(owner_id,id)
);
-- statement-break
CREATE INDEX media_video_cursor ON collector_next.media_current(owner_id,asset_id DESC) WHERE media_type LIKE 'video/%';
-- statement-break
CREATE INDEX media_image_cursor ON collector_next.media_current(owner_id,asset_id DESC) WHERE media_type LIKE 'image/%';
-- statement-break
CREATE INDEX entry_asset_collection ON collector_next.collection_entry(asset_id,collection_id,position);
-- statement-break
CREATE FUNCTION collector_next.reject_label_rewrite() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN RAISE EXCEPTION 'collector label evidence is append-only'; END;
$$;
-- statement-break
DO $$ DECLARE name text; BEGIN
 FOREACH name IN ARRAY ARRAY['label_observation','label_operation','label_target','label_producer','label_policy','label_producer_kind'] LOOP
  EXECUTE format('CREATE TRIGGER immutable_rows BEFORE UPDATE OR DELETE ON collector_next.%I FOR EACH ROW EXECUTE FUNCTION collector_next.reject_label_rewrite()',name);
  EXECUTE format('CREATE TRIGGER immutable_truncate BEFORE TRUNCATE ON collector_next.%I FOR EACH STATEMENT EXECUTE FUNCTION collector_next.reject_label_rewrite()',name);
 END LOOP;
END $$;
-- statement-break
REVOKE ALL ON ALL TABLES IN SCHEMA collector_next FROM PUBLIC;
-- statement-break
REVOKE ALL ON ALL SEQUENCES IN SCHEMA collector_next FROM PUBLIC;
-- statement-break
ALTER TABLE collector_next.schema_version DROP CONSTRAINT schema_version_version_check;
-- statement-break
UPDATE collector_next.schema_version SET version=3 WHERE version=2;
-- statement-break
ALTER TABLE collector_next.schema_version ADD CONSTRAINT schema_version_version_check CHECK(version=3);
