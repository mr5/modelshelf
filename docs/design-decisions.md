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
