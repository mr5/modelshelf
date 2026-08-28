# ModelShelf

ModelShelf is a lightweight, centralized ingestion and immutable artifact store for machine-learning
models. Its source of truth is a POSIX filesystem plus a manifest in every artifact—not a database.
A disposable SQLite summary index accelerates catalog reads but can always be rebuilt from manifests.

The v1 implementation supports Hugging Face Hub, ModelScope, GitHub Releases, Kaggle Models, a
deliberately two-stage Generic HTTP workflow, and allowlisted server-local filesystem imports. A
separate NFS-Ganesha container exports published artifacts read-only over NFSv4.2; ModelShelf itself
only knows about the filesystem.

## Architecture

```text
React admin UI ─────┐
Go modelshelf CLI ──┼─ HTTP API ─ rebuildable SQLite summary index
                │          └── bounded ingestion scheduler ─ .staging ─ atomic rename ─ artifacts/
                └─ NFS info                                             └─ NFS-Ganesha (read-only)
```

Every artifact contains `.modelshelf/manifest.json` with provider/source, requested and resolved
revision, path, size and SHA-256 for every file, total size, file count and a content digest.
The generated JSON Schema is available at `schemas/manifest.schema.json`.
Persistent-format compatibility and upgrade rules are documented in
[`docs/schema-migrations.md`](docs/schema-migrations.md).

## Quick start with Docker Compose

Requirements: Docker with Compose, a Linux Docker host for the NFS exporter, and the configured
NFS TCP port (2049 by default) reachable from clients.

1. Generate an Argon2id password hash:

   ```bash
   # Python server CLI, when working from a source checkout:
   uv sync --package modelshelf-server
   uv run modelshelf-server hash-password

   # Or use the standalone Go client from any supported machine:
   modelshelf hash-password
   ```

   Both commands hide interactive input, require confirmation, and emit a server-compatible PHC
   string. Automation may explicitly pipe one password line with `--stdin`; plaintext command-line
   arguments are intentionally unsupported.

2. Copy `.env.example` to `.env`, set the hash (keep its single quotes so Compose treats `$`
   literally), a random session secret, at least one CLI write token, the externally reachable NFS
   endpoint, and private client CIDRs. The NFS settings deliberately distinguish the real listener
   from the endpoint given to clients:

   | Setting | Meaning |
   | --- | --- |
   | `MODELSHELF_NFS_PORT` | Real Ganesha/system NFS listen port. Compose fixes it to container port 2049. |
   | `MODELSHELF_NFS_ADVERTISED_HOST` | Client-reachable host returned by `/api/v1/info`. |
   | `MODELSHELF_NFS_ADVERTISED_PORT` | Optional client-facing port override; empty reuses `MODELSHELF_NFS_PORT`. |

   With Compose, an explicit advertised port is also the Docker host port mapped to container 2049.
   Thus an empty value maps and advertises 2049, while `32049` produces `32049:2049` and tells
   clients to connect to port 32049.

3. Start both isolated services:

   ```bash
   docker compose up --build -d
   ```

The management UI is served at `http://localhost:8080`. The data volume is writable only in the
server container and mounted read-only at `/export` in the NFS container. Public CIDRs are rejected
unless `MODELSHELF_NFS_ALLOW_PUBLIC=true` is also explicitly set.

For a bare-metal deployment, configure the real listen port in Ganesha (for example,
`NFS_Core_Param.NFS_Port`) or the selected system NFS server and set `MODELSHELF_NFS_PORT` to the
same value. Set `MODELSHELF_NFS_ADVERTISED_HOST` to the client-reachable host. Leave the advertised
port empty for direct access; set it only when NAT or port forwarding gives clients a different
port. ModelShelf computes the client endpoint as `advertised host:(advertised port or listen port)`.

The `/artifacts` page and read-only artifact catalog API are anonymously accessible by default so
clients can discover published models and copy the matching installer without a Web login. Set
`MODELSHELF_PUBLIC_ARTIFACTS=false` to require a valid Web session or CLI bearer token for artifact
list, search, and detail requests. Downloads and every management operation always require
authentication regardless of this setting.

When creating a task, the UI searches the selected Hub for model IDs after two characters are
entered. Once an ID is complete, it loads that model's branches, tags, releases, or versions and
selects the Hub default revision. Both fields remain editable, so private/unlisted IDs and explicit
commits can always be entered manually. These discovery APIs require the same web session or write
token as task creation and use short-lived in-memory caches only; they are not a source of truth.
After either an automatic or manual revision is selected, ModelShelf runs a provider preflight that
validates access and downloadability before enabling task creation. The UI shows provider-reported
total size and file count when available, the resolved immutable revision, useful source metadata,
and a canonical Hub page link. Task creation repeats the same cached validation server-side.

Download task state records transferred and total bytes, an exponentially smoothed current rate,
active-time average rate, and ETA. The UI shows these values on both the queue and task detail pages;
paused time is excluded from the average and the instantaneous rate becomes zero while paused.
The scheduler runs up to `MODELSHELF_MAX_CONCURRENT_DOWNLOADS` tasks globally (default 2), while
`MODELSHELF_MAX_CONCURRENT_DOWNLOADS_PER_SOURCE` caps simultaneous tasks for any one source
(default 1). Both limits must be satisfied. A blocked same-source task does not prevent a queued task
from another source from using a free global slot. These task limits are separate from concurrency
inside a provider SDK, which may download several files for one task in parallel.

Optional outbound routing is configured with `MODELSHELF_HUGGINGFACE_MIRROR`,
`MODELSHELF_MODELSCOPE_CN_MIRROR`, `MODELSHELF_MODELSCOPE_AI_MIRROR`, and
`MODELSHELF_HTTP_PROXY`. The HTTP(S) proxy applies to every provider; mirrors apply only to their
named source. The task form indicates active routing without exposing proxy credentials and offers
independent “bypass mirror” and “bypass proxy” controls. These choices are persisted in the task and
shared by preflight and the actual download. SDK providers use an isolated direct worker when
bypassing a configured proxy, so concurrent operations keep their normal routing. All SDK downloads
run in a supervised subprocess so pause and cancellation stop the actual provider process rather
than only the surrounding async task. Pausing preserves task staging and provider caches for resume;
cancelling removes staging. Verification and atomic publication are intentionally too short and
consistency-sensitive to pause. Reported download sizes include both binary units and decimal GB.

ModelScope CN (`modelscope-cn`) and ModelScope AI (`modelscope-ai`) are separate sources, not mirrors
of one another. Their account systems issue site-scoped `MODELSCOPE_CN_API_TOKEN` and
`MODELSCOPE_AI_API_TOKEN` credentials; a token from one site is never sent to the other. Public Git
revision resolution is attempted anonymously first so a stale token cannot block public models.

Catalog list, search and detail lookups use a disposable SQLite summary index. Manifests remain the
only artifact source of truth: publication completes with the filesystem rename first, then performs
a best-effort index upsert. Startup reconciles missing/stale rows from manifests, removes rows whose
artifacts disappeared, and preserves then rebuilds a corrupt or incompatible database. The index
does not store `relativePath`; that path is deterministically generated from provider, source ID and
resolved revision. Its location is fixed at
`<MODELSHELF_STORAGE_ROOT>/.modelshelf/catalog.sqlite3`; it has no separate configuration because it
belongs to the shelf metadata for that storage root. The NFS exporter exposes only the sibling
`artifacts/` directory, never `.modelshelf/`, staging, incoming files, or task state.
The artifact API accepts optional `limit` and `offset` parameters, and the UI loads 48 cards at a
time. Existing clients that omit pagination remain compatible.

## Local development

The HTTP server and ingestion providers are Python 3.12+, the admin UI is React/TypeScript on Node
24, and the client CLI is Go 1.25+. The Python workspace contains only `modelshelf-core` and
`modelshelf-server`; the client is not a Python package.

```bash
# Python server/core dependencies
uv sync --all-packages --all-extras

# React UI dependencies
pnpm install

# Terminal 1: Python HTTP server
MODELSHELF_ADMIN_PASSWORD_HASH='...' \
MODELSHELF_WRITE_TOKENS='dev-token' \
uv run modelshelf-server

# Terminal 2: React development server
pnpm --filter @modelshelf/ui dev
```

For a source checkout that should also serve local client packages, run
`./scripts/package_client.sh` and set `MODELSHELF_CLIENT_DIST=packages/client/dist` on the server.

Build and validate:

```bash
# Python server and filesystem core
uv run ruff check .
uv run mypy
uv run pytest

# React admin UI
pnpm typecheck
pnpm build

# Go client CLI
cd packages/client
go test -race ./...
go vet ./...
```

## Client configuration

The default config path is `~/.config/modelshelf/config.yml`; override it with
`MODELSHELF_CONFIG`. The default `localBasePath` is `~/.local/share/modelshelf`.

```yaml
schemaVersion: 1
serverUrl: http://modelshelf.internal:8080
nfsLocalPath: /mnt/modelshelf
localBasePath: /var/lib/modelshelf
writeToken: a-server-configured-write-token
models:
  - alias: llama-1b
    provider: huggingface
    id: meta-llama/Llama-3.2-1B-Instruct
    revision: main # optional; branch, tag, or immutable commit
  - alias: qwen-7b
    provider: modelscope-cn
    id: Qwen/Qwen2.5-7B-Instruct
    revision: master
    path: runtime/qwen-2.5-7b
```

Every non-empty `alias` must be globally unique. Multiple aliases may declare the same exact
`provider + id + revision`; they resolve to one immutable local artifact and do not duplicate model
bytes. The same `provider + id` may also appear at multiple revisions when every entry has an alias.
`sync` atomically maintains `config.lock.yml` beside the user-managed config. Use `sync --update`
to refresh moving branches/tags and `sync --frozen-lockfile` to reject any config/lock difference.

Local content and references are deliberately separate:

```text
/var/lib/modelshelf/
├── .modelshelf/layout.json             # local directory-layout version
├── models/<source>/<vendor>/<model>/
│   ├── <resolved-revision>/            # one canonical immutable copy
│   └── <requested-revision> -> <resolved-revision>/
└── aliases/<alias> -> ../models/.../<resolved-revision>/
```

An optional `path` is another symbolic-link reference, relative to `localBasePath` or absolute; the
model is never copied into that path. Alias and path links are atomically switched when a moving
revision resolves to a different artifact. A requested branch or tag such as `main` is also exposed
beside the immutable revision directory and follows the lock; only `sync --update` moves it. Removing
one declaration removes only its exclusive references; shared requested-revision links and canonical
content remain while another lock entry still uses them.

The `modelshelf` client is a standalone Go binary with no Python, rsync, or Hub SDK dependency.
Do not install it with `pip`, `pipx`, or `uv`; use the installer, a release archive, or `go build`.
Every server
image bundles the client built from the same source for Linux/macOS on amd64/arm64. The public
Integration page documents direct NFS and client CLI usage, and shows the matching one-line
installer with a copy button:

```bash
curl -fsSL 'https://modelshelf.internal/install.sh' | sh
```

The server-rendered installer downloads its matching bundled client, selects the operating system
and architecture automatically, and verifies the archive against SHA-256 checksums before
installing. To install the newest published client directly from GitHub instead:

```bash
curl -fsSL https://raw.githubusercontent.com/modelshelf/modelshelf/main/packages/client/install.sh | sh
```

The default destination is `/usr/local/bin/modelshelf`; set `MODELSHELF_INSTALL_DIR` to use another
directory. The GitHub installer accepts `MODELSHELF_VERSION=vX.Y.Z` to pin a release. Plain Linux
and macOS archives use stable names such as `modelshelf_linux_amd64.tar.gz` and remain available on
GitHub Releases for manual installation. To build the current source instead:

```bash
cd packages/client
go build -trimpath -o modelshelf ./cmd/modelshelf
sudo install -m 0755 modelshelf /usr/local/bin/modelshelf
```

On Linux, `mount` manages a systemd NFSv4.2 automount and requires `nfs-utils`/`nfs-common`,
`systemd-escape`, and sudo access. macOS uses the system `mount_nfs` command. Then:

```bash
modelshelf mount
modelshelf add huggingface sentence-transformers/all-MiniLM-L6-v2 --revision main --alias mini-lm
modelshelf sync mini-lm
modelshelf list
modelshelf status mini-lm
modelshelf verify mini-lm
modelshelf verify --full mini-lm
modelshelf tui
modelshelf upgrade --check
modelshelf upgrade
```

`status` exit codes are stable: `0` desired state satisfied, `2` not ready, `3` corrupt, and `4`
unavailable/not configured.

`upgrade` obtains the matching client distribution from the configured ModelShelf server by
default. Use `upgrade --check` to compare versions without changing the executable, or
`upgrade --github` for the latest GitHub release (`--version vX.Y.Z` pins one). It verifies the
published archive SHA-256 and the downloaded binary's reported version, then replaces the current
executable atomically. Development builds, reinstalls, and explicit downgrades require `--force`.
The executable directory must be writable; otherwise rerun with sufficient privileges and pass an
explicit `--config` path when needed.

`sync` is an idempotent reconcile. Its native Go copier writes into the same-filesystem
`models/.staging` directory, runs quick verification, then publishes with atomic rename or atomic directory exchange.
Quick verification checks manifest structure, every expected path and size. `verify --full`
additionally hashes every file; `--unexpected` also reports files absent from the manifest.

## Generic HTTP workflow

Generic URLs are not immutable identities. A submitted URL is downloaded only into a task staging
directory. The UI clearly pauses at `awaiting_confirmation` and shows inferred name, version, format,
archive structure and confidence. No archive is silently extracted. After the administrator edits or
accepts metadata and chooses whether to extract, ModelShelf validates archive paths/member types,
creates the manifest, and atomically publishes an artifact identified by the actual content SHA-256.

## Server-local filesystem import

Operators can copy an existing model directory or file into the shelf without sending it through a
Hub or HTTP upload. Direct writes to `artifacts/` are never allowed. Configure one or more read-only
source roots, then run the synchronous server command:

```bash
MODELSHELF_IMPORT_ROOTS=/srv/model-imports \
modelshelf-server import /srv/model-imports/Qwen-7B \
  --id team/Qwen-7B \
  --name Qwen-7B \
  --version v1
```

The built-in `data/.incoming/` directory is always an allowed source root. In Docker, mount an
operator-controlled directory read-only and set `MODELSHELF_IMPORT_ROOTS=/imports`; then use
`docker compose exec server modelshelf-server import /imports/<model>`. Files and directories are
copied into same-filesystem staging, hashed, assigned a `filesystem` source with
`requestedRevision=content` and `resolvedRevision=sha256:<content-digest>`, manifested, frozen, and
atomically published. The source is never modified. Re-importing identical content is deduplicated;
changed content creates a new immutable revision. Symbolic links, special files, reserved
`.modelshelf` paths, storage-internal sources, and paths outside the allowlist are rejected.

Archive files remain archives unless `--extract` is explicitly supplied. Supported zip/tar archives
use the same traversal, link, and device protections as Generic HTTP extraction. The command emits a
machine-readable JSON result containing the artifact ID, digest, size, file count, relative path and
deduplication status.

## Filesystem layout

```text
data/
  .modelshelf/storage.json    # server storage-layout version
  .modelshelf/catalog.sqlite3 # disposable, rebuildable summary index
  .modelshelf/jobs/<task-id>.json
  .incoming/<operator-copied-model>/...
  .staging/<task-id>/...
  artifacts/<provider>/<source namespace...>/<resolved-revision>/
    .modelshelf/manifest.json
    ...model files
```

Only directories with a valid manifest under `artifacts/` are returned by the read API or exported.
Hub source IDs retain their natural hierarchy, for example
`modelscope-cn/openai-mirror/whisper-large-v3-turbo/<resolved-revision>/`. Only characters that are
unsafe inside a path segment are percent-escaped. Generic HTTP URLs remain one escaped source
segment because URL slashes are not a vendor/model namespace.
Artifacts are immutable; publishing different content to the same provider/source/resolved revision
is rejected as a collision.

## Scope

ModelShelf intentionally does not implement inference, scheduling, RBAC, multi-tenancy or a custom
storage abstraction/NFS protocol. Inference nodes normally mount the central export read-only and
reconcile selected artifacts to local NVMe before starting their runtime.

See [docs/acceptance.md](docs/acceptance.md) for the requirement/evidence matrix and
[docs/open-questions.md](docs/open-questions.md) for provisional choices to review before a stable
release.
