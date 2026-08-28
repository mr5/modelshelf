# ModelShelf client

The ModelShelf client is a standalone Go binary. It reads a declarative YAML file, discovers
immutable artifacts through the server HTTP API, and copies their files from the read-only NFS
export to local storage. It does not include model Hub SDKs and does not require Python or rsync.
There is no `modelshelf-client` Python package; install this binary with the shell installer, a
release archive, or build it with Go.

Build from source with Go 1.25 or newer:

```bash
go build -trimpath -o modelshelf ./cmd/modelshelf
./modelshelf --help
```

Release archives contain statically linked Linux and macOS binaries for amd64 and arm64. Install
the client that exactly matches a self-hosted server with:

```bash
curl -fsSL 'https://modelshelf.example/install.sh' | sh
```

Or install the latest GitHub release with:

```bash
curl -fsSL https://raw.githubusercontent.com/modelshelf/modelshelf/main/packages/client/install.sh | sh
```

The installer detects the platform and validates the archive SHA-256. Set
`MODELSHELF_INSTALL_DIR` to override `/usr/local/bin`, or `MODELSHELF_VERSION=vX.Y.Z` to pin a
GitHub release. Then create `~/.config/modelshelf/config.yml`; the root README contains a complete
configuration example. The default `localBasePath` is `~/.local/share/modelshelf`, with canonical
content below `models/` and alias symlinks below `aliases/`.

Each desired model may define an optional globally unique `alias`. Use `add --alias <alias>` to
set it, then pass the alias to `sync`, `status`, `remove`, or `verify` instead of repeating the
provider and model ID. Duplicate alias names are rejected, but multiple unique aliases may point to
the same exact model revision. They share one canonical copy under
`<localBasePath>/models/<source>/<vendor>/<model>/<resolved-revision>`; each alias is a symlink under
`<localBasePath>/aliases`. Multiple revisions of one `provider + id` are also allowed when each
entry has an alias.

A requested branch or tag is exposed as a sibling symlink to its locked immutable revision, for
example `.../model/main -> <resolved-revision>`. Normal `sync` preserves the lock and link target;
`sync --update` resolves the moving name again and atomically switches the link. Unsafe path
characters are percent-escaped, and a reference is never allowed to replace a real directory.

An optional model `path` creates another symlink instead of changing or duplicating the canonical
storage location. Relative paths are resolved below `localBasePath`; absolute paths are accepted.
Alias and path links update atomically when a branch or tag resolves to a new artifact.

The user configuration field is `revision` and defaults to `main`. `sync` stores immutable
resolutions in a generated lock file next to the config (`config.yml` → `config.lock.yml`) without
rewriting user configuration. Use `sync --update` to refresh branches/tags, or
`sync --frozen-lockfile` to require an exact existing lock.

Both files use `schemaVersion: 1`. The user-owned config is never silently rewritten merely because
it was read. The generated lock is migrated atomically when possible; an older CLI refuses a lock
written by a newer schema instead of silently resolving moving revisions again.

Generate a server-compatible Argon2id web password hash locally with:

```bash
modelshelf hash-password
```

The interactive password is hidden and confirmed. For automation, `--stdin` explicitly reads one
password line; plaintext password arguments are not accepted.

Upgrade to the client bundled with the configured ModelShelf server:

```bash
modelshelf upgrade --check
modelshelf upgrade
```

Use `modelshelf upgrade --github` for the latest GitHub release, or add `--version vX.Y.Z` to
select a release. Archives are checked against `checksums.txt`, the replacement binary must report
the expected semantic version, and publication uses an atomic same-directory rename. A development
build, reinstall, or downgrade requires `--force`. The executable directory must be writable.
