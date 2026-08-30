from __future__ import annotations

from .client_distribution import (
    CLIENT_PLATFORMS,
    archive_filename,
    distribution_metadata,
)
from .config import Settings

GITHUB_INSTALLER = (
    "https://raw.githubusercontent.com/mr5/modelshelf/main/packages/client/install.sh"
)


def _external_url(settings: Settings, request_base_url: str, path: str) -> str:
    base = settings.public_base_url or request_base_url
    return f"{base.rstrip('/')}/{path.lstrip('/')}"


def _shell_quote(value: str) -> str:
    escaped = value.replace("'", "'\"'\"'")
    return f"'{escaped}'"


def _nfs_source(host: str, export_path: str) -> str:
    normalized = f"[{host}]" if ":" in host and not host.startswith("[") else host
    return f"{normalized}:{export_path}"


def _fence(language: str, value: str) -> str:
    return f"```{language}\n{value}\n```"


def _platform_name(os_name: str, architecture: str) -> str:
    operating_system = (
        "macOS" if os_name == "darwin" else "Linux" if os_name == "linux" else os_name
    )
    display_arch = (
        "x86_64"
        if architecture == "amd64"
        else "arm64 / Apple Silicon"
        if architecture == "arm64"
        else architecture
    )
    return f"{operating_system} · {display_arch}"


def render_integration_markdown(settings: Settings, request_base_url: str) -> str:
    """Render the canonical human and agent integration documentation."""
    install_url = _external_url(settings, request_base_url, "/install.sh")
    server_base = install_url[: -len("/install.sh")]
    markdown_url = _external_url(settings, request_base_url, "/integration.md")
    host = settings.nfs_advertised_host or "modelshelf.internal"
    port = settings.nfs_advertised_port or settings.nfs_port
    export_path = settings.nfs_export_path
    source = _nfs_source(host, export_path)

    linux_mount = (
        "sudo mkdir -p /mnt/modelshelf\n"
        "sudo mount -t nfs4 "
        f"-o ro,vers=4.2,port={port},lookupcache=positive "
        f"{_shell_quote(source)} /mnt/modelshelf"
    )
    mac_mount = (
        "sudo mkdir -p /Volumes/modelshelf\n"
        f"sudo mount_nfs -o ro,vers=4,port={port} "
        f"{_shell_quote(source)} /Volumes/modelshelf"
    )
    fstab = (
        f"{source} /mnt/modelshelf nfs4 "
        f"ro,vers=4.2,port={port},lookupcache=positive,_netdev,nofail,"
        "x-systemd.automount 0 0"
    )
    compose_bind = """services:
  inference:
    image: your-inference-image
    volumes:
      - /mnt/modelshelf:/models:ro"""
    compose_nfs = f"""services:
  inference:
    image: your-inference-image
    volumes:
      - modelshelf:/models:ro

volumes:
  modelshelf:
    driver: local
    driver_opts:
      type: nfs
      o: {_shell_quote(f"addr={host},nfsvers=4.2,port={port},ro")}
      device: {_shell_quote(f":{export_path}")}"""
    kubernetes = f"""apiVersion: v1
kind: PersistentVolume
metadata:
  name: modelshelf
spec:
  capacity:
    storage: 1Pi
  accessModes: [ReadOnlyMany]
  mountOptions: [nfsvers=4.2, port={port}, ro]
  nfs:
    server: {host}
    path: {export_path}
    readOnly: true"""
    config = f"""schemaVersion: 2
serverUrl: {server_base}
nfsLocalPath: /mnt/modelshelf
localBasePath: /var/lib/modelshelf
# Optional: enables sync to create a server download task when missing.
# This is a CLI API token, not the Web UI password.
writeToken: replace-with-a-server-write-token
models:
  - alias: mini-lm
    provider: huggingface
    id: sentence-transformers/all-MiniLM-L6-v2
    # Optional; defaults to main. May be a branch, tag, or commit.
    revision: main
  - alias: qwen-gguf
    provider: modelscope-cn
    id: unsloth/Qwen3-8B-GGUF
    revision: master
    # Optional; must be one complete GGUF variant recognized by the server.
    files:
      - Qwen3-8B-Q4_K_M.gguf
    # Creates an additional symlink; model bytes remain in canonical storage.
    path: runtime/qwen-gguf"""
    local_layout = """/var/lib/modelshelf/
├── .modelshelf/layout.json
├── models/<source>/<model-id...>/
│   ├── <resolved-revision>/
│   └── <requested-revision> -> <resolved-revision>/
└── aliases/<alias> -> ../models/.../<resolved-revision>/"""
    lock_example = """schemaVersion: 2
models:
  - alias: qwen-gguf
    provider: modelscope-cn
    id: unsloth/Qwen3-8B-GGUF
    revision: master
    files:
      - Qwen3-8B-Q4_K_M.gguf
    resolvedRevision: 7dbbc90392e2f80f3d3c277d6e90027e55de9125
    artifactId: modelscope-cn:...:files:...
    relativePath: modelscope-cn/unsloth/Qwen3-8B-GGUF/<resolved-revision>~files-<digest>
    lockedAt: 2026-08-27T14:30:00Z"""

    client_metadata = distribution_metadata(settings.client_dist)
    client_available = client_metadata["available"] is True
    client_version_value = client_metadata.get("version")
    client_version = client_version_value if isinstance(client_version_value, str) else None
    platform_links = []
    if client_available:
        for os_name, architecture in CLIENT_PLATFORMS:
            filename = archive_filename(os_name, architecture)
            platform_links.append(
                f"- [{_platform_name(os_name, architecture)}]"
                f"({server_base}/api/v1/client/{filename}) — `{filename}`"
            )
        platform_links.append(
            f"- [SHA-256 checksums]({server_base}/api/v1/client/checksums.txt) — `checksums.txt`"
        )

    nfs_status = (
        f"> Advertised endpoint: **NFSv4.2** at `{host}:{port}` with export `{export_path}`."
        if settings.nfs_advertised_host
        else "> **NFS discovery is not configured.** The examples use placeholders. Set "
        "`MODELSHELF_NFS_ADVERTISED_HOST` to publish the real endpoint."
    )
    bundled_install = (
        f"Bundled client version: **v{client_version}**.\n\n" if client_version else ""
    )
    bundled_install += (
        _fence("bash", f"curl -fsSL {_shell_quote(install_url)} | sh")
        if client_available
        else "> **No bundled client distribution is available.** Use the GitHub installer below."
    )
    manual_downloads = (
        "\n".join(platform_links)
        if platform_links
        else "Bundled platform archives are not available from this server."
    )

    sections = [
        "# ModelShelf integration",
        "Mount immutable artifacts directly over read-only NFS, or reconcile selected models "
        "onto local storage with the client CLI.",
        f"Canonical agent-readable URL: <{markdown_url}>",
        "- [Direct NFS](#direct-nfs)",
        "- [Client CLI](#client-cli)",
        "## Direct NFS",
        "Use the read-only NFS export for browsing, shared development, or runtimes that "
        "tolerate network-backed reads. Clients must not write to the export.",
        nfs_status,
        "### Linux mount",
        "Install `nfs-common` on Debian/Ubuntu or `nfs-utils` on RHEL/Fedora, then mount "
        "read-only.",
        _fence("bash", linux_mount),
        "### macOS mount",
        "macOS uses its system NFS client and negotiates NFSv4 with `vers=4`.",
        _fence("bash", mac_mount),
        "### Persistent Linux automount",
        "For production hosts, prefer a systemd automount so boot does not block when the "
        "network is unavailable. This line is also suitable for `/etc/fstab`.",
        _fence("fstab", fstab),
        "Unmount with `sudo umount /mnt/modelshelf`. The client CLI's `modelshelf mount` "
        "command creates matching `.mount` and `.automount` units automatically.",
        "### Docker Compose with a host bind",
        "Recommended when the host already mounts ModelShelf.",
        _fence("yaml", compose_bind),
        "### Docker Compose with a managed NFS volume",
        "The Docker daemon performs this mount, so the NFS host must be reachable from the "
        "daemon or Docker Desktop VM.",
        _fence("yaml", compose_nfs),
        "### Kubernetes PersistentVolume",
        _fence("yaml", kubernetes),
        "Bind this PV to a PVC and mount the claim with `readOnly: true` in inference pods.",
        "> **Latency-sensitive inference:** mount NFS as the shared source, then use the "
        "client CLI to reconcile selected artifacts to local NVMe. Inference runtimes do not "
        "need to depend on central NFS availability.",
        "## Client CLI",
        "The standalone Go binary supports Linux and macOS on x86_64 and arm64. It discovers "
        "NFS through this server, verifies manifests, and publishes local copies atomically.",
        "### Install from this server",
        "This gets the client version bundled with this ModelShelf server, detects OS and "
        "architecture, and verifies SHA-256 before installing.",
        bundled_install,
        "It installs to `/usr/local/bin/modelshelf`. Override the directory with "
        '`MODELSHELF_INSTALL_DIR="$HOME/.local/bin"`.',
        "### Install the latest GitHub release",
        "Use this when you deliberately want the newest published client instead of the "
        "server-matched version. Set `MODELSHELF_VERSION=vX.Y.Z` to pin a release.",
        _fence("bash", f"curl -fsSL {_shell_quote(GITHUB_INSTALLER)} | sh"),
        "### Manual download",
        manual_downloads,
        "Download the archive for the target machine, compare it with `checksums.txt`, then:",
        _fence(
            "bash",
            "tar -xzf modelshelf_<os>_<arch>.tar.gz\n"
            "sudo install -m 0755 modelshelf /usr/local/bin/modelshelf\n"
            "modelshelf --version",
        ),
        "### Client configuration",
        "The default file is `~/.config/modelshelf/config.yml`. Override it globally with "
        "`MODELSHELF_CONFIG` or per invocation with `--config`.",
        _fence("yaml", config),
        "Configuration fields:",
        "- `schemaVersion`: configuration schema understood by the CLI.",
        "- `serverUrl`: HTTP API used for discovery, search, task creation, and NFS lookup.",
        "- `nfsLocalPath`: absolute path where the read-only server export is mounted.",
        "- `localBasePath`: root containing canonical `models/` content and stable `aliases/` "
        "symlinks.",
        "- `writeToken`: optional CLI API token used to create missing server downloads or "
        "access protected APIs. It is separate from the Web UI password.",
        "- `models`: desired-state list. Optional `revision` accepts a branch, tag, or commit "
        "and defaults to `main`.",
        "- `files`: optional paths for one complete GGUF variant recognized by the server. "
        "Every shard is required and the set is part of artifact identity.",
        "- `alias`: optional globally unique CLI name and stable symlink.",
        "- `path`: optional additional symlink, relative to `localBasePath` or absolute.",
        "### Local storage layout",
        "Canonical model bytes and human-friendly references have separate roles below "
        "`localBasePath`. A branch or tag such as `main` is a sibling symlink to its locked "
        "immutable revision.",
        _fence("text", local_layout),
        "The internal `models/.staging/` directory is temporary and is never a model "
        "reference path.",
        "> **References do not duplicate model bytes.** Multiple aliases may declare the same "
        "exact model and share one immutable directory. Requested-revision, alias, and path "
        "entries are symlinks only; `sync --update` atomically moves branch or tag links.",
        "### Generated lock file",
        "`sync` writes resolved commits to `config.lock.yml` beside `config.yml`; it never "
        "writes observed state into user configuration. A custom `app.yml` uses "
        "`app.lock.yml`.",
        _fence("yaml", lock_example),
        "Commit the lock with a shared config for reproducible deployments. It contains no "
        "API token.",
        "### Command reference",
        "All commands accept `--config <path>`. Provider names are `huggingface`, "
        "`modelscope-cn`, `modelscope-ai`, `github-release`, `kaggle`, `http`, and "
        "`filesystem`.",
        "- `modelshelf mount`: discover and mount the server NFS endpoint.",
        "- `modelshelf unmount`: remove the configured mount and generated Linux systemd units.",
        "- `modelshelf add <provider> <model-id> [-r revision] [--file path] [--alias alias] "
        "[--path path]`: add desired state and sync it. Repeat `--file` for all shards of one "
        "recognized GGUF variant.",
        "- `modelshelf remove <alias> [-y]`: remove desired state and symlink references. "
        "Canonical files are offered for deletion only when no lock entry references them.",
        "- `modelshelf search <query>`: search published artifacts by model name or ID.",
        "- `modelshelf sync [alias] [--update] [--frozen-lockfile]`: atomically reconcile all "
        "desired state or one model. `--update` refreshes moving revisions.",
        "- `modelshelf list`: show configured models, revisions, local state, size, and sync time.",
        "- `modelshelf status <alias>`: readiness check; exit `0` ready, `2` not ready, `3` "
        "corrupt, `4` unavailable.",
        "- `modelshelf verify <model-path-or-alias> [--unexpected]`: validate the manifest, "
        "paths, and sizes; optionally report unexpected files.",
        "- `modelshelf verify --full <model-path-or-alias>`: also recompute every SHA-256.",
        "- `modelshelf tui`: interactively browse local desired state and server artifacts.",
        "- `modelshelf upgrade [--check]`: upgrade from the client bundled with this server.",
        "- `modelshelf upgrade --github [--version vX.Y.Z]`: use GitHub instead; development "
        "builds and downgrades require `--force`.",
        "- `modelshelf hash-password [--stdin]`: generate an Argon2id Web UI password hash.",
        "Typical workflow:",
        _fence(
            "bash",
            "modelshelf mount\n"
            "modelshelf add huggingface sentence-transformers/all-MiniLM-L6-v2 "
            "--revision main --alias mini-lm\n"
            "modelshelf status mini-lm\n"
            "modelshelf sync mini-lm\n"
            "modelshelf verify --full mini-lm",
        ),
    ]
    return "\n\n".join(sections).rstrip() + "\n"
