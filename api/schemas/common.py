"""Shared Pydantic schemas for pagination and error responses."""

from datetime import datetime
from typing import Any, Generic, Optional, TypeVar

from pydantic import BaseModel, model_serializer

T = TypeVar("T")


class PagedResult(BaseModel, Generic[T]):
    query: str = ""
    offset: int = 0
    limit: int = 40
    total: int = 0
    totalBytes: int = 0  # sum of file_size over the whole filtered set
    results: list[T] = []


class MicroUser(BaseModel):
    name: str
    avatarUrl: Optional[str] = None


class MicroTag(BaseModel):
    id: int | None = None
    localizedNames: dict[str, str] | None = None
    names: list[str]
    category: str
    usages: int = 0
    # post_tag_pin.state ('present' = 施錠/人間が明示確定、タガー上書き不可)。
    # 単一 post 詳細でのみ populate — 一覧経路は None のまま (payload 節約)。
    pinned: str | None = None

    @model_serializer(mode='wrap')
    def serialize(self, handler):
        value = handler(self)
        if self.localizedNames is None:
            value.pop('localizedNames', None)
        return value


# Canonical machine responses omit the optional UI projection entirely.
class MicroPool(BaseModel):
    id: int
    names: list[str]
    category: str
    description: Optional[str] = None
    postCount: int = 0


class PoolSummary(BaseModel):
    id: int
    name: str
    names: list[str] = []
    version: int = 0
    postCount: int
    thumbnailUrl: Optional[str] = None
    firstPostSource: Optional[str] = None
    # Canonical source domain, derived server-side: named by an extension that knows
    # where the pool came from, else detected from the cover post's source. The
    # display badge reads this — the frontend never re-detects.
    domain: Optional[str] = None
    scope: Optional[str] = None
    # 検証 pool ライフサイクル (0058): None=通常 / 'pending'=検証待ち / 'verified'=検証済み
    verifyStatus: Optional[str] = None
    taggerSource: Optional[str] = None
    primarySourceUrl: Optional[str] = None
    # What installed extensions know about this pool, keyed by extension id
    # (extension_hooks.annotate_pools); empty in a core-only install.
    extensions: dict[str, Any] = {}
    hasCensored: bool = False     # pool has a human censor ANNOTATION (the few worked pools) → solid 💢
    hasCensoredTag: bool = False  # pool has a `censored`-tagged post (~89%, informational) → faint 💢
