"""Human evidence and current disagreements are not model predictions.

Pins take precedence while the asynchronous GT sync catches up. Counts are distinct
post/tag pairs, include ordinary corrections outside archived pools, and never use
post_tag as a proxy for a model's uncorrected output.
"""
EVIDENCE_SQL = """
WITH evidence AS (
 SELECT coalesce(g.tag_id, pin.tag_id) AS tag_id,
        coalesce(g.post_id, pin.post_id) AS post_id,
        CASE WHEN pin.state='present' THEN 'positive'
             WHEN pin.state='absent' THEN 'negative' ELSE g.verdict END AS verdict,
        (pin.state IS NOT NULL OR g.derived_from='human_correction') AS human
 FROM tagger_gt_label g
 FULL JOIN post_tag_pin pin ON pin.tag_id=g.tag_id AND pin.post_id=g.post_id
)
SELECT tn.name AS tag,
 count(*) FILTER (WHERE e.verdict='positive') AS positive,
 count(*) FILTER (WHERE e.verdict='negative') AS negative,
 count(*) FILTER (WHERE e.human AND e.verdict='negative') AS corrections,
 count(*) FILTER (WHERE e.human AND e.verdict='negative' AND pt.tag_id IS NOT NULL)
   AS unwanted_present,
 count(*) FILTER (WHERE e.human AND e.verdict='positive' AND pt.tag_id IS NULL)
   AS wanted_missing
FROM evidence e JOIN post p ON p.id=e.post_id AND p.status='ok'
JOIN tag_name tn ON tn.tag_id=e.tag_id AND tn.ord=0
LEFT JOIN post_tag pt ON pt.post_id=e.post_id AND pt.tag_id=e.tag_id
GROUP BY tn.name
"""


def evidence_counts(row):
    row = row or {}
    return (int(row.get('corrections', 0)),
            int(row.get('unwanted_present', 0)) + int(row.get('wanted_missing', 0)),
            int(row.get('positive', 0)), int(row.get('negative', 0)))
