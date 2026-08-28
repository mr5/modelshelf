from __future__ import annotations

import secrets
from pathlib import Path
from typing import Annotated
from urllib.parse import urlparse

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="MODELSHELF_", extra="ignore")

    host: str = "0.0.0.0"
    port: int = 8080
    storage_root: Path = Path("./data")
    import_roots: Annotated[tuple[Path, ...], NoDecode] = ()
    ui_dist: Path | None = None
    client_dist: Path | None = None
    admin_password_hash: str | None = None
    write_tokens: Annotated[tuple[str, ...], NoDecode] = ()
    session_secret: str = Field(default_factory=lambda: secrets.token_urlsafe(32), min_length=32)
    session_ttl_seconds: int = 12 * 60 * 60
    session_cookie_secure: bool = False
    public_artifacts: bool = True
    max_concurrent_downloads: int = Field(default=2, ge=1, le=16)
    max_concurrent_downloads_per_source: int = Field(default=1, ge=1, le=16)
    nfs_advertised_host: str | None = None
    nfs_port: int = Field(default=2049, ge=1, le=65535)
    nfs_advertised_port: int | None = Field(default=None, ge=1, le=65535)
    nfs_export_path: str = "/modelshelf"
    public_base_url: str | None = None
    github_token: str | None = None
    huggingface_mirror: str | None = None
    modelscope_cn_mirror: str | None = None
    modelscope_ai_mirror: str | None = None
    http_proxy: str | None = None

    @field_validator("admin_password_hash")
    @classmethod
    def require_argon2id(cls, value: str | None) -> str | None:
        if value is not None and not value.startswith("$argon2id$"):
            raise ValueError("admin password hash must use Argon2id")
        return value

    @field_validator("write_tokens", mode="before")
    @classmethod
    def split_tokens(cls, value: object) -> object:
        if isinstance(value, str):
            return tuple(token.strip() for token in value.split(",") if token.strip())
        return value

    @field_validator("import_roots", mode="before")
    @classmethod
    def split_import_roots(cls, value: object) -> object:
        if isinstance(value, str):
            return tuple(Path(item.strip()) for item in value.split(",") if item.strip())
        return value

    @field_validator("nfs_advertised_port", mode="before")
    @classmethod
    def empty_advertised_port_is_unset(cls, value: object) -> object:
        return None if value == "" else value

    @field_validator(
        "public_base_url",
        "huggingface_mirror",
        "modelscope_cn_mirror",
        "modelscope_ai_mirror",
        "http_proxy",
        mode="before",
    )
    @classmethod
    def require_http_url(cls, value: object) -> str | None:
        if value is None or value == "":
            return None
        if not isinstance(value, str):
            raise ValueError("network endpoint must be a string")
        parsed = urlparse(value)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("network endpoint must be an HTTP(S) URL")
        return value.rstrip("/")
