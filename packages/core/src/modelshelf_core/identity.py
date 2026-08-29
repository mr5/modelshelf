from __future__ import annotations

import base64
import hashlib
import re
import unicodedata
from pathlib import PurePosixPath
from urllib.parse import unquote

from .models import Provider


def encode_segment(value: str) -> str:
    return base64.urlsafe_b64encode(value.encode()).decode().rstrip("=")


def escape_path_segment(value: str) -> str:
    if not value:
        raise ValueError("artifact path segments cannot be empty")
    escaped: list[str] = []
    for character in value:
        unsafe = (
            character in {"/", "\\", "%"}
            or character.isspace()
            or unicodedata.category(character).startswith("C")
        )
        if unsafe:
            escaped.extend(f"%{byte:02X}" for byte in character.encode("utf-8"))
        else:
            escaped.append(character)
    result = "".join(escaped)
    if result in {".", ".."}:
        return result.replace(".", "%2E")
    return result


def unescape_path_segment(value: str) -> str:
    if not value or re.search(r"%(?![0-9A-F]{2})", value):
        raise ValueError("invalid escaped artifact path segment")
    return unquote(value, encoding="utf-8", errors="strict")


def _source_path_segments(provider: Provider, source_id: str) -> tuple[str, ...]:
    values = (source_id,) if provider is Provider.HTTP else tuple(source_id.split("/"))
    return tuple(escape_path_segment(value) for value in values)


_SELECTION_SUFFIX = re.compile(r"^(?P<revision>.+)~files-(?P<digest>[a-f0-9]{64})$")


def selection_digest(selected_paths: list[str] | tuple[str, ...] | None) -> str | None:
    if selected_paths is None:
        return None
    digest = hashlib.sha256()
    for path in sorted(set(selected_paths)):
        digest.update(path.encode("utf-8"))
        digest.update(b"\0")
    return digest.hexdigest()


def artifact_identity(
    provider: Provider,
    source_id: str,
    resolved_revision: str,
    selected_paths: list[str] | tuple[str, ...] | None = None,
) -> str:
    identity = f"{provider.value}:{encode_segment(source_id)}:{encode_segment(resolved_revision)}"
    digest = selection_digest(selected_paths)
    return f"{identity}:files:{digest}" if digest else identity


def artifact_relative_path(
    provider: Provider,
    source_id: str,
    resolved_revision: str,
    selected_paths: list[str] | tuple[str, ...] | None = None,
    *,
    selected_paths_digest: str | None = None,
) -> str:
    digest = selected_paths_digest or selection_digest(selected_paths)
    revision_segment = (
        resolved_revision if digest is None else f"{resolved_revision}~files-{digest}"
    )
    return PurePosixPath(
        provider.value,
        *_source_path_segments(provider, source_id),
        escape_path_segment(revision_segment),
    ).as_posix()


def artifact_identity_from_relative_path(relative_path: str) -> str | None:
    parts = PurePosixPath(relative_path).parts
    if len(parts) < 3:
        return None
    try:
        provider = Provider(parts[0])
        source_parts = tuple(unescape_path_segment(part) for part in parts[1:-1])
        if provider is Provider.HTTP and len(source_parts) != 1:
            return None
        source_id = source_parts[0] if provider is Provider.HTTP else "/".join(source_parts)
        revision_segment = unescape_path_segment(parts[-1])
        match = _SELECTION_SUFFIX.fullmatch(revision_segment)
        resolved_revision = match.group("revision") if match else revision_segment
        digest = match.group("digest") if match else None
        if (
            artifact_relative_path(
                provider,
                source_id,
                resolved_revision,
                selected_paths_digest=digest,
            )
            != relative_path
        ):
            return None
    except (UnicodeDecodeError, ValueError):
        return None
    identity = artifact_identity(provider, source_id, resolved_revision)
    return f"{identity}:files:{digest}" if digest else identity
