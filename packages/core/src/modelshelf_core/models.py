from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

MANIFEST_SCHEMA_VERSION: Literal[2] = 2
TASK_SCHEMA_VERSION: Literal[5] = 5
STORAGE_LAYOUT_SCHEMA_VERSION: Literal[1] = 1


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
    SCHEDULED = "scheduled"
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
    selected_paths: list[str] | None = None

    @field_validator("selected_paths")
    @classmethod
    def valid_selected_paths(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return None
        normalized = sorted(set(value))
        if not normalized:
            raise ValueError("selected paths cannot be empty")
        for path in normalized:
            FileEntry.relative_safe_path(path)
        return normalized


class ArtifactManifest(Model):
    schema_version: Literal[2]
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

    @model_validator(mode="after")
    def selected_paths_match_files(self) -> ArtifactManifest:
        if self.source.selected_paths is not None:
            actual = sorted(file.path for file in self.files)
            if self.source.selected_paths != actual:
                raise ValueError("source selected paths must exactly match manifest files")
        return self


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
    selection_digest: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    selected_paths: list[str] | None = None

    @field_validator("selected_paths")
    @classmethod
    def valid_summary_selected_paths(cls, value: list[str] | None) -> list[str] | None:
        return SourceReference.valid_selected_paths(value)


class InferredMetadata(Model):
    name: str
    version: str
    format: str | None = None
    archive: bool
    confidence: str = Field(pattern="^(low|medium|high)$")
    notes: list[str] = Field(default_factory=list)


class DownloadTask(Model):
    schema_version: Literal[5]
    id: str
    provider: Provider
    source_id: str
    requested_revision: str
    disable_mirror: bool = False
    mirror_url: str | None = None
    disable_proxy: bool = False
    scheduled_at: datetime | None = None
    queue_position: int | None = Field(default=None, ge=0)
    resume_from_stage: bool = False
    selected_paths: list[str] | None = None
    resolved_revision: str | None = None
    status: TaskStatus
    progress: int = Field(ge=0, le=100)
    bytes_downloaded: int = Field(default=0, ge=0)
    total_bytes: int | None = Field(default=None, ge=0)
    instantaneous_bytes_per_second: float = Field(default=0, ge=0)
    average_bytes_per_second: float = Field(default=0, ge=0)
    eta_seconds: int | None = Field(default=None, ge=0)
    download_elapsed_seconds: float = Field(default=0, ge=0)
    verification_bytes_completed: int = Field(default=0, ge=0)
    verification_total_bytes: int | None = Field(default=None, ge=0)
    verification_instantaneous_bytes_per_second: float = Field(default=0, ge=0)
    verification_average_bytes_per_second: float = Field(default=0, ge=0)
    verification_eta_seconds: int | None = Field(default=None, ge=0)
    verification_elapsed_seconds: float = Field(default=0, ge=0)
    verification_detail: str | None = None
    created_at: datetime
    updated_at: datetime
    error: str | None = None
    artifact_id: str | None = None
    inferred_metadata: InferredMetadata | None = None

    @field_validator("mirror_url")
    @classmethod
    def valid_mirror_url(cls, value: str | None) -> str | None:
        if value is None:
            return None
        parsed = urlparse(value)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("mirror URL must be an HTTP(S) URL")
        if parsed.username is not None or parsed.password is not None:
            raise ValueError("mirror URL must not contain credentials")
        return value.rstrip("/")

    @field_validator("scheduled_at")
    @classmethod
    def timezone_aware_schedule(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("scheduled start must include a timezone")
        return value.astimezone(UTC)

    @field_validator("selected_paths")
    @classmethod
    def valid_selected_paths(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return None
        normalized = sorted(set(value))
        if not normalized:
            raise ValueError("selected paths cannot be empty")
        for path in normalized:
            FileEntry.relative_safe_path(path)
        return normalized

    @model_validator(mode="after")
    def valid_mirror_route(self) -> DownloadTask:
        supported = {Provider.HUGGINGFACE, Provider.MODELSCOPE_CN, Provider.MODELSCOPE_AI}
        if self.mirror_url and self.provider not in supported:
            raise ValueError("temporary mirrors are not supported for this source")
        if self.mirror_url and self.disable_mirror:
            raise ValueError("temporary mirror and mirror bypass cannot be enabled together")
        if self.status is TaskStatus.SCHEDULED and self.scheduled_at is None:
            raise ValueError("scheduled task must include a start time")
        selectable = {Provider.HUGGINGFACE, Provider.MODELSCOPE_CN, Provider.MODELSCOPE_AI}
        if self.selected_paths and self.provider not in selectable:
            raise ValueError("file selection is not supported for this source")
        return self


class DesiredModel(Model):
    provider: Provider
    id: str = Field(min_length=1)
    requested_revision: str = "main"
    resolved_revision: str | None = None
    path: str | None = None
