# Open questions and provisional decisions

This file records product choices that are not uniquely determined by the initial specification.
Implementation continues with reversible defaults; these items can be reviewed together before a
stable release.

## Q1: Client distribution format

- Decision implemented: the client is a standalone Go binary, released for Linux/macOS and
  amd64/arm64 with SHA-256 checksums.
- Reason: the client only consumes ModelShelf's HTTP catalog and NFS artifact layout; it does not
  talk to model Hubs. A static binary removes Python, rsync, and Hub SDK installation requirements
  from inference nodes while retaining independent manifest verification.

## Q2: Artifact selection when a model has multiple revisions

- Decision implemented: the user-owned YAML accepts only `revision`, which is optional and defaults
  to `main`. It may contain a branch, tag, version, or immutable commit, but never client-observed
  `resolvedRevision` state.
- The Go client writes immutable resolutions to the generated `config.lock.yml`. Normal `sync`
  reconciles config additions, removals, and edits into that lock; `sync --update` deliberately
  refreshes moving revisions, while `sync --frozen-lockfile` rejects any config/lock difference.
- Multiple revisions of the same `provider + id` require unique aliases. Multiple unique aliases
  may also declare the exact same `provider + id + revision`; they share one canonical local copy
  and differ only in their symlink references.
- The local canonical directory is named by immutable resolved revision. A requested branch/tag is
  a sibling symlink to that directory and moves only when its lock resolution changes. It is a
  convenience reference, never artifact identity or lock state.

## Q3: GitHub Releases immutable identity

- Decision implemented for v1: resolve a tag or `latest` to GitHub's numeric release id plus tag,
  then append the downloaded release-assets content SHA-256 to the resolved revision.
- Reason: Git tags and release assets can be edited; the release id improves traceability while the
  content digest makes the final artifact identity immutable even if assets are later replaced.

## Q4: NFS mounting portability

- Provisional decision: support Linux systemd mount/automount as the production path and direct
  `mount`/`umount` commands on macOS for development.
- The Go CLI refuses unsupported platforms instead of guessing privileged commands.

## Q5: Read API authentication

- Decision implemented: the Artifacts page, artifact search, and manifest retrieval are anonymous by
  default. Operators can set `MODELSHELF_PUBLIC_ARTIFACTS=false` to require a write token or Web
  session for those endpoints. Server/client-distribution information and the installer remain
  public; task creation and management always require authentication.
- This keeps the default lightweight for read-only model discovery while allowing installations
  that expose the HTTP service beyond a trusted network to close catalog access.

## Q6: Atomic replacement on client platforms

- Provisional decision: initial publication uses atomic rename. Updating an existing destination uses
  Linux `renameat2(RENAME_EXCHANGE)` or macOS `renamex_np(RENAME_SWAP)` and then deletes the swapped-out
  directory. If the filesystem does not support atomic exchange, sync fails instead of exposing a
  partially replaced directory.
- Alternative: always use revision-addressed local directories plus a separately managed pointer.

## Q7: ModelScope sites and immutable revision resolution

- Decision implemented for v1: expose `modelscope-cn` and `modelscope-ai` as separate sources with
  fixed official endpoints, independent optional mirrors, and independent site-scoped tokens.
  Validate the requested revision with the official `modelscope-hub` SDK, resolve its Git branch/tag
  to a commit using `git ls-remote`, then pass that exact commit back to the SDK downloader.
- Reason: as of `modelscope-hub 0.2.0`, `get_valid_revision_detail(..., "master")` returns the mutable
  string `master` rather than a commit. Publishing it as a resolved revision would violate the core
  artifact identity invariant.
- Private repositories use the documented `oauth2:<token>` Git credential through an ephemeral Git
  HTTP header; the token is not included in the remote URL or error messages.
