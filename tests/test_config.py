from pathlib import Path

import pytest
from modelshelf_server.config import Settings
from pydantic import ValidationError
from pytest import MonkeyPatch


def test_settings_accept_comma_separated_write_tokens(
    monkeypatch: MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("MODELSHELF_STORAGE_ROOT", str(tmp_path))
    monkeypatch.setenv("MODELSHELF_WRITE_TOKENS", "first, second")

    settings = Settings()

    assert settings.write_tokens == ("first", "second")


def test_artifact_storage_root_is_optional_and_configurable(tmp_path: Path) -> None:
    assert Settings().artifact_storage_root is None

    configured = tmp_path / "artifact-storage"
    settings = Settings(artifact_storage_root=configured)

    assert settings.artifact_storage_root == configured


def test_settings_require_argon2id_hash() -> None:
    with pytest.raises(ValidationError, match="Argon2id"):
        Settings(admin_password_hash="$argon2i$v=19$invalid")


def test_public_artifacts_default_on_and_can_be_disabled(monkeypatch: MonkeyPatch) -> None:
    assert Settings().public_artifacts is True

    monkeypatch.setenv("MODELSHELF_PUBLIC_ARTIFACTS", "false")

    assert Settings().public_artifacts is False


def test_download_concurrency_defaults_and_validation() -> None:
    settings = Settings()
    assert settings.max_concurrent_downloads == 2
    assert settings.max_concurrent_downloads_per_source == 1
    assert Settings(max_concurrent_downloads=4).max_concurrent_downloads == 4
    assert Settings(max_concurrent_downloads_per_source=2).max_concurrent_downloads_per_source == 2
    with pytest.raises(ValidationError):
        Settings(max_concurrent_downloads=0)
    with pytest.raises(ValidationError):
        Settings(max_concurrent_downloads_per_source=0)


def test_nfs_ports_default_and_are_validated() -> None:
    assert Settings().nfs_port == 2049
    assert Settings().nfs_advertised_port is None
    assert Settings(nfs_port=32048).nfs_port == 32048
    assert Settings(nfs_advertised_port=32049).nfs_advertised_port == 32049
    assert Settings(nfs_advertised_port="").nfs_advertised_port is None  # type: ignore[arg-type]
    with pytest.raises(ValidationError):
        Settings(nfs_advertised_port=70000)
    with pytest.raises(ValidationError):
        Settings(nfs_port=0)
