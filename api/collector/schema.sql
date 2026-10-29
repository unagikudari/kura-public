-- Module-owned, canonical-only experimental namespace, deployed by Alembic 0168.
-- No publication changes, no public-table foreign keys, no legacy writes.
CREATE SCHEMA collector_next;
REVOKE ALL ON SCHEMA collector_next FROM PUBLIC;
CREATE TABLE collector_next.schema_version (version integer PRIMARY KEY CHECK(version=1));
INSERT INTO collector_next.schema_version VALUES (1);
CREATE TABLE collector_next.blob (
 id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
 sha256 bytea NOT NULL UNIQUE CHECK(octet_length(sha256)=32),
 byte_size bigint NOT NULL CHECK(byte_size>0)
);
CREATE TABLE collector_next.blob_location (
 blob_id bigint PRIMARY KEY REFERENCES collector_next.blob,
 storage_key text NOT NULL UNIQUE
);
CREATE TABLE collector_next.asset (
 id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
 uuid uuid NOT NULL DEFAULT gen_random_uuid() UNIQUE,
 owner_id integer NOT NULL,
 blob_id bigint NOT NULL REFERENCES collector_next.blob,
 created_at timestamptz NOT NULL DEFAULT now(),
 UNIQUE(owner_id,blob_id), UNIQUE(owner_id,id)
);
CREATE TABLE collector_next.media_current (
 asset_id bigint PRIMARY KEY REFERENCES collector_next.asset,
 owner_id integer NOT NULL,
 original_name text NOT NULL,
 media_type text NOT NULL,
 width integer NOT NULL CHECK(width>0), height integer NOT NULL CHECK(height>0),
 FOREIGN KEY(owner_id,asset_id) REFERENCES collector_next.asset(owner_id,id)
);
CREATE INDEX media_owner_cursor ON collector_next.media_current(owner_id,asset_id DESC);
CREATE TABLE collector_next.collection (
 id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
 owner_id integer NOT NULL, title text NOT NULL CHECK(length(title) BETWEEN 1 AND 200),
 request_key uuid NOT NULL, UNIQUE(owner_id,request_key),
 UNIQUE(owner_id,id)
);
CREATE TABLE collector_next.collection_entry (
 id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
 owner_id integer NOT NULL,
 collection_id bigint NOT NULL,
 asset_id bigint NOT NULL,
 position bigint NOT NULL CHECK(position>0),
 FOREIGN KEY(owner_id,collection_id) REFERENCES collector_next.collection(owner_id,id),
 FOREIGN KEY(owner_id,asset_id) REFERENCES collector_next.asset(owner_id,id),
 UNIQUE(collection_id,position)
);
CREATE TABLE collector_next.receipt (
 owner_id integer NOT NULL, request_key uuid NOT NULL,
 fingerprint bytea NOT NULL CHECK(octet_length(fingerprint)=32),
 original_name text NOT NULL,
 asset_id bigint NOT NULL, entry_id bigint REFERENCES collector_next.collection_entry,
 created_at timestamptz NOT NULL DEFAULT now(),
 FOREIGN KEY(owner_id,asset_id) REFERENCES collector_next.asset(owner_id,id),
 PRIMARY KEY(owner_id,request_key)
);
REVOKE ALL ON ALL TABLES IN SCHEMA collector_next FROM PUBLIC;
REVOKE ALL ON ALL SEQUENCES IN SCHEMA collector_next FROM PUBLIC;
