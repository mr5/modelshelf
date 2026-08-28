from __future__ import annotations

import zipfile
from pathlib import Path

import pytest
from modelshelf_core import Catalog, Provider
from modelshelf_core.catalog import verify_artifact
from modelshelf_server.config import Settings
from modelshelf_server.filesystem_import import (
    allowed_import_roots,
    import_filesystem,
)
from modelshelf_server.main import build_parser, run_import


def test_directory_import_is_content_addressed_atomic_and_deduplicated(
    tmp_path: Path,
) -> None:
    import_root = tmp_path / "imports"
    source = import_root / "tiny-model"
    source.mkdir(parents=True)
    (source / "config.json").write_text('{"model_type":"tiny"}')
    (source / "model.gguf").write_bytes(b"weights")
    catalog = Catalog(tmp_path / "storage")
    catalog.initialize()
    roots = allowed_import_roots(catalog, (import_root,))

    first = import_filesystem(catalog, source, roots=roots)
    second = import_filesystem(catalog, source, roots=roots)

    assert first.manifest.source.provider is Provider.FILESYSTEM
    assert first.manifest.source.requested_revision == "content"
    assert first.manifest.source.resolved_revision == (f"sha256:{first.manifest.content_sha256}")
    assert first.manifest.format == "GGUF"
    assert first.manifest.file_count == 2
    assert not first.deduplicated
    assert second.deduplicated
    assert source.is_dir()
    assert verify_artifact(first.destination, full=True) == []
    assert [item.artifact_id for item in catalog.list()] == [first.manifest.artifact_id]
    assert list(catalog.staging_root.iterdir()) == []


def test_changed_directory_content_creates_a_new_immutable_revision(tmp_path: Path) -> None:
    import_root = tmp_path / "imports"
    source = import_root / "model"
    source.mkdir(parents=True)
    weights = source / "model.safetensors"
    weights.write_bytes(b"first")
    catalog = Catalog(tmp_path / "storage")
    catalog.initialize()
    roots = allowed_import_roots(catalog, (import_root,))

    first = import_filesystem(catalog, source, roots=roots, source_id="team/model")
    weights.write_bytes(b"second")
    second = import_filesystem(catalog, source, roots=roots, source_id="team/model")

    assert first.manifest.source.resolved_revision != second.manifest.source.resolved_revision
    assert len(catalog.list()) == 2


def test_import_rejects_outside_symlink_and_reserved_sources(tmp_path: Path) -> None:
    import_root = tmp_path / "imports"
    import_root.mkdir()
    catalog = Catalog(tmp_path / "storage")
    catalog.initialize()
    roots = allowed_import_roots(catalog, (import_root,))

    outside = tmp_path / "outside.gguf"
    outside.write_bytes(b"weights")
    with pytest.raises(ValueError, match="outside configured import roots"):
        import_filesystem(catalog, outside, roots=roots)

    symlink_source = import_root / "linked.gguf"
    symlink_source.symlink_to(outside)
    with pytest.raises(ValueError, match="symbolic link"):
        import_filesystem(catalog, symlink_source, roots=roots)

    reserved_source = import_root / "reserved"
    (reserved_source / ".modelshelf").mkdir(parents=True)
    (reserved_source / ".modelshelf/foreign.json").write_text("{}")
    with pytest.raises(ValueError, match="reserved import path"):
        import_filesystem(catalog, reserved_source, roots=roots)


def test_archive_import_requires_explicit_extract(tmp_path: Path) -> None:
    import_root = tmp_path / "imports"
    import_root.mkdir()
    archive = import_root / "tiny-model-1.2.zip"
    with zipfile.ZipFile(archive, "w") as output:
        output.writestr("config.json", '{"model_type":"tiny"}')
        output.writestr("model.onnx", b"weights")
    catalog = Catalog(tmp_path / "storage")
    catalog.initialize()
    roots = allowed_import_roots(catalog, (import_root,))

    packed = import_filesystem(catalog, archive, roots=roots, source_id="packed")
    extracted = import_filesystem(
        catalog, archive, roots=roots, source_id="extracted", extract=True
    )

    assert [item.path for item in packed.manifest.files] == [archive.name]
    assert {item.path for item in extracted.manifest.files} == {"config.json", "model.onnx"}
    assert extracted.manifest.format == "ONNX"


def test_server_import_command_returns_machine_readable_result(tmp_path: Path) -> None:
    import_root = tmp_path / "imports"
    source = import_root / "model.gguf"
    source.parent.mkdir()
    source.write_bytes(b"weights")
    settings = Settings(
        storage_root=tmp_path / "storage",
        import_roots=(import_root,),
    )
    arguments = build_parser().parse_args(
        ["import", str(source), "--id", "offline/model", "--version", "v1"]
    )

    result = run_import(arguments, settings)

    assert result["artifactId"].startswith("filesystem:")
    assert result["deduplicated"] is False
    assert result["fileCount"] == 1
