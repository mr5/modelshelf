import zipfile
from pathlib import Path

import pytest
from modelshelf_server.archive import archive_entries, extract_archive, infer_metadata


def write_zip(path: Path, entries: dict[str, bytes]) -> None:
    with zipfile.ZipFile(path, "w") as archive:
        for name, value in entries.items():
            archive.writestr(name, value)


def test_archive_inference_and_explicit_extraction(tmp_path: Path) -> None:
    archive = tmp_path / "download.zip"
    write_zip(
        archive,
        {
            "tiny-model/config.json": b'{"model_type":"tiny"}',
            "tiny-model/model.safetensors": b"weights",
        },
    )
    metadata = infer_metadata(archive, "https://example.test/download.zip")
    assert metadata.name == "tiny-model"
    assert metadata.format == "safetensors"
    assert metadata.archive
    assert "config.json model_type: tiny" in metadata.notes
    assert not (tmp_path / "published").exists()

    extract_archive(archive, tmp_path / "published")
    assert (tmp_path / "published/tiny-model/model.safetensors").is_file()


def test_archive_path_traversal_is_rejected(tmp_path: Path) -> None:
    archive = tmp_path / "bad.zip"
    write_zip(archive, {"../escape": b"bad"})
    with pytest.raises(ValueError, match="unsafe archive path"):
        archive_entries(archive)
    assert not (tmp_path.parent / "escape").exists()


def test_readme_title_is_used_for_generic_archive_name(tmp_path: Path) -> None:
    archive = tmp_path / "download.zip"
    write_zip(
        archive,
        {
            "README.md": b"# Acme Tiny Model\n",
            "weights.gguf": b"weights",
        },
    )

    metadata = infer_metadata(archive, "https://example.test/download.zip")

    assert metadata.name == "Acme Tiny Model"
    assert metadata.format == "GGUF"
    assert "README title: Acme Tiny Model" in metadata.notes
