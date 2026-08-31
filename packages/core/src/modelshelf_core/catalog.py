from __future__ import annotations

import fcntl
import hashlib
import json
import logging
import os
import shutil
import sqlite3
import stat as stat_module
import tempfile
import threading
from collections.abc import Callable, Iterator, Mapping
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from .catalog_index import CatalogIndex
from .identity import (
    artifact_identity,
    artifact_identity_from_relative_path,
    artifact_relative_path,
    selection_digest,
)
from .models import (
    ArtifactAliases,
    ArtifactManifest,
    ArtifactStorageStats,
    ArtifactSummary,
    FileEntry,
    Provider,
    SourceReference,
    StorageLayout,
    validate_artifact_alias,
)
from .schema import (
    FutureSchemaVersionError,
    load_artifact_aliases_json,
    load_manifest_json,
    migrate_storage_layout_json,
)

logger = logging.getLogger(__name__)

_FICLONE = 0x40049409


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


def sha256_file(
    path: Path,
    *,
    progress: Callable[[int], None] | None = None,
    cancelled: threading.Event | None = None,
) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while block := stream.read(1024 * 1024):
            if cancelled is not None and cancelled.is_set():
                raise VerificationError("verification cancelled")
            digest.update(block)
            if progress is not None:
                progress(len(block))
    return digest.hexdigest()


def inventory(
    root: Path,
    *,
    workers: int = 1,
    progress: Callable[[int, int], None] | None = None,
    expected_sha256: Mapping[str, str] | None = None,
    trusted_sha256: Mapping[str, str] | None = None,
    cancelled: threading.Event | None = None,
) -> list[FileEntry]:
    if workers < 1:
        raise ValueError("inventory workers must be at least 1")
    discovered: list[tuple[str, Path, int]] = []
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
            discovered.append((relative, absolute, absolute.stat().st_size))
    discovered.sort(key=lambda item: item[0])

    expected = dict(expected_sha256 or {})
    trusted = dict(trusted_sha256 or {})
    for path, digest in trusted.items():
        expected_digest = expected.get(path)
        if expected_digest is None:
            raise VerificationError(f"trusted file is missing source metadata: {path}")
        if digest != expected_digest:
            raise VerificationError(
                f"trusted SHA-256 does not match source metadata: {path}; "
                f"expected {expected_digest}, got {digest}"
            )
    discovered_paths = {relative for relative, _path, _size in discovered}
    missing = sorted((set(expected) | set(trusted)) - discovered_paths)
    if missing:
        raise VerificationError("expected source files are missing: " + ", ".join(missing[:3]))

    total = sum(size for relative, _path, size in discovered if relative not in trusted)
    completed = 0
    progress_lock = threading.Lock()
    stop = cancelled or threading.Event()

    def report(byte_count: int) -> None:
        nonlocal completed
        with progress_lock:
            completed += byte_count
            current = completed
        if progress is not None:
            progress(current, total)

    def hash_entry(item: tuple[str, Path, int]) -> FileEntry:
        relative, absolute, size = item
        trusted_digest = trusted.get(relative)
        if trusted_digest is not None:
            return FileEntry(path=relative, size=size, sha256=trusted_digest)
        digest = sha256_file(absolute, progress=report, cancelled=stop)
        expected_digest = expected.get(relative)
        if expected_digest is not None and digest != expected_digest:
            raise VerificationError(
                f"source SHA-256 mismatch: {relative}; expected {expected_digest}, got {digest}"
            )
        return FileEntry(path=relative, size=size, sha256=digest)

    if progress is not None:
        progress(0, total)
    try:
        if workers == 1 or len(discovered) < 2:
            result = [hash_entry(item) for item in discovered]
        else:
            executor = ThreadPoolExecutor(
                max_workers=min(workers, len(discovered)),
                thread_name_prefix="modelshelf-verify",
            )
            try:
                result = list(executor.map(hash_entry, discovered))
            except BaseException:
                stop.set()
                executor.shutdown(wait=True, cancel_futures=True)
                raise
            else:
                executor.shutdown(wait=True)
    except BaseException:
        stop.set()
        raise
    if progress is not None:
        progress(total, total)
    return result


def content_digest(files: list[FileEntry]) -> str:
    digest = hashlib.sha256()
    for item in sorted(files, key=lambda entry: entry.path):
        digest.update(f"{item.path}\0{item.size}\0{item.sha256}\n".encode())
    return digest.hexdigest()


def clone_artifact_file(
    source: Path,
    destination: Path,
    *,
    expected_sha256: str | None = None,
) -> str:
    """Reuse an immutable artifact file, verifying the ordinary-copy fallback inline."""
    destination.parent.mkdir(parents=True, exist_ok=True)

    # Prefer hardlinks inside the artifact filesystem. They have the most
    # straightforward POSIX accounting and deletion semantics for immutable
    # files. Reflinks remain a fallback for filesystems that reject links but
    # support block cloning.
    try:
        os.link(source, destination)
    except OSError:
        destination.unlink(missing_ok=True)
    else:
        return "hardlink"

    try:
        source_descriptor = os.open(source, os.O_RDONLY)
        try:
            destination_descriptor = os.open(
                destination,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                source.stat().st_mode & 0o777,
            )
            try:
                fcntl.ioctl(destination_descriptor, _FICLONE, source_descriptor)
            finally:
                os.close(destination_descriptor)
        finally:
            os.close(source_descriptor)
    except OSError:
        destination.unlink(missing_ok=True)
    else:
        return "reflink"

    try:
        digest = hashlib.sha256()
        with source.open("rb") as source_stream, destination.open("xb") as destination_stream:
            while block := source_stream.read(1024 * 1024):
                destination_stream.write(block)
                digest.update(block)
        shutil.copystat(source, destination, follow_symlinks=False)
        if expected_sha256 is not None and digest.hexdigest() != expected_sha256:
            raise VerificationError(
                f"copied artifact file SHA-256 mismatch: {source}; "
                f"expected {expected_sha256}, got {digest.hexdigest()}"
            )
    except (OSError, VerificationError):
        destination.unlink(missing_ok=True)
        raise
    return "copy"


def _freeze_tree(root: Path) -> None:
    for current, directories, files in os.walk(root, topdown=False):
        for name in files:
            os.chmod(Path(current) / name, 0o444)
        for name in directories:
            os.chmod(Path(current) / name, 0o555)


def _unfreeze_tree(root: Path) -> None:
    if not root.exists():
        return
    for current, directories, _files in os.walk(root):
        os.chmod(current, 0o755)
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


def _ensure_artifact_parent_permissions(artifacts_root: Path, artifact_root: Path) -> None:
    artifacts_root = artifacts_root.resolve()
    artifact_root = artifact_root.resolve()
    if artifact_root == artifacts_root or artifacts_root not in artifact_root.parents:
        raise VerificationError("artifact path must stay below the artifact root")
    os.chmod(artifacts_root, 0o755)
    current = artifact_root.parent
    while current != artifacts_root:
        os.chmod(current, 0o755)
        current = current.parent


class Catalog:
    def __init__(self, storage_root: Path) -> None:
        self.storage_root = storage_root.resolve()
        self.artifacts_root = self.storage_root / "artifacts"
        self.staging_root = self.storage_root / ".staging"
        self.incoming_root = self.storage_root / ".incoming"
        self.jobs_root = self.storage_root / ".modelshelf" / "jobs"
        self.layout_path = self.storage_root / ".modelshelf" / "storage.json"
        self.aliases_path = self.storage_root / ".modelshelf" / "artifact-aliases.json"
        self.index_path = self.storage_root / ".modelshelf" / "catalog.sqlite3"
        self.index = CatalogIndex(self.index_path)
        self._aliases_lock = threading.RLock()

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
            layout, migrated = migrate_storage_layout_json(
                self.layout_path.read_text(encoding="utf-8")
            )
            if migrated:
                for manifest_path in _artifact_manifest_paths(self.artifacts_root):
                    _ensure_artifact_parent_permissions(
                        self.artifacts_root, manifest_path.parent.parent
                    )
                atomic_write_json(
                    self.layout_path,
                    layout.model_dump(mode="json", by_alias=True),
                )
        else:
            layout = StorageLayout(schema_version=2)
            atomic_write_json(
                self.layout_path,
                layout.model_dump(mode="json", by_alias=True),
            )
        if self.aliases_path.exists():
            load_artifact_aliases_json(self.aliases_path.read_text(encoding="utf-8"))
        else:
            aliases = ArtifactAliases(schema_version=1)
            atomic_write_json(
                self.aliases_path,
                aliases.model_dump(mode="json", by_alias=True),
            )
        os.chmod(self.artifacts_root, 0o755)
        self.index.initialize()
        self.reconcile_index()

    def staging_path(self, task_id: str) -> Path:
        return self.staging_root / task_id

    def artifact_path(
        self,
        provider: Provider,
        source_id: str,
        resolved_revision: str,
        selected_paths: list[str] | None = None,
    ) -> Path:
        return self.artifacts_root / artifact_relative_path(
            provider, source_id, resolved_revision, selected_paths
        )

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
            schema_version=2,
            artifact_id=artifact_identity(
                source.provider,
                source.id,
                source.resolved_revision,
                source.selected_paths,
            ),
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
            manifest.source.provider,
            manifest.source.id,
            manifest.source.resolved_revision,
            manifest.source.selected_paths,
        )
        destination.parent.mkdir(parents=True, exist_ok=True)
        _ensure_artifact_parent_permissions(self.artifacts_root, destination)
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

    def _read_aliases(self) -> ArtifactAliases:
        return load_artifact_aliases_json(self.aliases_path.read_text(encoding="utf-8"))

    def _aliases_by_artifact(self) -> dict[str, str]:
        with self._aliases_lock:
            return dict(self._read_aliases().artifacts)

    def _summary(
        self,
        artifact_root: Path,
        manifest: ArtifactManifest,
        *,
        aliases: Mapping[str, str] | None = None,
    ) -> ArtifactSummary:
        relative_path = artifact_relative_path(
            manifest.source.provider,
            manifest.source.id,
            manifest.source.resolved_revision,
            manifest.source.selected_paths,
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
            alias=(aliases if aliases is not None else self._aliases_by_artifact()).get(
                manifest.artifact_id
            ),
            selection_digest=selection_digest(manifest.source.selected_paths),
            selected_paths=manifest.source.selected_paths,
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
        aliases = self._aliases_by_artifact()
        for manifest_path in _artifact_manifest_paths(self.artifacts_root):
            artifact_root = manifest_path.parent.parent
            relative_path = artifact_root.relative_to(self.artifacts_root).as_posix()
            candidate_id = artifact_identity_from_relative_path(relative_path)
            if candidate_id is None:
                continue
            try:
                stat = manifest_path.stat()
                if existing.get(candidate_id) == (
                    stat.st_mtime_ns,
                    stat.st_size,
                    aliases.get(candidate_id),
                ):
                    valid_artifact_ids.append(candidate_id)
                    continue
                manifest = self.read_manifest(artifact_root)
                summary = self._summary(artifact_root, manifest, aliases=aliases)
                changed.append((summary, stat.st_mtime_ns, stat.st_size))
                valid_artifact_ids.append(summary.artifact_id)
            except FutureSchemaVersionError:
                raise
            except (OSError, ValueError, VerificationError):
                continue
        self.index.reconcile(changed, valid_artifact_ids)

    def set_alias(self, artifact_id: str, alias: str | None) -> ArtifactSummary:
        found = self.find(artifact_id)
        if found is None:
            raise KeyError(f"unknown artifact {artifact_id}")
        summary, manifest = found
        normalized = validate_artifact_alias(alias) if alias is not None else None
        with self._aliases_lock:
            registry = self._read_aliases()
            for existing_artifact_id, existing_alias in registry.artifacts.items():
                if existing_alias == normalized and existing_artifact_id != artifact_id:
                    raise ValueError(f"artifact alias {normalized!r} is already in use")
            updated = dict(registry.artifacts)
            updated.pop(artifact_id, None)
            if normalized is not None:
                updated[artifact_id] = normalized
            atomic_write_json(
                self.aliases_path,
                ArtifactAliases(schema_version=1, artifacts=updated).model_dump(
                    mode="json", by_alias=True
                ),
            )
        artifact_root = self.artifacts_root / summary.relative_path
        self._index_artifact(artifact_root, manifest)
        refreshed = self.index.find(artifact_id)
        if refreshed is None or refreshed.alias != normalized:
            self.reconcile_index()
            refreshed = self.index.find(artifact_id)
        if refreshed is None or refreshed.alias != normalized:
            raise VerificationError("artifact index update did not preserve the artifact")
        return refreshed

    def alias_owner(self, alias: str) -> str | None:
        normalized = validate_artifact_alias(alias)
        with self._aliases_lock:
            return next(
                (
                    artifact_id
                    for artifact_id, existing_alias in self._read_aliases().artifacts.items()
                    if existing_alias == normalized
                ),
                None,
            )

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

    def count(
        self, *, query: str | None = None, provider: Provider | None = None
    ) -> int:
        try:
            return self.index.count(query=query, provider=provider)
        except sqlite3.DatabaseError:
            self.index.preserve_and_recreate()
            self.reconcile_index()
            return self.index.count(query=query, provider=provider)

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

    def storage_stats(self, artifact_id: str) -> ArtifactStorageStats:
        """Inspect inode/link metadata without reading artifact file contents."""
        found = self.find(artifact_id)
        if found is None:
            raise KeyError(artifact_id)
        summary, manifest = found
        artifact_root = self.artifacts_root / summary.relative_path

        def allocated_bytes(result: os.stat_result) -> int:
            blocks = getattr(result, "st_blocks", None)
            if blocks is None:
                return ((result.st_size + 511) // 512) * 512
            return int(blocks) * 512

        inode_groups: dict[tuple[int, int], dict[str, int]] = {}
        artifact_directories = {artifact_root}
        for entry in manifest.files:
            candidate = artifact_root / entry.path
            result = candidate.lstat()
            if not stat_module.S_ISREG(result.st_mode):
                raise VerificationError(f"artifact entry is not a regular file: {entry.path}")
            if result.st_size != entry.size:
                raise VerificationError(
                    f"artifact entry size changed: {entry.path}; "
                    f"expected {entry.size}, got {result.st_size}"
                )
            key = (result.st_dev, result.st_ino)
            group = inode_groups.setdefault(
                key,
                {
                    "logical": 0,
                    "allocated": allocated_bytes(result),
                    "local_links": 0,
                    "total_links": result.st_nlink,
                },
            )
            group["logical"] += entry.size
            group["local_links"] += 1
            if group["total_links"] != result.st_nlink:
                raise VerificationError(f"artifact inode changed during scan: {entry.path}")
            parent = candidate.parent
            while parent != artifact_root:
                artifact_directories.add(parent)
                parent = parent.parent

        shared_logical = 0
        shared_allocated = 0
        shared_files = 0
        exclusive_logical = 0
        exclusive_allocated = 0
        exclusive_files = 0
        for group in inode_groups.values():
            if group["total_links"] < group["local_links"]:
                raise VerificationError("artifact hardlink count changed during scan")
            if group["total_links"] > group["local_links"]:
                shared_logical += group["logical"]
                shared_allocated += group["allocated"]
                shared_files += group["local_links"]
            else:
                exclusive_logical += group["logical"]
                exclusive_allocated += group["allocated"]
                exclusive_files += group["local_links"]

        metadata_paths = set(artifact_directories)
        internal_root = artifact_root / ".modelshelf"
        for current, directories, files in os.walk(internal_root, followlinks=False):
            current_path = Path(current)
            metadata_paths.add(current_path)
            metadata_paths.update(current_path / name for name in directories)
            metadata_paths.update(current_path / name for name in files)
        metadata_allocated = 0
        metadata_inodes: set[tuple[int, int]] = set()
        for path in metadata_paths:
            result = path.lstat()
            key = (result.st_dev, result.st_ino)
            if key in metadata_inodes:
                continue
            metadata_inodes.add(key)
            metadata_allocated += allocated_bytes(result)

        estimated_reclaimable = exclusive_allocated + metadata_allocated
        return ArtifactStorageStats(
            artifact_id=artifact_id,
            logical_size=manifest.total_size,
            allocated_size=shared_allocated + estimated_reclaimable,
            shared_logical_size=shared_logical,
            shared_allocated_size=shared_allocated,
            shared_file_count=shared_files,
            exclusive_logical_size=exclusive_logical,
            exclusive_allocated_size=exclusive_allocated,
            exclusive_file_count=exclusive_files,
            metadata_allocated_size=metadata_allocated,
            estimated_reclaimable_size=estimated_reclaimable,
            scanned_at=datetime.now(UTC),
        )

    def delete(self, artifact_id: str) -> bool:
        """Atomically hide an artifact, then remove its files and index entry."""
        found = self.find(artifact_id)
        if found is None:
            return False
        summary, _manifest = found
        artifact_root = self.artifacts_root / summary.relative_path
        tombstone = self.staging_root / f".deleting-{uuid4()}"
        os.chmod(artifact_root, 0o755)
        try:
            os.rename(artifact_root, tombstone)
        except Exception:
            os.chmod(artifact_root, 0o555)
            raise
        with self._aliases_lock:
            registry = self._read_aliases()
            updated = dict(registry.artifacts)
            updated.pop(artifact_id, None)
            try:
                if updated != registry.artifacts:
                    atomic_write_json(
                        self.aliases_path,
                        ArtifactAliases(schema_version=1, artifacts=updated).model_dump(
                            mode="json", by_alias=True
                        ),
                    )
                self.index.delete(artifact_id)
            except Exception:
                atomic_write_json(
                    self.aliases_path,
                    registry.model_dump(mode="json", by_alias=True),
                )
                os.rename(tombstone, artifact_root)
                os.chmod(artifact_root, 0o555)
                raise
        _unfreeze_tree(tombstone)
        shutil.rmtree(tombstone)
        parent = artifact_root.parent
        while parent != self.artifacts_root:
            try:
                parent.rmdir()
            except OSError:
                break
            parent = parent.parent
        return True


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
        manifest.source.selected_paths,
    )
    if manifest.artifact_id != expected_id:
        failures.append("manifest artifactId does not match source identity")
    return failures
