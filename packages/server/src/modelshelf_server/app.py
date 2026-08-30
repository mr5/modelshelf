from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import os
import time
from collections.abc import AsyncIterator, Awaitable
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import PurePosixPath
from typing import Annotated, Any, Literal
from urllib.parse import urlparse

import httpx
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError
from fastapi import (
    Cookie,
    Depends,
    FastAPI,
    Header,
    HTTPException,
    Query,
    Request,
    Response,
    status,
)
from fastapi.responses import FileResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from modelshelf_core import Catalog, Provider
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from . import __version__
from .client_distribution import (
    distribution_file,
    distribution_metadata,
    render_installer,
)
from .config import Settings
from .integration_docs import render_integration_markdown
from .providers import (
    DownloadEstimate,
    ModelSearch,
    ProviderRequestError,
    ProviderUnavailable,
    RevisionDiscovery,
    discover_gguf_variants,
    discover_revisions,
    estimate_download,
    provider_failure_detail,
    search_models,
)
from .tasks import TaskManager


class LoginRequest(BaseModel):
    password: str


class CreateTaskRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    provider: Provider
    id: str = Field(min_length=1)
    revision: str = Field(default="main", min_length=1)
    disable_mirror: bool = Field(default=False, alias="disableMirror")
    mirror_url: str | None = Field(default=None, alias="mirrorUrl")
    disable_proxy: bool = Field(default=False, alias="disableProxy")
    scheduled_at: datetime | None = Field(default=None, alias="scheduledAt")
    selected_paths: list[str] | None = Field(default=None, alias="selectedPaths")

    @field_validator("mirror_url", mode="before")
    @classmethod
    def validate_mirror_url(cls, value: object) -> str | None:
        if value is None or value == "":
            return None
        if not isinstance(value, str):
            raise ValueError("temporary mirror address must be a string")
        candidate = value.strip().rstrip("/")
        parsed = urlparse(candidate)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("temporary mirror address must be an HTTP(S) URL")
        if parsed.username is not None or parsed.password is not None:
            raise ValueError("temporary mirror address must not contain credentials")
        return candidate

    @field_validator("scheduled_at")
    @classmethod
    def validate_scheduled_at(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("scheduled start must include a timezone")
        return value.astimezone(UTC)

    @field_validator("selected_paths")
    @classmethod
    def validate_selected_paths(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return None
        normalized = sorted(set(value))
        if not normalized:
            raise ValueError("selected paths cannot be empty")
        for path in normalized:
            candidate = PurePosixPath(path)
            if candidate.is_absolute() or ".." in candidate.parts or path in {"", "."}:
                raise ValueError("selected paths must be safe relative paths")
        return normalized

    @model_validator(mode="after")
    def validate_network_route(self) -> CreateTaskRequest:
        mirror_providers = {
            Provider.HUGGINGFACE,
            Provider.MODELSCOPE_CN,
            Provider.MODELSCOPE_AI,
        }
        if self.mirror_url and self.provider not in mirror_providers:
            raise ValueError("temporary mirrors are not supported for this source")
        if self.mirror_url and self.disable_mirror:
            raise ValueError("temporary mirror and mirror bypass cannot be enabled together")
        selectable = {Provider.HUGGINGFACE, Provider.MODELSCOPE_CN, Provider.MODELSCOPE_AI}
        if self.selected_paths and self.provider not in selectable:
            raise ValueError("file selection is not supported for this source")
        return self


class ReorderTasksRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    ordered_task_ids: list[str] = Field(alias="orderedTaskIds")

    @field_validator("ordered_task_ids")
    @classmethod
    def validate_ordered_task_ids(cls, value: list[str]) -> list[str]:
        if len(value) != len(set(value)):
            raise ValueError("queued task order contains duplicate task IDs")
        return value


def _selected_estimate(
    estimate: DownloadEstimate, selected_paths: list[str] | None
) -> tuple[list[str] | None, int | None, int | None]:
    if selected_paths is None:
        return None, estimate.total_size, estimate.file_count
    available = {file.path: file.size for file in estimate.files}
    unknown = sorted(set(selected_paths) - available.keys())
    if unknown:
        preview = ", ".join(unknown[:3])
        suffix = "…" if len(unknown) > 3 else ""
        raise ValueError(
            f"selected files are not present at the resolved revision: {preview}{suffix}"
        )
    normalized = sorted(set(selected_paths))
    variants = discover_gguf_variants(estimate.files)
    if not any(sorted(variant.paths) == normalized for variant in variants):
        raise ValueError(
            "selected files must exactly match one complete, recognized GGUF variant; "
            "download the full repository when variant selection is unavailable"
        )
    if len(normalized) == len(available):
        return None, estimate.total_size, estimate.file_count
    sizes = [available[path] for path in normalized]
    total = (
        sum(size for size in sizes if size is not None)
        if all(size is not None for size in sizes)
        else None
    )
    return normalized, total, len(normalized)


class ConfirmHttpRequest(BaseModel):
    name: str = Field(min_length=1)
    version: str = Field(min_length=1)
    format: str | None = None
    extract: bool = False


class ResumeTaskRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    scheduled_at: datetime | None = Field(default=None, alias="scheduledAt")

    @field_validator("scheduled_at")
    @classmethod
    def validate_scheduled_at(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("scheduled resume must include a timezone")
        return value.astimezone(UTC)


def _sign_session(settings: Settings, expires: int) -> str:
    nonce = hashlib.sha256(str(time.time_ns()).encode()).digest()[:12]
    payload = f"{expires}:{base64.urlsafe_b64encode(nonce).decode()}"
    signature = hmac.new(
        settings.session_secret.encode(), payload.encode(), hashlib.sha256
    ).hexdigest()
    return f"{payload}:{signature}"


def _valid_session(settings: Settings, token: str | None) -> bool:
    if not token:
        return False
    try:
        payload, signature = token.rsplit(":", 1)
        expires = int(payload.split(":", 1)[0])
    except (ValueError, IndexError):
        return False
    expected = hmac.new(
        settings.session_secret.encode(), payload.encode(), hashlib.sha256
    ).hexdigest()
    return expires > int(time.time()) and hmac.compare_digest(signature, expected)


def _network_url_display(value: str) -> str:
    parsed = urlparse(value)
    host = parsed.hostname or "configured"
    if ":" in host:
        host = f"[{host}]"
    authority = f"{host}:{parsed.port}" if parsed.port else host
    return f"{parsed.scheme}://{authority}{parsed.path.rstrip('/')}"


def _external_url(settings: Settings, request: Request, path: str) -> str:
    base = settings.public_base_url or str(request.base_url)
    return f"{base.rstrip('/')}/{path.lstrip('/')}"


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings()
    if settings.huggingface_mirror:
        os.environ["HF_ENDPOINT"] = settings.huggingface_mirror
    if settings.http_proxy:
        os.environ["HTTP_PROXY"] = settings.http_proxy
        os.environ["HTTPS_PROXY"] = settings.http_proxy
    settings.storage_root = settings.storage_root.resolve()
    catalog = Catalog(settings.storage_root)
    catalog.initialize()
    manager = TaskManager(
        catalog,
        github_token=settings.github_token,
        huggingface_mirror=settings.huggingface_mirror,
        modelscope_cn_mirror=settings.modelscope_cn_mirror,
        modelscope_ai_mirror=settings.modelscope_ai_mirror,
        proxy_url=settings.http_proxy,
        max_concurrent_downloads=settings.max_concurrent_downloads,
        max_concurrent_downloads_per_source=settings.max_concurrent_downloads_per_source,
    )
    revision_cache: dict[tuple[Provider, str], tuple[float, RevisionDiscovery]] = {}
    revision_inflight: dict[tuple[Provider, str], asyncio.Task[RevisionDiscovery]] = {}
    model_cache: dict[tuple[Provider, str], tuple[float, ModelSearch]] = {}
    model_inflight: dict[tuple[Provider, str], asyncio.Task[ModelSearch]] = {}
    estimate_cache: dict[
        tuple[Provider, str, str, bool, str | None, bool], tuple[float, DownloadEstimate]
    ] = {}
    estimate_inflight: dict[
        tuple[Provider, str, str, bool, str | None, bool], asyncio.Task[DownloadEstimate]
    ] = {}

    def track_inflight[Result, Key](
        inflight: dict[Key, asyncio.Task[Result]],
        key: Key,
        task: asyncio.Task[Result],
    ) -> None:
        def finished(completed: asyncio.Task[Result]) -> None:
            if inflight.get(key) is completed:
                inflight.pop(key, None)
            if not completed.cancelled():
                completed.exception()

        task.add_done_callback(finished)

    async def provider_metadata[Result](
        operation: str,
        provider: Provider,
        request: Awaitable[Result],
    ) -> Result:
        try:
            async with asyncio.timeout(settings.provider_metadata_timeout_seconds):
                return await request
        except TimeoutError as error:
            seconds = settings.provider_metadata_timeout_seconds
            timeout = TimeoutError(f"timed out after {seconds:g} seconds waiting for the provider")
            raise HTTPException(
                status.HTTP_504_GATEWAY_TIMEOUT,
                provider_failure_detail(provider, operation, timeout),
            ) from error

    async def cached_revisions(provider: Provider, source_id: str) -> RevisionDiscovery:
        key = (provider, source_id)
        now = time.monotonic()
        cached = revision_cache.get(key)
        if cached and cached[0] > now:
            return cached[1]
        task = revision_inflight.get(key)
        if task is None:
            task = asyncio.create_task(
                discover_revisions(
                    provider,
                    source_id,
                    github_token=settings.github_token,
                    modelscope_cn_mirror=settings.modelscope_cn_mirror,
                    modelscope_ai_mirror=settings.modelscope_ai_mirror,
                )
            )
            revision_inflight[key] = task
            track_inflight(revision_inflight, key, task)
        result = await asyncio.shield(task)
        if len(revision_cache) >= 256:
            oldest = min(revision_cache, key=lambda candidate: revision_cache[candidate][0])
            revision_cache.pop(oldest, None)
        revision_cache[key] = (time.monotonic() + 60, result)
        return result

    async def cached_models(provider: Provider, query: str) -> ModelSearch:
        key = (provider, query.casefold())
        now = time.monotonic()
        cached = model_cache.get(key)
        if cached and cached[0] > now:
            return cached[1]
        task = model_inflight.get(key)
        if task is None:
            task = asyncio.create_task(
                search_models(
                    provider,
                    query,
                    github_token=settings.github_token,
                    modelscope_cn_mirror=settings.modelscope_cn_mirror,
                    modelscope_ai_mirror=settings.modelscope_ai_mirror,
                )
            )
            model_inflight[key] = task
            track_inflight(model_inflight, key, task)
        result = await asyncio.shield(task)
        if len(model_cache) >= 256:
            oldest = min(model_cache, key=lambda candidate: model_cache[candidate][0])
            model_cache.pop(oldest, None)
        model_cache[key] = (time.monotonic() + 30, result)
        return result

    async def cached_estimate(
        provider: Provider,
        source_id: str,
        revision: str,
        disable_mirror: bool,
        mirror_url: str | None,
        disable_proxy: bool,
    ) -> DownloadEstimate:
        key = (provider, source_id, revision, disable_mirror, mirror_url, disable_proxy)
        now = time.monotonic()
        cached = estimate_cache.get(key)
        if cached and cached[0] > now:
            return cached[1]
        task = estimate_inflight.get(key)
        if task is None:
            task = asyncio.create_task(
                estimate_download(
                    provider,
                    source_id,
                    revision,
                    github_token=settings.github_token,
                    huggingface_mirror=settings.huggingface_mirror,
                    modelscope_cn_mirror=settings.modelscope_cn_mirror,
                    modelscope_ai_mirror=settings.modelscope_ai_mirror,
                    proxy_url=settings.http_proxy,
                    disable_mirror=disable_mirror,
                    mirror_url=mirror_url,
                    disable_proxy=disable_proxy,
                )
            )
            estimate_inflight[key] = task
            track_inflight(estimate_inflight, key, task)
        result = await asyncio.shield(task)
        if len(estimate_cache) >= 512:
            oldest = min(estimate_cache, key=lambda candidate: estimate_cache[candidate][0])
            estimate_cache.pop(oldest, None)
        estimate_cache[key] = (time.monotonic() + 30, result)
        return result

    async def validated_estimate(
        provider: Provider,
        source_id: str,
        revision: str,
        disable_mirror: bool = False,
        mirror_url: str | None = None,
        disable_proxy: bool = False,
    ) -> DownloadEstimate:
        try:
            return await provider_metadata(
                "download preflight",
                provider,
                cached_estimate(
                    provider,
                    source_id.strip(),
                    revision.strip(),
                    disable_mirror,
                    mirror_url,
                    disable_proxy,
                ),
            )
        except ValueError as error:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT,
                provider_failure_detail(provider, "download preflight", error),
            ) from error
        except ProviderUnavailable as error:
            raise HTTPException(
                status.HTTP_503_SERVICE_UNAVAILABLE,
                provider_failure_detail(provider, "download preflight", error),
            ) from error
        except ProviderRequestError as error:
            raise HTTPException(
                status.HTTP_424_FAILED_DEPENDENCY,
                provider_failure_detail(provider, "download preflight", error),
            ) from error
        except HTTPException:
            raise
        except Exception as error:
            upstream_status = getattr(error, "status_code", None) or getattr(
                getattr(error, "response", None), "status_code", None
            )
            response_status = (
                status.HTTP_404_NOT_FOUND
                if upstream_status == status.HTTP_404_NOT_FOUND
                else status.HTTP_502_BAD_GATEWAY
            )
            raise HTTPException(
                response_status,
                provider_failure_detail(provider, "download preflight", error),
            ) from error

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        await manager.start()
        try:
            yield
        finally:
            await manager.stop()

    app = FastAPI(title="ModelShelf", version=__version__, lifespan=lifespan)
    app.state.settings = settings
    app.state.catalog = catalog
    app.state.tasks = manager

    async def require_write(
        authorization: Annotated[str | None, Header()] = None,
        session: Annotated[str | None, Cookie(alias="modelshelf_session")] = None,
    ) -> None:
        bearer = (
            authorization[7:] if authorization and authorization.startswith("Bearer ") else None
        )
        token_valid = bearer is not None and any(
            hmac.compare_digest(bearer, expected) for expected in settings.write_tokens
        )
        if not token_valid and not _valid_session(settings, session):
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "authentication required")

    async def require_artifact_read(
        authorization: Annotated[str | None, Header()] = None,
        session: Annotated[str | None, Cookie(alias="modelshelf_session")] = None,
    ) -> None:
        if settings.public_artifacts:
            return
        await require_write(authorization, session)

    @app.get("/api/v1/info")
    async def info(request: Request) -> dict[str, Any]:
        nfs = (
            {
                "host": settings.nfs_advertised_host,
                "port": settings.nfs_advertised_port or settings.nfs_port,
                "exportPath": settings.nfs_export_path,
                "version": "4.2",
            }
            if settings.nfs_advertised_host
            else None
        )
        mirrors = {
            provider: _network_url_display(endpoint)
            for provider, endpoint in (
                (Provider.HUGGINGFACE.value, settings.huggingface_mirror),
                (Provider.MODELSCOPE_CN.value, settings.modelscope_cn_mirror),
                (Provider.MODELSCOPE_AI.value, settings.modelscope_ai_mirror),
            )
            if endpoint
        }
        proxy_display = None
        if settings.http_proxy:
            proxy_display = _network_url_display(settings.http_proxy)
        return {
            "name": "ModelShelf",
            "version": __version__,
            "publicArtifacts": settings.public_artifacts,
            "downloads": {
                "maxConcurrent": settings.max_concurrent_downloads,
                "maxConcurrentPerSource": settings.max_concurrent_downloads_per_source,
            },
            "nfs": nfs,
            "client": {
                **distribution_metadata(settings.client_dist),
                "installUrl": _external_url(settings, request, "/install.sh"),
                "downloadUrl": _external_url(settings, request, "/api/v1/client"),
            },
            "documentation": {
                "humanUrl": _external_url(settings, request, "/integration"),
                "agentUrl": _external_url(settings, request, "/integration.md"),
            },
            "network": {
                "mirrors": mirrors,
                "proxyConfigured": settings.http_proxy is not None,
                "proxyDisplay": proxy_display,
            },
        }

    @app.get("/integration.md", include_in_schema=False)
    async def integration_markdown(request: Request) -> PlainTextResponse:
        return PlainTextResponse(
            render_integration_markdown(settings, str(request.base_url)),
            media_type="text/markdown",
            headers={
                "Cache-Control": "no-cache",
                "Content-Disposition": 'inline; filename="modelshelf-integration.md"',
            },
        )

    @app.get("/install.sh", include_in_schema=False)
    async def client_installer(request: Request) -> PlainTextResponse:
        try:
            script = render_installer(
                settings.client_dist,
                _external_url(settings, request, "/api/v1/client"),
            )
        except (OSError, ValueError) as error:
            raise HTTPException(
                status.HTTP_503_SERVICE_UNAVAILABLE,
                "this server does not include a client distribution",
            ) from error
        return PlainTextResponse(
            script,
            media_type="text/x-shellscript",
            headers={"Cache-Control": "no-cache"},
        )

    @app.get("/api/v1/client/{filename}", include_in_schema=False)
    async def client_package(filename: str) -> FileResponse:
        package = distribution_file(settings.client_dist, filename)
        if package is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "client package not found")
        return FileResponse(package, headers={"Cache-Control": "no-cache"})

    @app.post("/api/v1/auth/login")
    async def login(body: LoginRequest, request: Request, response: Response) -> dict[str, bool]:
        if not settings.admin_password_hash:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid credentials")
        try:
            PasswordHasher().verify(settings.admin_password_hash, body.password)
        except (VerifyMismatchError, InvalidHashError):
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid credentials") from None
        ttl = settings.session_ttl_seconds
        response.set_cookie(
            "modelshelf_session",
            _sign_session(settings, int(time.time()) + ttl),
            max_age=ttl,
            httponly=True,
            samesite="strict",
            secure=settings.session_cookie_secure or request.url.scheme == "https",
            path="/",
        )
        return {"ok": True}

    @app.post("/api/v1/auth/logout")
    async def logout(response: Response) -> dict[str, bool]:
        response.delete_cookie("modelshelf_session", path="/")
        return {"ok": True}

    @app.get("/api/v1/auth/session")
    async def session(
        token: Annotated[str | None, Cookie(alias="modelshelf_session")] = None,
    ) -> dict[str, bool]:
        return {"authenticated": _valid_session(settings, token)}

    @app.get("/api/v1/artifacts", dependencies=[Depends(require_artifact_read)])
    async def artifacts(
        q: Annotated[str | None, Query(max_length=200)] = None,
        provider: Provider | None = None,
        sort_by: Annotated[Literal["created", "name", "size"], Query(alias="sortBy")] = "created",
        sort_order: Annotated[Literal["asc", "desc"], Query(alias="sortOrder")] = "desc",
        limit: Annotated[int | None, Query(ge=1, le=500)] = None,
        offset: Annotated[int, Query(ge=0)] = 0,
    ) -> list[dict[str, Any]]:
        items = catalog.list(
            query=q,
            provider=provider,
            sort_by=sort_by,
            sort_order=sort_order,
            limit=limit,
            offset=offset,
        )
        return [item.model_dump(mode="json", by_alias=True) for item in items]

    @app.get("/api/v1/artifacts/{artifact_id}", dependencies=[Depends(require_artifact_read)])
    async def artifact(artifact_id: str) -> dict[str, Any]:
        found = catalog.find(artifact_id)
        if found is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "artifact not found")
        summary, manifest = found
        return {
            "summary": summary.model_dump(mode="json", by_alias=True),
            "manifest": manifest.model_dump(mode="json", by_alias=True, exclude_none=True),
        }

    @app.delete(
        "/api/v1/artifacts/{artifact_id}",
        status_code=status.HTTP_204_NO_CONTENT,
        dependencies=[Depends(require_write)],
    )
    async def delete_artifact(artifact_id: str) -> Response:
        if not await manager.delete_artifact(artifact_id):
            raise HTTPException(status.HTTP_404_NOT_FOUND, "artifact not found")
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    @app.get("/api/v1/providers/{provider}/revisions", dependencies=[Depends(require_write)])
    async def provider_revisions(
        provider: Provider,
        source_id: Annotated[str, Query(alias="id", min_length=1, max_length=512)],
    ) -> dict[str, object]:
        try:
            result = await provider_metadata(
                "revision discovery", provider, cached_revisions(provider, source_id.strip())
            )
        except ValueError as error:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT,
                provider_failure_detail(provider, "revision discovery", error),
            ) from error
        except ProviderUnavailable as error:
            raise HTTPException(
                status.HTTP_503_SERVICE_UNAVAILABLE,
                provider_failure_detail(provider, "revision discovery", error),
            ) from error
        except ProviderRequestError as error:
            raise HTTPException(
                status.HTTP_424_FAILED_DEPENDENCY,
                provider_failure_detail(provider, "revision discovery", error),
            ) from error
        except HTTPException:
            raise
        except httpx.HTTPStatusError as error:
            upstream_status = error.response.status_code
            response_status = (
                status.HTTP_404_NOT_FOUND
                if upstream_status == status.HTTP_404_NOT_FOUND
                else status.HTTP_502_BAD_GATEWAY
            )
            raise HTTPException(
                response_status,
                provider_failure_detail(provider, "revision discovery", error),
            ) from error
        except Exception as error:
            raise HTTPException(
                status.HTTP_502_BAD_GATEWAY,
                provider_failure_detail(provider, "revision discovery", error),
            ) from error
        return result.as_dict()

    @app.get("/api/v1/providers/{provider}/models", dependencies=[Depends(require_write)])
    async def provider_models(
        provider: Provider,
        query: Annotated[str, Query(alias="q", min_length=2, max_length=100)],
    ) -> dict[str, object]:
        try:
            result = await provider_metadata(
                "model search", provider, cached_models(provider, query.strip())
            )
        except ValueError as error:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT,
                provider_failure_detail(provider, "model search", error),
            ) from error
        except ProviderUnavailable as error:
            raise HTTPException(
                status.HTTP_503_SERVICE_UNAVAILABLE,
                provider_failure_detail(provider, "model search", error),
            ) from error
        except ProviderRequestError as error:
            raise HTTPException(
                status.HTTP_424_FAILED_DEPENDENCY,
                provider_failure_detail(provider, "model search", error),
            ) from error
        except HTTPException:
            raise
        except httpx.HTTPStatusError as error:
            raise HTTPException(
                status.HTTP_502_BAD_GATEWAY,
                provider_failure_detail(provider, "model search", error),
            ) from error
        except Exception as error:
            raise HTTPException(
                status.HTTP_502_BAD_GATEWAY,
                provider_failure_detail(provider, "model search", error),
            ) from error
        return result.as_dict()

    @app.get("/api/v1/providers/{provider}/estimate", dependencies=[Depends(require_write)])
    async def provider_estimate(
        provider: Provider,
        source_id: Annotated[str, Query(alias="id", min_length=1, max_length=2048)],
        revision: Annotated[str, Query(min_length=1, max_length=256)],
        disable_mirror: Annotated[bool, Query(alias="disableMirror")] = False,
        mirror_url: Annotated[
            str | None, Query(alias="mirrorUrl", min_length=1, max_length=2048)
        ] = None,
        disable_proxy: Annotated[bool, Query(alias="disableProxy")] = False,
        selected_path: Annotated[list[str] | None, Query(alias="selectedPath")] = None,
    ) -> dict[str, object]:
        try:
            request = CreateTaskRequest(
                provider=provider,
                id=source_id,
                revision=revision,
                disableMirror=disable_mirror,
                mirrorUrl=mirror_url,
                disableProxy=disable_proxy,
                selectedPaths=selected_path,
            )
        except ValidationError as error:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT,
                error.errors(include_url=False, include_context=False, include_input=False),
            ) from error
        result = await validated_estimate(
            request.provider,
            request.id,
            request.revision,
            request.disable_mirror,
            request.mirror_url,
            request.disable_proxy,
        )
        response = result.as_dict()
        try:
            normalized_selection, selected_total, selected_count = _selected_estimate(
                result, request.selected_paths
            )
        except ValueError as error:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(error)) from error
        if normalized_selection is not None:
            response["selectedPaths"] = normalized_selection
            response["totalSize"] = selected_total
            response["fileCount"] = selected_count
        duplicate = manager.find_duplicate(
            result.provider,
            result.source_id,
            result.requested_revision,
            resolved_revision=result.resolved_revision,
            disable_mirror=request.disable_mirror,
            mirror_url=request.mirror_url,
            disable_proxy=request.disable_proxy,
            selected_paths=normalized_selection,
        )
        if duplicate is not None:
            response["duplicate"] = duplicate.as_dict()
        return response

    @app.get("/api/v1/tasks", dependencies=[Depends(require_write)])
    async def tasks() -> list[dict[str, Any]]:
        return [
            item.model_dump(mode="json", by_alias=True, exclude_none=True)
            for item in manager.store.list()
        ]

    @app.post("/api/v1/tasks/reorder", dependencies=[Depends(require_write)])
    async def reorder_tasks(body: ReorderTasksRequest) -> list[dict[str, Any]]:
        try:
            reordered = manager.reorder_queued(body.ordered_task_ids)
        except ValueError as error:
            raise HTTPException(status.HTTP_409_CONFLICT, str(error)) from error
        return [
            item.model_dump(mode="json", by_alias=True, exclude_none=True) for item in reordered
        ]

    @app.get("/api/v1/tasks/{task_id}", dependencies=[Depends(require_write)])
    async def task(task_id: str) -> dict[str, Any]:
        item = manager.store.get(task_id)
        if item is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "task not found")
        return item.model_dump(mode="json", by_alias=True, exclude_none=True)

    @app.delete(
        "/api/v1/tasks/{task_id}",
        status_code=status.HTTP_204_NO_CONTENT,
        dependencies=[Depends(require_write)],
    )
    async def delete_task(
        task_id: str,
        delete_artifact: Annotated[bool, Query(alias="deleteArtifact")] = False,
    ) -> Response:
        try:
            await manager.delete_task(task_id, delete_artifact=delete_artifact)
        except KeyError as error:
            raise HTTPException(status.HTTP_404_NOT_FOUND, str(error)) from error
        except ValueError as error:
            raise HTTPException(status.HTTP_409_CONFLICT, str(error)) from error
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    async def control_task(
        task_id: str, operation: Literal["pause", "cancel", "start"]
    ) -> dict[str, Any]:
        try:
            if operation == "pause":
                item = await manager.pause(task_id)
            elif operation == "start":
                item = await manager.start_now(task_id)
            else:
                item = await manager.cancel(task_id)
        except KeyError as error:
            raise HTTPException(status.HTTP_404_NOT_FOUND, str(error)) from error
        except ValueError as error:
            raise HTTPException(status.HTTP_409_CONFLICT, str(error)) from error
        return item.model_dump(mode="json", by_alias=True, exclude_none=True)

    @app.post("/api/v1/tasks/{task_id}/pause", dependencies=[Depends(require_write)])
    async def pause_task(task_id: str) -> dict[str, Any]:
        return await control_task(task_id, "pause")

    @app.post("/api/v1/tasks/{task_id}/resume", dependencies=[Depends(require_write)])
    async def resume_task(task_id: str, body: ResumeTaskRequest | None = None) -> dict[str, Any]:
        try:
            item = await manager.resume(
                task_id, scheduled_at=body.scheduled_at if body is not None else None
            )
        except KeyError as error:
            raise HTTPException(status.HTTP_404_NOT_FOUND, str(error)) from error
        except ValueError as error:
            raise HTTPException(status.HTTP_409_CONFLICT, str(error)) from error
        return item.model_dump(mode="json", by_alias=True, exclude_none=True)

    @app.post("/api/v1/tasks/{task_id}/cancel", dependencies=[Depends(require_write)])
    async def cancel_task(task_id: str) -> dict[str, Any]:
        return await control_task(task_id, "cancel")

    @app.post("/api/v1/tasks/{task_id}/start", dependencies=[Depends(require_write)])
    async def start_task_now(task_id: str) -> dict[str, Any]:
        return await control_task(task_id, "start")

    @app.post(
        "/api/v1/tasks",
        status_code=status.HTTP_202_ACCEPTED,
        dependencies=[Depends(require_write)],
    )
    async def create_task(body: CreateTaskRequest) -> dict[str, Any]:
        estimate = await validated_estimate(
            body.provider,
            body.id,
            body.revision,
            body.disable_mirror,
            body.mirror_url,
            body.disable_proxy,
        )
        try:
            selected_paths, total_bytes, _selected_count = _selected_estimate(
                estimate, body.selected_paths
            )
        except ValueError as error:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(error)) from error
        creation = await manager.create_with_result(
            estimate.provider,
            estimate.source_id,
            estimate.requested_revision,
            resolved_revision=estimate.resolved_revision,
            total_bytes=total_bytes,
            disable_mirror=body.disable_mirror,
            mirror_url=body.mirror_url,
            disable_proxy=body.disable_proxy,
            scheduled_at=body.scheduled_at,
            selected_paths=selected_paths,
        )
        item = creation.task
        result = item.model_dump(mode="json", by_alias=True, exclude_none=True)
        result["deduplicated"] = creation.deduplication_reason is not None
        if creation.deduplication_reason is not None:
            result["deduplicationReason"] = creation.deduplication_reason
        if body.provider is Provider.HTTP:
            result["warning"] = (
                "Generic HTTP content remains in staging and requires explicit metadata/extraction "
                "confirmation before publication."
            )
        return result

    @app.post("/api/v1/tasks/{task_id}/confirm", dependencies=[Depends(require_write)])
    async def confirm(task_id: str, body: ConfirmHttpRequest) -> dict[str, Any]:
        try:
            item = await manager.confirm_http(
                task_id,
                name=body.name,
                version=body.version,
                format=body.format,
                extract=body.extract,
            )
        except KeyError as error:
            raise HTTPException(status.HTTP_404_NOT_FOUND, str(error)) from error
        except ValueError as error:
            raise HTTPException(status.HTTP_409_CONFLICT, str(error)) from error
        return item.model_dump(mode="json", by_alias=True, exclude_none=True)

    ui_dist = settings.ui_dist
    if ui_dist and ui_dist.is_dir():
        assets = ui_dist / "assets"
        if assets.is_dir():
            app.mount("/assets", StaticFiles(directory=assets), name="ui-assets")

        @app.get("/{path:path}", include_in_schema=False)
        async def ui(path: str, request: Request) -> FileResponse:
            if path.startswith("api/"):
                raise HTTPException(status.HTTP_404_NOT_FOUND)
            candidate = (ui_dist / path).resolve()
            if candidate.is_file() and candidate.is_relative_to(ui_dist.resolve()):
                return FileResponse(candidate)
            return FileResponse(
                ui_dist / "index.html",
                headers={"Cache-Control": "no-cache"},
            )

    return app
