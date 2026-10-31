"""Post-related Pydantic schemas — mirrors szurubooru's JSON response shape."""

from datetime import datetime
from typing import Any, Optional

from pydantic import BaseModel

from api.schemas.common import MicroPool, MicroTag, MicroUser


class PostResponse(BaseModel):
    id: int
    version: int
    creationTime: Optional[datetime] = None
    lastEditTime: Optional[datetime] = None
    safety: str
    source: Optional[str] = None
    taggingSource: Optional[str] = None
    taggingCompletedAt: Optional[datetime] = None
    taggingTagsWritten: Optional[list[str]] = None
    originalFilename: Optional[str] = None
    type: str
    mimeType: Optional[str] = None
    checksum: Optional[str] = None
    checksumMD5: Optional[str] = None
    fileSize: Optional[int] = None
    canvasWidth: Optional[int] = None
    canvasHeight: Optional[int] = None
    contentUrl: str = ""
    thumbnailUrl: str = ""
    flags: list[str] = []
    tags: list[MicroTag] = []
    relations: list[Any] = []
    user: Optional[MicroUser] = None
    score: int = 0
    ownScore: int = 0
    ownFavorite: bool = False
    tagCount: int = 0
    favoriteCount: int = 0
    commentCount: int = 0
    noteCount: int = 0
    relationCount: int = 0
    featureCount: int = 0
    lastFeatureTime: Optional[datetime] = None
    favoritedBy: list[MicroUser] = []
    hasCustomThumbnail: bool = False
    notes: list[Any] = []
    comments: list[Any] = []
    pools: list[MicroPool] = []
