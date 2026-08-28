# Acceptance checklist

This checklist maps the v1 product constraints to executable evidence. It was last run on
2026-08-27 against Python 3.12, Node 24, Go 1.27 (module baseline 1.25), Docker Compose 5.1,
and NFS-Ganesha 4.3.

## Automated checks

```bash
uv run ruff check .
uv run mypy
uv run pytest -q
pnpm --filter @modelshelf/ui typecheck
pnpm --filter @modelshelf/ui build
cd packages/client
go test -race ./...
go vet ./...
CGO_ENABLED=0 GOOS=linux GOARCH=amd64 go build ./cmd/modelshelf
CGO_ENABLED=0 GOOS=linux GOARCH=arm64 go build ./cmd/modelshelf
CGO_ENABLED=0 GOOS=darwin GOARCH=amd64 go build ./cmd/modelshelf
CGO_ENABLED=0 GOOS=darwin GOARCH=arm64 go build ./cmd/modelshelf
cd ../..
docker compose --env-file .env.example config --quiet
docker build -t modelshelf:acceptance .
docker build -t modelshelf-nfs:acceptance docker/nfs
```

The test suite covers manifest creation and validation, quick/full verification, immutable
collision and deduplication, native Go client sync and atomic exchange including read-only trees, archive inference
and traversal rejection, Generic HTTP's confirmation gate, durable pause/resume/cancel task control,
environment parsing,
ModelScope CN/AI source isolation and ref selection, Kaggle version resolution, GitHub release
asset downloads, and the
platform-detecting client installer with SHA-256 verification.

## Requirement matrix

| Area | Acceptance evidence |
| --- | --- |
| Filesystem is the source of truth | Published manifests and durable task JSON remain authoritative. SQLite contains only rebuildable artifact summaries, is reconciled from manifests at startup, and stores no artifact relative path. |
| Immutable identity | Hugging Face and both ModelScope sources resolve branches/tags to commits; Kaggle resolves a numeric model version; GitHub combines release id/tag with downloaded-content SHA-256; Generic HTTP uses downloaded-content SHA-256. |
| Atomic ingestion | Provider output is written below `data/.staging`; manifest validation completes before a same-filesystem `rename`; collisions never overwrite published content. |
| Immutable artifacts | Published roots/directories are `0555`, files are `0444`, and the NFS volume is separately mounted read-only. |
| Manifest | Every artifact has `.modelshelf/manifest.json` with source, requested/resolved revision, canonical content digest, every path/size/SHA-256, totals, and counts. The JSON Schema is generated at `schemas/manifest.schema.json`. |
| Verification | Quick verification checks every expected path and size plus manifest invariants; full verification adds each SHA-256; `--unexpected` checks extra files. |
| Generic HTTP | Creation returns an explicit warning; download stops at `awaiting_confirmation`; metadata inference inspects URL/Content-Disposition/name/archive root/README/config; extraction is opt-in and traversal/link/device-safe; confirmation creates and atomically publishes the final artifact. |
| Filesystem import | `modelshelf-server import` accepts only allowlisted server-local files/directories, copies through same-filesystem staging, rejects links/special/reserved paths, uses a content-addressed immutable revision, and atomically publishes or deduplicates without modifying the source. Archive extraction is explicit. |
| Authentication | Web login accepts only an Argon2id hash and creates an HttpOnly/SameSite=Strict signed session cookie. CLI write tokens are separate bearer tokens. The Artifacts page and read-only catalog API are anonymous by default; `MODELSHELF_PUBLIC_ARTIFACTS=false` protects both with the same session/bearer validation while management APIs remain protected in either mode. |
| Outbound routing | Hugging Face, ModelScope CN, and ModelScope AI accept source-specific mirror endpoints; one HTTP(S) proxy can cover every provider. The two ModelScope sites are independent sources with separate site-scoped token variables, never mirrors or authentication fallbacks for one another. The UI reports configured routing without proxy credentials and persists independent per-task mirror/proxy bypass flags. Preflight and download share the flags; SDK proxy bypass runs in an isolated direct worker so concurrent requests are unaffected. |
| Management UI | URL-addressable React routes cover login, task list, task creation, task progress/detail/confirmation, pause/resume/cancel controls, and artifact search. Task progress exposes transferred/total bytes, a smoothed instantaneous rate, active-time average rate, and ETA. Pause terminates the supervised provider process while retaining staging/cache state; resume reconciles from that state; cancel terminates work and deletes staging. Task creation searches model IDs and discovers provider branches/tags/releases/versions with debouncing, short-lived server caching, automatic provider defaults, and unrestricted manual fallback. Automatic and manual revisions both run an authenticated provider preflight; the task button remains disabled until availability is verified, and the UI displays estimated size/file count, immutable revision, useful metadata, and a canonical Hub link. The Artifacts page exposes the current server's client installer as a copyable curl command. The API repeats preflight before saving a task. Production SPA fallback and assets were exercised from the server image. |
| CLI desired state | The standalone Go binary supports YAML config, `add`, `remove`, `search`, `sync`, `list`, `status`, `mount`, `unmount`, `verify`, and `tui`. Its native copier uses a same-parent `.staging` directory, verifies, and atomically renames/exchanges without Python or rsync. The server image bundles checksummed Linux/macOS amd64/arm64 packages from the same source and serves a source-pinned installer; the repository installer resolves the latest or an explicitly pinned GitHub release. |
| Stable status | Exit `0` means ready, `2` not ready, `3` corrupt, and `4` unavailable/not configured. Missing local content reports matching server task progress when a write token is configured. |
| TUI | Server and local rows can be searched together and show revision, file count, size, and observed state; local browsing remains available if the server is unavailable. |
| NFS boundary | A separate NFS-Ganesha container exports only `artifacts/` as NFSv4.2/RO. It starts with a read-only root filesystem and only `DAC_READ_SEARCH`, `NET_BIND_SERVICE`, `SETGID`, `SETUID`, and `SYS_ADMIN`. Attribute/directory caching is disabled because the server publishes artifacts out-of-band via atomic rename. |
| NFS network policy | Defaults contain private RFC1918/ULA CIDRs and reject wildcard/public CIDRs unless `MODELSHELF_NFS_ALLOW_PUBLIC=true`. |

## Live provider evidence

- Hugging Face: `hf-internal-testing/tiny-random-bert` requested as `main` resolved to commit
  `f171d7baecaf37b5da5a3616d8833b9969753535`; 10 files were published and passed full SHA-256
  plus unexpected-file verification.
- ModelScope CN: `apeganov/tiny-model-for-testing-downloading` requested as `master` resolved to commit
  `52a7cb1082bdfd14e5195e5722e0584fb271638e`; the published artifact passed full verification.
- GitHub Releases: a local GitHub API contract server verified release lookup, release id/tag
  resolution, streamed asset download, and byte progress without consuming external rate limits.
- Kaggle Models: an official `kagglehub.model_download` adapter test verified the returned
  `/versions/7` path becomes immutable `version:7` and only the model payload is materialized.
- Generic HTTP: a real local HTTP server served an archive; the API remained empty before explicit
  confirmation, then safely extracted and published it with a content-addressed revision.

External private-provider acceptance requires supplying the corresponding credentials and is not
part of the repository's credential-free test run.

## NFS and client end-to-end evidence

An independent privileged Ubuntu client container containing neither Python nor rsync mounted the
NFS-Ganesha export using `ro,vers=4.2,lookupcache=positive`. The exporter was started before the
artifact existed; after the server atomically published the real Hugging Face artifact, the Go
client executed:

```text
modelshelf add huggingface hf-internal-testing/tiny-random-bert
modelshelf list
modelshelf sync
modelshelf status huggingface hf-internal-testing/tiny-random-bert
modelshelf verify --full --unexpected /models/huggingface--hf-internal-testing--tiny-random-bert
modelshelf search tiny-random-bert
modelshelf remove -y huggingface hf-internal-testing/tiny-random-bert
```

All ready-state commands exited `0`; a second sync was idempotent, full hashes matched, removal
deleted the local tree, and post-removal `status` exited `4`. The local sync state recorded the
immutable artifact id, server URL, and sync timestamp. Starting the exporter with
`MODELSHELF_NFS_CLIENTS=0.0.0.0/0` and no public opt-in exited `64` before Ganesha started.

## Reviewer workflow

1. Follow the Docker Compose quick start in the root README.
2. Log in, submit a Generic HTTP zip/tar URL, and confirm that the UI pauses before extraction.
3. Inspect the inferred metadata and choose whether to extract, then confirm publication.
4. Mount the advertised NFS export from a client and run `modelshelf add`, `status`, and
   `verify --full`.
5. Review provisional product choices in `docs/open-questions.md`; none changes the v1 filesystem
   or immutable-artifact invariants.
