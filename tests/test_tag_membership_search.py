"""Tag membership preserves aliases, one-hop implications, NULLs and counts."""
import sqlalchemy as sa

from test_collector_next import postgres, run  # noqa: F401
from test_pool_search_candidates import search_database  # noqa: F401
from api.routers import posts


def test_membership_filters_and_total_on_private_postgres(search_database, monkeypatch):
    monkeypatch.setattr(posts, "FAST_TOTAL_MIN", 1)

    async def scenario(sessions):
        async with sessions.kw["bind"].begin() as db:
            statements = [
                "CREATE TABLE post(id int PRIMARY KEY, status text, safety text, tags_search text[])",
                "CREATE TABLE tag(id int PRIMARY KEY, post_count int)",
                "CREATE TABLE tag_name(tag_id int, name text, ord int)",
                "CREATE TABLE tag_implication(parent_id int, child_id int)",
                "CREATE TABLE post_tag(post_id int, tag_id int, PRIMARY KEY(post_id,tag_id))",
                "INSERT INTO tag VALUES (1,3),(2,1),(3,1),(4,2),(5,1),(6,1)",
                "INSERT INTO tag_name VALUES (1,'parent',0),(1,'alias',1),(2,'child',0),"
                "(3,'grandchild',0),(4,'glasses',0),(5,'advertisement',0),(6,'child_(other)',0)",
                "INSERT INTO tag_implication VALUES (1,2),(2,3)",
                "INSERT INTO post VALUES "
                "(1,'ok','safe',ARRAY['parent','child']),"
                "(2,'ok','safe',ARRAY['child','grandchild']),"
                "(3,'ok','safe',ARRAY['grandchild']),"
                "(4,'ok','safe',ARRAY['glasses']),"
                "(5,'ok','safe',NULL),"
                "(6,'pending','safe',ARRAY['parent','child','glasses']),"
                "(7,'ok','safe',ARRAY['parent','child','advertisement']),"
                "(8,'ok','safe',ARRAY['child_(other)']),"
                "(9,'ok','safe',ARRAY[]::text[])",
                "INSERT INTO post_tag VALUES (1,1),(2,2),(3,3),(4,4),(6,1),(6,4),(7,1),(7,5),(8,6)",
            ]
            for statement in statements:
                await db.execute(sa.text(statement))
        async with sessions() as db:
            cases = {
                "parent": {1, 6, 7}, "ALIAS": {1, 6, 7},
                "child": {1, 2, 6, 7, 8}, "grandchild": {2, 3},
                "child glasses": {6}, "-child": {3, 4, 5, 9},
                "-missing": set(range(1, 10)), "par*": {1, 6, 7},
                "parent -glasses": {1, 7}, "missing": set(),
            }
            for query, expected in cases.items():
                clauses, _ = await posts._build_where_clauses(db, query)
                actual = set((await db.execute(sa.select(posts.Post.post_id).where(*clauses))).scalars())
                assert actual == expected, query
            # Count excludes one non-ok post and one advertisement, including
            # when the user searches an alias of the same tag.
            for query in ("parent", "alias"):
                assert await posts._fast_total_single_tag(db, query, safe=False) == 1
            assert await posts._fast_total_single_tag(db, "child", safe=False) is None
            assert await posts._fast_total_single_tag(db, "parent", safe=True) is None

    run(search_database, scenario)
