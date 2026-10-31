-- Label producers know which library their evidence came from (#152). A restored package's
-- producers keep the source library's identity, so a local edit by an account with the same
-- number in this library is a different producer and the two never merge.
CREATE TABLE collector_next.library(
 singleton boolean PRIMARY KEY DEFAULT true CHECK(singleton),
 id uuid NOT NULL
);
-- statement-break
INSERT INTO collector_next.library(id) VALUES (gen_random_uuid());
-- statement-break
COMMENT ON TABLE collector_next.library IS 'This library''s identity, made once per install (migration, baseline initial rows); never copied between installs';
-- statement-break
ALTER TABLE collector_next.label_producer ADD COLUMN origin text NOT NULL DEFAULT ''
 CHECK(origin='' OR origin ~ '^(unknown:)?[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$');
-- statement-break
COMMENT ON COLUMN collector_next.label_producer.origin IS
 'empty = this library; a library id = evidence restored from that library; unknown:<export id> = from a package that did not say';
-- statement-break
ALTER TABLE collector_next.label_producer DROP CONSTRAINT label_producer_owner_id_kind_id_reference_version_key;
-- statement-break
ALTER TABLE collector_next.label_producer ADD CONSTRAINT label_producer_identity UNIQUE(owner_id,kind_id,reference,version,origin);
-- statement-break
ALTER TABLE collector_next.schema_version DROP CONSTRAINT schema_version_version_check;
-- statement-break
UPDATE collector_next.schema_version SET version=6 WHERE version=5;
-- statement-break
ALTER TABLE collector_next.schema_version ADD CONSTRAINT schema_version_version_check CHECK(version=6);
