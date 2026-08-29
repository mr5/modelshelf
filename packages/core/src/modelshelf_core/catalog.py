from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import sqlite3
import tempfile
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .catalog_index import CatalogIndex
from .identity import (
    artifact_identity,
    artifact_identity_from_relative_path,
    artifact_relative_path,
)
from .models import (
    ArtifactManifest,
    ArtifactSummary,
    FileEntry,
    Provider,
    SourceReference,
    StorageLayout,
)
from .schema import FutureSchemaVersionError, load_manifest_json, load_storage_layout_json

logger = logging.getLogger(__name__)


class VerificationError(RuntimeError):
    pass


def atomic_write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(value, stream, indent=2, ensure_ascii=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while block := stream.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def inventory(root: Path) -> list[FileEntry]:
    result: list[FileEntry] = []
    for current, directories, files in os.walk(root, followlinks=False):
        for name in directories:
            absolute = Path(current) / name
            if absolute.is_symlink():
                raise VerificationError(f"symbolic links are not allowed: {absolute}")
        directories[:] = sorted(item for item in directories if item != ".modelshelf")
        for name in sorted(files):
            absolute = Path(current) / name
            if absolute.is_symlink():
                raise VerificationError(f"symbolic links are not allowed: {absolute}")
            relative = absolute.relative_to(root).as_posix()
            result.append(
                FileEntry(path=relative, size=absolute.stat().st_size, sha256=sha256_file(absolute))
            )
    return result


def content_digest(files: list[FileEntry]) -> str:
    digest = hashlib.sha256()
    for item in sorted(files, key=lambda entry: entry.path):
        digest.update(f"{item.path}\0{item.size}\0{item.sha256}\n".encode())
    return digest.hexdigest()


def _freeze_tree(root: Path) -> None:
    for current, directories, files in os.walk(root, topdown=False):
        for name in files:
            os.chmod(Path(current) / name, 0o444)
        for name in directories:
            os.chmod(Path(current) / name, 0o555)


def _unfreeze_tree(root: Path) -> None:
    if not root.exists():
        return
    for current, directories, files in os.walk(root):
        os.chmod(current, 0o755)
        for name in files:
            os.chmod(Path(current) / name, 0o644)
        for name in directories:
            os.chmod(Path(current) / name, 0o755)


def _artifact_manifest_paths(root: Path) -> Iterator[Path]:
    for current, directories, _files in os.walk(root):
        artifact_root = Path(current)
        manifest_path = artifact_root / ".modelshelf" / "manifest.json"
        if manifest_path.is_file():
            yield manifest_path
            directories.clear()
            continue
        directories[:] = sorted(name for name in directories if name != ".modelshelf")


class Catalog:
    def __init__(self, storage_root: Path) -> None:
        self.storage_root = storage_root.resolve()
        self.artifacts_root = self.storage_root / "artifacts"
        self.staging_root = self.storage_root / ".staging"
        self.incoming_root = self.storage_root / ".incoming"
        self.jobs_root = self.storage_root / ".modelshelf" / "jobs"
        self.layout_path = self.storage_root / ".modelshelf" / "storage.json"
        self.index_path = self.storage_root / ".modelshelf" / "catalog.sqlite3"
        self.index = CatalogIndex(self.index_path)

    def initialize(self) -> None:
        for directory in (
            self.artifacts_root,
            self.staging_root,
            self.incoming_root,
            self.jobs_root,
            self.index_path.parent,
        ):
            directory.mkdir(parents=True, exist_ok=True)
        if self.layout_path.exists():
            load_storage_layout_json(self.layout_path.read_text(encoding="utf-8"))
        else:
            layout = StorageLayout(schema_version=1)
            atomic_write_json(
                self.layout_path,
                layout.model_dump(mode="json", by_alias=True),
            )
        self.index.initialize()
        self.reconcile_index()

    def staging_path(self, task_id: str) -> Path:
        return self.staging_root / task_id

    def artifact_path(self, provider: Provider, source_id: str, resolved_revision: str) -> Path:
        return self.artifacts_root / artifact_relative_path(provider, source_id, resolved_revision)

    def create_manifest(
        self,
        root: Path,
        *,
        name: str,
        version: str,
        source: SourceReference,
        format: str | None = None,
        files: list[FileEntry] | None = None,
    ) -> ArtifactManifest:
        files = inventory(root) if files is None else files
        digest = content_digest(files)
        manifest = ArtifactManifest(
            schema_version=1,
            artifact_id=artifact_identity(source.provider, source.id, source.resolved_revision),
            name=name,
            version=version,
            format=format,
            source=source,
            content_sha256=digest,
            created_at=datetime.now(UTC),
            total_size=sum(item.size for item in files),
            file_count=len(files),
            files=files,
        )
        atomic_write_json(
            root / ".modelshelf" / "manifest.json",
            manifest.model_dump(mode="json", by_alias=True, exclude_none=True),
        )
        return manifest

    def publish(self, staging: Path, manifest: ArtifactManifest) -> tuple[Path, bool]:
        destination = self.artifact_path(
            manifest.source.provider, manifest.source.id, manifest.source.resolved_revision
        )
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists():
            existing = self.read_manifest(destination)
            if existing.content_sha256 != manifest.content_sha256:
                raise VerificationError(f"immutable artifact collision at {destination}")
            shutil.rmtree(staging)
            self._index_artifact(destination, existing)
            return destination, True
        _freeze_tree(staging)
        try:
            os.rename(staging, destination)
        except OSError:
            _unfreeze_tree(staging)
            if destination.exists():
                existing = self.read_manifest(destination)
                if existing.content_sha256 != manifest.content_sha256:
                    raise VerificationError(
                        f"immutable artifact collision at {destination}"
                    ) from None
                shutil.rmtree(staging)
                self._index_artifact(destination, existing)
                return destination, True
            raise
        os.chmod(destination, 0o555)
        self._index_artifact(destination, manifest)
        return destination, False

    @staticmethod
    def read_manifest(artifact_root: Path) -> ArtifactManifest:
        raw = (artifact_root / ".modelshelf" / "manifest.json").read_text(encoding="utf-8")
        return load_manifest_json(raw)

    def _summary(self, artifact_root: Path, manifest: ArtifactManifest) -> ArtifactSummary:
        relative_path = artifact_relative_path(
            manifest.source.provider, manifest.source.id, manifest.source.resolved_revision
        )
        if artifact_root != self.artifacts_root / relative_path:
            raise VerificationError("manifest identity does not match its artifact path")
        return ArtifactSummary(
            artifact_id=manifest.artifact_id,
            name=manifest.name,
            version=manifest.version,
            provider=manifest.source.provider,
            source_id=manifest.source.id,
            requested_revision=manifest.source.requested_revision,
            resolved_revision=manifest.source.resolved_revision,
            total_size=manifest.total_size,
            file_count=manifest.file_count,
            created_at=manifest.created_at,
            relative_path=relative_path,
        )

    def _index_artifact(self, artifact_root: Path, manifest: ArtifactManifest) -> None:
        manifest_path = artifact_root / ".modelshelf" / "manifest.json"
        try:
            stat = manifest_path.stat()
            self.index.upsert(
                self._summary(artifact_root, manifest),
                manifest_mtime_ns=stat.st_mtime_ns,
                manifest_size=stat.st_size,
            )
        except (OSError, sqlite3.DatabaseError, VerificationError):
            logger.warning("artifact published but catalog index update failed", exc_info=True)

    def reconcile_index(self) -> None:
        try:
            existing = self.index.manifest_metadata()
        except sqlite3.DatabaseError:
            self.index.preserve_and_recreate()
            existing = {}
        changed: list[tuple[ArtifactSummary, int, int]] = []
        valid_artifact_ids: list[str] = []
        for manifest_path in _artifact_manifest_paths(self.artifacts_root):
            artifact_root = manifest_path.parent.parent
            relative_path = artifact_root.relative_to(self.artifacts_root).as_posix()
            candidate_id = artifact_identity_from_relative_path(relative_path)
            if candidate_id is None:
                continue
            try:
                stat = manifest_path.stat()
                if existing.get(candidate_id) == (stat.st_mtime_ns, stat.st_size):
                    valid_artifact_ids.append(candidate_id)
                    continue
                manifest = self.read_manifest(artifact_root)
                summary = self._summary(artifact_root, manifest)
                changed.append((summary, stat.st_mtime_ns, stat.st_size))
                valid_artifact_ids.append(summary.artifact_id)
            except FutureSchemaVersionError:
                raise
            except (OSError, ValueError, VerificationError):
                continue
        self.index.reconcile(changed, valid_artifact_ids)

    def list(
        self,
        *,
        query: str | None = None,
        provider: Provider | None = None,
        sort_by: str = "created",
        sort_order: str = "desc",
        limit: int | None = None,
        offset: int = 0,
    ) -> list[ArtifactSummary]:
        try:
            return self.index.list(
                query=query,
                provider=provider,
                sort_by=sort_by,
                sort_order=sort_order,
                limit=limit,
                offset=offset,
            )
        except sqlite3.DatabaseError:
            self.index.preserve_and_recreate()
            self.reconcile_index()
            return self.index.list(
                query=query,
                provider=provider,
                sort_by=sort_by,
                sort_order=sort_order,
                limit=limit,
                offset=offset,
            )

    def find(self, artifact_id: str) -> tuple[ArtifactSummary, ArtifactManifest] | None:
        try:
            summary = self.index.find(artifact_id)
        except sqlite3.DatabaseError:
            self.index.preserve_and_recreate()
            self.reconcile_index()
            summary = self.index.find(artifact_id)
        if summary is None:
            return None
        artifact_root = self.artifacts_root / summary.relative_path
        try:
            return summary, self.read_manifest(artifact_root)
        except FutureSchemaVersionError:
            raise
        except (OSError, ValueError):
            return None


def verify_artifact(root: Path, *, full: bool, unexpected: bool = False) -> list[str]:
    manifest = Catalog.read_manifest(root)
    failures: list[str] = []
    expected = {item.path for item in manifest.files}
    for item in manifest.files:
        candidate = root / item.path
        if not candidate.is_file():
            failures.append(f"missing: {item.path}")
            continue
        actual_size = candidate.stat().st_size
        if actual_size != item.size:
            failures.append(f"size: {item.path} expected={item.size} actual={actual_size}")
        elif full:
            actual_digest = sha256_file(candidate)
            if actual_digest != item.sha256:
                failures.append(f"sha256: {item.path}")
    if unexpected:
        actual = {item.path for item in inventory(root)}
        for path in sorted(actual - expected):
            failures.append(f"unexpected: {path}")
    if manifest.file_count != len(manifest.files):
        failures.append("manifest fileCount does not match files length")
    if manifest.total_size != sum(item.size for item in manifest.files):
        failures.append("manifest totalSize does not match file sizes")
    if content_digest(manifest.files) != manifest.content_sha256:
        failures.append("manifest contentSha256 does not match file entries")
    expected_id = artifact_identity(
        manifest.source.provider,
        manifest.source.id,
        manifest.source.resolved_revision,
    )
    if manifest.artifact_id != expected_id:
        failures.append("manifest artifactId does not match source identity")
    return failures
