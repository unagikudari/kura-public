"""Canonical, evidence-backed inputs for the standalone heads trainer."""
from contextlib import closing

from api.tagger_core.post_exclusion import not_excluded
from ..kura_db import connect_canonical
from .fetch_excluded import SOURCE_SQL

CONCEPTS = (
    "dark_labia_majora", "dark_labia_minora", "dark_penis", "dark_perineum",
    "half-erect", "phimosis", "small_penis", "wrinkled_labia",
)
FLOOR_KEYS = ("min_gt_positive", "min_gt_negative", "min_val_pos_support")

QUALITY_KEYS = ("min_precision", "min_recall")


from api.tagger_core.post_exclusion import EVALUATION_AUDITS as EVALUATION_AUDITS_SQL  # one list with the main GT readers


def not_evaluation_audit(audit_id):
    return f"NOT EXISTS (SELECT 1 FROM ({EVALUATION_AUDITS_SQL}) e WHERE e.audit_id={audit_id})"


LABELS_SQL = f"""
WITH legacy_exclusions AS MATERIALIZED ({SOURCE_SQL}),
     evaluation_audits AS MATERIALIZED ({EVALUATION_AUDITS_SQL}),
     candidates AS MATERIALIZED (
SELECT current_name.name, g.post_id, g.verdict, a.id AS audit_id
FROM tagger_gt_label g
JOIN tag_name current_name ON current_name.tag_id=g.tag_id AND current_name.ord=0
JOIN post p ON p.id=g.post_id
JOIN post_tag_pin pin ON pin.post_id=g.post_id AND pin.tag_id=g.tag_id
JOIN post_tag_audit a ON a.id=pin.audit_id AND a.post_id=g.post_id
LEFT JOIN post_tag pt ON pt.post_id=g.post_id AND pt.tag_id=g.tag_id
WHERE current_name.name=ANY(%s)
  AND g.derived_from='human_correction'
  AND a.actor_type='human' AND btrim(a.actor_id)<>''
  AND p.status='ok' AND p.type='image' AND p.media_location='pg'
  AND (p.data IS NOT NULL OR EXISTS (
      SELECT 1 FROM post_rendition r WHERE r.post_id=p.id AND r.kind='view'
  ))
  AND ((g.verdict='positive' AND pin.state='present' AND pt.post_id IS NOT NULL)
    OR (g.verdict='negative' AND pin.state='absent' AND pt.post_id IS NULL))
  AND {not_excluded('p.id')}
  AND NOT EXISTS (SELECT 1 FROM legacy_exclusions ex WHERE ex.post_id=p.id)
)
SELECT c.name,c.post_id,c.verdict,c.audit_id
FROM candidates c
WHERE NOT EXISTS (SELECT 1 FROM evaluation_audits e WHERE e.audit_id=c.audit_id)
ORDER BY c.name,c.post_id
"""


def read_inputs(conn):
    """Read labels and floors in one snapshot; missing floors fail closed."""
    with conn.cursor() as cursor:
        cursor.execute("SET LOCAL statement_timeout='30s'")
        cursor.execute("SELECT key,value FROM tagger_config WHERE key=ANY(%s)", (list(FLOOR_KEYS + QUALITY_KEYS),))
        floors = {key: float(value) if key in QUALITY_KEYS else int(value) for key, value in cursor.fetchall()}
        if (set(floors) != set(FLOOR_KEYS + QUALITY_KEYS)
                or any(floors[key] < 0 for key in FLOOR_KEYS)
                or any(not 0 <= floors[key] <= 1 for key in QUALITY_KEYS)):
            raise ValueError("heads require valid canonical GT floors")
        cursor.execute(LABELS_SQL, (list(CONCEPTS),))
        samples = {tag: {"positive": {}, "negative": {}} for tag in CONCEPTS}
        for tag, pid, verdict, audit_id in cursor.fetchall():
            samples[tag][verdict][pid] = audit_id
    return samples, floors


def load_inputs():
    with closing(connect_canonical()) as conn:
        conn.set_session(readonly=True, isolation_level="REPEATABLE READ")
        return read_inputs(conn)


def coverage(samples, floors):
    """Report actual inputs and shortfalls per verdict, including empty tags."""
    return {
        tag: {
            "positive": len(labels["positive"]),
            "negative": len(labels["negative"]),
            "need_positive": max(0, floors["min_gt_positive"] - len(labels["positive"])),
            "need_negative": max(0, floors["min_gt_negative"] - len(labels["negative"])),
        }
        for tag, labels in samples.items()
    }
