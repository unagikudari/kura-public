"""Lazy canonical observation of one frozen training-support population.

Own a separate bounded read-only transaction. Never touch auth-session state,
labels, lifecycle decisions, or any trainer. Group independence is not measured.
"""
import asyncio
import hashlib
import json
import os
import re
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from api.errors import KuraError
from api.services.review_queues import load_queues, ReviewQueueRefused
from api.tagger_core import training_eligibility as eligibility

TOTAL_TIMEOUT_S = 8


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def refuse(code, status=503):
    raise KuraError(status, code, fix='現在の照合結果は不明です。保存した回答は変更されていません。')


def frozen_queue(ref, rows=None):
    if not isinstance(ref,str) or re.fullmatch('[a-f0-9]{64}',ref) is None:
        refuse('training_readiness_queue_unknown',404)
    try:
        rows=(load_queues() or []) if rows is None else rows
    except ReviewQueueRefused:
        refuse('training_readiness_queue_unavailable')
    found=[q for q in rows if q['queue_sha256']==ref]
    if len(found)!=1:
        refuse('training_readiness_queue_unknown',404)
    q=found[0]
    if (q.get('superseded_by') or q['schema']!='human-review-set/2' or
        q['purpose']!='training_support' or q['model_sha256'] is not None):
        refuse('training_readiness_queue_not_active_support',409)
    return q


def statement(sql,args):
    """Bind the shared query without interpolating values into SQL text."""
    parts=sql.split('%s')
    if len(parts)!=len(args)+1:
        raise ValueError('training_readiness_parameter_mismatch')
    return text(''.join(part+(f':p{i}' if i<len(args) else '') for i,part in enumerate(parts))), {f'p{i}':v for i,v in enumerate(args)}


async def overlay(conn, tag, snapshot, post_ids, *, queue_sha256=None):
    sql,args=eligibility.query_spec(tag,queue_sha256=queue_sha256,post_ids=post_ids)
    stmt,params=statement(sql,args)
    rows=(await conn.execute(stmt,params)).all()
    result=eligibility.seal([row[0] for row in rows],tag,snapshot)
    eligibility.validate(result)
    return result


async def observe(engine, q):
    async with engine.connect() as conn:
        conn=await conn.execution_options(isolation_level='REPEATABLE READ')
        async with conn.begin():
            await conn.execute(text('SET TRANSACTION READ ONLY'))
            await conn.execute(text("SET LOCAL statement_timeout='5s'"))
            await conn.execute(text("SET LOCAL lock_timeout='1s'"))
            at,snapshot=(await conn.execute(text('SELECT transaction_timestamp(),txid_current_snapshot()::text'))).one()
            tags=(await conn.execute(text('SELECT tag_id FROM tag_name WHERE name=:name AND ord=0'),{'name':q['tag']})).all()
            if len(tags)!=1:
                refuse('training_readiness_current_tag_unavailable')
            current=await overlay(conn,q['tag'],snapshot,q['post_ids'])
            responses=await overlay(conn,q['tag'],snapshot,q['post_ids'],queue_sha256=q['queue_sha256'])
            result=eligibility.readiness(current,responses,q['queue_sha256'],q['post_ids'],None)
            result.update(scope='queue_population_only',observed_at=at.isoformat(),tag=q['tag'],tag_id=tags[0][0],
                frame_sha256=q['frame_sha256'],source_provenance=q['source_provenance'],
                source_provenance_sha256=digest(q['source_provenance']),population_sha256=digest(q['post_ids']),
                current_overlay_sha256=current['sha256'],responses_overlay_sha256=responses['sha256'],
                transaction_snapshot=snapshot,model_sha256=None,training_dispatched=False,
                current_eligibility_counts=current['counts'],
                excluded_from_next_training=len(eligibility.excluded(current)))
            return dict(result,sha256=digest(result))


async def read(ref, engine):
    if os.environ.get('KURA_DB_ROLE','canonical').lower()!='canonical':
        refuse('training_readiness_canonical_required')
    try:
        async with asyncio.timeout(TOTAL_TIMEOUT_S):
            from api.services.review_set_registry import load_configured
            async with engine.connect() as connection:
                rows=await load_configured(connection)
            q=frozen_queue(ref, rows=rows or [])
            return await observe(engine,q)
    except KuraError:
        raise
    except TimeoutError:
        refuse('training_readiness_read_timeout')
    except SQLAlchemyError:
        refuse('training_readiness_storage_unavailable')
    except (ValueError,KeyError,TypeError):
        refuse('training_readiness_evidence_unavailable')
