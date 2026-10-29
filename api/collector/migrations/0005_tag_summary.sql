-- One current-state summary per tag, never a second count authority.
ALTER TABLE collector_next.tag_usage
 ADD COLUMN logical_bytes bigint NOT NULL DEFAULT 0 CHECK(logical_bytes>=0),
 ADD COLUMN representative_asset_id bigint,
 ADD COLUMN revision bigint NOT NULL DEFAULT 0 CHECK(revision>=0),
 ADD COLUMN representative_policy smallint NOT NULL DEFAULT 1 CHECK(representative_policy=1),
 ADD CONSTRAINT tag_representative_owner FOREIGN KEY(owner_id,representative_asset_id)
   REFERENCES collector_next.asset(owner_id,id);
-- statement-break
CREATE INDEX tag_owner_cursor ON collector_next.tag(owner_id,id DESC);
-- statement-break
INSERT INTO collector_next.tag_usage(tag_id,owner_id,positive_count)
 SELECT id,owner_id,0 FROM collector_next.tag ORDER BY id ON CONFLICT(tag_id) DO NOTHING;
-- statement-break
UPDATE collector_next.tag_usage u SET positive_count=s.n,logical_bytes=s.bytes,
 representative_asset_id=s.cover,revision=1
FROM (SELECT t.id,count(h.asset_id) AS n,COALESCE(sum(b.byte_size),0) AS bytes,min(h.asset_id) AS cover
 FROM collector_next.tag t
 LEFT JOIN collector_next.label_head h ON h.tag_id=t.id AND h.owner_id=t.owner_id AND h.present
 LEFT JOIN collector_next.asset a ON a.id=h.asset_id
 LEFT JOIN collector_next.blob b ON b.id=a.blob_id GROUP BY t.id) s WHERE u.tag_id=s.id;
-- statement-break
ALTER TABLE collector_next.tag_usage ADD CONSTRAINT tag_summary_empty
 CHECK(positive_count>0 OR (logical_bytes=0 AND representative_asset_id IS NULL));
-- statement-break
ALTER TABLE collector_next.schema_version DROP CONSTRAINT schema_version_version_check;
-- statement-break
UPDATE collector_next.schema_version SET version=5 WHERE version=4;
-- statement-break
ALTER TABLE collector_next.schema_version ADD CONSTRAINT schema_version_version_check CHECK(version=5);
