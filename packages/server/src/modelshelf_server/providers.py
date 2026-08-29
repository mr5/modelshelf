from __future__ import annotations

import asyncio
import base64
import contextlib
import email.message
import glob
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Awaitable, Callable, Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path, PurePath
from typing import Any
from urllib.parse import quote, unquote, urlparse

import httpx
from modelshelf_core import Provider

Progress = Callable[[int, int | None], Awaitable[None]]
OFFICIAL_HUGGINGFACE_ENDPOINT = "https://huggingface.co"
OFFICIAL_MODELSCOPE_CN_ENDPOINT = "https://modelscope.cn"
OFFICIAL_MODELSCOPE_AI_ENDPOINT = "https://modelscope.ai"
MODELSCOPE_PROVIDERS = frozenset({Provider.MODELSCOPE_CN, Provider.MODELSCOPE_AI})
ISOLATED_DOWNLOAD_PROVIDERS = frozenset(
    {Provider.HUGGINGFACE, Provider.KAGGLE} | MODELSCOPE_PROVIDERS
)
_WORKER_PREFIX = "MODELSHELF_JSON:"


@dataclass(frozen=True)
class ProviderResult:
    resolved_revision: str
    source_url: str | None = None
    downloaded_file: str | None = None
    content_disposition: str | None = None


@dataclass(frozen=True)
class RevisionOption:
    name: str
    kind: str
    resolved_revision: str | None = None

    def as_dict(self) -> dict[str, str]:
        result = {"name": self.name, "kind": self.kind}
        if self.resolved_revision:
            result["resolvedRevision"] = self.resolved_revision
        return result


@dataclass(frozen=True)
class RevisionDiscovery:
    provider: Provider
    source_id: str
    default_revision: str
    revisions: tuple[RevisionOption, ...]
    supports_discovery: bool = True

    def as_dict(self) -> dict[str, object]:
        return {
            "provider": self.provider.value,
            "sourceId": self.source_id,
            "defaultRevision": self.default_revision,
            "supportsDiscovery": self.supports_discovery,
            "revisions": [revision.as_dict() for revision in self.revisions],
        }


@dataclass(frozen=True)
class ModelOption:
    id: str
    name: str
    detail: str | None = None

    def as_dict(self) -> dict[str, str]:
        result = {"id": self.id, "name": self.name}
        if self.detail:
            result["detail"] = self.detail
        return result


@dataclass(frozen=True)
class ModelSearch:
    provider: Provider
    query: str
    models: tuple[ModelOption, ...]
    supports_search: bool = True

    def as_dict(self) -> dict[str, object]:
        return {
            "provider": self.provider.value,
            "query": self.query,
            "supportsSearch": self.supports_search,
            "models": [model.as_dict() for model in self.models],
        }


@dataclass(frozen=True)
class EstimateMetadata:
    label: str
    value: str

    def as_dict(self) -> dict[str, str]:
        return {"label": self.label, "value": self.value}


@dataclass(frozen=True)
class SourceFile:
    path: str
    size: int | None = None

    def as_dict(self) -> dict[str, object]:
        result: dict[str, object] = {"path": self.path}
        if self.size is not None:
            result["size"] = self.size
        return result


@dataclass(frozen=True)
class GgufVariant:
    label: str
    files: tuple[SourceFile, ...]

    @property
    def paths(self) -> tuple[str, ...]:
        return tuple(file.path for file in self.files)

    @property
    def total_size(self) -> int | None:
        if any(file.size is None for file in self.files):
            return None
        return sum(file.size or 0 for file in self.files)

    def as_dict(self) -> dict[str, object]:
        result: dict[str, object] = {
            "label": self.label,
            "paths": list(self.paths),
            "fileCount": len(self.files),
        }
        if self.total_size is not None:
            result["totalSize"] = self.total_size
        return result


_GGUF_SPLIT_PATTERN = re.compile(
    r"^(?P<prefix>.+)-(?P<index>\d{5})-of-(?P<total>\d{5})(?P<suffix>\.gguf)$",
    re.IGNORECASE,
)
_GGUF_DEPENDENCY_MARKERS = (
    "adapter",
    "draft",
    "lora",
    "mmproj",
    "mtp",
    "projector",
)
_OTHER_WEIGHT_SUFFIXES = (
    ".bin",
    ".ckpt",
    ".engine",
    ".h5",
    ".onnx",
    ".pt",
    ".pth",
    ".safetensors",
    ".tflite",
)


def discover_gguf_variants(files: tuple[SourceFile, ...]) -> tuple[GgufVariant, ...]:
    """Return only unambiguous, independently downloadable GGUF weight groups."""
    if not files or len(files) > 5_000:
        return ()
    lowered_paths = tuple(file.path.casefold() for file in files)
    if any(path.endswith(_OTHER_WEIGHT_SUFFIXES) for path in lowered_paths):
        return ()

    gguf_files = tuple(
        file
        for file in files
        if file.path.casefold().endswith(".gguf")
        and not any(
            marker in PurePath(file.path).name.casefold() for marker in _GGUF_DEPENDENCY_MARKERS
        )
    )
    if not gguf_files:
        return ()

    singles: list[GgufVariant] = []
    split_groups: dict[str, list[tuple[int, int, SourceFile]]] = {}
    labels: dict[str, str] = {}
    for file in gguf_files:
        match = _GGUF_SPLIT_PATTERN.match(file.path)
        if match is None:
            singles.append(GgufVariant(file.path, (file,)))
            continue
        label = f"{match.group('prefix')}{match.group('suffix')}"
        key = label.casefold()
        labels.setdefault(key, label)
        split_groups.setdefault(key, []).append(
            (int(match.group("index")), int(match.group("total")), file)
        )

    variants = singles
    for key, members in split_groups.items():
        totals = {total for _index, total, _file in members}
        if len(totals) != 1:
            return ()
        total = totals.pop()
        indexes = [index for index, _total, _file in members]
        if total < 1 or sorted(indexes) != list(range(1, total + 1)):
            return ()
        variants.append(
            GgufVariant(
                labels[key],
                tuple(file for _index, _total, file in sorted(members)),
            )
        )

    variant_labels = [variant.label.casefold() for variant in variants]
    if len(variant_labels) != len(set(variant_labels)):
        return ()
    return tuple(sorted(variants, key=lambda variant: variant.label.casefold()))


@dataclass(frozen=True)
class DownloadEstimate:
    provider: Provider
    source_id: str
    requested_revision: str
    resolved_revision: str | None
    total_size: int | None
    file_count: int | None
    hub_url: str | None = None
    metadata: tuple[EstimateMetadata, ...] = ()
    files: tuple[SourceFile, ...] = ()

    def as_dict(self) -> dict[str, object]:
        result: dict[str, object] = {
            "provider": self.provider.value,
            "sourceId": self.source_id,
            "requestedRevision": self.requested_revision,
            "downloadable": True,
            "metadata": [item.as_dict() for item in self.metadata],
        }
        if self.resolved_revision:
            result["resolvedRevision"] = self.resolved_revision
        if self.total_size is not None:
            result["totalSize"] = self.total_size
        if self.file_count is not None:
            result["fileCount"] = self.file_count
        if self.hub_url:
            result["hubUrl"] = self.hub_url
        selectable = self.provider in MODELSCOPE_PROVIDERS | {Provider.HUGGINGFACE}
        variants = discover_gguf_variants(self.files) if selectable else ()
        result["ggufVariantSelectionAvailable"] = bool(variants)
        if variants:
            result["ggufVariants"] = [variant.as_dict() for variant in variants]
            auxiliary = [
                file.as_dict()
                for file in self.files
                if file.path.casefold().endswith(".gguf")
                and any(
                    marker in PurePath(file.path).name.casefold()
                    for marker in _GGUF_DEPENDENCY_MARKERS
                )
            ]
            if auxiliary:
                result["ggufAuxiliaryFiles"] = auxiliary
        return result


class ProviderUnavailable(RuntimeError):
    pass


class ProviderRequestError(RuntimeError):
    """Provider failure whose sanitized message is safe to return to an authenticated user."""


class IsolatedProviderError(RuntimeError):
    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


_PROVIDER_LABELS = {
    Provider.HUGGINGFACE: "Hugging Face",
    Provider.MODELSCOPE_CN: "ModelScope CN",
    Provider.MODELSCOPE_AI: "ModelScope AI",
    Provider.GITHUB_RELEASE: "GitHub Releases",
    Provider.KAGGLE: "Kaggle",
    Provider.HTTP: "Generic HTTP",
    Provider.FILESYSTEM: "Filesystem",
}
_SECRET_ENV_MARKERS = ("TOKEN", "PASSWORD", "SECRET", "API_KEY", "APIKEY")


def safe_error_message(error: BaseException | str, *, secrets: Iterable[str] = ()) -> str:
    """Return useful error detail without echoing credentials or unbounded upstream bodies."""

    message = str(error).strip()
    if not message and isinstance(error, BaseException):
        message = type(error).__name__
    for name, value in os.environ.items():
        secret_name = any(marker in name.upper() for marker in _SECRET_ENV_MARKERS)
        if value and len(value) >= 4 and secret_name:
            message = message.replace(value, "[redacted]")
    for secret in secrets:
        if secret and len(secret) >= 4:
            message = message.replace(secret, "[redacted]")
    message = re.sub(r"(?i)(https?://)[^/@\s]+@", r"\1[redacted]@", message)
    message = re.sub(
        r"(?i)(\b(?:authorization|proxy-authorization)\s*[:=]\s*)(?:bearer|basic)?\s*[^\s,;]+",
        r"\1[redacted]",
        message,
    )
    message = re.sub(
        r"(?i)([?&](?:access_token|api[_-]?key|token|signature|sig|x-amz-signature)=)[^&\s]+",
        r"\1[redacted]",
        message,
    )
    message = re.sub(
        r"(?i)(\b(?:password|passwd|token|secret|api[_-]?key)\s*[:=]\s*)[^\s,;]+",
        r"\1[redacted]",
        message,
    )
    return message[:4_000] or "unknown error"


def provider_failure_detail(provider: Provider, operation: str, error: BaseException) -> str:
    """Build a user-visible provider failure that retains the original cause."""

    message = safe_error_message(error)
    error_type = type(error).__name__
    label = _PROVIDER_LABELS.get(provider, provider.value)
    if isinstance(error, (ProviderRequestError, ProviderUnavailable, ValueError)):
        return f"{label} {operation} failed: {message}"
    return f"{label} {operation} failed ({error_type}): {message}"


def _provider_endpoint(provider: Provider, mirror: str | None, disable_mirror: bool) -> str | None:
    if provider is Provider.HUGGINGFACE:
        return OFFICIAL_HUGGINGFACE_ENDPOINT if disable_mirror or not mirror else mirror.rstrip("/")
    if provider in MODELSCOPE_PROVIDERS:
        official = _modelscope_official_endpoint(provider)
        return official if disable_mirror or not mirror else mirror.rstrip("/")
    return None


def _modelscope_official_endpoint(provider: Provider) -> str:
    return (
        OFFICIAL_MODELSCOPE_CN_ENDPOINT
        if provider is Provider.MODELSCOPE_CN
        else OFFICIAL_MODELSCOPE_AI_ENDPOINT
    )


def _modelscope_token_name(provider: Provider) -> str:
    return (
        "MODELSCOPE_CN_API_TOKEN"
        if provider is Provider.MODELSCOPE_CN
        else "MODELSCOPE_AI_API_TOKEN"
    )


def _modelscope_token(provider: Provider) -> str | None:
    return os.environ.get(_modelscope_token_name(provider)) or None


def _huggingface_token() -> str | None:
    return os.environ.get("HF_TOKEN") or None


def _modelscope_mirror(
    provider: Provider, cn_mirror: str | None, ai_mirror: str | None
) -> str | None:
    return cn_mirror if provider is Provider.MODELSCOPE_CN else ai_mirror


def _translate_modelscope_error(
    error: Exception, *, provider: Provider, endpoint: str
) -> ProviderRequestError | None:
    error_name = type(error).__name__
    status_code = getattr(error, "status_code", None) or getattr(
        getattr(error, "response", None), "status_code", None
    )
    if error_name == "CacheError":
        return ProviderRequestError(
            "ModelScope SDK storage is not writable. Configure MODELSCOPE_HOME and "
            "MODELSCOPE_CACHE to writable directories."
        )
    if error_name == "AuthenticationError" or status_code in {401, 403}:
        upstream_host = urlparse(endpoint).hostname or endpoint
        source_host = urlparse(_modelscope_official_endpoint(provider)).hostname
        token_name = _modelscope_token_name(provider)
        return ProviderRequestError(
            f"ModelScope authentication failed while contacting {upstream_host}. Tokens are "
            f"site-scoped; {token_name} must contain a token issued by {source_host}. Clear it "
            "when accessing a public model if the configured token is stale or belongs to the "
            "other ModelScope site."
        )
    return None


def _directory_size(root: Path) -> int:
    return sum(
        item.stat().st_size
        for item in root.rglob("*")
        if item.is_file() and ".git" not in item.relative_to(root).parts
    )


async def _blocking_download(
    operation: Callable[[], str],
    destination: Path,
    progress: Progress,
    measure: Callable[[Path], int] = _directory_size,
) -> str:
    future = asyncio.create_task(asyncio.to_thread(operation))
    while not future.done():
        await progress(measure(destination), None)
        await asyncio.sleep(0.5)
    result = await future
    final_size = _directory_size(destination)
    await progress(final_size, final_size)
    return result


def _optional_import_error(provider: str, package: str, error: ImportError) -> ProviderUnavailable:
    return ProviderUnavailable(
        f"{provider} provider is not installed; install modelshelf-server[providers] "
        f"(missing {package}: {error})"
    )


def _order_revision_options(
    default_revision: str, options: list[RevisionOption]
) -> tuple[RevisionOption, ...]:
    unique: dict[str, RevisionOption] = {}
    for option in options:
        unique.setdefault(option.name, option)
    ordered = list(unique.values())
    ordered.sort(key=lambda option: option.name != default_revision)
    return tuple(ordered[:200])


def _unique_model_options(options: list[ModelOption]) -> tuple[ModelOption, ...]:
    unique: dict[str, ModelOption] = {}
    for option in options:
        if option.id:
            unique.setdefault(option.id, option)
    return tuple(unique.values())[:50]


def _metadata(*items: tuple[str, object | None]) -> tuple[EstimateMetadata, ...]:
    return tuple(
        EstimateMetadata(label, str(value))
        for label, value in items
        if value is not None and value != ""
    )


async def _search_huggingface_models(query: str) -> ModelSearch:
    try:
        from huggingface_hub import HfApi
    except ImportError as error:
        raise _optional_import_error("Hugging Face", "huggingface-hub", error) from error

    def operation() -> list[object]:
        return list(HfApi(token=_huggingface_token()).list_models(search=query, limit=30))

    models = await asyncio.to_thread(operation)
    options = []
    for model in models:
        model_id = getattr(model, "id", "")
        downloads = getattr(model, "downloads", None)
        pipeline = getattr(model, "pipeline_tag", None)
        detail_parts = [
            str(part) for part in (pipeline, downloads and f"{downloads} downloads") if part
        ]
        options.append(ModelOption(model_id, model_id, " · ".join(detail_parts) or None))
    return ModelSearch(Provider.HUGGINGFACE, query, _unique_model_options(options))


async def _search_modelscope_models(
    provider: Provider, query: str, endpoint: str, token: str | None
) -> ModelSearch:
    try:
        from modelscope_hub.api import HubApi
    except ImportError as error:
        raise _optional_import_error("ModelScope", "modelscope-hub", error) from error

    def operation() -> dict[str, Any]:
        api = HubApi(endpoint=endpoint, token=token)
        return api.openapi.list_models(search=query, page_size=30)

    try:
        response = await asyncio.to_thread(operation)
    except Exception as error:
        translated = _translate_modelscope_error(error, provider=provider, endpoint=endpoint)
        if translated:
            raise translated from error
        raise
    options = []
    for model in response.get("models", []):
        if not isinstance(model, dict):
            continue
        model_id = model.get("id")
        if not isinstance(model_id, str):
            continue
        name = model.get("display_name")
        tasks = model.get("tasks")
        detail = ", ".join(str(task) for task in tasks) if isinstance(tasks, list) else None
        options.append(
            ModelOption(model_id, name if isinstance(name, str) and name else model_id, detail)
        )
    return ModelSearch(provider, query, _unique_model_options(options))


async def _search_github_repositories(query: str, token: str | None) -> ModelSearch:
    api_base = os.environ.get("GITHUB_API_URL", "https://api.github.com").rstrip("/")
    async with httpx.AsyncClient(headers=_github_headers(token), timeout=15) as client:
        response = await client.get(
            f"{api_base}/search/repositories", params={"q": query, "per_page": 30}
        )
        response.raise_for_status()
    options = []
    for repository in response.json().get("items", []):
        repository_id = repository.get("full_name")
        if not isinstance(repository_id, str):
            continue
        description = repository.get("description")
        stars = repository.get("stargazers_count")
        detail_parts = [
            str(part) for part in (description, isinstance(stars, int) and f"★ {stars}") if part
        ]
        options.append(ModelOption(repository_id, repository_id, " · ".join(detail_parts) or None))
    return ModelSearch(Provider.GITHUB_RELEASE, query, _unique_model_options(options))


def _search_kaggle_models_blocking(query: str) -> ModelSearch:
    try:
        from kagglehub.clients import build_kaggle_client  # type: ignore[import-untyped]
        from kagglehub.enum import enum_to_str  # type: ignore[import-untyped]
        from kagglesdk.models.types.model_api_service import (  # type: ignore[import-untyped]
            ApiListModelsRequest,
        )
    except ImportError as error:
        raise _optional_import_error("Kaggle", "kagglehub", error) from error
    request = ApiListModelsRequest()
    request.search = query
    request.page_size = 15
    with build_kaggle_client() as client:
        response = client.models.model_api_client.list_models(request)
    options = []
    for model in response.models or []:
        if not model or not model.ref:
            continue
        for instance in model.instances or []:
            if not instance or not instance.slug:
                continue
            framework = enum_to_str(instance.framework)
            model_id = f"{model.ref}/{framework}/{instance.slug}"
            options.append(ModelOption(model_id, model.title or model.ref, model_id))
    return ModelSearch(Provider.KAGGLE, query, _unique_model_options(options))


async def search_models(
    provider: Provider,
    query: str,
    *,
    github_token: str | None,
    modelscope_cn_mirror: str | None = None,
    modelscope_ai_mirror: str | None = None,
) -> ModelSearch:
    query = query.strip()
    if len(query) < 2:
        raise ValueError("model search query must contain at least 2 characters")
    if provider is Provider.HUGGINGFACE:
        return await _search_huggingface_models(query)
    if provider in MODELSCOPE_PROVIDERS:
        endpoint = _provider_endpoint(
            provider,
            _modelscope_mirror(provider, modelscope_cn_mirror, modelscope_ai_mirror),
            False,
        )
        assert endpoint is not None
        return await _search_modelscope_models(
            provider, query, endpoint, _modelscope_token(provider)
        )
    if provider is Provider.GITHUB_RELEASE:
        return await _search_github_repositories(query, github_token)
    if provider is Provider.KAGGLE:
        return await asyncio.to_thread(_search_kaggle_models_blocking, query)
    if provider is Provider.FILESYSTEM:
        return ModelSearch(provider, query, (), supports_search=False)
    return ModelSearch(Provider.HTTP, query, (), supports_search=False)


async def _discover_huggingface_revisions(source_id: str) -> RevisionDiscovery:
    try:
        from huggingface_hub import HfApi
    except ImportError as error:
        raise _optional_import_error("Hugging Face", "huggingface-hub", error) from error
    refs = await asyncio.to_thread(
        HfApi(token=_huggingface_token()).list_repo_refs,
        source_id,
        repo_type="model",
    )
    options = [RevisionOption(ref.name, "branch", ref.target_commit) for ref in refs.branches] + [
        RevisionOption(ref.name, "tag", ref.target_commit) for ref in refs.tags
    ]
    names = {option.name for option in options}
    default = "main" if "main" in names else (options[0].name if options else "main")
    return RevisionDiscovery(
        Provider.HUGGINGFACE,
        source_id,
        default,
        _order_revision_options(default, options),
    )


async def _discover_modelscope_revisions(
    provider: Provider, source_id: str, endpoint: str, token: str | None
) -> RevisionDiscovery:
    try:
        from modelscope_hub.compat import LegacyHubApi
    except ImportError as error:
        raise _optional_import_error("ModelScope", "modelscope-hub", error) from error
    api = LegacyHubApi(endpoint=endpoint, token=token)
    try:
        branches, tags = await asyncio.to_thread(api.get_model_branches_and_tags, source_id)
    except Exception as error:
        translated = _translate_modelscope_error(error, provider=provider, endpoint=endpoint)
        if translated:
            raise translated from error
        raise
    options = [RevisionOption(name, "branch") for name in branches] + [
        RevisionOption(name, "tag") for name in tags
    ]
    names = {option.name for option in options}
    default = next(
        (candidate for candidate in ("master", "main") if candidate in names),
        options[0].name if options else "master",
    )
    return RevisionDiscovery(
        provider,
        source_id,
        default,
        _order_revision_options(default, options),
    )


def _github_headers(token: str | None) -> dict[str, str]:
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "modelshelf/0.1",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


async def _get_github_release(
    source_id: str,
    revision: str,
    token: str | None,
    *,
    proxy_url: str | None = None,
    disable_proxy: bool = False,
) -> dict[str, Any]:
    parts = source_id.split("/")
    if len(parts) != 2 or not all(parts):
        raise ValueError("GitHub release id must be owner/repository")
    owner, repo = parts
    api_base = os.environ.get("GITHUB_API_URL", "https://api.github.com").rstrip("/")
    endpoint = (
        f"{api_base}/repos/{owner}/{repo}/releases/latest"
        if revision == "latest"
        else f"{api_base}/repos/{owner}/{repo}/releases/tags/{revision}"
    )
    async with httpx.AsyncClient(
        headers=_github_headers(token),
        follow_redirects=True,
        timeout=20,
        proxy=None if disable_proxy else proxy_url,
        trust_env=False,
    ) as client:
        response = await client.get(endpoint)
        response.raise_for_status()
    release = response.json()
    if not isinstance(release, dict):
        raise RuntimeError("GitHub returned invalid release metadata")
    return release


async def _discover_github_revisions(source_id: str, token: str | None) -> RevisionDiscovery:
    parts = source_id.split("/")
    if len(parts) != 2 or not all(parts):
        raise ValueError("GitHub release id must be owner/repository")
    owner, repo = parts
    api_base = os.environ.get("GITHUB_API_URL", "https://api.github.com").rstrip("/")
    async with httpx.AsyncClient(headers=_github_headers(token), timeout=15) as client:
        response = await client.get(
            f"{api_base}/repos/{owner}/{repo}/releases", params={"per_page": 100}
        )
        response.raise_for_status()
    releases = response.json()
    options = [RevisionOption("latest", "alias")]
    for release in releases:
        if release.get("draft"):
            continue
        tag = release.get("tag_name")
        release_id = release.get("id")
        if not isinstance(tag, str) or not tag or not isinstance(release_id, int):
            continue
        kind = "prerelease" if release.get("prerelease") else "release"
        options.append(RevisionOption(tag, kind, f"release:{release_id}:{tag}"))
    return RevisionDiscovery(
        Provider.GITHUB_RELEASE,
        source_id,
        "latest",
        _order_revision_options("latest", options),
    )


def _discover_kaggle_revisions_blocking(source_id: str) -> RevisionDiscovery:
    try:
        from kagglehub.clients import build_kaggle_client
        from kagglehub.enum import to_enum
        from kagglesdk.models.types.model_api_service import (
            ApiListModelInstanceVersionsRequest,
        )
        from kagglesdk.models.types.model_enums import (  # type: ignore[import-untyped]
            ModelFramework,
        )
    except ImportError as error:
        raise _optional_import_error("Kaggle", "kagglehub", error) from error
    parts = source_id.strip("/").split("/")
    if len(parts) != 4 or not all(parts):
        raise ValueError("Kaggle model id must be owner/model/framework/variation")
    owner, model, framework, variation = parts
    versions: set[int] = set()
    page_token = ""
    with build_kaggle_client() as client:
        for _page in range(5):
            request = ApiListModelInstanceVersionsRequest()
            request.owner_slug = owner
            request.model_slug = model
            request.framework = to_enum(ModelFramework, framework)
            request.instance_slug = variation
            request.page_size = 100
            if page_token:
                request.page_token = page_token
            response = client.models.model_api_client.list_model_instance_versions(request)
            version_list = response.version_list
            for version in version_list.versions if version_list else []:
                if version and version.variation_slug == variation and version.version_number > 0:
                    versions.add(version.version_number)
            page_token = response.next_page_token
            if not page_token or len(versions) >= 200:
                break
    options = [RevisionOption("latest", "alias")]
    options.extend(
        RevisionOption(str(version), "version", f"version:{version}")
        for version in sorted(versions, reverse=True)
    )
    return RevisionDiscovery(
        Provider.KAGGLE,
        source_id,
        "latest",
        _order_revision_options("latest", options),
    )


async def discover_revisions(
    provider: Provider,
    source_id: str,
    *,
    github_token: str | None,
    modelscope_cn_mirror: str | None = None,
    modelscope_ai_mirror: str | None = None,
) -> RevisionDiscovery:
    source_id = source_id.strip()
    if not source_id:
        raise ValueError("model id is required")
    if provider is Provider.HUGGINGFACE:
        return await _discover_huggingface_revisions(source_id)
    if provider in MODELSCOPE_PROVIDERS:
        endpoint = _provider_endpoint(
            provider,
            _modelscope_mirror(provider, modelscope_cn_mirror, modelscope_ai_mirror),
            False,
        )
        assert endpoint is not None
        return await _discover_modelscope_revisions(
            provider, source_id, endpoint, _modelscope_token(provider)
        )
    if provider is Provider.GITHUB_RELEASE:
        return await _discover_github_revisions(source_id, github_token)
    if provider is Provider.KAGGLE:
        return await asyncio.to_thread(_discover_kaggle_revisions_blocking, source_id)
    if provider is Provider.FILESYSTEM:
        return RevisionDiscovery(
            provider,
            source_id,
            "content",
            (),
            supports_discovery=False,
        )
    return RevisionDiscovery(
        Provider.HTTP,
        source_id,
        "content",
        (),
        supports_discovery=False,
    )


async def _estimate_huggingface(source_id: str, revision: str, endpoint: str) -> DownloadEstimate:
    try:
        from huggingface_hub import HfApi
    except ImportError as error:
        raise _optional_import_error("Hugging Face", "huggingface-hub", error) from error
    info = await asyncio.to_thread(
        HfApi(endpoint=endpoint, token=_huggingface_token()).model_info,
        source_id,
        revision=revision,
        files_metadata=True,
    )
    siblings = list(info.siblings or [])
    sizes = [getattr(item, "size", None) for item in siblings]
    total_size = (
        sum(size for size in sizes if isinstance(size, int))
        if all(isinstance(size, int) for size in sizes)
        else None
    )
    return DownloadEstimate(
        Provider.HUGGINGFACE,
        source_id,
        revision,
        info.sha,
        total_size,
        len(siblings),
        (f"{OFFICIAL_HUGGINGFACE_ENDPOINT}/{source_id}/tree/{quote(revision, safe='')}"),
        _metadata(
            ("Task", getattr(info, "pipeline_tag", None)),
            ("Library", getattr(info, "library_name", None)),
            ("Last modified", getattr(info, "last_modified", None)),
        ),
        tuple(
            SourceFile(str(getattr(item, "rfilename", "")), getattr(item, "size", None))
            for item in siblings
            if getattr(item, "rfilename", None)
        ),
    )


async def _estimate_modelscope(
    provider: Provider,
    source_id: str,
    revision: str,
    endpoint: str,
    token: str | None,
) -> DownloadEstimate:
    resolved = await _resolve_modelscope_revision(
        provider, source_id, revision, endpoint=endpoint, token=token
    )
    total_size, files = await asyncio.to_thread(
        _estimate_modelscope_git,
        source_id,
        revision,
        resolved,
        endpoint,
        token,
    )
    official_endpoint = _modelscope_official_endpoint(provider)
    return DownloadEstimate(
        provider,
        source_id,
        revision,
        resolved,
        total_size,
        len(files),
        f"{official_endpoint}/models/{source_id}",
        (),
        files,
    )


async def _estimate_github_release(
    source_id: str,
    revision: str,
    token: str | None,
    proxy_url: str | None,
    disable_proxy: bool,
) -> DownloadEstimate:
    release = await _get_github_release(
        source_id, revision, token, proxy_url=proxy_url, disable_proxy=disable_proxy
    )
    assets = release.get("assets")
    if not isinstance(assets, list) or not assets:
        raise ValueError("GitHub release has no downloadable assets")
    sizes = [asset.get("size") for asset in assets if isinstance(asset, dict)]
    total_size = (
        sum(size for size in sizes if isinstance(size, int))
        if sizes and all(isinstance(size, int) for size in sizes)
        else None
    )
    release_id = release.get("id")
    tag = release.get("tag_name")
    if not isinstance(release_id, int) or not isinstance(tag, str) or not tag:
        raise RuntimeError("GitHub did not return a release id and tag")
    return DownloadEstimate(
        Provider.GITHUB_RELEASE,
        source_id,
        revision,
        f"release:{release_id}:{tag}",
        total_size,
        len(assets),
        release.get("html_url") if isinstance(release.get("html_url"), str) else None,
        _metadata(
            ("Release", release.get("name") or tag),
            ("Published", release.get("published_at")),
            ("Prerelease", "yes" if release.get("prerelease") else None),
        ),
    )


def _estimate_kaggle_blocking(source_id: str, revision: str) -> DownloadEstimate:
    try:
        from kagglehub.clients import build_kaggle_client
        from kagglehub.enum import enum_to_str, to_enum
        from kagglesdk.models.types.model_api_service import (
            ApiGetModelInstanceRequest,
            ApiListModelInstanceVersionsRequest,
        )
        from kagglesdk.models.types.model_enums import (
            ModelFramework,
        )
    except ImportError as error:
        raise _optional_import_error("Kaggle", "kagglehub", error) from error
    parts = source_id.strip("/").split("/")
    if len(parts) != 4 or not all(parts):
        raise ValueError("Kaggle model id must be owner/model/framework/variation")
    owner, model, framework, variation = parts
    framework_value = to_enum(ModelFramework, framework)
    instance_request = ApiGetModelInstanceRequest()
    instance_request.owner_slug = owner
    instance_request.model_slug = model
    instance_request.framework = framework_value
    instance_request.instance_slug = variation
    with build_kaggle_client() as client:
        instance = client.models.model_api_client.get_model_instance(instance_request)
        latest = instance.version_number
        if revision in {"latest", "main"}:
            selected = latest
        else:
            try:
                selected = int(revision)
            except ValueError as error:
                raise ValueError("Kaggle revision must be latest or a numeric version") from error
            if selected <= 0:
                raise ValueError("Kaggle revision must be a positive version number")
            if selected != latest:
                request = ApiListModelInstanceVersionsRequest()
                request.owner_slug = owner
                request.model_slug = model
                request.framework = framework_value
                request.instance_slug = variation
                request.page_size = 100
                found = False
                page_token = ""
                for _page in range(20):
                    if page_token:
                        request.page_token = page_token
                    response = client.models.model_api_client.list_model_instance_versions(request)
                    version_list = response.version_list
                    found = any(
                        item
                        and item.variation_slug == variation
                        and item.version_number == selected
                        for item in (version_list.versions if version_list else [])
                    )
                    page_token = response.next_page_token
                    if found or not page_token:
                        break
                if not found:
                    raise ValueError(f"Kaggle model version {selected} was not found")
    total_size = instance.total_uncompressed_bytes if selected == latest else None
    return DownloadEstimate(
        Provider.KAGGLE,
        source_id,
        revision,
        f"version:{selected}",
        total_size or None,
        None,
        instance.url or None,
        _metadata(
            ("Framework", enum_to_str(instance.framework)),
            ("Version", selected),
            ("Size basis", "uncompressed payload" if total_size else None),
            ("License", instance.license_name),
            ("Fine-tunable", "yes" if instance.fine_tunable else None),
        ),
    )


async def _estimate_http(
    source_id: str,
    revision: str,
    proxy_url: str | None,
    disable_proxy: bool,
) -> DownloadEstimate:
    if revision != "content":
        raise ValueError("Generic HTTP requested revision must be content")
    parsed = urlparse(source_id)
    if parsed.scheme not in {"http", "https"}:
        raise ValueError("Generic HTTP provider accepts only HTTP(S) URLs")
    async with httpx.AsyncClient(
        follow_redirects=True,
        timeout=20,
        proxy=None if disable_proxy else proxy_url,
        trust_env=False,
    ) as client:
        response = await client.request("HEAD", source_id)
        if response.status_code in {405, 501}:
            request = client.build_request("GET", source_id)
            response = await client.send(request, stream=True)
        try:
            response.raise_for_status()
            total_raw = response.headers.get("content-length")
            total_size = int(total_raw) if total_raw and total_raw.isdigit() else None
            disposition = response.headers.get("content-disposition")
            from_header = _content_disposition_filename(disposition)
            from_url = Path(unquote(urlparse(str(response.url)).path)).name
            filename = from_header or from_url or None
            content_type = response.headers.get("content-type")
        finally:
            await response.aclose()
    return DownloadEstimate(
        Provider.HTTP,
        source_id,
        revision,
        None,
        total_size,
        1,
        source_id,
        _metadata(("File", filename), ("Content type", content_type)),
    )


def _direct_worker_environment() -> dict[str, str]:
    environment = os.environ.copy()
    for name in (
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "http_proxy",
        "https_proxy",
        "all_proxy",
    ):
        environment.pop(name, None)
    environment["NO_PROXY"] = "*"
    environment["no_proxy"] = "*"
    return environment


def _worker_error(record: dict[str, Any]) -> Exception:
    message = safe_error_message(str(record.get("message") or "isolated provider operation failed"))
    kind = record.get("errorKind")
    if kind == "ValueError":
        return ValueError(message)
    if kind == "ProviderUnavailable":
        return ProviderUnavailable(message)
    if kind == "ProviderRequestError":
        return ProviderRequestError(message)
    status_code = record.get("statusCode")
    if isinstance(kind, str) and kind:
        message = f"{kind}: {message}"
    return IsolatedProviderError(
        message,
        status_code if isinstance(status_code, int) else None,
    )


def _worker_process_error(operation: str, returncode: int | None, stderr: bytes) -> Exception:
    detail = safe_error_message(stderr.decode(errors="replace").strip())
    if detail == "unknown error":
        detail = "worker exited without a structured error record"
    return IsolatedProviderError(
        f"isolated provider {operation} failed (exit code {returncode}): {detail}"
    )


def _estimate_from_dict(data: dict[str, Any]) -> DownloadEstimate:
    metadata = tuple(
        EstimateMetadata(str(item["label"]), str(item["value"]))
        for item in data.get("metadata", [])
        if isinstance(item, dict) and "label" in item and "value" in item
    )
    files = tuple(
        SourceFile(str(item["path"]), item.get("size"))
        for item in data.get("files", [])
        if isinstance(item, dict) and item.get("path")
    )
    return DownloadEstimate(
        Provider(data["provider"]),
        str(data["sourceId"]),
        str(data["requestedRevision"]),
        data.get("resolvedRevision"),
        data.get("totalSize"),
        data.get("fileCount"),
        data.get("hubUrl"),
        metadata,
        files,
    )


async def _isolated_estimate(
    provider: Provider,
    source_id: str,
    revision: str,
    *,
    github_token: str | None,
    huggingface_mirror: str | None,
    modelscope_cn_mirror: str | None,
    modelscope_ai_mirror: str | None,
    disable_mirror: bool,
    mirror_url: str | None,
) -> DownloadEstimate:
    payload = {
        "operation": "estimate",
        "provider": provider.value,
        "sourceId": source_id,
        "revision": revision,
        "githubToken": github_token,
        "huggingfaceMirror": huggingface_mirror,
        "modelscopeCnMirror": modelscope_cn_mirror,
        "modelscopeAiMirror": modelscope_ai_mirror,
        "disableMirror": disable_mirror,
        "mirrorUrl": mirror_url,
    }
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "modelshelf_server.provider_worker",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=_direct_worker_environment(),
    )
    stdout, stderr = await process.communicate(json.dumps(payload).encode())
    try:
        records = [
            json.loads(line.removeprefix(_WORKER_PREFIX))
            for line in stdout.decode(errors="replace").splitlines()
            if line.startswith(_WORKER_PREFIX)
        ]
    except (json.JSONDecodeError, UnicodeDecodeError) as error:
        raise IsolatedProviderError(
            f"isolated provider preflight returned an invalid result: {safe_error_message(error)}"
        ) from error
    if not records:
        raise _worker_process_error("preflight", process.returncode, stderr)
    record = records[-1]
    if record.get("type") == "error":
        raise _worker_error(record)
    if process.returncode != 0 or record.get("type") != "result":
        raise _worker_process_error("preflight", process.returncode, stderr)
    return _estimate_from_dict(record["estimate"])


async def _isolated_download(
    provider: Provider,
    source_id: str,
    revision: str,
    destination: Path,
    progress: Progress,
    *,
    github_token: str | None,
    huggingface_mirror: str | None,
    modelscope_cn_mirror: str | None,
    modelscope_ai_mirror: str | None,
    disable_mirror: bool,
    mirror_url: str | None,
    direct: bool,
    expected_resolved_revision: str | None,
    selected_paths: list[str] | None,
) -> ProviderResult:
    payload = {
        "operation": "download",
        "provider": provider.value,
        "sourceId": source_id,
        "revision": revision,
        "destination": str(destination),
        "githubToken": github_token,
        "huggingfaceMirror": huggingface_mirror,
        "modelscopeCnMirror": modelscope_cn_mirror,
        "modelscopeAiMirror": modelscope_ai_mirror,
        "disableMirror": disable_mirror,
        "mirrorUrl": mirror_url,
        "expectedResolvedRevision": expected_resolved_revision,
        "selectedPaths": selected_paths,
    }
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "modelshelf_server.provider_worker",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=_direct_worker_environment() if direct else os.environ.copy(),
    )
    assert process.stdin is not None
    assert process.stdout is not None
    assert process.stderr is not None
    process.stdin.write(json.dumps(payload).encode())
    await process.stdin.drain()
    process.stdin.close()
    stderr_task = asyncio.create_task(process.stderr.read())
    final: dict[str, Any] | None = None
    stderr = b""
    try:
        while line := await process.stdout.readline():
            decoded = line.decode().strip()
            if not decoded.startswith(_WORKER_PREFIX):
                continue
            try:
                record = json.loads(decoded.removeprefix(_WORKER_PREFIX))
            except json.JSONDecodeError as error:
                raise IsolatedProviderError(
                    "isolated provider download returned an invalid result: "
                    f"{safe_error_message(error)}"
                ) from error
            if record.get("type") == "progress":
                await progress(int(record["downloaded"]), record.get("total"))
            else:
                final = record
        await process.wait()
        stderr = await stderr_task
    except asyncio.CancelledError:
        if process.returncode is None:
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), timeout=5)
            except TimeoutError:
                process.kill()
                await process.wait()
        stderr_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await stderr_task
        raise
    if final and final.get("type") == "error":
        raise _worker_error(final)
    if process.returncode != 0 or not final or final.get("type") != "result":
        raise _worker_process_error("download", process.returncode, stderr)
    result = final["result"]
    return ProviderResult(
        resolved_revision=str(result["resolvedRevision"]),
        source_url=result.get("sourceUrl"),
        downloaded_file=result.get("downloadedFile"),
        content_disposition=result.get("contentDisposition"),
    )


async def estimate_download(
    provider: Provider,
    source_id: str,
    revision: str,
    *,
    github_token: str | None,
    huggingface_mirror: str | None = None,
    modelscope_cn_mirror: str | None = None,
    modelscope_ai_mirror: str | None = None,
    proxy_url: str | None = None,
    disable_mirror: bool = False,
    mirror_url: str | None = None,
    disable_proxy: bool = False,
    _isolated: bool = False,
) -> DownloadEstimate:
    source_id = source_id.strip()
    revision = revision.strip()
    if not source_id:
        raise ValueError("model id is required")
    if not revision:
        raise ValueError("requested revision is required")
    if (
        disable_proxy
        and proxy_url
        and provider in ({Provider.HUGGINGFACE, Provider.KAGGLE} | MODELSCOPE_PROVIDERS)
        and not _isolated
    ):
        return await _isolated_estimate(
            provider,
            source_id,
            revision,
            github_token=github_token,
            huggingface_mirror=huggingface_mirror,
            modelscope_cn_mirror=modelscope_cn_mirror,
            modelscope_ai_mirror=modelscope_ai_mirror,
            disable_mirror=disable_mirror,
            mirror_url=mirror_url,
        )
    if provider is Provider.HUGGINGFACE:
        endpoint = _provider_endpoint(provider, mirror_url or huggingface_mirror, disable_mirror)
        assert endpoint is not None
        return await _estimate_huggingface(source_id, revision, endpoint)
    if provider in MODELSCOPE_PROVIDERS:
        endpoint = _provider_endpoint(
            provider,
            mirror_url or _modelscope_mirror(provider, modelscope_cn_mirror, modelscope_ai_mirror),
            disable_mirror,
        )
        assert endpoint is not None
        return await _estimate_modelscope(
            provider, source_id, revision, endpoint, _modelscope_token(provider)
        )
    if provider is Provider.GITHUB_RELEASE:
        return await _estimate_github_release(
            source_id, revision, github_token, proxy_url, disable_proxy
        )
    if provider is Provider.KAGGLE:
        return await asyncio.to_thread(_estimate_kaggle_blocking, source_id, revision)
    if provider is Provider.FILESYSTEM:
        raise ValueError("filesystem artifacts must be created with modelshelf-server import")
    return await _estimate_http(source_id, revision, proxy_url, disable_proxy)


async def download_huggingface(
    source_id: str,
    revision: str,
    destination: Path,
    progress: Progress,
    endpoint: str,
    selected_paths: list[str] | None = None,
) -> ProviderResult:
    cache = destination.parent / ".huggingface-cache"
    os.environ["HF_HOME"] = str(cache)
    os.environ["HF_HUB_CACHE"] = str(cache / "hub")
    os.environ["HF_XET_CACHE"] = str(cache / "xet")
    try:
        from huggingface_hub import HfApi, snapshot_download
    except ImportError as error:
        raise _optional_import_error("Hugging Face", "huggingface-hub", error) from error
    token = _huggingface_token()
    info = await asyncio.to_thread(
        HfApi(endpoint=endpoint, token=token).model_info,
        source_id,
        revision=revision,
    )
    resolved = info.sha
    if not resolved:
        raise RuntimeError("Hugging Face did not return an immutable commit SHA")

    def operation() -> str:
        return snapshot_download(
            repo_id=source_id,
            revision=resolved,
            local_dir=destination,
            cache_dir=cache / "hub",
            token=token,
            endpoint=endpoint,
            allow_patterns=[glob.escape(path) for path in selected_paths]
            if selected_paths
            else None,
        )

    try:
        await _blocking_download(operation, destination, progress)
    finally:
        shutil.rmtree(cache, ignore_errors=True)
        shutil.rmtree(destination / ".cache" / "huggingface", ignore_errors=True)
    return ProviderResult(resolved_revision=resolved)


async def download_modelscope(
    provider: Provider,
    source_id: str,
    revision: str,
    destination: Path,
    progress: Progress,
    endpoint: str,
    token: str | None,
    expected_resolved_revision: str | None = None,
    selected_paths: list[str] | None = None,
) -> ProviderResult:
    resolved = await _resolve_modelscope_revision(
        provider, source_id, revision, endpoint=endpoint, token=token
    )
    if expected_resolved_revision and resolved != expected_resolved_revision:
        raise RuntimeError(
            "ModelScope requested revision changed after preflight: "
            f"expected {expected_resolved_revision}, got {resolved}"
        )
    if selected_paths:
        await _download_modelscope_selected(
            source_id,
            revision,
            destination,
            progress,
            endpoint,
            token,
            selected_paths,
        )
        resolved_after_download = await _resolve_modelscope_revision(
            provider, source_id, revision, endpoint=endpoint, token=token
        )
        if resolved_after_download != resolved:
            raise RuntimeError(
                "ModelScope requested revision changed during the selected-file download: "
                f"expected {resolved}, got {resolved_after_download}"
            )
    else:
        await _download_modelscope_git(
            source_id,
            revision,
            resolved,
            destination,
            progress,
            endpoint,
            token,
        )
    return ProviderResult(resolved_revision=resolved)


async def _download_modelscope_selected(
    source_id: str,
    revision: str,
    destination: Path,
    progress: Progress,
    endpoint: str,
    token: str | None,
    selected_paths: list[str],
) -> None:
    try:
        from modelscope_hub.compat import snapshot_download
    except ImportError as error:
        raise _optional_import_error("ModelScope", "modelscope-hub", error) from error
    cache = destination.parent / ".modelscope-cache"

    def operation() -> str:
        return snapshot_download(
            repo_id=source_id,
            repo_type="model",
            revision=revision,
            local_dir=str(destination),
            cache_dir=str(cache),
            allow_patterns=[glob.escape(path) for path in selected_paths],
            token=token,
            endpoint=endpoint,
        )

    try:
        await _blocking_download(operation, destination, progress)
    finally:
        shutil.rmtree(cache, ignore_errors=True)


async def _download_modelscope_git(
    source_id: str,
    revision: str,
    resolved_revision: str,
    destination: Path,
    progress: Progress,
    endpoint: str,
    token: str | None,
) -> None:
    lfs_paths: set[Path] = set()

    def operation() -> str:
        lfs = subprocess.run(
            ["git", "lfs", "version"],
            capture_output=True,
            text=True,
            check=False,
        )
        if lfs.returncode != 0:
            raise ProviderUnavailable("ModelScope downloads require git-lfs")
        shutil.rmtree(destination, ignore_errors=True)
        environment = _prepare_modelscope_git_checkout(
            source_id,
            revision,
            resolved_revision,
            destination,
            endpoint,
            token,
            skip_lfs=True,
        )
        lfs_paths.update(
            path.relative_to(destination)
            for path in destination.rglob("*")
            if path.is_file() and _modelscope_lfs_pointer_size(path) is not None
        )
        environment.pop("GIT_LFS_SKIP_SMUDGE", None)
        _checked_modelscope_git(["git", "-C", str(destination), "lfs", "pull"], environment)
        _checked_modelscope_git(["git", "-C", str(destination), "lfs", "fsck"], environment)
        shutil.rmtree(destination / ".git")
        return str(destination)

    def download_size(root: Path) -> int:
        git = root / ".git"
        if not git.exists():
            return _directory_size(root)
        worktree_size = sum(
            path.stat().st_size
            for path in root.rglob("*")
            if path.is_file()
            and ".git" not in path.relative_to(root).parts
            and path.relative_to(root) not in lfs_paths
        )
        lfs_size = sum(
            path.stat().st_size
            for directory in (git / "lfs" / "objects", git / "lfs" / "incomplete")
            if directory.exists()
            for path in directory.rglob("*")
            if path.is_file()
        )
        return worktree_size + lfs_size

    await _blocking_download(operation, destination, progress, download_size)


def _modelscope_git_environment(token: str | None, *, skip_lfs: bool) -> dict[str, str]:
    environment = os.environ.copy()
    if skip_lfs:
        environment["GIT_LFS_SKIP_SMUDGE"] = "1"
    if token:
        credential = base64.b64encode(f"oauth2:{token}".encode()).decode()
        environment.update(
            {
                "GIT_CONFIG_COUNT": "1",
                "GIT_CONFIG_KEY_0": "http.extraHeader",
                "GIT_CONFIG_VALUE_0": f"Authorization: Basic {credential}",
            }
        )
    return environment


def _checked_modelscope_git(command: list[str], environment: dict[str, str]) -> str:
    result = subprocess.run(
        command,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        detail = safe_error_message(result.stderr.strip() or result.stdout.strip())
        raise ProviderRequestError(f"ModelScope Git operation failed: {detail}")
    return result.stdout.strip()


def _prepare_modelscope_git_checkout(
    source_id: str,
    revision: str,
    resolved_revision: str,
    destination: Path,
    endpoint: str,
    token: str | None,
    *,
    skip_lfs: bool,
) -> dict[str, str]:
    if shutil.which("git") is None:
        raise ProviderUnavailable("ModelScope Git metadata lookup requires git")
    remote = f"{endpoint.rstrip('/')}/{source_id}.git"
    errors: list[ProviderRequestError] = []
    for attempt_token in (None, token) if token else (None,):
        shutil.rmtree(destination, ignore_errors=True)
        destination.mkdir(parents=True)
        environment = _modelscope_git_environment(attempt_token, skip_lfs=skip_lfs)
        try:
            _checked_modelscope_git(["git", "init", "--quiet", str(destination)], environment)
            _checked_modelscope_git(
                ["git", "-C", str(destination), "remote", "add", "origin", remote],
                environment,
            )
            _checked_modelscope_git(
                ["git", "-C", str(destination), "fetch", "--depth=1", "origin", revision],
                environment,
            )
            fetched = _checked_modelscope_git(
                ["git", "-C", str(destination), "rev-parse", "FETCH_HEAD^{commit}"],
                environment,
            )
            if fetched != resolved_revision:
                raise RuntimeError(
                    "ModelScope requested revision changed before download: "
                    f"expected {resolved_revision}, got {fetched}"
                )
            _checked_modelscope_git(
                [
                    "git",
                    "-C",
                    str(destination),
                    "checkout",
                    "--quiet",
                    "--detach",
                    "FETCH_HEAD",
                ],
                environment,
            )
            return environment
        except ProviderRequestError as error:
            errors.append(error)
    raise errors[-1]


def _modelscope_lfs_pointer_size(path: Path) -> int | None:
    if path.is_symlink():
        return None
    with path.open("rb") as stream:
        prefix = stream.read(512)
    if not prefix.startswith(b"version https://git-lfs.github.com/spec/v1\n"):
        return None
    match = re.search(rb"(?:^|\n)size (\d+)(?:\n|$)", prefix)
    return int(match.group(1)) if match else None


def _modelscope_git_file_size(path: Path) -> int:
    if path.is_symlink():
        return len(os.readlink(path).encode())
    return _modelscope_lfs_pointer_size(path) or path.stat().st_size


def _estimate_modelscope_git(
    source_id: str,
    revision: str,
    resolved_revision: str,
    endpoint: str,
    token: str | None,
) -> tuple[int, tuple[SourceFile, ...]]:
    with tempfile.TemporaryDirectory(prefix="modelshelf-modelscope-metadata-") as directory:
        destination = Path(directory) / "repository"
        _prepare_modelscope_git_checkout(
            source_id,
            revision,
            resolved_revision,
            destination,
            endpoint,
            token,
            skip_lfs=True,
        )
        paths = [
            path
            for path in destination.rglob("*")
            if (path.is_file() or path.is_symlink()) and ".git" not in path.parts
        ]
        files = tuple(
            SourceFile(
                path.relative_to(destination).as_posix(),
                _modelscope_git_file_size(path),
            )
            for path in paths
        )
        return sum(file.size or 0 for file in files), files


def _parse_ls_remote(output: str, revision: str) -> str:
    refs: dict[str, str] = {}
    for line in output.splitlines():
        fields = line.split("\t", 1)
        if len(fields) == 2 and re.fullmatch(r"[a-f0-9]{40,64}", fields[0]):
            refs[fields[1]] = fields[0]
    candidates = (
        f"refs/tags/{revision}^{{}}",
        f"refs/heads/{revision}",
        f"refs/tags/{revision}",
        revision if revision.startswith("refs/") else "",
    )
    for candidate in candidates:
        if candidate and candidate in refs:
            return refs[candidate]
    raise RuntimeError("ModelScope Git remote did not resolve the requested branch or tag")


async def _resolve_modelscope_revision(
    provider: Provider,
    source_id: str,
    revision: str,
    *,
    endpoint: str,
    token: str | None,
) -> str:
    if re.fullmatch(r"[a-f0-9]{40,64}", revision):
        return revision
    resolved_endpoint = endpoint.rstrip("/")
    url = f"{resolved_endpoint}/{source_id}.git"
    patterns = [
        f"refs/heads/{revision}",
        f"refs/tags/{revision}",
        f"refs/tags/{revision}^{{}}",
    ]
    # Public repositories should not be made dependent on an optional token being current or valid.
    # Try the documented anonymous Git path first, then use an ephemeral HTTP header for private
    # repositories. The credential is never embedded in the remote URL or error output.
    attempts = (None, token) if token else (None,)
    for attempt_token in attempts:
        environment = os.environ.copy()
        if attempt_token:
            credential = base64.b64encode(f"oauth2:{attempt_token}".encode()).decode()
            environment.update(
                {
                    "GIT_CONFIG_COUNT": "1",
                    "GIT_CONFIG_KEY_0": "http.extraHeader",
                    "GIT_CONFIG_VALUE_0": f"Authorization: Basic {credential}",
                }
            )
        process = await asyncio.create_subprocess_exec(
            "git",
            "ls-remote",
            url,
            *patterns,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=environment,
        )
        stdout, _stderr = await process.communicate()
        if process.returncode == 0:
            try:
                return _parse_ls_remote(stdout.decode(), revision)
            except RuntimeError:
                if attempt_token or not token:
                    raise
    upstream_host = urlparse(resolved_endpoint).hostname or resolved_endpoint
    source_host = urlparse(_modelscope_official_endpoint(provider)).hostname
    token_name = _modelscope_token_name(provider)
    raise ProviderRequestError(
        f"ModelScope could not resolve revision {revision!r} through {upstream_host}. Verify the "
        f"model ID, "
        f"revision and network access. If {token_name} is configured, it must be issued by "
        f"{source_host}; ModelScope CN and AI tokens are not interchangeable."
    )


@contextlib.contextmanager
def _temporary_environment(name: str, value: str) -> Iterator[None]:
    previous = os.environ.get(name)
    os.environ[name] = value
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = previous


async def download_kaggle(
    source_id: str, revision: str, destination: Path, progress: Progress
) -> ProviderResult:
    try:
        import kagglehub  # type: ignore[import-untyped]
    except ImportError as error:
        raise _optional_import_error("Kaggle", "kagglehub", error) from error
    parts = source_id.strip("/").split("/")
    if len(parts) != 4:
        raise ValueError("Kaggle model id must be owner/model/framework/variation")
    handle = source_id if revision in {"latest", "main"} else f"{source_id}/{revision}"
    cache = destination.parent / ".kaggle-cache"

    def operation() -> str:
        with _temporary_environment("KAGGLEHUB_CACHE", str(cache)):
            return str(kagglehub.model_download(handle, force_download=True))

    try:
        downloaded = Path(await _blocking_download(operation, cache, progress))
        match = re.search(r"[/\\]versions[/\\](\d+)(?:[/\\]|$)", str(downloaded))
        resolved = revision if revision.isdigit() else (match.group(1) if match else "")
        if not resolved:
            raise RuntimeError("Kaggle SDK did not expose the resolved immutable model version")
        shutil.copytree(downloaded, destination, dirs_exist_ok=True)
    finally:
        shutil.rmtree(cache, ignore_errors=True)
    await progress(_directory_size(destination), _directory_size(destination))
    return ProviderResult(resolved_revision=f"version:{resolved}")


async def download_github_release(
    source_id: str,
    revision: str,
    destination: Path,
    progress: Progress,
    token: str | None,
    proxy_url: str | None = None,
    disable_proxy: bool = False,
) -> ProviderResult:
    parts = source_id.split("/")
    if len(parts) != 2 or not all(parts):
        raise ValueError("GitHub release id must be owner/repository")
    owner, repo = parts
    headers = _github_headers(token)
    api_base = os.environ.get("GITHUB_API_URL", "https://api.github.com").rstrip("/")
    endpoint = (
        f"{api_base}/repos/{owner}/{repo}/releases/latest"
        if revision == "latest"
        else f"{api_base}/repos/{owner}/{repo}/releases/tags/{revision}"
    )
    destination.mkdir(parents=True, exist_ok=True)
    async with httpx.AsyncClient(
        headers=headers,
        follow_redirects=True,
        timeout=None,
        proxy=None if disable_proxy else proxy_url,
        trust_env=False,
    ) as client:
        response = await client.get(endpoint)
        response.raise_for_status()
        release = response.json()
        assets = release.get("assets", [])
        if not assets:
            raise RuntimeError(f"GitHub release {release.get('tag_name')} has no assets")
        total = sum(int(asset.get("size", 0)) for asset in assets)
        completed = 0
        for asset in assets:
            name = Path(str(asset["name"])).name
            async with client.stream("GET", str(asset["browser_download_url"])) as download:
                download.raise_for_status()
                with (destination / name).open("wb") as stream:
                    async for chunk in download.aiter_bytes():
                        stream.write(chunk)
                        completed += len(chunk)
                        await progress(completed, total)
    release_id = release.get("id")
    tag = release.get("tag_name")
    if release_id is None or not tag:
        raise RuntimeError("GitHub did not return a release id and tag")
    return ProviderResult(
        resolved_revision=f"release:{release_id}:{tag}", source_url=release.get("html_url")
    )


def _content_disposition_filename(value: str | None) -> str | None:
    if not value:
        return None
    message = email.message.Message()
    message["content-disposition"] = value
    filename = message.get_filename()
    return Path(filename).name if filename else None


async def download_http(
    source_id: str,
    destination: Path,
    progress: Progress,
    proxy_url: str | None = None,
    disable_proxy: bool = False,
) -> tuple[str, str | None, str | None]:
    parsed = urlparse(source_id)
    if parsed.scheme not in {"http", "https"}:
        raise ValueError("Generic HTTP provider accepts only HTTP(S) URLs")
    destination.mkdir(parents=True, exist_ok=True)
    async with (
        httpx.AsyncClient(
            follow_redirects=True,
            timeout=None,
            proxy=None if disable_proxy else proxy_url,
            trust_env=False,
        ) as client,
        client.stream("GET", source_id) as response,
    ):
        response.raise_for_status()
        disposition = response.headers.get("content-disposition")
        from_header = _content_disposition_filename(disposition)
        from_url = Path(unquote(urlparse(str(response.url)).path)).name
        filename = from_header or from_url or "download.bin"
        filename = Path(filename).name
        total_raw = response.headers.get("content-length")
        total = int(total_raw) if total_raw and total_raw.isdigit() else None
        downloaded = 0
        with (destination / filename).open("wb") as stream:
            async for chunk in response.aiter_bytes():
                stream.write(chunk)
                downloaded += len(chunk)
                await progress(downloaded, total)
        return filename, disposition, str(response.url)


async def run_provider(
    provider: Provider,
    source_id: str,
    revision: str,
    destination: Path,
    progress: Progress,
    *,
    github_token: str | None,
    huggingface_mirror: str | None = None,
    modelscope_cn_mirror: str | None = None,
    modelscope_ai_mirror: str | None = None,
    proxy_url: str | None = None,
    disable_mirror: bool = False,
    mirror_url: str | None = None,
    disable_proxy: bool = False,
    expected_resolved_revision: str | None = None,
    selected_paths: list[str] | None = None,
    _isolated: bool = False,
) -> ProviderResult:
    if provider in ISOLATED_DOWNLOAD_PROVIDERS and not _isolated:
        return await _isolated_download(
            provider,
            source_id,
            revision,
            destination,
            progress,
            github_token=github_token,
            huggingface_mirror=huggingface_mirror,
            modelscope_cn_mirror=modelscope_cn_mirror,
            modelscope_ai_mirror=modelscope_ai_mirror,
            disable_mirror=disable_mirror,
            mirror_url=mirror_url,
            direct=disable_proxy,
            expected_resolved_revision=expected_resolved_revision,
            selected_paths=selected_paths,
        )
    if provider is Provider.HUGGINGFACE:
        endpoint = _provider_endpoint(provider, mirror_url or huggingface_mirror, disable_mirror)
        assert endpoint is not None
        return await download_huggingface(
            source_id, revision, destination, progress, endpoint, selected_paths
        )
    if provider in MODELSCOPE_PROVIDERS:
        endpoint = _provider_endpoint(
            provider,
            mirror_url or _modelscope_mirror(provider, modelscope_cn_mirror, modelscope_ai_mirror),
            disable_mirror,
        )
        assert endpoint is not None
        return await download_modelscope(
            provider,
            source_id,
            revision,
            destination,
            progress,
            endpoint,
            _modelscope_token(provider),
            expected_resolved_revision,
            selected_paths,
        )
    if provider is Provider.KAGGLE:
        return await download_kaggle(source_id, revision, destination, progress)
    if provider is Provider.GITHUB_RELEASE:
        return await download_github_release(
            source_id,
            revision,
            destination,
            progress,
            github_token,
            proxy_url,
            disable_proxy,
        )
    if provider is Provider.FILESYSTEM:
        raise ValueError("filesystem artifacts must be created with modelshelf-server import")
    filename, disposition, url = await download_http(
        source_id, destination, progress, proxy_url, disable_proxy
    )
    return ProviderResult(
        resolved_revision="",
        source_url=url,
        downloaded_file=filename,
        content_disposition=disposition,
    )
