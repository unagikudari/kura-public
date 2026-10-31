"""Pydantic schemas for the internal tagging pipeline."""

from __future__ import annotations

from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel, Field, field_validator


TaggingKind = Literal["auto_tag", "pool_meta"]
TaggingStatus = Literal["pending", "done", "failed", "skipped"]
CompleteStatus = Literal["ok", "failed"]


class ClaimedItem(BaseModel):
    """One unit of work leased to a worker."""

    id: int
    kind: TaggingKind
    attempts: int
    lease_expires_at: datetime

    # auto_tag-specific fields (populated when kind == "auto_tag")
    # Current pipeline: PixAI primary + ml-danbooru extras + WD14 character fallback.
    content_url: Optional[str] = None
    mime_type: Optional[str] = None

    # pool_meta-specific fields (populated when kind == "pool_meta")
    name: Optional[str] = None
    post_ids: Optional[list[int]] = None


class ClaimResponse(BaseModel):
    claimed: list[ClaimedItem]
    lease_seconds: int


class TagItem(BaseModel):
    """One tag to apply, optionally with a category (genre) name.

    category は tag_category.name (general/character/copyright/artist/meta)。
    None の場合はカテゴリ未指定 → 新規タグは default(general) で作成される。
    既存タグのカテゴリは変更しない (分類専用、上書きしない)。
    """

    name: str
    category: Optional[str] = None


class CompleteRequest(BaseModel):
    id: int
    kind: TaggingKind
    status: CompleteStatus
    tags: list[TagItem] = Field(default_factory=list, description="Tags to apply on ok")
    error: Optional[str] = Field(default=None, description="Failure reason on status=failed")
    source: Optional[str] = Field(
        default=None,
        description=(
            "Worker-reported provenance identifier for this tagging run "
            "(a tagger_release id once the registry pipeline exists, or a "
            "worker-computed model identifier today). Omitted/None from "
            "older workers -- provenance stays unset on post, not an error."
        ),
    )
    host: Optional[str] = Field(
        default=None,
        description=(
            "Worker-reported hostname this run executed on (nodec/primary/"
            "trainer/...), stored on post_tag_audit.host (0072). Separate "
            "from `source`, which stays an opaque model-identifier blob for "
            "backward compat -- `host` exists specifically so 'which node "
            "tagged this' is a plain column, not string-parsing."
        ),
    )
    release_id: Optional[int] = Field(
        default=None,
        description=(
            "tagger_release.id this run's tags came from, when applicable "
            "(stored on post_tag_audit.release_id, 0072). Omit/null for "
            "patch-head-only workers or runs with no corresponding release "
            "row -- never invent one."
        ),
    )
    reap_scope: Literal["off", "own"] = Field(
        default="off",
        description=(
            "rev7 (plan kura-tagger-audit-and-remediation, fact 00702a48): "
            "replaces the emergency-fix `reap: bool` field. Full-replacement "
            "reap (rev6's mechanism -- 'this completion's tags is the FULL "
            "authoritative set, delete anything unlocked not in it') had "
            "zero valid callers and can never gain one -- production runs "
            "many independent per-head specialist workers, none of which "
            "represents the full tag ensemble for a post -- so it is "
            "removed at the type level, not just left dormant behind a "
            "flag. 'own' opts into SELF-SCOPED retraction instead: this "
            "actor (by `source`) may retract only tags IT currently claims "
            "(post_tag_actor_claim, 0073) and no longer proposes; it can "
            "never touch a tag any other actor currently claims, a locked "
            "upload-origin tag, or a human present-pin. Defaults 'off' "
            "(pure-additive, no retraction at all) pending an opt-in/shadow "
            "period with zero incidents on real traffic -- two review "
            "rounds found two real gaps in this exact mechanism, so "
            "default-on is earned by evidence, not assumed. A legacy "
            "payload sending `reap: true|false` is silently ignored "
            "(unknown field), not an error."
        ),
    )
    veto_gates: list[str] = Field(
        default_factory=list,
        description=(
            "plan:kura-tagger-self-improvement-loop (veto-authority reap, "
            "user 2026-07-22, generalized same day「最終的に一般化」): the "
            "correction-authoritative GATE tags this worker is entitled to "
            "reap — the release's patched rows ∪ its veto heads (NOT just the "
            "one veto head; e.g. phimosis/erection carry far larger human "
            "correction volume than anilingus). See tagger.infer.reap_domain. "
            "Grants the tagger DELETE authority over these tags independent "
            "of post_tag_actor_claim: for each gate present on the post but "
            "absent from this run's output, the server reaps it (unless "
            "locked or human present-pinned) so the improved tagger cancels "
            "the old tagger's over-applied tag instead of only suppressing "
            "re-adds. Empty (older/gen<2 workers) → no veto reap, pure no-op "
            "— backward compatible. Authority is fail-closed: the reap fires "
            "ONLY when source+host+release_id are all present (per-node "
            "identification + non-repudiation), recorded in a dedicated "
            "post_tag_audit row (source='auto_tag_veto') attributing the "
            "deletion to this actor/host/release."
        ),
    )
    generation: Optional[int] = Field(
        default=None,
        description=(
            "Worker code generation (WORKER_GENERATION). Recorded alongside "
            "veto-authority reaps for non-repudiation — which tagger "
            "generation exercised the veto. Omit from older workers."
        ),
    )

    @field_validator("tags", mode="before")
    @classmethod
    def _coerce_tags(cls, v):
        """旧 worker が送る flat string ["a","b"] を {name} に正規化 (後方互換)。"""
        if not v:
            return v
        return [{"name": item} if isinstance(item, str) else item for item in v]


class CompleteResponse(BaseModel):
    id: int
    kind: TaggingKind
    final_status: TaggingStatus
    tags_applied: int = 0
    attempts: int
    tags_skipped_pinned: int = Field(
        default=0,
        description=(
            "auto_tag only: tags the worker sent that were NOT applied "
            "because a human explicitly removed them from this post "
            "(INV-M human pin, migration 0057). Informational — the "
            "completion still succeeds; older workers can ignore this."
        ),
    )


class KindStats(BaseModel):
    pending: int = 0
    in_flight: int = 0
    done: int = 0
    failed: int = 0
    skipped: int = 0


class StatsResponse(BaseModel):
    post: KindStats
    pool: KindStats
