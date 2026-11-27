"""Canonical measurement intake backed by 0201, with no lifecycle authority."""
import json
from typing import Annotated, Literal
from fastapi import APIRouter, Depends, Path, Query, Response, Request
from fastapi.routing import APIRoute
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession
from api.auth import get_current_user
from api.db.models import User
from api.db.session import get_db
from api.errors import KuraError
from api.services import measurement_evidence as service
from api.services import measurement_collection, measurement_continuation
from starlette.concurrency import run_in_threadpool
from api.tagger_core import qualification_evidence as q
from .tagger_qualification import canonical_only


class MeasurementRoute(APIRoute):
    def get_route_handler(self):
        handler = super().get_route_handler()
        async def bounded_errors(request):
            try:
                return await handler(request)
            except (RequestValidationError, KuraError, HTTPException) as exc:
                if isinstance(exc, RequestValidationError):
                    exc = KuraError(422, 'measurement_request_invalid',
                        fix='Use the closed measurement or evidence request; retain original receipts.')
                if self.endpoint.__name__ in ('discover', 'continuation') and request.method == 'GET':
                    # Authentication/canonical/query refusals are private too, not empty collections.
                    detail = exc.body() if isinstance(exc, KuraError) else exc.detail
                    status = exc.status if isinstance(exc, KuraError) else exc.status_code
                    return JSONResponse(status_code=status, content={'detail': detail},
                                        headers={**(getattr(exc, 'headers', None) or {}), 'Cache-Control': 'private, no-store'})
                raise exc
        return bounded_errors


router = APIRouter(prefix='/api/tagger/measurements', dependencies=[Depends(canonical_only)],
                   route_class=MeasurementRoute)
HashPath = Annotated[str, Path(pattern=r'^[0-9a-f]{64}$')]


class MeasurementIn(BaseModel):
    model_config = ConfigDict(extra='forbid')
    tag_id: int = Field(strict=True, ge=1, le=2**63-1)
    tag: str = Field(min_length=1, max_length=200)
    measurement: dict


class EvidenceIn(BaseModel):
    model_config = ConfigDict(extra='forbid')
    expected_measurement_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    kind: Literal['human_sample', 'runtime', 'independent_review']
    report: dict


def refused(exc):
    reason = str(exc)
    status = 404 if reason in ('measurement_anchor_missing', 'measurement_tag_missing') else (
        409 if reason in ('measurement_anchor_mismatch', 'measurement_tag_identity_mismatch', 'measurement_receipt_collision',
                         'measurement_review_evidence_mismatch') else 422)
    return KuraError(status, reason, fix='Check the original measurement and evidence; no lifecycle state was changed.')


def database_refusal(exc):
    if getattr(exc.orig, 'sqlstate', None) in {'55P03', '57014', '40P01'}:
        return KuraError(503, 'measurement_database_busy', fix='Retry the identical receipt after contention clears.')
    detail = getattr(getattr(exc.orig, '__cause__', None), 'message', None)
    if getattr(exc.orig, 'sqlstate', None) == 'P0001' and detail in {
        'measurement_anchor_mismatch', 'measurement_tag_identity_mismatch', 'measurement_receipt_collision'
    }:
        return KuraError(409, detail, fix='Read the frozen identity again; preserve original receipts.')
    return None


@router.post('')
async def submit(body: MeasurementIn, response: Response, db: AsyncSession = Depends(get_db),
                 user: User = Depends(get_current_user)):
    response.headers['Cache-Control'] = 'private, no-store'
    try:
        result = await service.submit(db, tag_id=body.tag_id, tag=body.tag,
                                      measurement=body.measurement, user_id=user.user_id)
        await db.commit()
        return result
    except q.QualificationEvidenceRefused as exc:
        await db.rollback(); raise refused(exc) from exc
    except DBAPIError as exc:
        await db.rollback()
        mapped = database_refusal(exc)
        if mapped is not None:
            raise mapped from exc
        raise


@router.get('')
async def discover(request: Request, response: Response,
                   tag_id: Annotated[str | None, Query(pattern=r'^[1-9][0-9]{0,18}$')] = None,
                   limit: Annotated[str, Query(pattern=r'^[1-9][0-9]?$')] = '20',
                   cursor: Annotated[str | None, Query(max_length=512)] = None,
                   db: AsyncSession = Depends(get_db), user: User = Depends(get_current_user)):
    response.headers['Cache-Control'] = 'private, no-store'
    if (any(key not in {'tag_id', 'limit', 'cursor'} or len(request.query_params.getlist(key)) != 1
            for key in request.query_params) or int(limit) > 50 or
            (tag_id is not None and int(tag_id) > 2**63-1) or cursor == ''):
        raise KuraError(422, 'measurement_request_invalid', fix='Use one valid tag_id, limit from 1 to 50, and the returned opaque cursor.')
    try:
        return await measurement_collection.collection(db, tag_id=int(tag_id) if tag_id else None,
                                                        limit=int(limit), cursor=cursor)
    except q.QualificationEvidenceRefused as exc:
        raise KuraError(404 if str(exc) == 'measurement_cursor_missing' else 422, str(exc),
                        fix='Restart the collection from its first page with the intended tag filter.') from exc
    except DBAPIError as exc:
        if getattr(exc.orig, 'sqlstate', None) == '57014':
            raise KuraError(503, 'measurement_collection_read_budget_exceeded',
                            fix='Use a narrower tag filter or ask the operator to inspect the bounded query; do not retry automatically.') from exc
        if getattr(exc.orig, 'sqlstate', None) in {'55P03', '40P01'}:
            raise KuraError(503, 'measurement_database_busy',
                            fix='Retry this read after database contention clears.') from exc
        if getattr(exc.orig, 'sqlstate', None) in {'42P01', '42703'}:
            raise KuraError(503, 'measurement_collection_unavailable',
                            fix='Restore the canonical measurement store; do not interpret this as an empty collection.') from exc
        raise


@router.get('/{measurement_sha256}')
async def read(measurement_sha256: HashPath, response: Response,
               db: AsyncSession = Depends(get_db), user: User = Depends(get_current_user)):
    response.headers['Cache-Control'] = 'private, no-store'
    try:
        original = await service.anchor(db, measurement_sha256)
        return dict(outcome='succeeded', state='original_measurement_read_unverified',
                    reason='historical_internal_measurement_only',
                    next='attach_assertions_without_inventing_a_training_event',
                    measurement_sha256=measurement_sha256, measurement=original, **q.admission_status())
    except q.QualificationEvidenceRefused as exc:
        raise refused(exc) from exc


@router.post('/{measurement_sha256}/qualification-evidence')
async def attach(measurement_sha256: HashPath, body: EvidenceIn, response: Response,
                 db: AsyncSession = Depends(get_db), user: User = Depends(get_current_user)):
    response.headers['Cache-Control'] = 'private, no-store'
    try:
        result = await service.attach(db, measurement_sha256,
            expected_measurement_sha256=body.expected_measurement_sha256, kind=body.kind,
            report=body.report, user_id=user.user_id)
        await db.commit()
        return result
    except q.QualificationEvidenceRefused as exc:
        await db.rollback(); raise refused(exc) from exc
    except DBAPIError as exc:
        await db.rollback()
        mapped = database_refusal(exc)
        if mapped is not None:
            raise mapped from exc
        raise


@router.get('/{measurement_sha256}/qualification-evidence')
async def listing(measurement_sha256: HashPath, response: Response,
                  db: AsyncSession = Depends(get_db), user: User = Depends(get_current_user),
                  before: Annotated[str | None, Query(pattern=r'^[0-9a-f]{64}$')] = None,
                  kind: Literal['human_sample', 'runtime', 'independent_review'] | None = None):
    response.headers['Cache-Control'] = 'private, no-store'
    try:
        await service.anchor(db, measurement_sha256)
    except q.QualificationEvidenceRefused as exc:
        raise refused(exc) from exc
    params = {'anchor': measurement_sha256}
    boundary = ''
    if before is not None:
        at = (await db.execute(text('SELECT received_at FROM tagger_measurement_evidence '
            'WHERE measurement_sha256=:anchor AND receipt_sha256=:ref'),
            dict(params, ref=before))).scalar_one_or_none()
        if at is None:
            raise KuraError(404, 'measurement_cursor_missing', fix='Start from the latest evidence page.')
        params.update(at=at, ref=before)
        boundary = 'AND (received_at,receipt_sha256)<(:at,:ref) '
    if kind is not None:
        params['kind'] = kind; boundary += "AND body->>'kind'=:kind "
    rows = (await db.execute(text('SELECT receipt_sha256,body->>\'kind\' AS kind,submitted_by,received_at, '
        'octet_length(canonical_body) AS body_bytes FROM tagger_measurement_evidence '
        'WHERE measurement_sha256=:anchor ' + boundary +
        'ORDER BY received_at DESC,receipt_sha256 DESC LIMIT 101'), params)).mappings().all()
    return dict(outcome='succeeded', state='measurement_evidence_list_unverified', reason='intake_is_not_admission',
                next='review_each_report_and_its_provenance', measurement_sha256=measurement_sha256,
                receipts=[dict(row) for row in rows[:100]], more_available=len(rows)>100,
                next_cursor=rows[99]['receipt_sha256'] if len(rows)>100 else None, **q.admission_status())


@router.get('/{measurement_sha256}/qualification-evidence/{receipt_sha256}')
async def exact(measurement_sha256: HashPath, receipt_sha256: HashPath, response: Response,
                db: AsyncSession = Depends(get_db), user: User = Depends(get_current_user)):
    response.headers['Cache-Control'] = 'private, no-store'
    row = (await db.execute(text('SELECT receipt_sha256,measurement_sha256,canonical_body,body,submitted_by,received_at '
        'FROM tagger_measurement_evidence WHERE measurement_sha256=:anchor AND receipt_sha256=:ref'),
        {'anchor': measurement_sha256, 'ref': receipt_sha256})).mappings().one_or_none()
    if row is None:
        raise KuraError(404, 'measurement_evidence_missing', fix='List evidence for this original measurement.')
    return dict(outcome='succeeded', state='measurement_evidence_read_unverified', reason='intake_is_not_admission',
                next='review_the_report_and_its_provenance', receipt=dict(row, body=json.loads(row['canonical_body'])), **q.admission_status())


@router.get('/{measurement_sha256}/continuation')
async def continuation(measurement_sha256: HashPath, request: Request, response: Response,
                       db: AsyncSession = Depends(get_db), user: User = Depends(get_current_user)):
    response.headers['Cache-Control'] = 'private, no-store'
    if request.query_params:
        raise KuraError(422, 'measurement_request_invalid', fix='Use the measurement reference without filesystem paths or query options.')
    try:
        original = await service.anchor(db, measurement_sha256)
    except q.QualificationEvidenceRefused as exc:
        raise refused(exc) from exc
    return await run_in_threadpool(measurement_continuation.projection, measurement_sha256, original['body'])
