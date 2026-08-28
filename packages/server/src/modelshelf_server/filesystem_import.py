from __future__ import annotations

import os
import shutil
import stat
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from uuid import uuid4

from modelshelf_core import ArtifactManifest, Catalog, Provider, SourceReference
from modelshelf_core.catalog import content_digest, inventory

from .archive import FORMATS, archive_entries, extract_archive, infer_metadata

RESERVED_COMPONENT = ".modelshelf"


@dataclass(frozen=True)
class FilesystemImportResult:
    manifest: ArtifactManifest
    destination: Path
    deduplicated: bool


def allowed_import_roots(catalog: Catalog, configured: tuple[Path, ...]) -> tuple[Path, ...]:
    roots: list[Path] = [catalog.incoming_root.resolve()]
    for configured_root in configured:
        resolved = configured_root.expanduser().resolve()
        if resolved == Path(resolved.anchor):
            raise ValueError("filesystem root cannot be configured as an import root")
        if resolved not in roots:
            roots.append(resolved)
    return tuple(roots)


def resolve_import_source(source: Path, *, roots: tuple[Path, ...], catalog: Catalog) -> Path:
    expanded = source.expanduser()
    try:
        original_stat = expanded.lstat()
    except FileNotFoundError:
        raise ValueError(f"import source does not exist: {source}") from None
    if stat.S_ISLNK(original_stat.st_mode):
        raise ValueError("import source cannot be a symbolic link")
    resolved = expanded.resolve(strict=True)
    if not (resolved.is_file() or resolved.is_dir()):
        raise ValueError("import source must be a regular file or directory")
    if RESERVED_COMPONENT in resolved.parts:
        raise ValueError(f"import source cannot contain a {RESERVED_COMPONENT} path component")

    forbidden = (catalog.artifacts_root, catalog.staging_root, catalog.jobs_root.parent)
    if any(resolved == path or resolved.is_relative_to(path) for path in forbidden):
        raise ValueError(
            "cannot import from ModelShelf artifacts, staging, or metadata directories"
        )

    if not any(resolved != root and resolved.is_relative_to(root) for root in roots):
        configured = ", ".join(str(root) for root in roots)
        raise ValueError(f"import source is outside configured import roots: {configured}")
    return resolved


def _copy_regular_file(source: Path, destination: Path) -> None:
    source_stat = source.lstat()
    if not stat.S_ISREG(source_stat.st_mode):
        raise ValueError(f"unsupported import entry type: {source}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, destination, follow_symlinks=False)


def _copy_directory(source: Path, destination: Path) -> None:
    destination.mkdir(parents=True)
    for current, directories, filenames in os_walk_sorted(source):
        relative = current.relative_to(source)
        target_directory = destination / relative
        target_directory.mkdir(parents=True, exist_ok=True)
        for directory_name in directories:
            candidate = current / directory_name
            mode = candidate.lstat().st_mode
            if directory_name == RESERVED_COMPONENT:
                raise ValueError(f"reserved import path: {candidate}")
            if not stat.S_ISDIR(mode) or stat.S_ISLNK(mode):
                raise ValueError(f"unsupported import entry type: {candidate}")
            (target_directory / directory_name).mkdir(exist_ok=True)
        for filename in filenames:
            candidate = current / filename
            if filename == RESERVED_COMPONENT:
                raise ValueError(f"reserved import path: {candidate}")
            _copy_regular_file(candidate, target_directory / filename)


def os_walk_sorted(root: Path) -> Iterator[tuple[Path, list[str], list[str]]]:
    for current, directories, filenames in os.walk(root, followlinks=False):
        directories.sort()
        filenames.sort()
        yield Path(current), directories.copy(), filenames


def _extract_local_archive(source: Path, destination: Path) -> None:
    entries = archive_entries(source)
    if not entries:
        raise ValueError("--extract requires a supported zip/tar archive")
    for entry in entries:
        if RESERVED_COMPONENT in PurePosixPath(entry).parts:
            raise ValueError(f"archive contains reserved path: {entry}")
    extract_archive(source, destination)


def _detected_format(paths: list[str]) -> str | None:
    detected = {
        FORMATS[Path(path).suffix.casefold()]
        for path in paths
        if Path(path).suffix.casefold() in FORMATS
    }
    return next(iter(detected)) if len(detected) == 1 else None


def import_filesystem(
    catalog: Catalog,
    source: Path,
    *,
    roots: tuple[Path, ...],
    name: str | None = None,
    version: str | None = None,
    format_name: str | None = None,
    source_id: str | None = None,
    extract: bool = False,
) -> FilesystemImportResult:
    resolved_source = resolve_import_source(source, roots=roots, catalog=catalog)
    if extract and not resolved_source.is_file():
        raise ValueError("--extract can only be used with an archive file")

    inferred = (
        infer_metadata(resolved_source, resolved_source.as_uri())
        if resolved_source.is_file()
        else None
    )
    inferred_name = (
        resolved_source.stem
        if inferred is not None and not inferred.archive
        else inferred.name
        if inferred is not None
        else resolved_source.name
    )
    final_name = (name or inferred_name).strip()
    final_version = (version or (inferred.version if inferred else "content-addressed")).strip()
    final_source_id = (source_id or final_name).strip()
    if not final_name or not final_version or not final_source_id:
        raise ValueError("import name, version, and source ID cannot be empty")

    stage = catalog.staging_path(f"import-{uuid4()}")
    publish_root = stage / "artifact"
    try:
        stage.mkdir(parents=True)
        if extract:
            _extract_local_archive(resolved_source, publish_root)
        elif resolved_source.is_dir():
            _copy_directory(resolved_source, publish_root)
        else:
            publish_root.mkdir()
            _copy_regular_file(resolved_source, publish_root / resolved_source.name)

        files = inventory(publish_root)
        if not files:
            raise ValueError("import source contains no regular files")
        digest = content_digest(files)
        resolved_revision = f"sha256:{digest}"
        detected_format = _detected_format([item.path for item in files])
        manifest = catalog.create_manifest(
            publish_root,
            name=final_name,
            version=final_version,
            format=(format_name.strip() if format_name and format_name.strip() else None)
            or detected_format
            or (inferred.format if inferred else None),
            files=files,
            source=SourceReference(
                provider=Provider.FILESYSTEM,
                id=final_source_id,
                requested_revision="content",
                resolved_revision=resolved_revision,
            ),
        )
        destination, deduplicated = catalog.publish(publish_root, manifest)
        return FilesystemImportResult(manifest, destination, deduplicated)
    finally:
        shutil.rmtree(stage, ignore_errors=True)
