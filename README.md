# ModelShelf

[简体中文](README.zh-CN.md)

ModelShelf is a lightweight service for downloading models into a centralized, immutable artifact
shelf. A POSIX filesystem and per-artifact manifests are the source of truth; SQLite is only a
rebuildable catalog index.

The v1 server supports Hugging Face Hub, ModelScope CN, ModelScope AI, GitHub Releases, Kaggle
Models, Generic HTTP URLs, and allowlisted server-local imports. A separate NFS-Ganesha container
exports completed artifacts read-only over NFSv4.2. The client is a standalone Go binary for Linux
and macOS on amd64 and arm64.

## Core guarantees

- Hub branches, tags, and versions resolve to immutable commits/versions before publication.
- Downloads and client syncs use same-filesystem staging and atomic publication.
- Every artifact contains `.modelshelf/manifest.json` with its source, requested and resolved
  revision, content digest, and each file's path, size, and SHA-256.
- Published artifacts are immutable. Quick verification checks paths and sizes; full verification
  also checks SHA-256.
- The filesystem and manifests remain authoritative. A corrupt or incompatible SQLite index is
  preserved and rebuilt.
- ModelShelf provides ingestion and storage, not inference, RBAC, multi-tenancy, or scheduling.

```text
Admin UI / Go CLI ── HTTP API ── ingestion scheduler ── .staging ── artifacts/
                           └──── rebuildable SQLite index                 │
Go CLI ── NFS mount ───────────────────────────────────── NFS-Ganesha (RO)
```

## Docker Compose quick start

Requirements: Docker Compose, a Linux Docker host for NFS-Ganesha, and a client-reachable NFS TCP
port (2049 by default).

1. Create the environment file:

   ```bash
   cp .env.example .env
   ```

2. Generate the Web password hash. Both ModelShelf commands use Argon2id, hide interactive input,
   and require confirmation:

   ```bash
   uv sync --package modelshelf-server
   uv run modelshelf-server hash-password

   # The standalone client produces the same PHC string:
   modelshelf hash-password
   ```

   [OpenSSL 3.2+](https://docs.openssl.org/3.6/man7/EVP_KDF-ARGON2/) builds with ARGON2ID enabled can
   also compute it through `openssl kdf`. The KDF command returns only the derived key, so this
   example adds the parameters and salt required by the PHC format that ModelShelf accepts:

   ```bash
   password="$(openssl rand -base64 24 | tr -d '\n')"
   salt_padded="$(openssl rand -base64 16 | tr -d '\n')"
   salt="$(printf '%s' "$salt_padded" | tr -d '=')"
   salt_hex="$(printf '%s' "$salt_padded" | openssl base64 -d -A | od -An -tx1 | tr -d ' \n')"
   digest="$(openssl kdf -binary -keylen 32 \
     -kdfopt "pass:$password" -kdfopt "hexsalt:$salt_hex" \
     -kdfopt iter:3 -kdfopt memcost:65536 -kdfopt lanes:4 ARGON2ID |
     openssl base64 -A | tr -d '=')"
   printf 'Admin password: %s\n' "$password"
   printf "MODELSHELF_ADMIN_PASSWORD_HASH='\$argon2id\$v=19\$m=65536,t=3,p=4\$%s\$%s'\n" \
     "$salt" "$digest"
   unset password salt_padded salt salt_hex digest
   ```

   Keep the hash in single quotes in `.env`, because it contains `$`. Generate independent
   session and API secrets with:

   ```bash
   openssl rand -hex 32 # MODELSHELF_SESSION_SECRET
   openssl rand -hex 32 # one MODELSHELF_WRITE_TOKENS value
   ```

3. Set the NFS endpoint and allowed private client CIDRs in `.env`, then start the services:

   ```bash
   docker compose up --build -d
   ```

The UI is available at `http://localhost:8080`. Compose mounts one writable data volume at `/data`
in the server. NFS-Ganesha receives it read-only and exports only the server's `/data/artifacts`
(mounted there as `/export/artifacts`); SQLite, jobs, staging, and other metadata are not in the NFS
namespace.

### NFS settings

| Setting                          | Meaning                                                                     |
| -------------------------------- | --------------------------------------------------------------------------- |
| `MODELSHELF_NFS_PORT`            | Real Ganesha/system listener. Compose fixes the container listener to 2049. |
| `MODELSHELF_NFS_ADVERTISED_HOST` | Client-reachable host returned by `/api/v1/info`.                           |
| `MODELSHELF_NFS_ADVERTISED_PORT` | Optional client-facing port. Empty reuses the listener port.                |
| `MODELSHELF_NFS_CLIENTS`         | Comma-separated allowed CIDRs. Defaults should remain private.              |

With Compose, an advertised port also becomes the Docker host port mapped to container port 2049.
Public CIDRs require the explicit `MODELSHELF_NFS_ALLOW_PUBLIC=true` opt-in. For bare-metal
deployments, set `MODELSHELF_STORAGE_ROOT` and configure the system NFS server to export only its
`artifacts/` child read-only.

Artifact browsing and read-only catalog APIs are public by default. Set
`MODELSHELF_PUBLIC_ARTIFACTS=false` to require a Web session or bearer token. Task creation and all
management operations always require authentication.

## Ingestion

The task form searches model IDs, discovers revisions, runs an authenticated preflight, and shows
the resolved immutable revision, estimated size/file count, metadata, and source page before the
task can be saved. Manual IDs and revisions use the same validation.

Downloads expose transferred bytes, current/average speed, and ETA. They can be paused, resumed, or
cancelled. Concurrency is bounded by `MODELSHELF_MAX_CONCURRENT_DOWNLOADS` globally and
`MODELSHELF_MAX_CONCURRENT_DOWNLOADS_PER_SOURCE` per source.

Optional source-specific mirrors and the global HTTP(S) proxy are configured in `.env`. The UI
shows when routing is active and can bypass mirror and proxy independently for one task. ModelScope
CN and AI are separate sources with separate endpoints and tokens; neither is a mirror or auth
fallback for the other.

Generic HTTP uses a two-stage flow: the URL is downloaded to staging, metadata is inferred, and the
administrator explicitly chooses whether to extract before publication. URL text is not the
artifact identity; the downloaded content digest is.

For existing server-local files, configure `MODELSHELF_IMPORT_ROOTS` and run:

```bash
modelshelf-server import /srv/imports/Qwen-7B \
  --id team/Qwen-7B --name Qwen-7B --version v1
```

The import command copies through staging, rejects links/special files and paths outside the
allowlist, and deduplicates identical content. Archives are extracted only with `--extract`.

## Client CLI

Install the client version bundled with a self-hosted server:

```bash
curl -fsSL 'https://modelshelf.example/install.sh' | sh
```

Or install the newest GitHub release:

```bash
curl -fsSL https://raw.githubusercontent.com/modelshelf/modelshelf/main/packages/client/install.sh | sh
```

The installer selects Linux/macOS and amd64/arm64 automatically and verifies SHA-256. The default
destination is `/usr/local/bin`; override it with `MODELSHELF_INSTALL_DIR`. The GitHub installer
also accepts `MODELSHELF_VERSION=vX.Y.Z`.

The default configuration path is `~/.config/modelshelf/config.yml` (override with
`MODELSHELF_CONFIG`):

```yaml
schemaVersion: 1
serverUrl: http://modelshelf.internal:8080
nfsLocalPath: /mnt/modelshelf
localBasePath: /var/lib/modelshelf
writeToken: optional-server-write-token
models:
  - alias: mini-lm
    provider: huggingface
    id: sentence-transformers/all-MiniLM-L6-v2
    revision: main # optional; defaults to main
  - alias: qwen-7b
    provider: modelscope-cn
    id: Qwen/Qwen2.5-7B-Instruct
    revision: master
    path: runtime/qwen-2.5-7b
```

`sync` writes immutable resolutions to `config.lock.yml` without modifying the desired-state
config. Normal sync preserves the lock; `sync --update` refreshes moving revisions;
`sync --frozen-lockfile` rejects any required lock change. Aliases are unique references, but
multiple aliases can share one canonical model copy. `path` creates another symlink and never
changes where model bytes are stored.

```text
<localBasePath>/
├── .modelshelf/layout.json
├── models/<source>/<model-id...>/
│   ├── <resolved-revision>/
│   └── <requested-revision> -> <resolved-revision>/
└── aliases/<alias> -> ../models/.../<resolved-revision>/
```

Common commands:

```bash
modelshelf mount
modelshelf add huggingface sentence-transformers/all-MiniLM-L6-v2 --alias mini-lm
modelshelf sync [alias] [--update | --frozen-lockfile]
modelshelf list
modelshelf search <query>
modelshelf status <alias>
modelshelf verify [--full] [--unexpected] <alias-or-path>
modelshelf remove [-y] <alias>
modelshelf tui
modelshelf unmount
modelshelf upgrade [--check] [--github]
```

`status` exits `0` when ready, `2` when not ready, `3` when corrupt, and `4` when unavailable
or not configured. Linux mounting uses systemd NFSv4.2 automount and requires
`nfs-utils`/`nfs-common`, `systemd-escape`, and sudo. macOS uses `mount_nfs`.

See [packages/client/README.md](packages/client/README.md) for client-only build and distribution
details. The Web Integration page contains the same deployment-specific commands.

## Storage layout and schema

```text
data/
├── .modelshelf/storage.json
├── .modelshelf/catalog.sqlite3   # disposable index; never exported over NFS
├── .modelshelf/jobs/<task-id>.json
├── .incoming/
├── .staging/
└── artifacts/<source>/<model-id...>/<resolved-revision>/
    ├── .modelshelf/manifest.json
    └── ...model files
```

Only valid artifact directories below `artifacts/` are listed or exported. Hub IDs keep their
natural `vendor/model` hierarchy; unsafe path-segment characters are percent-escaped. Persistent
formats are independently versioned; see [schema migrations](docs/schema-migrations.md).

## Development

Requirements: Python 3.12+, Node 24, pnpm, and Go 1.25+.

```bash
uv sync --all-packages --all-extras
pnpm install

# Server
MODELSHELF_ADMIN_PASSWORD_HASH='...' MODELSHELF_WRITE_TOKENS=dev-token \
  uv run modelshelf-server

# UI, in another terminal
pnpm dev
```

Validation:

```bash
uv run ruff check .
uv run mypy
uv run pytest -q
pnpm typecheck
pnpm build
(cd packages/client && go test -race ./... && go vet ./...)
```

Additional documents:

- [Acceptance evidence](docs/acceptance.md)
- [Schema and migration policy](docs/schema-migrations.md)
- [Design decisions](docs/design-decisions.md)
