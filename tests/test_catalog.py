import sqlite3
from pathlib import Path

import pytest
from modelshelf_core import Catalog, Provider, SourceReference, VerificationError
from modelshelf_core.catalog import verify_artifact
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
