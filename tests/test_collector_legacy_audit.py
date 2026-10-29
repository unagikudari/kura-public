"""Read-only migration evidence preserves shared scope and refuses partial scans."""
import pytest
from sqlalchemy import text
from test_collector_next import postgres,run  # noqa:F401
from test_pool_search_candidates import search_database  # noqa:F401
from api.collector.adapters.legacy_audit import inspect_pool


def test_shared_snapshot_matches_bridge_without_inventing_ownership_or_intent(search_database):
    async def scenario(sessions):
        async with sessions.begin() as db:
            statements=[
              'CREATE TABLE pool(id int PRIMARY KEY,version int)',
              'CREATE TABLE pool_name(pool_name_id int,pool_id int,name text,ord int)',
              'CREATE TABLE pool_post(pool_id int,post_id int,ord int)',
              'CREATE TABLE post(id int PRIMARY KEY,version int,type text,original_filename text,status text,safety text,"mime-type" text,file_size bigint,sha256 bytea)',
              'CREATE TABLE tag(id int PRIMARY KEY,category_id int)',
              'CREATE TABLE tag_category(id int PRIMARY KEY,name text)',
              'CREATE TABLE tag_name(tag_name_id int,tag_id int,name text,ord int)',
              'CREATE TABLE post_tag(post_id int,tag_id int,locked bool)',
              'CREATE TABLE post_tag_pin(post_id int,tag_id int,state text,pinned_by text,audit_id bigint,pinned_at timestamptz,PRIMARY KEY(post_id,tag_id))',
              'CREATE TABLE post_tag_audit(id bigint PRIMARY KEY,post_id int,actor_type text,actor_id text,source text,added_tags text[],removed_tags text[],created_at timestamptz,host text,release_id int)',
              'CREATE TABLE post_tag_actor_claim(post_id int,tag_id int,actor_id text,claimed_at timestamptz,PRIMARY KEY(post_id,tag_id,actor_id))',
              "INSERT INTO pool VALUES(7,3),(8,1)",
              "INSERT INTO pool_name VALUES(1,7,'Book',0)",
              "INSERT INTO post SELECT i,1,'image',i||'.png',CASE WHEN i%13=0 THEN 'pending' ELSE 'ok' END,CASE WHEN i%2=0 THEN 'safe' ELSE 'unknown' END,'image/png',100,sha256(i::text::bytea) FROM generate_series(1,128)i",
              'INSERT INTO pool_post SELECT 7,id,id/3 FROM post',
              "INSERT INTO tag_category VALUES(1,'artist')",
              'INSERT INTO tag VALUES(1,1),(2,1),(3,1),(4,1),(5,1)',
              "INSERT INTO tag_name VALUES(1,1,'Artist',0),(2,2,'Ａｒｔｉｓｔ',0),(3,3,'advertisement',0),(4,4,'bad,tag',0),(5,5,'removed-tag',0)",
              'INSERT INTO post_tag SELECT id,1,id=7 FROM post',
              'INSERT INTO post_tag SELECT id,2,false FROM post WHERE id%3=0',
              'INSERT INTO post_tag SELECT id,3,false FROM post WHERE id%17=0',
              'INSERT INTO post_tag VALUES(1,4,false)',
              "INSERT INTO post_tag_pin VALUES(1,5,'absent','human:owner',10,'2026-01-01Z')",
              "INSERT INTO post_tag_audit VALUES(10,1,'human','owner','manual','{}',ARRAY['old-name'],'2026-01-01Z',NULL,NULL),(11,1,'release','model-a','auto_tag',ARRAY['Artist'],'{}','2026-01-02Z','primary',42),(12,999,'human','other','manual','{}',ARRAY['outside'],'2026-01-01Z',NULL,NULL)",
              "INSERT INTO post_tag_actor_claim VALUES(1,1,'model-a','2026-01-02Z'),(1,1,'model-b','2026-01-02Z'),(2,5,'model-b','2026-01-02Z')",
            ]
            for sql in statements:await db.execute(text(sql))
            with pytest.raises(ValueError,match='read-only'):
                await inspect_pool(db,7)
        async with sessions() as db:
            await db.execute(text('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY'))
            first=await inspect_pool(db,7)
            second=await inspect_pool(db,7)
            assert first==second
            assert first['summary']['entries']==128 and first['summary']['read_contract_verified']
            assert first['summary']['locked_memberships']==1
            assert first['readiness']['ready_for_import'] is False
            assert first['ownership']=='shared-unassigned'
            assert first['readiness']['normalized_collisions']==[{'normalized_name':'artist','tag_ids':[1,2]}]
            assert first['readiness']['unsupported_names']==[{'tag_id':4,'name':'bad,tag'}]
            assert first['readiness']['unmapped_provenance_tables']==['post_tag_actor_claim','post_tag_audit','post_tag_pin']
            captured=await inspect_pool(db,7,include_provenance=True)
            assert captured==await inspect_pool(db,7,include_provenance=True)
            assert captured['format']=='kura-legacy-read-audit-v2'
            evidence=captured['provenance']
            assert evidence['summary']['rows']==6 and not evidence['missing_tables']
            exact=await inspect_pool(db,7,include_provenance=True,max_provenance_rows=6,
                max_provenance_bytes=evidence['summary']['source_json_bytes'])
            assert exact==captured
            assert evidence['tables']['post_tag_pin'][0]['state']=='absent'
            assert evidence['tables']['post_tag_audit'][0]['removed_tags']==['old-name']
            assert evidence['tables']['post_tag_audit'][1]['release_id']==42
            assert [r['actor_id'] for r in evidence['tables']['post_tag_actor_claim'][:2]]==['model-a','model-b']
            assert 5 in [r['id'] for r in captured['dictionary']]
            assert captured['summary']['memberships']==first['summary']['memberships']
            assert not captured['readiness']['provenance_converted']
            assert not captured['readiness']['ready_for_import']
            assert captured['snapshot_sha256']!=first['snapshot_sha256']
            assert (await inspect_pool(db,8,include_provenance=True))['provenance']['summary']['rows']==0
            for options, message in [({'max_provenance_rows':5},'row budget'),
                                     ({'max_provenance_bytes':10},'byte budget'),
                                     ({'max_provenance_rows':0},'Invalid provenance')]:
                with pytest.raises(ValueError,match=message):
                    await inspect_pool(db,7,include_provenance=True,**options)
            safe=await inspect_pool(db,7,safe=True)
            assert safe['summary']['displayable_entries']<first['summary']['displayable_entries']
            assert all(r['safety']=='safe' for r in safe['entries'] if r['displayable'])
            assert not any(r['displayable'] for r in safe['entries'] if r['id']%17==0)
            assert (await inspect_pool(db,8))['summary']['entries']==0
            with pytest.raises(ValueError,match='Entry budget'):
                await inspect_pool(db,7,max_entries=100)
            with pytest.raises(ValueError,match='Membership budget'):
                await inspect_pool(db,7,max_memberships=10)
        async with sessions.begin() as db:
            await db.execute(text('DROP TABLE post_tag_actor_claim'))
        async with sessions() as db:
            await db.execute(text('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY'))
            absent=await inspect_pool(db,7,include_provenance=True)
            assert absent['provenance']['missing_tables']==['post_tag_actor_claim']
            assert not absent['readiness']['ready_for_import']
        async with sessions.begin() as db:
            await db.execute(text('INSERT INTO pool_post VALUES(7,9999,9999)'))
        async with sessions() as db:
            await db.execute(text('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY'))
            with pytest.raises(ValueError,match='dangling'):
                await inspect_pool(db,7)
    run(search_database,scenario)
