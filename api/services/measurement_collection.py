"""Bounded discovery of immutable caller assertions; never lifecycle authority."""
import base64
import binascii
import json
import re
from datetime import datetime, timezone
from sqlalchemy import text
from api.tagger_core.qualification_evidence import QualificationEvidenceRefused
from .measurement_evidence import limits


def stamp(value):
    return value.astimezone(timezone.utc).isoformat(timespec='microseconds')


def cursor_for(at, sha, tag_id):
    body = json.dumps(dict(v=1, at=stamp(at), sha=sha, tag_id=tag_id), sort_keys=True, separators=(',', ':'))
    return base64.urlsafe_b64encode(body.encode()).decode().rstrip('=')


def parse_cursor(value, tag_id):
    try:
        if not isinstance(value, str) or not 1 <= len(value) <= 512 or not re.fullmatch(r'[A-Za-z0-9_-]+', value):
            raise ValueError
        body = json.loads(base64.b64decode(value + '=' * (-len(value) % 4), altchars=b'-_', validate=True))
        if set(body) != {'v', 'at', 'sha', 'tag_id'} or type(body['v']) is not int or body['v'] != 1:
            raise ValueError
        if body['tag_id'] != tag_id or (body['tag_id'] is not None and type(body['tag_id']) is not int):
            raise ValueError
        if not isinstance(body['sha'], str) or not re.fullmatch('[0-9a-f]{64}', body['sha']):
            raise ValueError
        at = datetime.fromisoformat(body['at'])
        if at.tzinfo is None or cursor_for(at, body['sha'], tag_id) != value:
            raise ValueError
        return at, body['sha']
    except (ValueError, TypeError, KeyError, binascii.Error, OverflowError):
        raise QualificationEvidenceRefused('measurement_cursor_invalid') from None


async def collection(db, *, tag_id=None, limit=20, cursor=None):
    boundary = parse_cursor(cursor, tag_id) if cursor else None
    # Roll back even successful reads: SET LOCAL must not leak into callers' transactions.
    scope = await db.begin_nested()
    try:
        await limits(db)
        params = dict(tag=None if tag_id is None else str(tag_id), take=limit + 1)
        where = "(CAST(:tag AS text) IS NULL OR a.body->>'tag_id' = CAST(:tag AS text))"
        if boundary:
            at, sha = boundary
            exists = (await db.execute(text("SELECT 1 FROM tagger_measurement_anchor a WHERE " + where +
                ' AND a.measurement_sha256=:sha AND a.received_at=:at'), dict(params, sha=sha, at=at))).scalar()
            if exists is None:
                raise QualificationEvidenceRefused('measurement_cursor_missing')
            where += ' AND (a.received_at,a.measurement_sha256)<(:at,:sha)'
            params.update(at=at, sha=sha)
        rows = (await db.execute(text('''SELECT a.measurement_sha256, a.received_at,
            a.body->>'tag_id' AS tag_id, a.body->>'tag' AS tag,
            a.body->>'artifact_sha256' AS artifact_sha256,
            a.body->'threshold' AS threshold,
            (SELECT jsonb_build_object(
                'runtime', count(*) FILTER (WHERE e.body->>'kind'='runtime'),
                'human_sample', count(*) FILTER (WHERE e.body->>'kind'='human_sample'),
                'independent_review', count(*) FILTER (WHERE e.body->>'kind'='independent_review'))
             FROM (SELECT body FROM tagger_measurement_evidence
                   WHERE measurement_sha256=a.measurement_sha256
                   ORDER BY received_at,receipt_sha256 LIMIT 100) e) AS evidence_counts,
            EXISTS (SELECT 1 FROM tagger_measurement_evidence
                    WHERE measurement_sha256=a.measurement_sha256
                    ORDER BY received_at,receipt_sha256 OFFSET 100 LIMIT 1) AS counts_partial
            FROM tagger_measurement_anchor a WHERE ''' + where + '''
            ORDER BY a.received_at DESC,a.measurement_sha256 DESC LIMIT :take'''), params)).mappings().all()
        observed = (await db.execute(text('SELECT clock_timestamp()'))).scalar_one()
        items = [dict(row, stored_count_scope='first_100', tag_id=int(row['tag_id']), received_at=stamp(row['received_at']),
                      provenance='caller_assertion', measurement_scope='internal_training',
                      quality_granted=False, admission='not_granted') for row in rows[:limit]]
        next_cursor = cursor_for(rows[limit-1]['received_at'], rows[limit-1]['measurement_sha256'], tag_id) if len(rows)>limit else None
        return dict(outcome='succeeded', state='available', observed_at=stamp(observed),
                    reason='stored_internal_measurements_only', next='inspect_original_and_assertions',
                    items=items, next_cursor=next_cursor,
                    evidence_count_semantics='stored_caller_assertions_not_validations')
    finally:
        await scope.rollback()
