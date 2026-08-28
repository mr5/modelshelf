# Acceptance checklist

This document records reproducible v1 checks without repeating the user guide. The automated suite
was last run on 2026-08-29 with Python 3.12, Node 24, Go 1.27 (module baseline 1.25), Docker Compose
5.1, and NFS-Ganesha 4.3.

## Automated checks

```bash
uv run ruff check .
uv run mypy
uv run pytest -q
pnpm typecheck
pnpm build
(cd packages/client && go test -race ./... && go vet ./...)
./scripts/package_client.sh # Linux/macOS × amd64/arm64

docker compose --env-file .env.example config --quiet
docker build -t modelshelf:acceptance .
docker build -t modelshelf-nfs:acceptance docker/nfs
```

| Area           | Evidence covered by tests                                                                                          |
| -------------- | ------------------------------------------------------------------------------------------------------------------ |
| Storage        | Manifest authority, rebuildable SQLite, immutable collisions, deduplication, same-filesystem atomic publication    |
| Schema         | Independent v1 markers, future-version rejection, atomic task/lock migration, SQLite preservation and rebuild      |
| Providers      | Immutable revision resolution, CN/AI isolation, archive safety, Generic HTTP confirmation                          |
| Authentication | Argon2id Web sessions, separate bearer tokens, optional public artifact catalog                                    |
| Task UI/API    | Search, preflight, duplicate detection, progress/rates/ETA, pause/resume/cancel, SPA routing                       |
| Client         | Desired state and lock reconciliation, canonical storage plus symlinks, quick/full verification, stable exit codes |
| Distribution   | Four client platforms, SHA-256 installer validation, server/GitHub upgrades                                        |
| NFS            | Artifact-only read-only export, private-CIDR policy, no metadata/SQLite exposure                                   |

## Provider evidence

- Hugging Face `hf-internal-testing/tiny-random-bert`: `main` resolved to
  `f171d7baecaf37b5da5a3616d8833b9969753535`; all 10 files passed full and unexpected-file
  verification.
- ModelScope CN `apeganov/tiny-model-for-testing-downloading`: `master` resolved to
  `52a7cb1082bdfd14e5195e5722e0584fb271638e` and passed full verification.
- GitHub Releases: a local API contract server verified release lookup, id/tag resolution, streamed
  assets, and byte progress.
- Kaggle Models: the official `kagglehub.model_download` adapter returned `/versions/7`, which
  became immutable revision `version:7`.
- Generic HTTP: a real local server verified staging-only download, explicit confirmation, safe
  extraction, and content-addressed publication.

Private-provider acceptance requires the corresponding credentials and is not part of the
credential-free suite.

## NFS and client evidence

A privileged Ubuntu client container containing neither Python nor rsync mounted NFS-Ganesha with
`ro,vers=4.2,lookupcache=positive`. The exporter started before the artifact existed; after atomic
publication the client ran:

```text
modelshelf add huggingface hf-internal-testing/tiny-random-bert --alias tiny-bert
modelshelf list
modelshelf sync tiny-bert
modelshelf status tiny-bert
modelshelf verify --full --unexpected tiny-bert
modelshelf search tiny-random-bert
modelshelf remove -y tiny-bert
```

Ready-state commands exited `0`; a second sync was idempotent; hashes matched; removal deleted the
unreferenced local tree; post-removal status exited `4`. Starting the exporter with
`MODELSHELF_NFS_CLIENTS=0.0.0.0/0` without public opt-in exited `64` before Ganesha started.

## Reviewer workflow

1. Follow the Docker Compose quick start in the root README.
2. Create a Generic HTTP archive task and confirm that extraction waits for approval.
3. Review inferred metadata, choose extraction behavior, and publish.
4. Mount NFS and run `modelshelf add`, `status`, and `verify --full`.
5. Review compatibility-sensitive choices in `docs/design-decisions.md`.
