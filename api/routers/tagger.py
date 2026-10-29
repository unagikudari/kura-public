"""wd14 tagger release distribution — plan:kura-tagger-registry rev2 (fact 58082f48) Step 4.

Read-only from this router's point of view: releases are created by
`tagger/publish_release.py` (a direct, transactional DB write — see that file's
docstring for why release *creation* is not an HTTP endpoint here), atomically
naming the new tagger_release_active pointer in the same transaction as the
insert. This router only ever serves whatever that pointer currently names, and
records what each node reports having verified.
"""
from __future__ import annotations

import re
import asyncio

import datetime as dt
import hashlib
import json
import os
import tempfile
from pathlib import Path

import sqlalchemy as sa
from fastapi import APIRouter, Depends, Form, HTTPException, Request, Response, UploadFile
from starlette.responses import FileResponse
from api.errors import KuraError
from pydantic import BaseModel, field_validator
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from api.auth import get_current_user
from api.db.models import TaggerRelease, TaggerReleaseActive, User
from api.db.session import get_db
from api.tagger_core.artifact_kind import CONTENT_HEAD_BUNDLE

router = APIRouter(prefix="/api/tagger", tags=["tagger"])

# Where the tagger worker (infer.py) reads veto-head safetensors from. Same
# path both processes resolve (primary, one home). Kept in sync with
# tagger/infer.py::VETO_HEADS_DIR.
_VETO_HEADS_DIR = Path(
    os.environ.get("KURA_MODELS_DIR")
    or (Path.home() / ".cache" / "kura-tagger" / "models")
) / "veto-heads"


class TaggerReleaseMeta(BaseModel):
    id: int
    commit_sha: str
    base_model_repo: str
    base_model_revision: str
    patch_set_digest: str
    model_sha256: str
    n_stock: int
    n_out: int
    patched: dict
    created_at: dt.datetime
    created_by: str | None


@router.get("/situation")
async def get_situation(db: AsyncSession = Depends(get_db)) -> dict:
    """What the tagger is doing right now — the dashboard's facts, as JSON.

    The HTML dashboard already derived all of this and none of it was reachable
    without a browser session, so an agent asking "is tagging running" reached for
    whatever it could query and got the wrong answer from something that merely
    sounded right (see api/services/tagger_situation for the four measured cases).
    This route exists so the reachable answer and the correct answer are the same
    one. It is IN the schema on purpose: a surface nothing can discover is a
    surface nobody uses.

    training_control describes the automatic reconciler only, not dedicated
    training jobs. Inspect current, observation_state, generated_at and paused
    before using its per-tag decisions: tags remain historical evidence when
    current is false. Dispatch/in_flight does not prove execution has started.

    Read-only. No key required to read these operational observations.
    """
    from api.services import tagger_situation

    active = (await db.execute(
        select(TaggerReleaseActive.release_id).where(TaggerReleaseActive.id == 1)
    )).scalar_one_or_none()
    return await tagger_situation.situation(db, int(active or 0))


@router.get("/release/active", response_model=TaggerReleaseMeta)
async def get_active_release(db: AsyncSession = Depends(get_db)):
    active = (await db.execute(
        select(TaggerReleaseActive.release_id).where(TaggerReleaseActive.id == 1)
    )).scalar_one_or_none()
    if active is None:
        raise HTTPException(status_code=404, detail="no active tagger release published yet")
    # model_data/selected_tags_csv are `deferred` on TaggerRelease -- select(TaggerRelease)
    # does not pull the BYTEA payload for a metadata-only read.
    row = (await db.execute(
        select(TaggerRelease).where(TaggerRelease.id == active)
    )).scalar_one_or_none()
    if row is None:
        # tagger_release_active pointed at a release row that no longer exists.
        # Should be unreachable (no delete path exists on tagger_release), but
        # fail loudly rather than serve a wrong release if it ever happens.
        raise HTTPException(status_code=500, detail="active release_id has no matching row")
    return TaggerReleaseMeta.model_validate(row, from_attributes=True)


@router.get("/release/{release_id}/data")
async def get_release_data(release_id: int, part: str = "model", db: AsyncSession = Depends(get_db)):
    if part not in ("model", "tags"):
        raise HTTPException(status_code=400, detail="part must be 'model' or 'tags'")
    col = TaggerRelease.model_data if part == "model" else TaggerRelease.selected_tags_csv
    data = (await db.execute(
        select(col).where(TaggerRelease.id == release_id)
    )).scalar_one_or_none()
    if data is None:
        raise HTTPException(status_code=404, detail="release not found")
    filename = "wd14-fixed.pt" if part == "model" else "selected_tags.csv"
    return Response(bytes(data), media_type="application/octet-stream",
                     headers={"Content-Disposition": f'attachment; filename="{filename}"'})


# ── head policy の配布 (適用側の唯一の入口) ──────────────────────────────
#
# **worker は DB を持たない** (HTTP でここに来る)。policy を worker のコード
# 定数に持たせていたのが 2026-08-05 まで直っていなかった原因なので、ここから
# 配る。release と同じ「メタ → sha 検証つき bytes」の 2 段構成に揃える。


class HeadPolicyOut(BaseModel):
    tag: str
    category: str = "general"
    patch_version: int
    content_sha256: str
    threshold: float
    apply_mode: str
    gate_tags: list[str]
    operating_point: str


@router.get("/head-policies", response_model=list[HeadPolicyOut])
async def list_head_policies(db: AsyncSession = Depends(get_db)):
    """配備中の head と**その当て方**を返す。

    タグ名で分岐するコードを worker から無くすための窓口。行が無ければ空 —
    「既定で全部当てる」にはしない (policy の無い head は適用しない)。
    """
    rows = (await db.execute(text(
        "SELECT p.tag, p.patch_version, p.content_sha256, p.threshold, "
        "       p.apply_mode, p.gate_tags, p.operating_point, c.name AS category, "
        "       p.artifact_kind AS kind "
        "FROM tagger_current_head_policy p "
        "JOIN tag vocab ON vocab.id = p.tag_id "
        "JOIN tag_category c ON c.id = vocab.category_id "
        "ORDER BY p.tag"
    ))).mappings().all()
    # **tag ごとに、効力のある 1 版だけを配る** (`tagger_current_head_policy`、0186:
    # 最新の approved 版)。全版を配ると、worker は head を tag で持つので後から
    # 読んだ版が前の版を上書きし、どの版の閾値で当たるかが行の並び次第になる
    # (2026-10-25 実測: horse_penis は approved 7 版で閾値 0.5〜0.99)。
    # **head でないバイトを配らない。** 配備できる content head の定義は
    # `tagger_deployable_head` (0186 から同じ view を読む) と同じ「効力のある版の
    # kind='content_head_bundle'」。0139 の trigger は新しい行を
    # 止めるが、それ以前の 2 行 (anilingus v3 = probe_safetensors、
    # dark_labia_majora v7 = measurement_json) は残っていて、全 worker が毎回
    # torch.load に失敗していた (2026-10-25 実測、nodec 40 回)。NULL は未分類で通さない。
    # 最新版が bundle でなければ、その tag は配らない (古い版に戻さない)。
    not_heads = [f'{r["tag"]} v{r["patch_version"]}={r.get("kind") or "NULL"}'
                 for r in rows if r.get("kind") != CONTENT_HEAD_BUNDLE]
    for r in rows:
        if r.get("kind") != CONTENT_HEAD_BUNDLE and r.get("category") == "character":
            raise KuraError(
                503, "character_head_unavailable",
                context={"tag": r["tag"], "content_sha256": r["content_sha256"],
                         "kind": r.get("kind")},
                fix="Point the approved character policy at a content_head_bundle artifact before resuming tagging.",
                ref="docs/notes/character-completion.md",
            )
    rows = [r for r in rows if r.get("kind") == CONTENT_HEAD_BUNDLE]
    # **配れない policy を配らない。** `tagger_artifact` の行は「この bytes を
    # 知っている」の意味でしかなく、**このノードに実体があるかは別**。
    # 実測 2026-08-05: backfill が行だけ作って bytes を置かず、policy 5 件に
    # 対して artifact は全部 404 だった。適用側を policy 由来に切り替えた直後に
    # これを配ると、worker は「5 件あるのに 1 つも load できない」状態になる。
    #
    # 実体が無いものは**返さず、サーバのログに出す** — 静かに 404 を踏ませると
    # 各ノードで同じ調査が繰り返される。
    from api.tagger_core.artifact_store import default_store
    store = default_store()
    # Release this read transaction before potentially hashing large models.
    # No mutations or subsequent DB reads belong to this metadata endpoint.
    await db.rollback()
    from api.services.policy_artifacts import policy_artifact_availability
    available = await policy_artifact_availability(store, [r["content_sha256"] for r in rows])
    out, absent = [], []
    for r in rows:
        if not available[r["content_sha256"]]:
            if r.get("category") == "character":
                raise KuraError(
                    503, "character_head_unavailable",
                    context={"tag": r["tag"], "content_sha256": r["content_sha256"]},
                    fix="Restore the approved character-head artifact before resuming tagging.",
                    ref="docs/notes/character-completion.md",
                )
            absent.append(f'{r["tag"]}({r["content_sha256"][:12]})')
            continue
        out.append(HeadPolicyOut(
            tag=r["tag"], category=r.get("category", "general"), patch_version=r["patch_version"],
            content_sha256=r["content_sha256"],
            threshold=float(r["threshold"]), apply_mode=r["apply_mode"],
            gate_tags=list(r["gate_tags"] or []),
            operating_point=r["operating_point"]))
    if not_heads:
        print(f"[tagger] head-policies: head bundle でない artifact を指す policy {len(not_heads)} 件を "
              f"除外 — {', '.join(not_heads)} (配るなら content_head_bundle を登録し直す)",
              flush=True)
    if absent:
        print(f"[tagger] head-policies: bytes がこのノードに無い {len(absent)} 件を "
              f"除外 — {', '.join(absent)} "
              f"(scripts/backfill_head_policy.py か runner の upload が要る)",
              flush=True)
    return out


# 書き込みは認証必須 (registration-request / veto-head と同じ扱い)。
# **content-addressed だから安全、ではない** — hash 検証は「入った bytes が
# 名乗りどおり」を保証するだけで、誰でも書けるならディスクは埋められる。
MAX_ARTIFACT_BYTES = 512 * 1024 * 1024


_artifact_upload_slots = asyncio.Semaphore(1)


def _store_uploaded_artifact(path: Path, content_sha256: str) -> str:
    """Blocking integrity, atomic persistence and structural classification."""
    from api.tagger_core.artifact_store import ArtifactVerifyError, default_store, ingest_file, sha256_file
    from api.tagger_core.artifact_kind import classify
    actual = sha256_file(path)
    if actual != content_sha256:
        raise HTTPException(status_code=400,
                            detail=f"bytes hash to {actual}, not {content_sha256}")
    store = default_store()
    stored = ingest_file(store, path, move=True)
    if stored != content_sha256:
        raise ArtifactVerifyError("artifact changed during staging")
    return classify(store.path_for(content_sha256))


@router.put("/artifact/{content_sha256}")
async def put_artifact(content_sha256: str, request: Request,
                       db: AsyncSession = Depends(get_db),
                       user: User = Depends(get_current_user)):
    """head の bytes を受け取る。**中身の hash が identity** なので冪等。

    送られてきた bytes を実際に hash し直して URL の sha と一致しなければ
    受け取らない — 名前だけ正しい別物が入ると、以後どのノードもそれを
    「検証済み」として load する。
    """
    from api.services.owned_work import await_owned
    if not re.fullmatch(r"[0-9a-f]{64}", content_sha256):
        raise HTTPException(status_code=400, detail="content_sha256 must be hex64")
    # Authentication has completed; no DB connection is needed during receipt.
    await db.rollback()
    async with _artifact_upload_slots:
        # Stage bounded chunks rather than retaining a whole model in RAM.
        with tempfile.TemporaryDirectory(prefix="kura-artifact-") as work:
            path = Path(work) / "upload"
            stream = path.open("wb")
            size = 0
            try:
                async for chunk in request.stream():
                    size += len(chunk)
                    if size > MAX_ARTIFACT_BYTES:
                        raise HTTPException(status_code=413,
                                            detail=f"artifact exceeds {MAX_ARTIFACT_BYTES} bytes")
                    await await_owned(asyncio.create_task(asyncio.to_thread(stream.write, chunk)))
            finally:
                await await_owned(asyncio.create_task(asyncio.to_thread(stream.close)))
            kind = await await_owned(asyncio.create_task(asyncio.to_thread(
                _store_uploaded_artifact, path, content_sha256)))

        # DO NOTHING ではなく UPDATE。2026-08-06 実測: backfill が byte_size=0 の行を
        # 先に作っていたため、347 MB を上げ直しても行は 0 のままだった。**行が
        # 実体と食い違ったまま「登録済み」に見える**のが一番たちが悪い。
        await db.execute(text(
            "INSERT INTO tagger_artifact (content_sha256, byte_size, kind, kind_checked_at) "
            "VALUES (:s, :n, :k, now()) "
            "ON CONFLICT (content_sha256) DO UPDATE SET "
            "  byte_size = EXCLUDED.byte_size, kind = EXCLUDED.kind, "
            "  kind_checked_at = EXCLUDED.kind_checked_at"),
            {"s": content_sha256, "n": size, "k": kind})
        await db.commit()
        return {"content_sha256": content_sha256, "byte_size": size, "kind": kind}


@router.get("/artifact/{content_sha256}")
async def get_artifact(content_sha256: str):
    """head の bytes を返す。呼ぶ側は受け取った bytes を必ず hash し直すこと
    (`release_client.pull` と同じ規律 — 名前は約束にならない)。"""
    from api.tagger_core.artifact_store import default_store
    if not re.fullmatch(r"[0-9a-f]{64}", content_sha256):
        raise HTTPException(status_code=400, detail="content_sha256 must be hex64")
    path = default_store().path_for(content_sha256)
    if not path.is_file():
        raise HTTPException(status_code=404, detail="artifact not present on this node")
    return FileResponse(path, media_type="application/octet-stream",
                        filename=f"{content_sha256}.safetensors")


class TaggerNodeStateIn(BaseModel):
    host: str
    release_id: int | None = None
    verified: bool
    detail: str | None = None


@router.post("/node-state")
async def upsert_node_state(body: TaggerNodeStateIn, db: AsyncSession = Depends(get_db)):
    """Idempotent upsert keyed by host -- each node reports its own release_id +
    verification result after a pull (tagger/release_client.py, Step 5). A plain
    SELECT over this table is the fleet drift check ("which node is on what")."""
    await db.execute(sa.text("""
        INSERT INTO tagger_node_state (host, release_id, verified, detail, reported_at)
        VALUES (:host, :release_id, :verified, :detail, now())
        ON CONFLICT (host) DO UPDATE SET
            release_id = EXCLUDED.release_id,
            verified = EXCLUDED.verified,
            detail = EXCLUDED.detail,
            reported_at = now()
    """), body.model_dump())
    await db.commit()
    return {"ok": True}


@router.get("/tag-rules")
async def list_tag_rules(db: AsyncSession = Depends(get_db)):
    """タグ別の採用規則。**worker/infer がコードに閾値も gate も置かないための窓口。**

    `tagger_tag_application` (0138) が唯一の読み口で、head policy と既製 producer の
    規則を同じ形で返す。呼ぶ側は producer 名で絞る。

    行が無ければ空 — 「既定で全部当てる」にはしない。producer の既定閾値もまた
    行 (`tagger_producer_threshold`) で、コード側には置かない。
    """
    rules = (await db.execute(sa.text(
        "SELECT producer, tag, threshold, remove_below, may_add, may_remove, "
        "       all_tags, any_tags, none_tags "
        "FROM tagger_tag_application ORDER BY producer, tag"
    ))).mappings().all()
    defaults = (await db.execute(sa.text(
        "SELECT producer, category, threshold FROM tagger_producer_threshold "
        "ORDER BY producer, category"
    ))).mappings().all()
    # 排他は tag_id で持っているので、読む側が使う名前に開いて返す。
    exclusions = (await db.execute(sa.text(
        "SELECT a.name AS tag_a, b.name AS tag_b, "
        "       c.all_tags, c.any_tags, c.none_tags "
        "  FROM tagger_tag_exclusion x "
        "  JOIN tagger_tag_condition c ON c.name = x.condition "
        "  JOIN tag_name a ON a.tag_id = x.tag_a_id AND a.ord = 0 "
        "  JOIN tag_name b ON b.tag_id = x.tag_b_id AND b.ord = 0 "
        " ORDER BY a.name, b.name"
    ))).mappings().all()
    return {"rules": [dict(r) for r in rules],
            "producer_defaults": [dict(d) for d in defaults],
            "exclusions": [dict(e) for e in exclusions]}


class NodeHeadIn(BaseModel):
    tag: str
    content_sha256: str
    loaded: bool
    detail: str


class NodeHeadsIn(BaseModel):
    host: str
    heads: list[NodeHeadIn]


@router.post("/node-heads")
async def upsert_node_heads(body: NodeHeadsIn, db: AsyncSession = Depends(get_db)):
    """このノードが **どの head を実際に積めたか** を記録する。

    2026-08-06 まで、これは worker が完了ごとに `veto_gates` 配列で送っていた。
    ノードが head を積めたかはノード固有の事実なのに置き場所がスキーマに
    無かったので、リクエストに載せて運んでいた。結果:

      - サーバはノードの申告を信じるしかない (積んでいない head を申告されても
        認可済みなら消える)
      - 「どのノードが greyscale を積んだか」を調べるのに 4 台の journal を
        grep することになる — 実際にそうなった

    `tagger_node_state` が release 粒度で持っているものの head 粒度版。
    削除の権威は `tagger_node_reap_authority` view がここから導出する。

    **報告に無い head はそのノードから消す。** 積まなくなったことも事実で、
    残しておくと「昔積めた」が権威として残り続ける。
    """
    rows = [{"host": body.host, "tag": h.tag, "sha": h.content_sha256,
             "loaded": h.loaded, "detail": h.detail or "(理由なし)"}
            for h in body.heads]
    if rows:
        await db.execute(sa.text("""
            INSERT INTO tagger_node_head (host, tag_id, tag, content_sha256, loaded, detail, reported_at)
            SELECT :host, public.tagger_tag_id_of(:tag), :tag, :sha, :loaded, :detail, now()
             WHERE public.tagger_tag_id_of(:tag) IS NOT NULL
            ON CONFLICT (host, tag_id) DO UPDATE SET
                content_sha256 = EXCLUDED.content_sha256,
                loaded = EXCLUDED.loaded,
                detail = EXCLUDED.detail,
                reported_at = now()
        """), rows)
    await db.execute(sa.text("""
        DELETE FROM tagger_node_head
         WHERE host = :host
           AND tag <> ALL(CAST(:tags AS text[]))
    """), {"host": body.host, "tags": [h.tag for h in body.heads]})
    await db.commit()
    return {"ok": True, "recorded": len(rows)}


class RegistrationRequestIn(BaseModel):
    tag: str
    source_host: str
    artifact_ref: str
    artifact_sha256: str
    metrics: dict | None = None
    gt: dict | None = None
    # **測定の識別子** (INV-R11 の冪等鍵の片割れ)。HOLD のときは artifact が
    # 作られず artifact_sha256 が固定値になるため、これが無いと同じタグの
    # HOLD がすべて 1 行に潰れ、GT が育っても記録が初回のまま残る (0132)。
    lifecycle_event_id: int | None = None

    @field_validator("gt")
    @classmethod
    def _gt_carries_the_baseline(cls, v: dict | None) -> dict | None:
        """`gt` is free-form diagnostics EXCEPT for two keys the reconciler
        depends on: 'positive' and 'negative' are the baseline a later round's
        GT growth is measured against (tagger/reconciler.py, hold/release).

        That dependency used to be convention only, and convention lost: round-8's
        anal_object_insertion registration (id=33) wrote 'pos'/'neg', the hold
        baseline read as 0/0, "GT grew" came out true against a byte-identical
        label set, and the tag was re-dispatched for a round that reproduced the
        previous one exactly (trainer, task ca47c3f6). 3 of 32 stored rows have
        the same defect. The reconciler now fails closed on an unreadable
        baseline, but that only contains the damage -- this is the door.

        Validates in place rather than reshaping into a typed model: submitters
        carry substantial extra diagnostics in `gt` (corpus counts, fingerprints,
        derivation notes) and a model with declared fields would silently drop
        them on re-serialisation.
        """
        if v is None:
            return v
        missing = [k for k in ("positive", "negative") if k not in v]
        if missing:
            raise ValueError(
                f"gt must carry the reconciler's growth baseline -- missing {missing}. "
                f"'positive'/'negative' are the tagger_gt_label counts at submit time; "
                f"the next round's hold/release decision is measured against them. "
                f"Extra diagnostic keys are kept as-is, but these two are required "
                f"(got: {sorted(v)[:12]})."
            )
        for k in ("positive", "negative"):
            if isinstance(v[k], bool) or not isinstance(v[k], int):
                raise ValueError(
                    f"gt['{k}'] must be an int label count, got {type(v[k]).__name__} ({v[k]!r})"
                )
        return v


class RegistrationRequestOut(BaseModel):
    id: int


@router.post("/registration-request", response_model=RegistrationRequestOut)
async def submit_registration_request(
    body: RegistrationRequestIn,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> RegistrationRequestOut:
    """The ONLY way a training node (trainer) may contribute a candidate
    tag/head to kura -- plan:kura-tagger-lifecycle-redesign Step 13.
    Canonical-writer invariant: trainer is a read-only DB replica and must
    never write to kura's DB directly; it calls this endpoint over the
    network (primary:8443) instead, same as every other kura write.

    This does NOT create a tagger_patch row. Registration and promotion
    are deliberately separate: tagger_patch's actual source of truth is
    still the git-tracked tagger/patches/<tag>/vN.json + sync_patch_
    registry.py mechanism (unchanged by this endpoint) -- a human/agent
    reviews a validated registration and creates the real patch file
    referencing this request's artifact, exactly as today. See
    tagger_governed_submit_registration's migration (0086) docstring for
    the full reasoning.
    """
    row = (await db.execute(
        sa.text(
            "SELECT tagger_governed_submit_registration(:tag, :host, :ref, :sha, "
            "CAST(:metrics AS jsonb), CAST(:gt AS jsonb), :event_id) AS id"
        ),
        {
            "tag": body.tag, "host": body.source_host, "ref": body.artifact_ref,
            "sha": body.artifact_sha256,
            "metrics": None if body.metrics is None else json.dumps(body.metrics),
            "gt": None if body.gt is None else json.dumps(body.gt),
            "event_id": body.lifecycle_event_id,
        },
    )).one()
    await db.commit()
    return RegistrationRequestOut(id=row.id)


class RegistrationStatusIn(BaseModel):
    status: str  # "validated" | "rejected"
    rejected_reason: str | None = None
    # 0154 (#55): 判定 5 分類。省略時は governed fn が validated→deployable を補完
    review_class: str | None = None


@router.post("/registration-request/{request_id}/status")
async def set_registration_status(
    request_id: int,
    body: RegistrationStatusIn,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict:
    """primary-side review action: mark a pending registration validated or
    rejected. Deliberately manual -- no automatic content-hash/provenance/
    dedup/compat validation happens here (see 0086's docstring for why).
    Marking 'validated' does not itself promote to tagger_patch; that is
    still a separate, human-driven step via the existing git-file mechanism.
    """
    if body.status not in ("validated", "rejected"):
        raise HTTPException(status_code=400, detail="status must be validated|rejected")
    try:
        await db.execute(
            sa.text("SELECT tagger_governed_set_registration_status(:id, :status, :reason, :rc)"),
            {"id": request_id, "status": body.status, "reason": body.rejected_reason,
             "rc": body.review_class},
        )
        await db.commit()
    except Exception as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    return {"ok": True}


class CandidateDispositionIn(BaseModel):
    tag: str
    disposition: str  # proceed | defer | dismiss | block | supersede
    reason_class: str
    reason: str
    decided_by: str
    actor_type: str  # human | policy | automation
    valid_until: dt.datetime | None = None
    release_condition: dict | None = None  # closed DSL v1: {metric, op, value}
    supersedes_seq: int | None = None


@router.post("/candidate-disposition")
async def record_candidate_disposition(
    body: CandidateDispositionIn,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict:
    """issue #55 P1: 裁定を disposition ledger に記録する。

    実行ノード (replica、a33518a 以降 mutating は 403) からの**唯一の**書き込み
    経路 — registration-request と同格。検証 (5 値 / actor_type / closed DSL /
    P7 supersede 保護) は governed fn 側が持つ。裁定を broker task note の散文に
    書かない (trainer-cc R1 の実害の止血)。
    """
    try:
        row = (await db.execute(
            sa.text(
                "SELECT tagger_governed_record_candidate_disposition("
                ":tag, :disposition, :reason_class, :reason, :decided_by, "
                ":actor_type, :valid_until, CAST(:release_condition AS jsonb), "
                ":supersedes_seq) AS seq"
            ),
            {
                "tag": body.tag, "disposition": body.disposition,
                "reason_class": body.reason_class, "reason": body.reason,
                "decided_by": body.decided_by, "actor_type": body.actor_type,
                "valid_until": body.valid_until,
                "release_condition": None if body.release_condition is None
                else json.dumps(body.release_condition),
                "supersedes_seq": body.supersedes_seq,
            },
        )).one()
        await db.commit()
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    return {"seq": row.seq}


@router.get("/candidate-disposition/{tag}")
async def get_candidate_disposition(
    tag: str, db: AsyncSession = Depends(get_db)
) -> dict:
    """現在の裁定 (fold = tag ごと最新行)。無ければ disposition: null。"""
    row = (await db.execute(
        sa.text(
            "SELECT seq, disposition, reason_class, reason, decided_by, actor_type, "
            "decided_at, valid_until, release_condition, cond_version, supersedes_seq "
            "FROM tagger_candidate_disposition_current WHERE tag = :tag"
        ), {"tag": tag},
    )).one_or_none()
    if row is None:
        return {"tag": tag, "disposition": None}
    out = dict(row._mapping)
    out["tag"] = tag
    return out


@router.post("/veto-head")
async def upload_veto_head(
    file: UploadFile,
    gate: str = Form(...),
    sha256: str = Form(...),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict:
    """Delivery half of a gated-veto head (plan:kura-tagger-self-improvement-loop,
    step-0 the audit found missing). trainer is a read-only DB replica, so it
    POSTs the self-contained safetensors here (over primary:8443) instead of writing
    the DB or scp-ing a file (INV-D). We: verify sha256, persist the bytes to
    VETO_HEADS_DIR/<gate>.safetensors where the tagger worker's infer.py reads
    them, and record a provenance tagger_patch row (head_type=content_head,
    model_data=the bytes). The safetensors' __metadata__ carries gate/threshold/
    apply — self-contained, so authority (DB row) and effect (file) share one
    content-addressed object (INV-D)."""
    data = await file.read()
    actual = hashlib.sha256(data).hexdigest()
    if actual != sha256:
        raise HTTPException(status_code=400,
                            detail=f"sha256 mismatch: got {actual}, expected {sha256}")
    if not gate or "/" in gate or ".." in gate:
        raise HTTPException(status_code=400, detail="invalid gate tag")
    _VETO_HEADS_DIR.mkdir(parents=True, exist_ok=True)
    dest = _VETO_HEADS_DIR / f"{gate}.safetensors"
    tmp = dest.with_suffix(".safetensors.partial")
    tmp.write_bytes(data)
    os.replace(tmp, dest)  # atomic
    # provenance row (governed). content_head = linear head on features; the
    # veto application semantics live in the safetensors __metadata__.
    try:
        await db.execute(sa.text(
            "SELECT tagger_governed_upsert_patch(:tag, :ver, 'approved', :sha, :why, NULL, "
            "NULL, NULL, 'content_head', :repo, NULL, NULL, :cat, :by, :md)"
        ), {
            "tag": gate, "ver": 1, "sha": sha256,
            "why": (f"gated-veto head (plan:kura-tagger-self-improvement-loop). "
                    f"McNemar-validated in-gate vs v1. bytes={len(data)}. "
                    f"apply=sigmoid(((f-mu)/sd)@w+b)<threshold → veto {gate}."),
            "repo": "SmilingWolf/wd-swinv2-tagger-v3",
            "cat": dt.date.today(), "by": f"veto-head-upload:{user.name}", "md": data,
        })
        await db.commit()
    except Exception as e:  # noqa: BLE001 — file already persisted; provenance is best-effort
        await db.rollback()
        return {"ok": True, "stored": str(dest), "bytes": len(data),
                "provenance_warning": str(e)}
    return {"ok": True, "stored": str(dest), "bytes": len(data)}
