-- Durable plans; bytes remain in the browser until each original is accepted.
CREATE TABLE collector_next.import_operation (
 owner_id integer NOT NULL, request_key uuid NOT NULL,
 fingerprint bytea NOT NULL CHECK(octet_length(fingerprint)=32),
 item_count integer NOT NULL CHECK(item_count BETWEEN 1 AND 10000),
 registered_count integer NOT NULL DEFAULT 0 CHECK(registered_count>=0),
 rejected_count integer NOT NULL DEFAULT 0 CHECK(rejected_count>=0),
 created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now(),
 PRIMARY KEY(owner_id,request_key), CHECK(registered_count+rejected_count<=item_count)
);
-- statement-break
CREATE INDEX import_operation_owner_recent ON collector_next.import_operation(owner_id,created_at DESC,request_key DESC);
-- statement-break
CREATE TABLE collector_next.import_item (
 owner_id integer NOT NULL, operation_key uuid NOT NULL, position integer NOT NULL CHECK(position>0),
 request_key uuid NOT NULL, original_name varchar(255) NOT NULL,
 relative_path text, byte_size bigint NOT NULL CHECK(byte_size>=0), sha256 bytea,
 collection_id bigint, collection_position bigint CHECK(collection_position>0), state smallint NOT NULL DEFAULT 0 CHECK(state IN (0,1,2)),
 error_code text, error_message varchar(500),
 PRIMARY KEY(owner_id,operation_key,position), UNIQUE(owner_id,request_key),
 UNIQUE(collection_id,collection_position), CHECK((collection_id IS NULL)=(collection_position IS NULL)),
 FOREIGN KEY(owner_id,operation_key) REFERENCES collector_next.import_operation(owner_id,request_key),
 FOREIGN KEY(owner_id,collection_id) REFERENCES collector_next.collection(owner_id,id),
 CHECK(sha256 IS NULL OR octet_length(sha256)=32),
 CHECK(sha256 IS NOT NULL OR state=2)
);
-- statement-break
ALTER TABLE collector_next.schema_version DROP CONSTRAINT schema_version_version_check;
-- statement-break
UPDATE collector_next.schema_version SET version=4 WHERE version=3;
-- statement-break
ALTER TABLE collector_next.schema_version ADD CONSTRAINT schema_version_version_check CHECK(version=4);
-- statement-break
REVOKE ALL ON collector_next.import_operation,collector_next.import_item FROM PUBLIC;
