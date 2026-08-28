from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

MANIFEST_SCHEMA_VERSION = 1
TASK_SCHEMA_VERSION = 1
STORAGE_LAYOUT_SCHEMA_VERSION = 1


def to_camel(value: str) -> str:
    head, *tail = value.split("_")
    return head + "".join(word.title() for word in tail)


class Model(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, extra="forbid")


class Provider(StrEnum):
    HUGGINGFACE = "huggingface"
    MODELSCOPE_CN = "modelscope-cn"
    MODELSCOPE_AI = "modelscope-ai"
    GITHUB_RELEASE = "github-release"
    KAGGLE = "kaggle"
    HTTP = "http"
    FILESYSTEM = "filesystem"


class TaskStatus(StrEnum):
    QUEUED = "queued"
    RESOLVING = "resolving"
    DOWNLOADING = "downloading"
    VERIFYING = "verifying"
    AWAITING_CONFIRMATION = "awaiting_confirmation"
    PUBLISHING = "publishing"
    PAUSED = "paused"
    CANCELLED = "cancelled"
    COMPLETED = "completed"
    FAILED = "failed"


class FileEntry(Model):
    path: str
    size: int = Field(ge=0)
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")

    @field_validator("path")
    @classmethod
    def relative_safe_path(cls, value: str) -> str:
        candidate = Path(value)
        if candidate.is_absolute() or ".." in candidate.parts or value in {"", "."}:
            raise ValueError("file path must be a safe relative path")
        return value


class SourceReference(Model):
    provider: Provider
    id: str = Field(min_length=1)
    requested_revision: str = Field(min_length=1)
    resolved_revision: str = Field(min_length=1)
    url: str | None = None


class ArtifactManifest(Model):
    schema_version: Literal[1]
    artifact_id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    version: str = Field(min_length=1)
    format: str | None = None
    source: SourceReference
    content_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    created_at: datetime
    total_size: int = Field(ge=0)
    file_count: int = Field(ge=0)
    files: list[FileEntry]


class StorageLayout(Model):
    schema_version: Literal[1]
    kind: Literal["modelshelf-storage"] = "modelshelf-storage"


class ArtifactSummary(Model):
    artifact_id: str
    name: str
    version: str
    provider: Provider
    source_id: str
    requested_revision: str
    resolved_revision: str
    total_size: int
    file_count: int
    created_at: datetime
    relative_path: str


class InferredMetadata(Model):
    name: str
    version: str
    format: str | None = None
    archive: bool
    confidence: str = Field(pattern="^(low|medium|high)$")
    notes: list[str] = Field(default_factory=list)


class DownloadTask(Model):
    schema_version: Literal[1]
    id: str
    provider: Provider
    source_id: str
    requested_revision: str
    disable_mirror: bool = False
    disable_proxy: bool = False
    resolved_revision: str | None = None
    status: TaskStatus
    progress: int = Field(ge=0, le=100)
    bytes_downloaded: int = Field(default=0, ge=0)
    total_bytes: int | None = Field(default=None, ge=0)
    instantaneous_bytes_per_second: float = Field(default=0, ge=0)
    average_bytes_per_second: float = Field(default=0, ge=0)
    eta_seconds: int | None = Field(default=None, ge=0)
    download_elapsed_seconds: float = Field(default=0, ge=0)
    created_at: datetime
    updated_at: datetime
    error: str | None = None
    artifact_id: str | None = None
    inferred_metadata: InferredMetadata | None = None


class DesiredModel(Model):
    provider: Provider
    id: str = Field(min_length=1)
    requested_revision: str = "main"
    resolved_revision: str | None = None
    path: str | None = None
