from __future__ import annotations

import shlex
from pathlib import Path
from typing import TypedDict

INSTALLER_MARKER = "# MODELSHELF_SERVER_DOWNLOAD_BASE"
CLIENT_PLATFORMS = (
    ("linux", "amd64"),
    ("linux", "arm64"),
    ("darwin", "amd64"),
    ("darwin", "arm64"),
)


class ClientPlatform(TypedDict):
    os: str
    arch: str
    filename: str


def archive_filename(os_name: str, architecture: str) -> str:
    return f"modelshelf_{os_name}_{architecture}.tar.gz"


def allowed_files() -> frozenset[str]:
    return frozenset(
        {"checksums.txt", "version.txt"}
        | {archive_filename(os_name, arch) for os_name, arch in CLIENT_PLATFORMS}
    )


def distribution_file(root: Path | None, filename: str) -> Path | None:
    if root is None or filename not in allowed_files():
        return None
    candidate = root.resolve() / filename
    return candidate if candidate.is_file() else None


def distribution_metadata(root: Path | None) -> dict[str, object]:
    if root is None:
        return {"available": False, "platforms": []}
    resolved = root.resolve()
    installer = resolved / "install.sh"
    checksums = resolved / "checksums.txt"
    platforms: list[ClientPlatform] = []
    for os_name, architecture in CLIENT_PLATFORMS:
        filename = archive_filename(os_name, architecture)
        if (resolved / filename).is_file():
            platforms.append({"os": os_name, "arch": architecture, "filename": filename})
    available = (
        installer.is_file() and checksums.is_file() and len(platforms) == len(CLIENT_PLATFORMS)
    )
    result: dict[str, object] = {"available": available, "platforms": platforms}
    version_path = resolved / "version.txt"
    if version_path.is_file():
        version = version_path.read_text(encoding="utf-8").strip()
        if version:
            result["version"] = version
    return result


def render_installer(root: Path | None, download_base: str) -> str:
    if not distribution_metadata(root)["available"]:
        raise FileNotFoundError("client distribution is not configured")
    assert root is not None
    installer = root.resolve() / "install.sh"
    script = installer.read_text(encoding="utf-8")
    if INSTALLER_MARKER not in script:
        raise ValueError("client installer is missing its server download marker")
    assignment = f"MODELSHELF_SERVER_DOWNLOAD_BASE={shlex.quote(download_base)}"
    return script.replace(INSTALLER_MARKER, assignment, 1)
