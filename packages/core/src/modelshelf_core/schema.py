from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from typing import Any

from .models import (
    MANIFEST_SCHEMA_VERSION,
    STORAGE_LAYOUT_SCHEMA_VERSION,
    TASK_SCHEMA_VERSION,
    ArtifactManifest,
    DownloadTask,
    StorageLayout,
)

Migration = Callable[[dict[str, Any]], dict[str, Any]]


class SchemaVersionError(ValueError):
    pass


class FutureSchemaVersionError(SchemaVersionError):
    pass


def _json_object(raw: str, document_name: str) -> dict[str, Any]:
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise SchemaVersionError(f"{document_name} must be a JSON object")
    return value


def _migrate_document(
    document: dict[str, Any],
    *,
    document_name: str,
    current_version: int,
    migrations: Mapping[int, Migration],
    missing_version: int | None = None,
) -> tuple[dict[str, Any], bool]:
    raw_version = document.get("schemaVersion", missing_version)
    if type(raw_version) is not int:  # bool is not a schema version
        raise SchemaVersionError(f"{document_name} schemaVersion must be an integer")
    if raw_version > current_version:
        raise FutureSchemaVersionError(
            f"{document_name} schemaVersion {raw_version} is newer than supported version "
            f"{current_version}; upgrade ModelShelf"
        )
    if raw_version < 0:
        raise SchemaVersionError(f"{document_name} schemaVersion cannot be negative")

    migrated = False
    result = dict(document)
    version = raw_version
    while version < current_version:
        migration = migrations.get(version)
        if migration is None:
            raise SchemaVersionError(
                f"{document_name} schemaVersion {version} cannot be migrated to {current_version}"
            )
        result = migration(result)
        version += 1
        result["schemaVersion"] = version
        migrated = True
    return result, migrated


def load_manifest_json(raw: str) -> ArtifactManifest:
    document, _migrated = _migrate_document(
        _json_object(raw, "artifact manifest"),
        document_name="artifact manifest",
        current_version=MANIFEST_SCHEMA_VERSION,
        migrations={},
    )
    return ArtifactManifest.model_validate(document)


def _task_v0_to_v1(document: dict[str, Any]) -> dict[str, Any]:
    # Pre-release task files had the v1 shape but no explicit version marker.
    return dict(document)


def load_task_json(raw: str) -> tuple[DownloadTask, bool]:
    document, migrated = _migrate_document(
        _json_object(raw, "download task"),
        document_name="download task",
        current_version=TASK_SCHEMA_VERSION,
        migrations={0: _task_v0_to_v1},
        missing_version=0,
    )
    return DownloadTask.model_validate(document), migrated


def load_storage_layout_json(raw: str) -> StorageLayout:
    document, _migrated = _migrate_document(
        _json_object(raw, "storage layout"),
        document_name="storage layout",
        current_version=STORAGE_LAYOUT_SCHEMA_VERSION,
        migrations={},
    )
    return StorageLayout.model_validate(document)
