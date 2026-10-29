"""Internal tagging pipeline endpoints.

Endpoints expose claim/complete/stats for batch workers that process the
`tagging_status='pending'` rows on post/pool. All routes require a valid
kura user token (szurubooru-compatible scheme); run the worker with a
dedicated service account.
"""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from api.auth import get_current_user
from api.db.models import User
from api.db.session import get_db
from api.schemas.tagging import (
    ClaimResponse,
    CompleteRequest,
    CompleteResponse,
    StatsResponse,
    TaggingKind,
)
from api.errors import KuraError
from api.services import governed_window
from api.services.tagging import (
    ClaimNotEligibleError,
    PostGone,
    claim_auto_tag,
    claim_pool_meta,
    complete_auto_tag,
    complete_pool_meta,
    enforce_claim_guard,
    get_stats,
    report_node_state,
)

router = APIRouter(prefix="/api/internal/tagging", tags=["tagging-internal"])


@router.post("/claim", response_model=ClaimResponse)
async def claim(
    kind: TaggingKind = Query(...),
    limit: int = Query(10, ge=1, le=200),
    lease_seconds: int = Query(300, ge=30, le=3600),
    host: str | None = Query(
        None, description="Worker-reported hostname, recorded into tagger_node_state"
    ),
    source: str | None = Query(
        None, description="Worker-reported model/release identifier, recorded into tagger_node_state"
    ),
    worker_id: str | None = Query(
        None, description="Stable per-process worker identity for tagger_worker_heartbeat "
                           "(claim guard, plan Step 11) — required for kind=auto_tag"
    ),
    profile: str | None = Query(
        None, description="Worker profile (e.g. full/wd14) — required for kind=auto_tag, "
                           "must match this host's tagger_expected_worker.profile"
    ),
    generation: int = Query(
        1, description="Worker code generation, recorded into the heartbeat — queue-ownership "
                        "gating on this value lands with Step 12 (tagger/worker.py), not enforced yet"
    ),
    release_id: int | None = Query(
        None, description="tagger_release.id the worker has loaded — required for kind=auto_tag, "
                           "must match the currently active release"
    ),
    provider: str | None = Query(
        None, description="ONNX execution provider actually initialized (e.g. CPUExecutionProvider, "
                           "ROCMExecutionProvider) — recorded for fleet observability, not gated"
    ),
    model_sha256: str | None = Query(None, description="Hash of the loaded model file(s), recorded for fleet observability"),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> ClaimResponse:
    started = governed_window.now()
    if host:
        # Fire-and-forget bookkeeping: a worker that can't reach this must
        # not fail the actual claim (drift visibility is best-effort, the
        # tagging queue itself is not). It commits on its own, and spends
        # from the same request deadline as the claim.
        try:
            async with asyncio.timeout_at(started + governed_window.DEADLINE_SECONDS):
                await report_node_state(db, host=host, source=source)
        except Exception:
            await db.invalidate()  # failed or cancelled mid-query: start the claim clean

    if kind == "auto_tag":
        if not (host and worker_id and profile):
            raise HTTPException(
                status_code=409,
                detail="auto_tag claim requires host, worker_id, profile "
                       "(claim guard, plan:kura-tagger-lifecycle-redesign Step 11)",
            )

        # Guard, heartbeat and claim are one transaction; across stage 3's window
        # it is rerun from here (api/services/governed_window.py).
        async def attempt():
            try:
                await enforce_claim_guard(
                    db, host=host, worker_id=worker_id, profile=profile,
                    release_id=release_id, generation=generation,
                    provider=provider or "unknown", model_sha256=model_sha256,
                )
            except ClaimNotEligibleError as e:
                raise HTTPException(status_code=409, detail=str(e)) from e
            return await claim_auto_tag(db, limit=limit, lease_seconds=lease_seconds, commit=False)

        try:
            items = await governed_window.run(
                db, attempt, governed_window.CLAIM, route="claim", started=started)
        except governed_window.WindowExpired:
            items = []  # nothing was claimed; the worker asks again
    else:
        items = await claim_pool_meta(db, limit=limit, lease_seconds=lease_seconds)
    return ClaimResponse(claimed=items, lease_seconds=lease_seconds)


@router.post("/complete", response_model=CompleteResponse)
async def complete(
    payload: CompleteRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> CompleteResponse:
    started = governed_window.now()
    skipped_pinned = 0
    if payload.kind == "auto_tag":
        function = (governed_window.COMPLETE_OK if payload.status == "ok"
                    else governed_window.COMPLETE_RETRY)
        try:
            final, applied, attempts, skipped_pinned = await governed_window.run(
                db,
                lambda: complete_auto_tag(
                    db, post_id=payload.id, status=payload.status,
                    tags=payload.tags, error=payload.error, source=payload.source,
                    reap_scope=payload.reap_scope, host=payload.host, release_id=payload.release_id,
                    veto_gates=payload.veto_gates, generation=payload.generation,
                    commit=False,
                ),
                function, route="complete", started=started,
            )
        except governed_window.WindowExpired as e:
            raise HTTPException(
                status_code=503,
                detail="tagging functions are being replaced; retry the completion",
            ) from e
        except PostGone as e:
            raise KuraError(
                404, "tagging_post_gone", context={"post_id": e.post_id},
                fix="drop this item: the post was deleted after it was claimed, "
                    "and its work row went with it; do not report it failed",
            ) from e
    else:
        final, applied, attempts = await complete_pool_meta(
            db, pool_id=payload.id, status=payload.status,
            tags=payload.tags, error=payload.error,
        )
    return CompleteResponse(
        id=payload.id,
        kind=payload.kind,
        final_status=final,
        tags_applied=applied,
        attempts=attempts,
        tags_skipped_pinned=skipped_pinned,
    )


@router.get("/stats", response_model=StatsResponse)
async def stats(
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> StatsResponse:
    return await get_stats(db)
