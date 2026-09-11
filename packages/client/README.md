# ModelShelf client

See the root [English README](../../README.md#client-cli) or
[中文 README](../../README.zh-CN.md#客户端-cli) for complete configuration and command examples.

The client is a standalone Go binary. It reads declarative YAML, queries the ModelShelf HTTP API,
and copies immutable artifacts from the read-only NFS mount to local storage. It does not contain
Hub SDKs and does not require Python or rsync.

## Install

Install the version bundled with a self-hosted server:

```bash
curl -fsSL 'https://modelshelf.example/install.sh' | sh
```

Install the latest GitHub release:

```bash
curl -fsSL https://raw.githubusercontent.com/mr5/modelshelf/main/packages/client/install.sh | sh
```

The installer supports Linux/macOS on amd64/arm64 and verifies SHA-256. Set
`MODELSHELF_INSTALL_DIR` to override `/usr/local/bin`; set `MODELSHELF_VERSION=vX.Y.Z` when using
GitHub to pin a release.

## Desired state and local storage

The default config is `~/.config/modelshelf/config.yml`; `MODELSHELF_CONFIG` overrides it.
`revision` is optional and defaults to `main`. Immutable resolutions are written to the generated
`config.lock.yml`, never back into user configuration.

`sync` preserves locked revisions, `sync --update` refreshes branches/tags, and
`sync --frozen-lockfile` rejects required lock changes. Config uses `schemaVersion: 3`; lock files remain at version 2. Older supported files migrate losslessly, while an older client refuses a future lock
instead of silently repinning a moving revision.

Optional `artifact` references one published artifact by alias (preferred) or immutable artifact
ID. The generated lock records the resolved artifact ID and selected files, so user configuration
and copied commands stay compact. Normal `sync` preserves that resolution; `sync --update`
resolves the reference again.

Optional `files` entries manually select source files when no published artifact reference is
available. The equivalent CLI form repeats `--file`; split GGUF variants must include every shard.
`artifact` and `files` are mutually exclusive.

Canonical bytes live once under:

```text
<localBasePath>/models/<source>/<model-id...>/<resolved-revision>
```

Unique aliases are symlinks below `<localBasePath>/aliases`. Multiple aliases may share one
canonical artifact. An optional `path` creates another symlink; it never changes or duplicates the
canonical storage location. Requested branch/tag names are sibling symlinks to their locked
immutable revision.

## Password hash and upgrade

```bash
modelshelf hash-password
modelshelf upgrade --check
modelshelf upgrade
```

`hash-password` emits the server-compatible Argon2id PHC string; `--stdin` is available for
automation. Upgrade uses the configured server by default. Add `--github` for GitHub Releases or
`--version vX.Y.Z` to select a release. Archives and reported binary versions are verified before
atomic replacement; reinstall, downgrade, and development builds require `--force`.

## Build

Go 1.25 or newer is required:

```bash
go build -trimpath -o modelshelf ./cmd/modelshelf
./modelshelf --help
```

Create all four release archives from the repository root with:

```bash
./scripts/package_client.sh
```

## Client distribution

A Linux client can publish completed models with `distribution.enabled: true` and an explicit
`distribution.allow` CIDR list. Followers only need `upstream.host`; `upstream.fallback` is false
unless explicitly enabled. Omitting upstream keeps the central server's NFS discovery behavior.

See [English deployment guide](../../docs/client-distribution.md) or
[中文部署指南](../../docs/client-distribution.zh-CN.md), including real NFS integration tests.

Both `upstream.port` and `distribution.port` are optional and default to 2049 (valid range
1–65535). The distribution service uses system-installed NFS-Ganesha; the CLI does not embed an
NFS server. For direct peer connections, configure the same port on publisher and follower.
