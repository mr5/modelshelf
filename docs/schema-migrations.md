# Schema and migration policy

ModelShelf versions each persisted format independently. There is no single application version
that is assumed to describe every file on disk. A writer always emits the current schema, readers
reject future schemas with an actionable “upgrade ModelShelf” error, and migrations proceed one
version at a time without skipping intermediate versions.

## Persistence classes

| State | Authority | Version marker | Upgrade policy |
| --- | --- | --- | --- |
| Artifact `.modelshelf/manifest.json` | Authoritative, immutable artifact metadata | JSON `schemaVersion` | Keep versioned readers. Never silently rewrite a published manifest. New artifacts use the current version. |
| Server storage layout | Authoritative directory contract | `.modelshelf/storage.json` | Validate before opening jobs or the index. A future version prevents startup. Layout migrations must be explicit, atomic, and run before serving. |
| Download jobs | Durable operational state | JSON `schemaVersion` in every `.modelshelf/jobs/*.json` | Migrate sequentially and atomically before scheduling. A future version prevents the older server from ignoring or corrupting work. |
| SQLite catalog index | Disposable projection of manifests | SQLite `PRAGMA user_version` | Do not maintain data migrations. Preserve the old database and its WAL/SHM files, create the current empty schema, then reconcile all manifests. |
| Client `config.yml` | User-authored desired state | YAML `schemaVersion` | Read supported versions but do not rewrite merely on load. User-triggered edits write the current schema. A future version fails closed. |
| Client `config.lock.yml` | Generated immutable resolution state | YAML `schemaVersion` | Migrate losslessly and atomically. `--frozen-lockfile` must fail if a rewrite is required. Never silently rebuild a future-version lock because that could repin a moving branch. |
| Client local layout | Canonical local cache and references | `<localBasePath>/.modelshelf/layout.json` | Validate on every client load and create atomically on first sync. A future version fails closed before touching model paths. |
| Client artifact `sync.json` | Disposable observation metadata | JSON `schemaVersion` | Ignore unsupported observation data and replace it on the next successful sync. It never determines artifact identity. |
| HTTP API | Network contract, not an on-disk migration | `/api/v1` | Evolve additively within v1; use a new API prefix for breaking changes. |

## Safety rules

1. Never start the scheduler or mutate client model paths until the relevant layout marker is understood.
2. Never downgrade a document written by a newer schema.
3. Authoritative migrations use a same-directory temporary file, `fsync`, atomic rename, and a preserved backup when the original cannot be reconstructed.
4. The SQLite index is rebuilt because manifests are its source of truth. Its preserved copy is for diagnosis, not rollback authority.
5. Published manifests are normalized in memory by version-specific readers. Payload identity and filesystem location do not change just because metadata syntax evolves.
6. A migration release must include fixtures for every supported source version, future-version rejection tests, interruption/atomicity tests, and downgrade tests.

## Version 1 baseline

The first public release establishes version 1 for manifest, task, server layout, client config,
client lock, client local layout, and sync observation documents. Pre-release task/config/lock files
that omitted the marker already had the v1 shape and are normalized to version 1. This exception is
only a pre-release bootstrap rule; after the first release, every schema change requires an explicit
`N -> N+1` migration.

The catalog SQLite schema also starts publicly at `user_version = 1`. Any pre-release database with
a different version, a corrupt database, or an unversioned non-empty schema is preserved and rebuilt
from manifests.
