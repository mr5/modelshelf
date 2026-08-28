from __future__ import annotations

import functools
import hashlib
import http.server
import os
import platform
import subprocess
import tarfile
import threading
from pathlib import Path

from fastapi.testclient import TestClient
from modelshelf_server.app import create_app
from modelshelf_server.client_distribution import CLIENT_PLATFORMS, archive_filename
from modelshelf_server.config import Settings


def make_distribution(root: Path) -> None:
    root.mkdir()
    installer_source = Path(__file__).parents[1] / "packages" / "client" / "install.sh"
    (root / "install.sh").write_bytes(installer_source.read_bytes())
    checksums: list[str] = []
    for os_name, architecture in CLIENT_PLATFORMS:
        filename = archive_filename(os_name, architecture)
        content = f"archive for {os_name}/{architecture}".encode()
        (root / filename).write_bytes(content)
        checksums.append(f"{hashlib.sha256(content).hexdigest()}  {filename}\n")
    (root / "checksums.txt").write_text("".join(checksums), encoding="utf-8")
    (root / "version.txt").write_text("v0.1.0\n", encoding="utf-8")


def test_server_distributes_matching_installer_and_client_packages(tmp_path: Path) -> None:
    distribution = tmp_path / "client"
    make_distribution(distribution)
    settings = Settings(
        storage_root=tmp_path / "storage",
        client_dist=distribution,
        public_base_url="https://models.example/base",
        session_secret="test-session-secret-with-32-bytes-minimum",
    )

    with TestClient(create_app(settings)) as client:
        info = client.get("/api/v1/info")
        installer = client.get("/install.sh")
        archive = client.get("/api/v1/client/modelshelf_linux_arm64.tar.gz")
        missing = client.get("/api/v1/client/unknown.tar.gz")

    assert info.status_code == 200
    assert info.json()["client"] == {
        "available": True,
        "version": "v0.1.0",
        "installUrl": "https://models.example/base/install.sh",
        "downloadUrl": "https://models.example/base/api/v1/client",
        "platforms": [
            {"os": os_name, "arch": arch, "filename": archive_filename(os_name, arch)}
            for os_name, arch in CLIENT_PLATFORMS
        ],
    }
    assert installer.status_code == 200
    assert installer.headers["content-type"].startswith("text/x-shellscript")
    assert (
        "MODELSHELF_SERVER_DOWNLOAD_BASE=https://models.example/base/api/v1/client"
        in installer.text
    )
    assert archive.content == b"archive for linux/arm64"
    assert missing.status_code == 404


class QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, format: str, *args: object) -> None:
        pass


def test_installer_detects_platform_verifies_checksum_and_installs(tmp_path: Path) -> None:
    os_name = {"Linux": "linux", "Darwin": "darwin"}.get(platform.system())
    architecture = {
        "x86_64": "amd64",
        "amd64": "amd64",
        "arm64": "arm64",
        "aarch64": "arm64",
    }.get(platform.machine())
    if os_name is None or architecture is None:
        return

    served = tmp_path / "served"
    served.mkdir()
    binary = tmp_path / "modelshelf"
    binary.write_text("#!/bin/sh\necho 'modelshelf test-version'\n", encoding="utf-8")
    binary.chmod(0o755)
    archive_name = archive_filename(os_name, architecture)
    archive = served / archive_name
    with tarfile.open(archive, "w:gz") as output:
        output.add(binary, arcname="modelshelf")
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    (served / "checksums.txt").write_text(f"{digest}  {archive_name}\n", encoding="utf-8")

    handler = functools.partial(QuietHandler, directory=served)
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    install_directory = tmp_path / "bin"
    environment = {
        **os.environ,
        "MODELSHELF_CLIENT_DOWNLOAD_BASE": f"http://127.0.0.1:{server.server_port}",
        "MODELSHELF_INSTALL_DIR": str(install_directory),
    }
    try:
        result = subprocess.run(
            ["sh", str(Path(__file__).parents[1] / "packages" / "client" / "install.sh")],
            check=False,
            capture_output=True,
            env=environment,
            text=True,
        )
    finally:
        server.shutdown()
        thread.join()

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "modelshelf test-version"
    installed = install_directory / "modelshelf"
    assert installed.is_file()
    assert installed.stat().st_mode & 0o111
