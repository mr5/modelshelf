from __future__ import annotations

import base64
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


def artifact_identity(provider: Provider, source_id: str, resolved_revision: str) -> str:
    return f"{provider.value}:{encode_segment(source_id)}:{encode_segment(resolved_revision)}"


def artifact_relative_path(provider: Provider, source_id: str, resolved_revision: str) -> str:
    return PurePosixPath(
        provider.value,
        *_source_path_segments(provider, source_id),
        escape_path_segment(resolved_revision),
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
        resolved_revision = unescape_path_segment(parts[-1])
        if artifact_relative_path(provider, source_id, resolved_revision) != relative_path:
            return None
    except (UnicodeDecodeError, ValueError):
        return None
    return artifact_identity(provider, source_id, resolved_revision)
