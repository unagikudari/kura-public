"""Canonical evidence intake with no registration-state or publication authority."""
import os
from typing import Annotated, Literal
from fastapi import APIRouter, Depends, Path, Query, Response
from fastapi.routing import APIRoute
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession
from api.auth import get_current_user
from api.db.models import User
from api.db.session import get_db
from api.errors import KuraError
from api.services import qualification_evidence as service
from api.tagger_core import qualification_evidence as contract


def canonical_only():
    if os.environ.get('KURA_DB_ROLE', 'canonical').strip().lower() != 'canonical':
        raise KuraError(503, 'qualification_canonical_required',
                        fix='Submit qualification evidence to the canonical Kura API.')


class EvidenceRoute(APIRoute):
    def get_route_handler(self):
        handler = super().get_route_handler()
        async def bounded_errors(request):
            try:
                return await handler(request)
            except RequestValidationError as exc:
                # Default validation echoes arbitrary input, including NaN that
                # JSONResponse cannot serialize. Never echo a report on refusal.
                raise KuraError(422, 'qualification_request_invalid',
                    fix='Use the typed evidence request and a positive int64 registration ID.') from exc
        return bounded_errors


router = APIRouter(prefix='/api/tagger/registration-request', dependencies=[Depends(canonical_only)],
                   route_class=EvidenceRoute)
RegistrationId = Annotated[int, Path(ge=1, le=2**63-1)]


class EvidenceIn(BaseModel):
    model_config = ConfigDict(extra='forbid')
    expected_context_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    kind: Literal['human_sample', 'runtime', 'independent_review']
    report: dict


def refused(exc):
    reason = str(exc)  # Contract emits a closed reason, never input or DB message.
    status = 404 if reason == 'qualification_registration_missing' else 409 if reason in (
        'registration_context_mismatch', 'qualification_review_evidence_mismatch',
        'qualification_receipt_collision') else 422
    return KuraError(status, reason, fix='Read the candidate context and correct the report; original evidence remains unchanged.')


@router.get('/{registration_id}/qualification-context')
async def get_context(registration_id: RegistrationId, response: Response,
                      db: AsyncSession = Depends(get_db), user: User = Depends(get_current_user),
                      include_evaluation: bool = False):
    response.headers['Cache-Control'] = 'private, no-store'
    try:
        context = await service.registration_context(db, registration_id)
    except contract.QualificationEvidenceRefused as exc:
        raise refused(exc) from exc
    result = dict(outcome='succeeded', state='measurement_context_read', reason='original_measurement_only',
                next='attach_typed_evidence_without_reopening_the_registration', context=context,
                context_sha256=contract.digest(context), **contract.admission_status())
    if include_evaluation:
        from api.tagger_core.config import load_config
        from api.tagger_core.canonical_evaluation import publication_evaluation_summary
        result["production_evaluation"] = await publication_evaluation_summary(
            db, dict(context, id=registration_id), await load_config(db)
        )
    return result


@router.post('/{registration_id}/qualification-evidence')
async def attach_evidence(registration_id: RegistrationId, body: EvidenceIn, response: Response,
                          db: AsyncSession = Depends(get_db), user: User = Depends(get_current_user)):
    response.headers['Cache-Control'] = 'private, no-store'
    try:
        result = await service.attach(db, registration_id,
            expected_context_sha256=body.expected_context_sha256, kind=body.kind,
            report=body.report, user_id=user.user_id)
        await db.commit()
        return result
    except contract.QualificationEvidenceRefused as exc:
        await db.rollback()
        raise refused(exc) from exc
    except DBAPIError as exc:
        await db.rollback()
        code = getattr(exc.orig, 'sqlstate', None)
        if code in {'55P03', '57014'}:
            raise KuraError(503, 'qualification_database_busy', fix='Retry the same receipt after contention clears.') from exc
        detail = getattr(getattr(exc.orig, '__cause__', None), 'message', None)
        if code == 'P0001' and detail == 'qualification_context_mismatch':
            raise KuraError(409, 'qualification_context_mismatch', fix='Read the candidate context again; original evidence is unchanged.') from exc
        raise


@router.get('/{registration_id}/qualification-evidence')
async def evidence(registration_id: RegistrationId, response: Response,
                   db: AsyncSession = Depends(get_db), user: User = Depends(get_current_user),
                   before: Annotated[str | None, Query(pattern=r'^[0-9a-f]{64}$')] = None,
                   kind: Literal['human_sample', 'runtime', 'independent_review'] | None = None):
    response.headers['Cache-Control'] = 'private, no-store'
    try:
        context = await service.registration_context(db, registration_id)
    except contract.QualificationEvidenceRefused as exc:
        raise refused(exc) from exc
    params = {'id': registration_id}
    boundary = ''
    if before is not None:
        at = (await db.execute(text(
            'SELECT received_at FROM tagger_qualification_evidence '
            'WHERE registration_id=:id AND receipt_sha256=:ref'),
            {'id': registration_id, 'ref': before})).scalar_one_or_none()
        if at is None:
            raise KuraError(404, 'qualification_cursor_missing', fix='Start from the latest evidence page.')
        params.update(at=at, ref=before)
        boundary = 'AND (received_at,receipt_sha256)<(:at,:ref) '
    if kind is not None:
        params['kind'] = kind
        boundary += "AND body->>'kind'=:kind "
    rows = (await db.execute(text(
        "SELECT receipt_sha256,context_sha256,body->>'kind' AS kind,submitted_by,received_at, "
        'octet_length(canonical_body) AS body_bytes '
        'FROM tagger_qualification_evidence WHERE registration_id=:id ' + boundary +
        'ORDER BY received_at DESC,receipt_sha256 DESC LIMIT 101'), params)).mappings().all()
    return dict(outcome='succeeded', state='unverified_evidence_list', reason='intake_is_not_admission',
                next='review_each_report_and_its_provenance', context_sha256=contract.digest(context),
                receipts=[dict(row) for row in rows[:100]], more_available=len(rows)>100,
                next_cursor=rows[99]['receipt_sha256'] if len(rows)>100 else None,
                **contract.admission_status())


@router.get('/{registration_id}/qualification-evidence/{receipt_sha256}')
async def evidence_by_ref(registration_id: RegistrationId, receipt_sha256: str, response: Response,
                          db: AsyncSession = Depends(get_db), user: User = Depends(get_current_user)):
    response.headers['Cache-Control'] = 'private, no-store'
    if contract.SHA.fullmatch(receipt_sha256) is None:
        raise KuraError(422, 'sha256_invalid', fix='Use the receipt hash returned by evidence intake.')
    row = (await db.execute(text(
        'SELECT receipt_sha256,context_sha256,canonical_body,context_canonical,body,submitted_by,received_at '
        'FROM tagger_qualification_evidence WHERE registration_id=:id AND receipt_sha256=:ref'),
        {'id': registration_id, 'ref': receipt_sha256})).mappings().one_or_none()
    if row is None:
        raise KuraError(404, 'qualification_receipt_missing', fix='List evidence for this registration.')
    return dict(outcome='succeeded', state='unverified_evidence_read', reason='intake_is_not_admission',
                next='review_the_report_and_its_provenance', receipt=dict(row), **contract.admission_status())
