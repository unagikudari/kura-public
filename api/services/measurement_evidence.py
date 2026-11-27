"""Intake for migration 0201; never reads or writes lifecycle tables.

Caller owns commit/rollback. Deployment requires the reviewed migration chain.
"""
import json
from sqlalchemy import text
from api.tagger_core import qualification_evidence as q
from api.tagger_core import measurement_evidence as contract


async def limits(db):
    await db.execute(text("SET LOCAL lock_timeout='2s'"))
    await db.execute(text("SET LOCAL statement_timeout='10s'"))


async def submit(db, *, tag_id, tag, measurement, user_id):
    q._integer(user_id)
    await limits(db)
    # Pin tag identity in the immutable request. An alias/merge/rename must not
    # silently turn a lost-ack replay into a different anchor.
    packet = contract.original_measurement(tag_id, tag, measurement)
    prior = (await db.execute(text(
        'SELECT canonical_body,submitted_by,received_at FROM tagger_measurement_anchor '
        'WHERE measurement_sha256=:ref'), {'ref': packet['measurement_sha256']})).mappings().one_or_none()
    if prior is not None:
        q.require(prior['canonical_body'] == packet['canonical_body'], 'measurement_receipt_collision')
        row = prior
    else:
        identity = (await db.execute(text(
            'SELECT t.id, n.name AS tag FROM tag t LEFT JOIN tag_name n ON n.tag_id=t.id '
            'WHERE t.id=:id ORDER BY n.ord,n.tag_name_id LIMIT 1'),
            {'id': tag_id})).mappings().one_or_none()
        q.require(identity is not None, 'measurement_tag_missing')
        q.require(identity['tag'] == tag, 'measurement_tag_identity_mismatch')
        row = await _insert_anchor(db, packet, user_id)
    return dict(outcome='succeeded', state='original_measurement_received_unverified',
                reason='internal_measurement_is_not_a_training_event',
                next='attach_assertions_without_changing_lifecycle',
                measurement_sha256=packet['measurement_sha256'], first_submitted_by=row['submitted_by'],
                first_received_at=row['received_at'], **q.admission_status())


async def _insert_anchor(db, packet, user_id):
    await db.execute(text(
        'INSERT INTO tagger_measurement_anchor '
        '(measurement_sha256,canonical_body,body,submitted_by) '
        'VALUES (:ref,CAST(:wire AS text),CAST(CAST(:wire AS text) AS jsonb),:uid) '
        'ON CONFLICT DO NOTHING'),
        {'ref': packet['measurement_sha256'], 'wire': packet['canonical_body'], 'uid': user_id})
    row = (await db.execute(text(
        'SELECT canonical_body,submitted_by,received_at FROM tagger_measurement_anchor '
        'WHERE measurement_sha256=:ref'), {'ref': packet['measurement_sha256']})).mappings().one()
    q.require(row['canonical_body'] == packet['canonical_body'], 'measurement_receipt_collision')
    return row


async def anchor(db, measurement_sha256):
    q._sha(measurement_sha256)
    row = (await db.execute(text(
        'SELECT canonical_body,body,submitted_by,received_at FROM tagger_measurement_anchor '
        'WHERE measurement_sha256=:ref'), {'ref': measurement_sha256})).mappings().one_or_none()
    q.require(row is not None, 'measurement_anchor_missing')
    # JSONB normalizes numeric spellings (1e16, negative zero). The immutable
    # canonical text is the byte identity; CHECK ties its parsed JSON to body.
    verified = contract.verify_anchor(measurement_sha256, json.loads(row['canonical_body']))
    q.require(verified['canonical_body'] == row['canonical_body'], 'measurement_anchor_mismatch')
    return dict(row, body=verified['body'])


async def attach(db, measurement_sha256, *, expected_measurement_sha256, kind, report, user_id):
    q._integer(user_id)
    await limits(db)
    original = await anchor(db, measurement_sha256)
    envelope = contract.assemble(measurement_sha256, original['body'], expected_measurement_sha256, kind, report)
    if kind == 'independent_review':
        refs = report['evidence_sha256s']
        existing = (await db.execute(text(
            'SELECT receipt_sha256 FROM tagger_measurement_evidence '
            'WHERE measurement_sha256=:anchor AND receipt_sha256=ANY(:refs)'),
            {'anchor': measurement_sha256, 'refs': refs})).scalars().all()
        q.require(set(existing) == set(refs), 'measurement_review_evidence_mismatch')
    await db.execute(text(
        'INSERT INTO tagger_measurement_evidence '
        '(receipt_sha256,measurement_sha256,canonical_body,body,submitted_by) '
        'VALUES (:ref,:anchor,CAST(:wire AS text),CAST(CAST(:wire AS text) AS jsonb),:uid) '
        'ON CONFLICT DO NOTHING'),
        {'ref': envelope['receipt_sha256'], 'anchor': measurement_sha256,
         'wire': envelope['canonical_body'], 'uid': user_id})
    row = (await db.execute(text(
        'SELECT measurement_sha256,canonical_body,submitted_by,received_at FROM tagger_measurement_evidence '
        'WHERE receipt_sha256=:ref'), {'ref': envelope['receipt_sha256']})).mappings().one()
    q.require(row['canonical_body'] == envelope['canonical_body'] and row['measurement_sha256'] == measurement_sha256,
              'measurement_receipt_collision')
    return dict(outcome='succeeded', state='measurement_evidence_received_unverified',
                reason='caller_assertion_bound_to_original_measurement',
                next='review_provenance_without_admission_or_lifecycle_changes',
                measurement_sha256=measurement_sha256, receipt_sha256=envelope['receipt_sha256'],
                first_submitted_by=row['submitted_by'], first_received_at=row['received_at'],
                **q.admission_status())
