ALTER TABLE collector_next.receipt ADD COLUMN relative_path varchar(4096)
 CHECK(relative_path IS NULL OR (length(relative_path)>0 AND left(relative_path,1)<>'/'));
CREATE UNIQUE INDEX receipt_entry_once ON collector_next.receipt(entry_id) WHERE entry_id IS NOT NULL;
ALTER TABLE collector_next.schema_version DROP CONSTRAINT schema_version_version_check;
UPDATE collector_next.schema_version SET version=2 WHERE version=1;
ALTER TABLE collector_next.schema_version ADD CONSTRAINT schema_version_version_check CHECK(version=2);
