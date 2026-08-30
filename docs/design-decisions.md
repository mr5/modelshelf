# Design decisions

This document records v1 choices that are important for compatibility but are not obvious from the
public interface.

## Client distribution

The client is a standalone Go binary for Linux/macOS on amd64/arm64. It consumes only ModelShelf's
HTTP API, manifest, and NFS layout, so inference nodes do not need Python, rsync, or Hub SDKs.

## Revision and local path semantics

User config contains an optional `revision`, never a resolved commit observed by the client.
`config.lock.yml` holds immutable resolutions. Normal sync preserves the lock; `--update`
refreshes moving revisions; `--frozen-lockfile` rejects differences.

Canonical model directories are named by resolved revision. Requested revisions, aliases, and
custom paths are symlinks. Multiple references share one canonical copy.

## Provider identity

- Hugging Face and ModelScope branches/tags resolve to Git commits.
- Kaggle uses its numeric model version.
- GitHub combines the release id/tag with the downloaded-assets content SHA-256 because tags and
  assets can be edited.
- Generic HTTP and filesystem imports use downloaded/imported content SHA-256.

ModelScope CN and AI are independent sources with fixed official endpoints, separate optional
mirrors, and separate site-scoped tokens. A token is never sent to the other site.

## Ingestion verification

Manifest SHA-256 inventory runs outside the HTTP event loop. Verification is globally bounded to
one artifact at a time and hashes two files concurrently, keeping API and health endpoints
responsive without creating unbounded disk concurrency. Progress, disk throughput, and ETA are
reported separately from download metrics.

For ModelScope Git LFS downloads, pointer OIDs are captured before LFS materialization. Newly
downloaded files are hashed against those expected SHA-256 values. Files reused from an immutable
artifact inherit the earlier manifest entry only after path, size, and SHA-256 all match; ingestion
still inventories every path and size but does not reread trusted content. This preserves a complete
manifest while making verification proportional to the changed content. Explicit `verify --full`
remains the full-content integrity audit.

When another ModelScope artifact for the same source exists, ingestion matches each new LFS pointer
against path, size, and SHA-256 entries from every earlier full or partial manifest. Matching files
are materialized in staging with hardlink, reflink, then local-copy fallback; only unmatched LFS
paths are fetched. Reuse is written to a temporary sibling and atomically replaces the pointer, so
concurrent artifact deletion safely falls back to downloading. The ordinary-copy fallback hashes
content during the copy; reflinks and hardlinks retain the trusted manifest hash without another
read. Publication still creates a distinct immutable resolved-revision artifact. Artifact deletion
only makes directories writable before unlinking files, so hardlinked files in other revisions
retain read-only inode metadata.

## Read authentication

Artifact browsing, catalog reads, server information, and client distribution are public by
default. `MODELSHELF_PUBLIC_ARTIFACTS=false` protects artifact metadata with a Web session or bearer
token. Task creation and management are always authenticated.

## NFS portability

Linux systemd mount/automount is the production client path. macOS uses `mount_nfs` for development.
Unsupported platforms fail rather than guessing privileged commands.

## Atomic client replacement

Initial publication uses atomic rename. Replacing existing content uses Linux
`renameat2(RENAME_EXCHANGE)` or macOS `renamex_np(RENAME_SWAP)`. If the filesystem cannot exchange
directories atomically, sync fails instead of exposing a partially replaced model.
