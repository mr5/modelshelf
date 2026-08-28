from __future__ import annotations

import json
import re
import stat
import tarfile
import zipfile
from pathlib import Path, PurePosixPath

from modelshelf_core import InferredMetadata

ARCHIVE_SUFFIXES = (".zip", ".tar", ".tar.gz", ".tgz", ".tar.bz2", ".tbz2", ".tar.xz")
FORMATS = {
    ".safetensors": "safetensors",
    ".gguf": "GGUF",
    ".onnx": "ONNX",
    ".pt": "PyTorch",
    ".pth": "PyTorch",
    ".tflite": "TensorFlow Lite",
}


def is_archive(path: Path) -> bool:
    lower = path.name.lower()
    return lower.endswith(ARCHIVE_SUFFIXES)


def _safe_archive_path(value: str) -> PurePosixPath:
    candidate = PurePosixPath(value.replace("\\", "/"))
    if candidate.is_absolute() or ".." in candidate.parts:
        raise ValueError(f"unsafe archive path: {value}")
    return candidate


def archive_entries(path: Path) -> list[str]:
    if zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as archive:
            result = []
            for zip_entry in archive.infolist():
                _safe_archive_path(zip_entry.filename)
                mode = zip_entry.external_attr >> 16
                file_type = stat.S_IFMT(mode)
                if file_type and not (stat.S_ISREG(mode) or stat.S_ISDIR(mode)):
                    raise ValueError(f"unsafe archive member type: {zip_entry.filename}")
                result.append(zip_entry.filename)
            return result
    if tarfile.is_tarfile(path):
        with tarfile.open(path) as archive:
            result = []
            for tar_entry in archive.getmembers():
                _safe_archive_path(tar_entry.name)
                if tar_entry.issym() or tar_entry.islnk() or tar_entry.isdev():
                    raise ValueError(f"unsafe archive member type: {tar_entry.name}")
                result.append(tar_entry.name)
            return result
    return []


def _read_archive_member(path: Path, name: str, limit: int = 1024 * 1024) -> bytes:
    if zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as zip_archive, zip_archive.open(name) as zip_member:
            return zip_member.read(limit)
    if tarfile.is_tarfile(path):
        with tarfile.open(path) as tar_archive:
            tar_member = tar_archive.getmember(name)
            extracted = tar_archive.extractfile(tar_member)
            return extracted.read(limit) if extracted else b""
    return b""


def extract_archive(path: Path, destination: Path) -> None:
    entries = archive_entries(path)
    if not entries:
        raise ValueError("downloaded file is not a supported zip/tar archive")
    destination.mkdir(parents=True, exist_ok=True)
    if zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as archive:
            archive.extractall(destination)
    else:
        with tarfile.open(path) as archive:
            archive.extractall(destination, filter="data")


def infer_metadata(download: Path, source_url: str) -> InferredMetadata:
    archive = is_archive(download)
    names = archive_entries(download) if archive else [download.name]
    cleaned = re.sub(r"(\.tar)?\.(gz|bz2|xz)$|\.zip$|\.tar$", "", download.name, flags=re.I)
    version_match = re.search(r"(?:^|[-_.])v?(\d+(?:\.\d+){0,3}(?:[-_.][a-z0-9]+)?)", cleaned, re.I)
    top_levels = {PurePosixPath(name).parts[0] for name in names if PurePosixPath(name).parts}
    name = (
        next(iter(top_levels))
        if archive and len(top_levels) == 1
        else cleaned[: version_match.start()].rstrip("-_.")
        if version_match
        else cleaned
    )
    version = version_match.group(1) if version_match else "content-addressed"
    detected = {
        FORMATS[Path(name).suffix.lower()] for name in names if Path(name).suffix.lower() in FORMATS
    }
    format_name = next(iter(detected)) if len(detected) == 1 else None
    notes: list[str] = []
    if archive:
        notes.append(f"Archive contains {len(names)} entries; extraction requires confirmation")
    if len(top_levels) == 1:
        notes.append(f"Single top-level archive directory: {next(iter(top_levels))}")
    if version == "content-addressed":
        notes.append(
            "No reliable version found; immutable identity uses downloaded content SHA-256"
        )
    config_candidates = [item for item in names if Path(item).name.casefold() == "config.json"]
    if config_candidates:
        try:
            raw = _read_archive_member(download, config_candidates[0])
            config = json.loads(raw)
            if isinstance(config, dict) and isinstance(config.get("model_type"), str):
                notes.append(f"config.json model_type: {config['model_type']}")
        except (KeyError, OSError, ValueError, json.JSONDecodeError, tarfile.TarError):
            pass
    readme_candidates = [item for item in names if Path(item).name.casefold().startswith("readme")]
    if readme_candidates:
        try:
            readme = _read_archive_member(download, readme_candidates[0], 128 * 1024).decode(
                "utf-8", errors="replace"
            )
            title = re.search(r"^#\s+(.+?)\s*$", readme, re.MULTILINE)
            if title:
                readme_title = title.group(1).strip()
                notes.append(f"README title: {readme_title}")
                if name.casefold() in {"archive", "download", "model"}:
                    name = readme_title
        except (KeyError, OSError, ValueError, tarfile.TarError):
            pass
    return InferredMetadata(
        name=name or urlparse_name(source_url),
        version=version,
        format=format_name,
        archive=archive,
        confidence="medium" if version_match and name else "low",
        notes=notes,
    )


def urlparse_name(value: str) -> str:
    from urllib.parse import urlparse

    return urlparse(value).hostname or "download"
