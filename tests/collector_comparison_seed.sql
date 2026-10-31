-- Synthetic metadata and organizational labels only; no source/user media.
INSERT INTO collector_next.blob(sha256,byte_size)
 SELECT sha256(i::text::bytea),100 FROM generate_series(1,:rows) i;
INSERT INTO collector_next.asset(owner_id,blob_id) SELECT 1,id FROM collector_next.blob;
INSERT INTO collector_next.media_current(asset_id,owner_id,original_name,media_type,width,height)
 SELECT id,1,'synthetic-'||id||'.png','image/png',512,512 FROM collector_next.asset;
CREATE TEMP TABLE corpus_tags AS
 SELECT id AS asset_id,1 AS tag_id FROM collector_next.asset
 UNION ALL SELECT id,2 FROM collector_next.asset WHERE id%10=0
 UNION ALL SELECT id,3 FROM collector_next.asset WHERE id%100=0
 UNION ALL SELECT id,4+(id%8)::int FROM collector_next.asset;
CREATE INDEX ON corpus_tags(asset_id);
INSERT INTO collector_next.tag(owner_id,name,normalized_name)
 SELECT 1,'tag_'||i,'tag_'||i FROM generate_series(1,11) i ORDER BY i;
INSERT INTO collector_next.label_producer(owner_id,kind_id,reference,version)
 VALUES(1,1,'1','collector-label-api-v1');
INSERT INTO collector_next.label_operation(owner_id,request_key,fingerprint,producer_id,asset_count,tag_count,created_at)
 SELECT 1,md5('seed:'||asset_id)::uuid,sha256(asset_id::text::bytea),1,1,count(*),'2026-01-01'
 FROM corpus_tags GROUP BY asset_id;
INSERT INTO collector_next.label_observation(owner_id,asset_id,tag_id,producer_id,request_key,present,prior_present,observed_at)
 SELECT 1,asset_id,tag_id,1,md5('seed:'||asset_id)::uuid,true,false,'2026-01-01' FROM corpus_tags;
INSERT INTO collector_next.label_head(owner_id,asset_id,tag_id,observation_id,present)
 SELECT owner_id,asset_id,tag_id,id,true FROM collector_next.label_observation;
INSERT INTO collector_next.asset_label_state(asset_id,owner_id,revision,positive_count,last_observed_at)
 SELECT asset_id,1,1,count(*),'2026-01-01' FROM corpus_tags GROUP BY asset_id;
INSERT INTO collector_next.label_target(owner_id,request_key,asset_id,before_revision,after_revision)
 SELECT 1,md5('seed:'||id)::uuid,id,0,1 FROM collector_next.asset;
INSERT INTO collector_next.tag_usage(tag_id,owner_id,positive_count,logical_bytes,representative_asset_id,revision)
 SELECT tag_id,1,count(*),count(*)*100,min(asset_id),1 FROM corpus_tags GROUP BY tag_id;
INSERT INTO public."user"(id,creation_time,version,name,password_revision,rank,avatar_style)
 VALUES(1,'2026-01-01',1,'synthetic',0,'regular','gravatar');
INSERT INTO public.tag_category(id,version,name,color,"default","order") VALUES(1,1,'default','#888',true,1);
INSERT INTO public.tag(id,category_id,version,creation_time,post_count)
 SELECT i,1,1,'2026-01-01',(SELECT count(*) FROM corpus_tags WHERE tag_id=i) FROM generate_series(1,11) i;
INSERT INTO public.tag_name(tag_id,name,ord) SELECT i,'tag_'||i,0 FROM generate_series(1,11) i;
INSERT INTO public.post(id,user_id,version,creation_time,safety,original_filename,type,checksum,file_size,image_width,image_height,"mime-type",tagging_status,tagging_attempts,status,media_location,flags,sha256)
 SELECT id,1,1,'2026-01-01','safe','synthetic-'||id||'.png','image',encode(sha256(id::text::bytea),'hex'),100,512,512,'image/png','done',0,'ok','pg','',sha256(id::text::bytea)
 FROM collector_next.asset;
INSERT INTO public.post_tag(post_id,tag_id,locked) SELECT asset_id,tag_id,false FROM corpus_tags;
UPDATE public.post p SET tags=t.names,tags_search=t.names FROM
 (SELECT asset_id,array_agg('tag_'||tag_id ORDER BY tag_id) names FROM corpus_tags GROUP BY asset_id) t WHERE p.id=t.asset_id;
-- Relevant live read indexes, including the covering count path. No deliberate
-- omission of the current index-only aggregate optimization.
CREATE INDEX IF NOT EXISTS ix_post_ok_file_size ON public.post(id) INCLUDE(file_size) WHERE status='ok';
CREATE INDEX IF NOT EXISTS ix_post_safe_ok_file_size ON public.post(id) INCLUDE(file_size) WHERE status='ok' AND safety='safe';
CREATE INDEX IF NOT EXISTS ix_post_status ON public.post(status) WHERE status IS NOT NULL;
CREATE INDEX IF NOT EXISTS ix_post_tags_search_gin ON public.post USING gin(tags_search);
CREATE INDEX IF NOT EXISTS ix_post_tag_tag_post ON public.post_tag(tag_id,post_id);
CREATE INDEX IF NOT EXISTS ix_tag_name_lower ON public.tag_name(lower(name));
