"""Dashboard read model over immutable sets and their indexed members."""
from collections import Counter

from sqlalchemy import text

from api.tagger_core.post_exclusion import EXCLUDING_TAGS
from api.tagger_core.review_censor_variants import review_candidate

_EXCLUDING = ", ".join(repr(tag) for tag in EXCLUDING_TAGS)

POPULATION = f"""
WITH active AS MATERIALIZED (
    SELECT q.*, n.name AS tag, c.kind AS cohort_kind
    FROM tagger_review_current_set q
    JOIN tag_name n ON n.tag_id=q.tag_id AND n.ord=0
    LEFT JOIN tagger_review_cohort c USING (snapshot_sha256)
), catalog_members AS MATERIALIZED (
    SELECT m.queue_sha256, m.post_id, m.ordinal
    FROM active a JOIN tagger_review_set_member m USING (queue_sha256)
), settled AS MATERIALIZED (
    SELECT a.queue_sha256, count(*) AS total
    FROM active a JOIN catalog_members m USING (queue_sha256)
    LEFT JOIN tagger_review_member_answer answer
      ON answer.queue_sha256=m.queue_sha256 AND answer.post_id=m.post_id
    WHERE a.schema_version<>1 AND NOT a.adjudication
    GROUP BY a.queue_sha256 HAVING count(*)=count(answer.event_id)
), open_members AS MATERIALIZED (
    SELECT m.* FROM catalog_members m
    WHERE NOT EXISTS (SELECT 1 FROM settled s WHERE s.queue_sha256=m.queue_sha256)
), population_ids AS MATERIALIZED (
    SELECT DISTINCT post_id FROM open_members
), exclusion_tags AS MATERIALIZED (
    SELECT DISTINCT tag_id FROM tag_name WHERE name IN ({_EXCLUDING})
), post_facts AS MATERIALIZED (
    SELECT p.id, p.status,
           coalesce(membership.hidden AND NOT membership.visible, FALSE) AS hidden,
           coalesce(membership.training, FALSE) AS pool_excluded,
           EXISTS (
              SELECT 1 FROM post_tag ex
              WHERE ex.post_id=p.id AND ex.tag_id=ANY(ARRAY(SELECT tag_id FROM exclusion_tags))
           ) AS tag_excluded
    FROM population_ids ids JOIN post p ON p.id=ids.post_id
    LEFT JOIN LATERAL (
       SELECT bool_or(pl.hidden) AS hidden, bool_or(NOT pl.hidden) AS visible,
              bool_or(ex.excluded) AS training
       FROM pool_post pp JOIN pool pl ON pl.id=pp.pool_id
       LEFT JOIN tagger_auto_exclude_pool ex ON ex.pool_id=pp.pool_id
       WHERE pp.post_id=p.id
    ) membership ON true
), members AS (
    SELECT a.*, m.post_id, m.ordinal, answer.decision, pin.state AS pin
    FROM active a JOIN open_members m USING (queue_sha256)
    JOIN post_facts p ON p.id=m.post_id
    LEFT JOIN tagger_review_member_answer answer ON answer.queue_sha256=m.queue_sha256 AND answer.post_id=m.post_id
    LEFT JOIN LATERAL (
        SELECT ppin.state FROM post_tag_pin ppin
        WHERE a.schema_version=1 AND ppin.post_id=m.post_id AND ppin.tag_id=a.tag_id LIMIT 1
    ) pin ON true
    WHERE NOT p.hidden
      AND (a.purpose NOT IN ('historical_label_recheck','training_support')
           OR a.adjudication
           OR (p.status='ok' AND NOT p.pool_excluded AND NOT p.tag_excluded
               AND (answer.decision IS NOT NULL OR {review_candidate("p.id", "a.tag")})))
)
SELECT queue_sha256, tag, purpose, schema_version, lane, review_kind, cohort_kind, created_at, count(*) AS total,
       count(*) FILTER (WHERE CASE WHEN schema_version=1 THEN pin IN ('present','absent')
          WHEN adjudication THEN decision IN ('present','absent')
          ELSE decision IS NOT NULL END) AS answered
FROM members GROUP BY queue_sha256, tag, purpose, schema_version, lane, review_kind, cohort_kind, created_at
UNION ALL
SELECT a.queue_sha256,a.tag,a.purpose,a.schema_version,a.lane,a.review_kind,a.cohort_kind,
       a.created_at,s.total,s.total AS answered
FROM active a JOIN settled s USING(queue_sha256)
"""


def classify(row):
    purpose = row["purpose"]
    if row["cohort_kind"] == "base_switch":
        return 0, "lifecycle", "乗り換え比較"
    if purpose in ("candidate_evaluation", "production_precision", "fixed_evaluation"):
        return 1, "lifecycle", "公開前確認"
    if row["review_kind"] == "age_check":
        return 2, "age_check", "年齢確認"
    if purpose == "training_support":
        return 3, "lifecycle", "正例集め"
    if row["schema_version"] == 3:
        return 4, "lifecycle", "盲検"
    if row["lane"] == "lora_findings":
        return 6, "lora_findings", "誤り候補"
    return 5, "lifecycle", "再確認"


def project(rows):
    pending = []
    completed = Counter()
    for row in rows:
        total = row["total"]
        remaining = total - row["answered"]
        if remaining <= 0:
            completed[row["tag"], row["purpose"]] += 1
            continue
        priority, lane, label = classify(row)
        href = "/tagger-review/" + row["queue_sha256"]
        if row["schema_version"] == 1:
            href = "/api/tagger-dashboard/review-queues/" + row["queue_sha256"] + "/open"
        pending.append((priority, row["created_at"], dict(
            tag=row["tag"], purpose=row["purpose"], label=label, lane=lane,
            total=total, remaining=remaining, href=href,
        )))
    pending.sort(key=lambda item: (item[0], item[1], item[2]["tag"]))
    return [item[2] for item in pending], [
        {"tag": tag, "purpose": purpose, "sets": sets}
        for (tag, purpose), sets in sorted(completed.items())
    ]


PRIORITY = """
    CASE WHEN cohort_kind='base_switch' THEN 0
         WHEN review_kind='prepublication' THEN 1
         WHEN review_kind='age_check' THEN 2
         WHEN review_kind='training_support' THEN 3
         WHEN schema_version=3 THEN 4
         WHEN lane='lora_findings' THEN 6 ELSE 5 END
"""


async def population(db):
    return (await db.execute(text("""
        SELECT * FROM tagger_review_dashboard_counts ORDER BY created_at DESC, queue_sha256
    """))).mappings().all()


async def first_pending(db):
    return (await db.execute(text(f"""
        SELECT *, count(*) OVER () AS pending_total FROM tagger_review_dashboard_counts
        WHERE answered < total ORDER BY {PRIORITY}, created_at, tag, queue_sha256 LIMIT 10
    """))).mappings().all()


async def history(db, *, offset, limit):
    return (await db.execute(text("""
        WITH completed AS MATERIALIZED (
            SELECT queue_sha256, tag, purpose, total, created_at, schema_version
            FROM tagger_review_dashboard_counts WHERE total=answered
        ), page AS (
            SELECT tag,purpose,total,created_at,queue_sha256,
                   CASE WHEN schema_version>1 THEN '/tagger-review/'||queue_sha256 END AS href
            FROM completed ORDER BY created_at DESC,queue_sha256 OFFSET :offset LIMIT :limit
        )
        SELECT (SELECT count(*) FROM completed) AS total,
               coalesce((SELECT jsonb_agg(to_jsonb(p)-'queue_sha256' ORDER BY created_at DESC,queue_sha256) FROM page p),
                        '[]'::jsonb) AS results
    """), {"offset": offset, "limit": limit})).mappings().one()
