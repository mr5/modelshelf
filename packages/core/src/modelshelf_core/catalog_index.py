from __future__ import annotations

import json
import os
import sqlite3
import time
from collections.abc import Iterable
from contextlib import closing
from pathlib import Path

from .identity import artifact_relative_path
from .models import ArtifactSummary, Provider

SCHEMA_VERSION = 3


class CatalogIndexVersionError(RuntimeError):
    pass


class CatalogIndex:
    """Disposable SQLite index for artifact summaries.

    Manifests remain the source of truth. This database may be deleted and rebuilt at any time.
    """

    def __init__(self, path: Path) -> None:
        self.path = path

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=5)
        try:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA busy_timeout = 5000")
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute("PRAGMA synchronous = NORMAL")
        except Exception:
            connection.close()
            raise
        return connection

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self._initialize_schema()
        except (sqlite3.DatabaseError, CatalogIndexVersionError):
            self.preserve_and_recreate()

    def _initialize_schema(self) -> None:
        with closing(self._connect()) as connection, connection:
            version = int(connection.execute("PRAGMA user_version").fetchone()[0])
            user_tables = {
                str(row[0])
                for row in connection.execute(
                    "SELECT name FROM sqlite_master "
                    "WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
                )
            }
            if version == 0 and user_tables:
                raise CatalogIndexVersionError(
                    "unversioned non-empty catalog index cannot be trusted"
                )
            if version not in {0, SCHEMA_VERSION}:
                raise CatalogIndexVersionError(
                    f"unsupported catalog index schema version {version}"
                )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS artifacts (
                    artifact_id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    alias TEXT,
                    version TEXT NOT NULL,
                    provider TEXT NOT NULL,
                    source_id TEXT NOT NULL,
                    requested_revision TEXT NOT NULL,
                    resolved_revision TEXT NOT NULL,
                    selection_digest TEXT,
                    selected_paths_json TEXT,
                    total_size INTEGER NOT NULL,
                    file_count INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    search_text TEXT NOT NULL,
                    manifest_mtime_ns INTEGER NOT NULL,
                    manifest_size INTEGER NOT NULL
                )
                """
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS artifacts_created_at "
                "ON artifacts(created_at DESC, artifact_id)"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS artifacts_source "
                "ON artifacts(provider, source_id, resolved_revision)"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS artifacts_provider_created "
                "ON artifacts(provider, created_at DESC, artifact_id)"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS artifacts_size ON artifacts(total_size, artifact_id)"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS artifacts_name "
                "ON artifacts(name COLLATE NOCASE, artifact_id)"
            )
            connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")

    def preserve_and_recreate(self) -> Path | None:
        preserved: Path | None = None
        suffix = f".invalid-{time.time_ns()}"
        for candidate in (self.path, Path(f"{self.path}-wal"), Path(f"{self.path}-shm")):
            if not candidate.exists():
                continue
            destination = candidate.with_name(candidate.name + suffix)
            os.replace(candidate, destination)
            if candidate == self.path:
                preserved = destination
        self._initialize_schema()
        return preserved

    @staticmethod
    def _values(
        summary: ArtifactSummary, manifest_mtime_ns: int, manifest_size: int
    ) -> tuple[object, ...]:
        return (
            summary.artifact_id,
            summary.name,
            summary.alias,
            summary.version,
            summary.provider.value,
            summary.source_id,
            summary.requested_revision,
            summary.resolved_revision,
            summary.selection_digest,
            json.dumps(summary.selected_paths, separators=(",", ":"))
            if summary.selected_paths is not None
            else None,
            summary.total_size,
            summary.file_count,
            summary.created_at.isoformat(),
            (
                f"{summary.alias or ''} {summary.name} "
                f"{summary.source_id} {summary.version}"
            ).casefold(),
            manifest_mtime_ns,
            manifest_size,
        )

    @staticmethod
    def _upsert_sql() -> str:
        return """
            INSERT INTO artifacts (
                artifact_id, name, alias, version, provider, source_id, requested_revision,
                resolved_revision, selection_digest, selected_paths_json,
                total_size, file_count, created_at,
                search_text, manifest_mtime_ns, manifest_size
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(artifact_id) DO UPDATE SET
                name = excluded.name,
                alias = excluded.alias,
                version = excluded.version,
                provider = excluded.provider,
                source_id = excluded.source_id,
                requested_revision = excluded.requested_revision,
                resolved_revision = excluded.resolved_revision,
                selection_digest = excluded.selection_digest,
                selected_paths_json = excluded.selected_paths_json,
                total_size = excluded.total_size,
                file_count = excluded.file_count,
                created_at = excluded.created_at,
                search_text = excluded.search_text,
                manifest_mtime_ns = excluded.manifest_mtime_ns,
                manifest_size = excluded.manifest_size
        """

    def upsert(
        self, summary: ArtifactSummary, *, manifest_mtime_ns: int, manifest_size: int
    ) -> None:
        with closing(self._connect()) as connection, connection:
            connection.execute(
                self._upsert_sql(), self._values(summary, manifest_mtime_ns, manifest_size)
            )

    def delete(self, artifact_id: str) -> None:
        with closing(self._connect()) as connection, connection:
            connection.execute("DELETE FROM artifacts WHERE artifact_id = ?", (artifact_id,))

    def manifest_metadata(self) -> dict[str, tuple[int, int, str | None]]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                "SELECT artifact_id, manifest_mtime_ns, manifest_size, alias FROM artifacts"
            )
            return {
                str(row["artifact_id"]): (
                    int(row["manifest_mtime_ns"]),
                    int(row["manifest_size"]),
                    str(row["alias"]) if row["alias"] is not None else None,
                )
                for row in rows
            }

    def reconcile(
        self,
        changed: Iterable[tuple[ArtifactSummary, int, int]],
        valid_artifact_ids: Iterable[str],
    ) -> None:
        with closing(self._connect()) as connection, connection:
            connection.execute("CREATE TEMP TABLE seen_artifacts (artifact_id TEXT PRIMARY KEY)")
            connection.executemany(
                "INSERT INTO seen_artifacts(artifact_id) VALUES (?)",
                ((artifact_id,) for artifact_id in valid_artifact_ids),
            )
            connection.executemany(
                self._upsert_sql(),
                (self._values(summary, mtime_ns, size) for summary, mtime_ns, size in changed),
            )
            connection.execute(
                "DELETE FROM artifacts WHERE artifact_id NOT IN "
                "(SELECT artifact_id FROM seen_artifacts)"
            )

    @staticmethod
    def _summary(row: sqlite3.Row) -> ArtifactSummary:
        return ArtifactSummary.model_validate(
            {
                "artifactId": row["artifact_id"],
                "name": row["name"],
                "alias": row["alias"],
                "version": row["version"],
                "provider": row["provider"],
                "sourceId": row["source_id"],
                "requestedRevision": row["requested_revision"],
                "resolvedRevision": row["resolved_revision"],
                "selectionDigest": row["selection_digest"],
                "selectedPaths": json.loads(row["selected_paths_json"])
                if row["selected_paths_json"] is not None
                else None,
                "totalSize": row["total_size"],
                "fileCount": row["file_count"],
                "createdAt": row["created_at"],
                "relativePath": artifact_relative_path(
                    Provider(row["provider"]),
                    row["source_id"],
                    row["resolved_revision"],
                    selected_paths_digest=row["selection_digest"],
                ),
            }
        )

    def list(
        self,
        *,
        query: str | None = None,
        provider: Provider | None = None,
        sort_by: str = "created",
        sort_order: str = "desc",
        limit: int | None = None,
        offset: int = 0,
    ) -> list[ArtifactSummary]:
        sort_columns = {
            "created": "created_at",
            "name": "COALESCE(alias, name) COLLATE NOCASE",
            "size": "total_size",
        }
        if sort_by not in sort_columns:
            raise ValueError(f"unsupported artifact sort field {sort_by!r}")
        if sort_order not in {"asc", "desc"}:
            raise ValueError(f"unsupported artifact sort order {sort_order!r}")
        sql = "SELECT * FROM artifacts"
        parameters: list[object] = []
        conditions: list[str] = []
        if query:
            conditions.append("instr(search_text, ?) > 0")
            parameters.append(query.casefold())
        if provider is not None:
            conditions.append("provider = ?")
            parameters.append(provider.value)
        if conditions:
            sql += " WHERE " + " AND ".join(conditions)
        sql += f" ORDER BY {sort_columns[sort_by]} {sort_order.upper()}, artifact_id ASC"
        if limit is not None:
            sql += " LIMIT ? OFFSET ?"
            parameters.extend((limit, offset))
        elif offset:
            sql += " LIMIT -1 OFFSET ?"
            parameters.append(offset)
        with closing(self._connect()) as connection:
            return [self._summary(row) for row in connection.execute(sql, parameters).fetchall()]

    def count(
        self, *, query: str | None = None, provider: Provider | None = None
    ) -> int:
        sql = "SELECT COUNT(*) FROM artifacts"
        parameters: list[object] = []
        conditions: list[str] = []
        if query:
            conditions.append("instr(search_text, ?) > 0")
            parameters.append(query.casefold())
        if provider is not None:
            conditions.append("provider = ?")
            parameters.append(provider.value)
        if conditions:
            sql += " WHERE " + " AND ".join(conditions)
        with closing(self._connect()) as connection:
            return int(connection.execute(sql, parameters).fetchone()[0])

    def find(self, artifact_id: str) -> ArtifactSummary | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT * FROM artifacts WHERE artifact_id = ?", (artifact_id,)
            ).fetchone()
            return self._summary(row) if row is not None else None
