"""Bounded representative image selection for tag catalog pages."""
from sqlalchemy import text

CANDIDATE_SCAN_LIMIT = 96
COLOR_CHOICES = 12


async def representatives(db, tag_ids: list[int]) -> dict[int, int]:
    if not tag_ids:
        return {}
    rows = (await db.execute(text('''
        SELECT u.id,
          ARRAY(SELECT p.id FROM (
              SELECT pt.post_id FROM post_tag pt WHERE pt.tag_id=u.id
              ORDER BY pt.post_id LIMIT :scan
            ) sample JOIN post p ON p.id=sample.post_id
            WHERE NOT EXISTS (SELECT 1 FROM post_tag x JOIN tag_name n ON n.tag_id=x.tag_id
                              WHERE x.post_id=p.id AND n.name IN ('monochrome','greyscale','comic'))
            ORDER BY p.id LIMIT :colors),
          (SELECT pt.post_id FROM post_tag pt WHERE pt.tag_id=u.id
           ORDER BY pt.post_id LIMIT 1)
        FROM unnest(CAST(:ids AS int[])) AS u(id)
    '''), {'ids': tag_ids, 'scan': CANDIDATE_SCAN_LIMIT, 'colors': COLOR_CHOICES})).all()
    candidates = {tid: (list(colors or []), fallback) for tid, colors, fallback in rows}
    used: set[int] = set()
    result: dict[int, int] = {}
    for tid in tag_ids:
        colors, fallback = candidates.get(tid, ([], None))
        pick = next((pid for pid in colors if pid not in used), None)
        if pick is None:
            pick = colors[0] if colors else fallback
        if pick is not None:
            result[tid] = pick
            used.add(pick)
    return result
