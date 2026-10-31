"""Validated commands shared by human HTTP and agent MCP adapters."""
from uuid import UUID
from pydantic import BaseModel, Field


class CollectionInput(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    request_key: UUID


class ImportEntryInput(BaseModel):
    request_key: UUID
    original_name: str = Field(min_length=1,max_length=255)
    relative_path: str | None = Field(None,max_length=4096)
    byte_size: int = Field(ge=0,le=2**53-1)
    sha256: str | None = Field(None,pattern=r"^[0-9a-f]{64}$")


class ImportPlanInput(BaseModel):
    request_key: UUID
    collection: int | None = Field(None,gt=0)
    items: list[ImportEntryInput] = Field(min_length=1,max_length=10000)


class LabelTarget(BaseModel):
    asset_id: int = Field(gt=0)
    revision: int = Field(ge=0)


class LabelCommand(BaseModel):
    request_key: UUID
    targets: list[LabelTarget] = Field(min_length=1, max_length=100)
    add: list[str] = Field(default_factory=list, max_length=16)
    remove: list[str] = Field(default_factory=list, max_length=16)
