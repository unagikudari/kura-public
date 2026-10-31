"""Old source-table operations used only while copying or reversing cutover."""
from sqlalchemy import text


async def candidates(db,pid,actor,keep_ids):
    rows=await db.execute(text(
        "SELECT c.tag_id FROM post_tag_actor_claim c "
        "WHERE c.post_id = :pid AND c.actor_id = :actor "
        "AND c.tag_id <> ALL(:keep_ids) "
        "AND NOT EXISTS ("
        "  SELECT 1 FROM post_tag_actor_claim o "
        "  WHERE o.post_id = :pid AND o.tag_id = c.tag_id AND o.actor_id <> :actor"
        ")"),{'pid':pid,'actor':actor,'keep_ids':keep_ids})
    return list(rows.scalars())


async def replace(db,pid,actor,tag_ids):
    await db.execute(text("DELETE FROM post_tag_actor_claim WHERE post_id = :pid AND actor_id = :actor"),{'pid':pid,'actor':actor})
    await db.execute(text(
        "INSERT INTO post_tag_actor_claim (post_id, tag_id, actor_id) "
        "SELECT :pid, x, :actor FROM unnest((:tag_ids)::int[]) AS x"),
        {'pid':pid,'actor':actor,'tag_ids':tag_ids})
