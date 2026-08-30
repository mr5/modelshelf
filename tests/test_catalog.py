import hashlib
import json
import os
import sqlite3
import threading
from pathlib import Path

import modelshelf_core.catalog as catalog_module
import pytest
from modelshelf_core import (
    Catalog,
    FutureSchemaVersionError,
    Provider,
    SourceReference,
    VerificationError,
)
from modelshelf_core.catalog import inventory, verify_artifact
from modelshelf_core.identity import (
    artifact_identity,
    artifact_identity_from_relative_path,
    artifact_relative_path,
)


def make_stage(catalog: Catalog, task: str, content: bytes = b"weights") -> Path:
    stage = catalog.staging_path(task)
    stage.mkdir(parents=True)
    (stage / "nested").mkdir()
    (stage / "nested/model.gguf").write_bytes(content)
    return stage


def source(revision: str = "abc123") -> SourceReference:
    return SourceReference(
        provider=Provider.HUGGINGFACE,
        id="owner/model",
        requested_revision="main",
        resolved_revision=revision,
    )


def test_parallel_inventory_reports_byte_progress_and_checks_expected_hashes(
    tmp_path: Path,
) -> None:
    first = b"a" * (1024 * 1024 + 17)
    second = b"b" * (1024 * 1024 + 29)
    (tmp_path / "first.bin").write_bytes(first)
    (tmp_path / "second.bin").write_bytes(second)
    updates: list[tuple[int, int]] = []

    files = inventory(
        tmp_path,
        workers=2,
        progress=lambda completed, total: updates.append((completed, total)),
        expected_sha256={
            "first.bin": hashlib.sha256(first).hexdigest(),
            "second.bin": hashlib.sha256(second).hexdigest(),
        },
    )

    total = len(first) + len(second)
    assert [file.path for file in files] == ["first.bin", "second.bin"]
    assert updates[0] == (0, total)
    assert updates[-1] == (total, total)
    assert all(left[0] <= right[0] for left, right in zip(updates, updates[1:], strict=False))

    with pytest.raises(VerificationError, match="source SHA-256 mismatch: first.bin"):
        inventory(tmp_path, workers=2, expected_sha256={"first.bin": "0" * 64})


def test_parallel_inventory_uses_two_file_hash_workers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "first.bin").write_bytes(b"first")
    (tmp_path / "second.bin").write_bytes(b"second")
    barrier = threading.Barrier(2)
    worker_names: set[str] = set()

    def synchronized_hash(
        path: Path,
        *,
        progress: object = None,
        cancelled: object = None,
    ) -> str:
        del cancelled
        worker_names.add(threading.current_thread().name)
        barrier.wait(timeout=1)
        payload = path.read_bytes()
        if callable(progress):
            progress(len(payload))
        return hashlib.sha256(payload).hexdigest()

    monkeypatch.setattr(catalog_module, "sha256_file", synchronized_hash)

    files = inventory(tmp_path, workers=2)

    assert len(files) == 2
    assert len(worker_names) == 2


def test_inventory_trusts_reused_manifest_entries_and_hashes_only_new_content(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    reused = b"already verified" * 100
    changed = b"new content"
    (tmp_path / "reused.bin").write_bytes(reused)
    (tmp_path / "changed.bin").write_bytes(changed)
    reused_digest = hashlib.sha256(reused).hexdigest()
    hashed_paths: list[str] = []
    original_sha256_file = catalog_module.sha256_file

    def tracked_hash(path: Path, **kwargs: object) -> str:
        hashed_paths.append(path.name)
        return original_sha256_file(path, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(catalog_module, "sha256_file", tracked_hash)
    updates: list[tuple[int, int]] = []

    files = inventory(
        tmp_path,
        workers=2,
        progress=lambda completed, total: updates.append((completed, total)),
        expected_sha256={
            "reused.bin": reused_digest,
            "changed.bin": hashlib.sha256(changed).hexdigest(),
        },
        trusted_sha256={"reused.bin": reused_digest},
    )

    assert hashed_paths == ["changed.bin"]
    assert updates[0] == (0, len(changed))
    assert updates[-1] == (len(changed), len(changed))
    assert {entry.path: entry.sha256 for entry in files} == {
        "changed.bin": hashlib.sha256(changed).hexdigest(),
        "reused.bin": reused_digest,
    }


def test_inventory_rejects_invalid_trusted_file_metadata(tmp_path: Path) -> None:
    payload = b"trusted"
    (tmp_path / "model.bin").write_bytes(payload)
    digest = hashlib.sha256(payload).hexdigest()

    with pytest.raises(VerificationError, match="trusted SHA-256"):
        inventory(
            tmp_path,
            expected_sha256={"model.bin": digest},
            trusted_sha256={"model.bin": "0" * 64},
        )

    with pytest.raises(VerificationError, match="missing source metadata"):
        inventory(
            tmp_path,
            trusted_sha256={"model.bin": digest},
        )


def test_manifest_publish_and_verify(tmp_path: Path) -> None:
    catalog = Catalog(tmp_path)
    catalog.initialize()
    stage = make_stage(catalog, "one")
    manifest = catalog.create_manifest(
        stage, name="model", version="1", source=source(), format="GGUF"
    )

    destination, deduplicated = catalog.publish(stage, manifest)

    assert not deduplicated
    assert destination.is_dir()
    assert verify_artifact(destination, full=False) == []
    assert verify_artifact(destination, full=True) == []
    [summary] = catalog.list()
    assert summary.relative_path == "huggingface/owner/model/abc123"
    assert not summary.relative_path.startswith("artifacts/")
    assert catalog.find(summary.artifact_id) is not None
    assert (destination.stat().st_mode & 0o222) == 0
    assert ((destination / "nested/model.gguf").stat().st_mode & 0o222) == 0

    (destination / "nested/model.gguf").chmod(0o644)
    (destination / "nested/model.gguf").write_bytes(b"changed")
    assert verify_artifact(destination, full=False) == []
    assert verify_artifact(destination, full=True) == ["sha256: nested/model.gguf"]


def test_clone_artifact_file_prefers_hardlink_without_copying(tmp_path: Path) -> None:
    source_path = tmp_path / "source.bin"
    destination = tmp_path / "nested/destination.bin"
    source_path.write_bytes(b"shared blocks")

    method = catalog_module.clone_artifact_file(source_path, destination)

    assert method == "hardlink"
    assert destination.read_bytes() == b"shared blocks"
    assert source_path.stat().st_ino == destination.stat().st_ino


def test_clone_artifact_file_hashes_the_copy_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source_path = tmp_path / "source.bin"
    destination = tmp_path / "destination.bin"
    source_path.write_bytes(b"copied content")

    monkeypatch.setattr(
        catalog_module.fcntl,
        "ioctl",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("unsupported")),
    )
    monkeypatch.setattr(
        catalog_module.os,
        "link",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("unsupported")),
    )

    digest = hashlib.sha256(source_path.read_bytes()).hexdigest()
    assert (
        catalog_module.clone_artifact_file(
            source_path,
            destination,
            expected_sha256=digest,
        )
        == "copy"
    )
    assert destination.read_bytes() == source_path.read_bytes()

    destination.unlink()
    with pytest.raises(VerificationError, match="copied artifact file SHA-256 mismatch"):
        catalog_module.clone_artifact_file(
            source_path,
            destination,
            expected_sha256="0" * 64,
        )
    assert not destination.exists()


def test_deleting_a_hardlinked_artifact_does_not_make_the_other_artifact_writable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    catalog = Catalog(tmp_path)
    catalog.initialize()
    first_stage = make_stage(catalog, "first-linked")
    first_manifest = catalog.create_manifest(
        first_stage, name="model", version="1", source=source("revision-1")
    )
    first_root, _ = catalog.publish(first_stage, first_manifest)
    shared_source = first_root / "nested/model.gguf"

    def unsupported_reflink(*_args: object, **_kwargs: object) -> None:
        raise OSError("reflink unavailable")

    monkeypatch.setattr(catalog_module.fcntl, "ioctl", unsupported_reflink)
    second_stage = catalog.staging_path("second-linked")
    shared_destination = second_stage / "nested/model.gguf"
    assert catalog_module.clone_artifact_file(shared_source, shared_destination) == "hardlink"
    second_manifest = catalog.create_manifest(
        second_stage, name="model", version="2", source=source("revision-2")
    )
    second_root, _ = catalog.publish(second_stage, second_manifest)

    assert catalog.delete(second_manifest.artifact_id)
    assert not second_root.exists()
    assert shared_source.read_bytes() == b"weights"
    assert (shared_source.stat().st_mode & 0o222) == 0


def test_future_manifest_and_storage_layout_versions_are_rejected(tmp_path: Path) -> None:
    catalog = Catalog(tmp_path)
    catalog.initialize()
    stage = make_stage(catalog, "future")
    catalog.create_manifest(stage, name="model", version="1", source=source())
    manifest_path = stage / ".modelshelf" / "manifest.json"
    document = json.loads(manifest_path.read_text(encoding="utf-8"))
    document["schemaVersion"] = 3
    manifest_path.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(FutureSchemaVersionError, match="upgrade ModelShelf"):
        catalog.read_manifest(stage)

    layout = json.loads(catalog.layout_path.read_text(encoding="utf-8"))
    layout["schemaVersion"] = 3
    catalog.layout_path.write_text(json.dumps(layout), encoding="utf-8")
    with pytest.raises(FutureSchemaVersionError, match="upgrade ModelShelf"):
        Catalog(tmp_path).initialize()


def test_future_artifact_alias_schema_is_rejected(tmp_path: Path) -> None:
    catalog = Catalog(tmp_path)
    catalog.initialize()
    catalog.aliases_path.write_text(
        json.dumps({"schemaVersion": 2, "artifacts": {}}), encoding="utf-8"
    )

    with pytest.raises(FutureSchemaVersionError, match="upgrade ModelShelf"):
        Catalog(tmp_path).initialize()


def test_storage_layout_v1_migration_repairs_only_artifact_ancestors(
    tmp_path: Path,
) -> None:
    catalog = Catalog(tmp_path)
    catalog.initialize()
    stage = make_stage(catalog, "legacy")
    manifest = catalog.create_manifest(stage, name="model", version="1", source=source())
    destination, _ = catalog.publish(stage, manifest)
    ancestors = [
        catalog.artifacts_root,
        catalog.artifacts_root / "huggingface",
        catalog.artifacts_root / "huggingface/owner",
        catalog.artifacts_root / "huggingface/owner/model",
    ]
    for ancestor in ancestors:
        ancestor.chmod(0o700)
    catalog.layout_path.write_text(
        json.dumps({"schemaVersion": 1, "kind": "modelshelf-storage"}),
        encoding="utf-8",
    )

    Catalog(tmp_path).initialize()

    migrated = json.loads(catalog.layout_path.read_text(encoding="utf-8"))
    assert migrated["schemaVersion"] == 2
    assert all((path.stat().st_mode & 0o777) == 0o755 for path in ancestors)
    assert (destination.stat().st_mode & 0o777) == 0o555
    assert ((destination / "nested/model.gguf").stat().st_mode & 0o777) == 0o444


def test_artifact_namespace_permissions_do_not_depend_on_umask(tmp_path: Path) -> None:
    previous = os.umask(0o077)
    try:
        catalog = Catalog(tmp_path)
        catalog.initialize()
        stage = make_stage(catalog, "private-umask")
        manifest = catalog.create_manifest(stage, name="model", version="1", source=source())
        destination, _ = catalog.publish(stage, manifest)
    finally:
        os.umask(previous)

    parents = [
        catalog.artifacts_root,
        catalog.artifacts_root / "huggingface",
        catalog.artifacts_root / "huggingface/owner",
        catalog.artifacts_root / "huggingface/owner/model",
    ]
    assert all((path.stat().st_mode & 0o777) == 0o755 for path in parents)
    assert (destination.stat().st_mode & 0o777) == 0o555
    assert ((destination / "nested/model.gguf").stat().st_mode & 0o777) == 0o444


def test_artifact_paths_keep_source_names_readable_and_escape_only_unsafe_characters() -> None:
    relative = artifact_relative_path(
        Provider.HUGGINGFACE,
        "owner/model name%",
        "refs/commit one",
    )
    assert relative == "huggingface/owner/model%20name%25/refs%2Fcommit%20one"
    assert artifact_identity_from_relative_path(relative) == artifact_identity(
        Provider.HUGGINGFACE,
        "owner/model name%",
        "refs/commit one",
    )

    http_relative = artifact_relative_path(
        Provider.HTTP,
        "https://models.example/model.gguf?version=1",
        "sha256:abc",
    )
    assert http_relative == ("http/https:%2F%2Fmodels.example%2Fmodel.gguf?version=1/sha256:abc")
    assert artifact_identity_from_relative_path(http_relative) == artifact_identity(
        Provider.HTTP,
        "https://models.example/model.gguf?version=1",
        "sha256:abc",
    )


def test_selected_file_artifacts_have_distinct_stable_identities(tmp_path: Path) -> None:
    catalog = Catalog(tmp_path)
    catalog.initialize()
    selected = ["nested/model.gguf", "README.md"]
    stage = make_stage(catalog, "selected")
    (stage / "README.md").write_text("model", encoding="utf-8")
    manifest = catalog.create_manifest(
        stage,
        name="model",
        version="abc123",
        source=SourceReference(
            provider=Provider.HUGGINGFACE,
            id="owner/model",
            requested_revision="main",
            resolved_revision="abc123",
            selected_paths=selected,
        ),
    )

    destination, _ = catalog.publish(stage, manifest)
    [summary] = catalog.list()

    assert summary.selected_paths == sorted(selected)
    assert summary.selection_digest is not None
    assert summary.artifact_id != artifact_identity(Provider.HUGGINGFACE, "owner/model", "abc123")
    assert destination.name == f"abc123~files-{summary.selection_digest}"
    assert artifact_identity_from_relative_path(summary.relative_path) == summary.artifact_id
    assert catalog.find(summary.artifact_id) == (summary, manifest)


def test_publish_deduplicates_and_rejects_collision(tmp_path: Path) -> None:
    catalog = Catalog(tmp_path)
    catalog.initialize()
    first = make_stage(catalog, "first")
    first_manifest = catalog.create_manifest(first, name="model", version="1", source=source())
    catalog.publish(first, first_manifest)

    duplicate = make_stage(catalog, "duplicate")
    duplicate_manifest = catalog.create_manifest(
        duplicate, name="model", version="1", source=source()
    )
    _, deduplicated = catalog.publish(duplicate, duplicate_manifest)
    assert deduplicated
    assert not duplicate.exists()

    collision = make_stage(catalog, "collision", b"different")
    collision_manifest = catalog.create_manifest(
        collision, name="model", version="1", source=source()
    )
    with pytest.raises(VerificationError, match="immutable artifact collision"):
        catalog.publish(collision, collision_manifest)


def test_unexpected_file_check_is_optional(tmp_path: Path) -> None:
    catalog = Catalog(tmp_path)
    catalog.initialize()
    stage = make_stage(catalog, "one")
    manifest = catalog.create_manifest(stage, name="model", version="1", source=source())
    destination, _ = catalog.publish(stage, manifest)
    destination.chmod(0o755)
    (destination / "extra.txt").write_text("extra")

    assert verify_artifact(destination, full=False, unexpected=False) == []
    assert verify_artifact(destination, full=False, unexpected=True) == ["unexpected: extra.txt"]


def test_sqlite_index_is_rebuildable_and_does_not_store_relative_path(
    tmp_path: Path,
) -> None:
    catalog = Catalog(tmp_path)
    catalog.initialize()
    stage = make_stage(catalog, "indexed")
    manifest = catalog.create_manifest(
        stage, name="Searchable Model", version="v1", source=source()
    )
    catalog.publish(stage, manifest)

    with sqlite3.connect(catalog.index_path) as connection:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(artifacts)").fetchall()}
    assert "relative_path" not in columns
    [summary] = catalog.list(query="searchable")
    assert (
        summary.relative_path
        == catalog.artifact_path(summary.provider, summary.source_id, summary.resolved_revision)
        .relative_to(catalog.artifacts_root)
        .as_posix()
    )

    catalog.index_path.unlink()
    rebuilt = Catalog(tmp_path)
    rebuilt.initialize()
    assert [item.artifact_id for item in rebuilt.list()] == [manifest.artifact_id]


def test_corrupt_index_is_preserved_and_rebuilt_from_manifests(tmp_path: Path) -> None:
    catalog = Catalog(tmp_path)
    catalog.initialize()
    stage = make_stage(catalog, "recover")
    manifest = catalog.create_manifest(stage, name="model", version="1", source=source())
    catalog.publish(stage, manifest)

    catalog.index_path.write_bytes(b"not a sqlite database")
    recovered = Catalog(tmp_path)
    recovered.initialize()

    assert [item.artifact_id for item in recovered.list()] == [manifest.artifact_id]
    assert list(catalog.index_path.parent.glob("catalog.sqlite3.invalid-*"))


def test_unversioned_nonempty_index_is_preserved_and_rebuilt(tmp_path: Path) -> None:
    catalog = Catalog(tmp_path)
    catalog.index_path.parent.mkdir(parents=True)
    with sqlite3.connect(catalog.index_path) as connection:
        connection.execute("CREATE TABLE artifacts (artifact_id TEXT PRIMARY KEY)")
    catalog.initialize()
    with sqlite3.connect(catalog.index_path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 3
        columns = {row[1] for row in connection.execute("PRAGMA table_info(artifacts)").fetchall()}
    assert "manifest_mtime_ns" in columns
    assert list(catalog.index_path.parent.glob("catalog.sqlite3.invalid-*"))


def test_reconciliation_removes_index_rows_without_artifacts(tmp_path: Path) -> None:
    catalog = Catalog(tmp_path)
    catalog.initialize()
    stage = make_stage(catalog, "orphan")
    manifest = catalog.create_manifest(stage, name="model", version="1", source=source())
    destination, _ = catalog.publish(stage, manifest)
    destination.chmod(0o755)
    destination.rename(tmp_path / "moved-away")

    catalog.reconcile_index()

    assert catalog.list() == []


def test_catalog_filters_provider_and_sorts_before_pagination(tmp_path: Path) -> None:
    catalog = Catalog(tmp_path)
    catalog.initialize()

    first = make_stage(catalog, "first", b"1")
    first_manifest = catalog.create_manifest(
        first, name="Zulu", version="1", source=source("first")
    )
    catalog.publish(first, first_manifest)

    second = make_stage(catalog, "second", b"123456789")
    second_manifest = catalog.create_manifest(
        second,
        name="Alpha",
        version="2",
        source=SourceReference(
            provider=Provider.MODELSCOPE_CN,
            id="owner/second",
            requested_revision="master",
            resolved_revision="second",
        ),
    )
    catalog.publish(second, second_manifest)

    assert [item.name for item in catalog.list(sort_by="name", sort_order="asc")] == [
        "Alpha",
        "Zulu",
    ]
    assert [item.name for item in catalog.list(sort_by="size", sort_order="desc")] == [
        "Alpha",
        "Zulu",
    ]
    assert [item.name for item in catalog.list(provider=Provider.MODELSCOPE_CN)] == ["Alpha"]
    assert [
        item.name for item in catalog.list(sort_by="name", sort_order="asc", limit=1, offset=1)
    ] == ["Zulu"]
    with pytest.raises(ValueError, match="unsupported artifact sort field"):
        catalog.list(sort_by="unsafe")


def test_publish_remains_successful_when_index_update_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    catalog = Catalog(tmp_path)
    catalog.initialize()
    stage = make_stage(catalog, "index-failure")
    manifest = catalog.create_manifest(stage, name="model", version="1", source=source())

    def fail_index(*_args: object, **_kwargs: object) -> None:
        raise sqlite3.OperationalError("simulated index failure")

    monkeypatch.setattr(catalog.index, "upsert", fail_index)
    destination, deduplicated = catalog.publish(stage, manifest)

    assert not deduplicated
    assert destination.is_dir()
    rebuilt = Catalog(tmp_path)
    rebuilt.initialize()
    assert rebuilt.find(manifest.artifact_id) is not None


def test_delete_removes_artifact_files_and_index_entry(tmp_path: Path) -> None:
    catalog = Catalog(tmp_path)
    catalog.initialize()
    stage = make_stage(catalog, "delete")
    manifest = catalog.create_manifest(stage, name="model", version="1", source=source())
    destination, _ = catalog.publish(stage, manifest)

    assert catalog.delete(manifest.artifact_id)

    assert not destination.exists()
    assert catalog.find(manifest.artifact_id) is None
    assert catalog.list() == []
    assert not catalog.delete(manifest.artifact_id)


def test_artifact_aliases_are_unique_persistent_and_outside_manifests(tmp_path: Path) -> None:
    catalog = Catalog(tmp_path)
    catalog.initialize()
    first_stage = make_stage(catalog, "alias-first")
    first = catalog.create_manifest(first_stage, name="model", version="1", source=source("first"))
    first_path, _ = catalog.publish(first_stage, first)
    second_stage = make_stage(catalog, "alias-second", b"other")
    second = catalog.create_manifest(
        second_stage, name="model", version="2", source=source("second")
    )
    catalog.publish(second_stage, second)

    updated = catalog.set_alias(first.artifact_id, "production-model")

    assert updated.alias == "production-model"
    assert [item.artifact_id for item in catalog.list(query="production-model")] == [
        first.artifact_id
    ]
    assert "alias" not in json.loads(
        (first_path / ".modelshelf/manifest.json").read_text(encoding="utf-8")
    )
    with pytest.raises(ValueError, match="already in use"):
        catalog.set_alias(second.artifact_id, "production-model")

    rebuilt = Catalog(tmp_path)
    rebuilt.initialize()
    found = rebuilt.find(first.artifact_id)
    assert found is not None and found[0].alias == "production-model"

    registry = json.loads(rebuilt.aliases_path.read_text(encoding="utf-8"))
    registry["artifacts"][first.artifact_id] = "manually-renamed"
    rebuilt.aliases_path.write_text(json.dumps(registry), encoding="utf-8")
    reindexed = Catalog(tmp_path)
    reindexed.initialize()
    found = reindexed.find(first.artifact_id)
    assert found is not None and found[0].alias == "manually-renamed"

    reindexed.set_alias(first.artifact_id, None)
    found = reindexed.find(first.artifact_id)
    assert found is not None and found[0].alias is None
    assert json.loads(reindexed.aliases_path.read_text(encoding="utf-8"))["artifacts"] == {}


def test_deleting_artifact_removes_its_alias(tmp_path: Path) -> None:
    catalog = Catalog(tmp_path)
    catalog.initialize()
    stage = make_stage(catalog, "aliased-delete")
    manifest = catalog.create_manifest(stage, name="model", version="1", source=source())
    catalog.publish(stage, manifest)
    catalog.set_alias(manifest.artifact_id, "temporary")

    assert catalog.delete(manifest.artifact_id)
    assert json.loads(catalog.aliases_path.read_text(encoding="utf-8"))["artifacts"] == {}
