"""Current positive support for registrations stopped before their human split."""
from urllib.parse import urlencode

from sqlalchemy import text

from api.tagger_core.post_exclusion import training_label

SQL = f"""
WITH latest AS (
    SELECT DISTINCT ON (tag) tag, status, rejected_reason, metrics
    FROM tagger_registration_request ORDER BY tag, id DESC
), floors AS (
    SELECT sum(ceil(value::numeric))::int AS required
    FROM tagger_config
    WHERE key IN ('min_val_pos_support','min_gt_positive')
    HAVING count(*)=2 AND min(value::numeric)>0
)
SELECT l.tag, f.required, (
    SELECT count(*)::int FROM tagger_gt_label g
    WHERE g.tag=l.tag AND g.verdict='positive' AND g.derived_from='human_correction'
      AND {training_label('g')}
) AS observed
FROM latest l CROSS JOIN floors f
WHERE l.status='rejected' AND (
    l.metrics->>'reason'='insufficient_human_split_support'
    OR l.rejected_reason ~ '(^|[ =])insufficient_human_split_support($|[[:space:]])'
)
ORDER BY l.tag
"""


async def read_human_support(db):
    rows = (await db.execute(text(SQL))).mappings()
    return [
        dict(tag=row["tag"], observed=row["observed"], required=row["required"],
             needed=max(0, row["required"] - row["observed"]),
             edit_url="/?" + urlencode(dict(
                 q=f"{row['tag']} -locked:{row['tag']}", bulk=1, bulkTag=row["tag"])))
        for row in rows
    ]
