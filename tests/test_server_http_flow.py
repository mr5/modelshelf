from __future__ import annotations

import threading
import time
import zipfile
from datetime import UTC, datetime, timedelta
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import modelshelf_server.app as app_module
import modelshelf_server.tasks as task_module
import pytest
from argon2 import PasswordHasher
from fastapi.testclient import TestClient
from modelshelf_core import Catalog, Provider, SourceReference, TaskStatus
from modelshelf_server.app import create_app
from modelshelf_server.config import Settings
from modelshelf_server.providers import (
    DownloadEstimate,
    EstimateMetadata,
    ModelOption,
    ModelSearch,
    ProviderResult,
    RevisionDiscovery,
    RevisionOption,
    SourceFile,
)
from modelshelf_server.tasks import TaskStore


def test_queued_and_paused_tasks_can_be_reordered_through_the_api(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    storage = tmp_path / "storage"
    catalog = Catalog(storage)
    catalog.initialize()
    store = TaskStore(catalog.jobs_root)
    first = store.create(
        Provider.HUGGINGFACE,
        "owner/first",
        "main",
        resolved_revision="a" * 40,
        total_bytes=5,
        disable_mirror=False,
        disable_proxy=False,
        queue_position=0,
    )
    second = store.create(
        Provider.HUGGINGFACE,
        "owner/second",
        "main",
        resolved_revision="b" * 40,
        total_bytes=5,
        disable_mirror=False,
        disable_proxy=False,
        queue_position=1,
    )
    store.update(second.id, {"status": TaskStatus.PAUSED})

    async def no_lifecycle_work(_manager: object) -> None:
        return None

    monkeypatch.setattr(task_module.TaskManager, "start", no_lifecycle_work)
    monkeypatch.setattr(task_module.TaskManager, "stop", no_lifecycle_work)
    settings = Settings(
        storage_root=storage,
        write_tokens=("write-token",),
        session_secret="test-session-secret-with-32-bytes-minimum",
    )
    headers = {"Authorization": "Bearer write-token"}
    with TestClient(create_app(settings)) as client:
        response = client.post(
            "/api/v1/tasks/reorder",
            json={"orderedTaskIds": [second.id, first.id]},
            headers=headers,
        )
        stale = client.post(
            "/api/v1/tasks/reorder",
            json={"orderedTaskIds": [first.id]},
            headers=headers,
        )

    assert response.status_code == 200
    assert [task["id"] for task in response.json()] == [second.id, first.id]
    assert [task["queuePosition"] for task in response.json()] == [0, 1]
    assert [task["status"] for task in response.json()] == ["paused", "queued"]
    assert stale.status_code == 409
    assert "queue changed" in stale.json()["detail"]


def test_manifest_verification_does_not_block_task_or_health_endpoints(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    storage = tmp_path / "storage"
    catalog = Catalog(storage)
    catalog.initialize()
    store = TaskStore(catalog.jobs_root)
    task = store.create(
        Provider.HUGGINGFACE,
        "owner/model",
        "main",
        resolved_revision="a" * 40,
        total_bytes=len(b"model"),
        disable_mirror=False,
        disable_proxy=False,
        queue_position=0,
    )
    verification_started = threading.Event()
    release_verification = threading.Event()
    original_inventory = task_module.inventory

    async def fake_provider(
        _provider: Provider,
        _source_id: str,
        _revision: str,
        destination: Path,
        _progress: object,
        **_options: object,
    ) -> ProviderResult:
        (destination / "model.bin").write_bytes(b"model")
        return ProviderResult(resolved_revision="a" * 40)

    def blocking_inventory(*args: object, **kwargs: object) -> list[object]:
        verification_started.set()
        assert release_verification.wait(timeout=2)
        return original_inventory(*args, **kwargs)

    monkeypatch.setattr(task_module, "run_provider", fake_provider)
    monkeypatch.setattr(task_module, "inventory", blocking_inventory)
    settings = Settings(
        storage_root=storage,
        write_tokens=("write-token",),
        session_secret="test-session-secret-with-32-bytes-minimum",
    )
    headers = {"Authorization": "Bearer write-token"}
    try:
        with TestClient(create_app(settings)) as client:
            assert verification_started.wait(timeout=2)
            started_at = time.monotonic()
            info = client.get("/api/v1/info")
            detail = client.get(f"/api/v1/tasks/{task.id}", headers=headers)
            elapsed = time.monotonic() - started_at

            assert info.status_code == 200
            assert detail.status_code == 200
            assert elapsed < 1
            assert detail.json()["status"] == "verifying"
            assert detail.json()["instantaneousBytesPerSecond"] == 0
            assert detail.json()["verificationDetail"] == (
                "Hashing files for the artifact manifest"
            )

            release_verification.set()
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline:
                completed = client.get(f"/api/v1/tasks/{task.id}", headers=headers)
                if completed.json()["status"] == "completed":
                    break
                time.sleep(0.02)
            assert completed.json()["status"] == "completed"
    finally:
        release_verification.set()


def test_delete_endpoints_require_write_access_and_remove_owned_data(tmp_path: Path) -> None:
    storage = tmp_path / "storage"
    catalog = Catalog(storage)
    catalog.initialize()
    artifact_stage = catalog.staging_path("seed-artifact")
    artifact_stage.mkdir()
    (artifact_stage / "model.bin").write_bytes(b"weights")
    manifest = catalog.create_manifest(
        artifact_stage,
        name="small-model",
        version="1",
        source=SourceReference(
            provider=Provider.HUGGINGFACE,
            id="owner/small-model",
            requested_revision="main",
            resolved_revision="a" * 40,
        ),
    )
    artifact_path, _ = catalog.publish(artifact_stage, manifest)

    store = TaskStore(catalog.jobs_root)
    failed = store.create(
        Provider.HUGGINGFACE,
        "owner/failed",
        "main",
        resolved_revision="b" * 40,
        total_bytes=7,
        disable_mirror=False,
        disable_proxy=False,
    )
    store.update(failed.id, {"status": TaskStatus.FAILED})
    failed_stage = catalog.staging_path(failed.id)
    failed_stage.mkdir()
    (failed_stage / "partial.bin").write_bytes(b"partial")
    paused = store.create(
        Provider.HUGGINGFACE,
        "owner/paused",
        "main",
        resolved_revision="c" * 40,
        total_bytes=7,
        disable_mirror=False,
        disable_proxy=False,
    )
    store.update(paused.id, {"status": TaskStatus.PAUSED})

    settings = Settings(
        storage_root=storage,
        write_tokens=("write-token",),
        session_secret="test-session-secret-with-32-bytes-minimum",
    )
    headers = {"Authorization": "Bearer write-token"}
    with TestClient(create_app(settings)) as client:
        assert client.delete(f"/api/v1/tasks/{failed.id}").status_code == 401
        assert client.delete(f"/api/v1/artifacts/{manifest.artifact_id}").status_code == 401
        active_delete = client.delete(f"/api/v1/tasks/{paused.id}", headers=headers)
        assert active_delete.status_code == 409
        assert "only completed, failed or cancelled" in active_delete.json()["detail"]

        assert client.delete(f"/api/v1/tasks/{failed.id}", headers=headers).status_code == 204
        assert store.get(failed.id) is None
        assert not failed_stage.exists()

        assert (
            client.delete(f"/api/v1/artifacts/{manifest.artifact_id}", headers=headers).status_code
            == 204
        )
        assert not artifact_path.exists()
        assert client.get("/api/v1/artifacts").json() == []
        assert (
            client.delete(f"/api/v1/artifacts/{manifest.artifact_id}", headers=headers).status_code
            == 404
        )


class QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, format: str, *args: object) -> None:
        pass


def test_artifacts_are_public_by_default_and_can_require_authentication(tmp_path: Path) -> None:
    public_settings = Settings(
        storage_root=tmp_path / "public-storage",
        nfs_advertised_host="modelshelf.internal",
        nfs_port=2049,
        nfs_advertised_port=32049,
        session_secret="test-session-secret-with-32-bytes-minimum",
    )
    with TestClient(create_app(public_settings)) as client:
        info = client.get("/api/v1/info").json()
        assert info["publicArtifacts"] is True
        assert info["downloads"] == {
            "maxConcurrent": 2,
            "maxConcurrentPerSource": 1,
        }
        assert info["nfs"] == {
            "host": "modelshelf.internal",
            "port": 32049,
            "exportPath": "/modelshelf",
            "version": "4.2",
        }
        assert client.get("/api/v1/artifacts").status_code == 200
        assert client.get("/api/v1/artifacts/not-present").status_code == 404

    fallback_settings = Settings(
        storage_root=tmp_path / "fallback-storage",
        nfs_advertised_host="modelshelf.internal",
        nfs_port=32048,
        session_secret="test-session-secret-with-32-bytes-minimum",
    )
    with TestClient(create_app(fallback_settings)) as client:
        assert client.get("/api/v1/info").json()["nfs"]["port"] == 32048

    private_settings = Settings(
        storage_root=tmp_path / "private-storage",
        public_artifacts=False,
        admin_password_hash=PasswordHasher().hash("secret"),
        write_tokens=("write-token",),
        session_secret="test-session-secret-with-32-bytes-minimum",
    )
    with TestClient(create_app(private_settings)) as client:
        assert client.get("/api/v1/info").json()["publicArtifacts"] is False
        assert client.get("/api/v1/artifacts").status_code == 401
        assert client.get("/api/v1/artifacts/not-present").status_code == 401
        assert (
            client.get(
                "/api/v1/artifacts", headers={"Authorization": "Bearer write-token"}
            ).status_code
            == 200
        )
        assert client.post("/api/v1/auth/login", json={"password": "secret"}).status_code == 200
        assert client.get("/api/v1/artifacts").status_code == 200


def test_generic_http_requires_confirmation_before_publish(tmp_path: Path) -> None:
    served = tmp_path / "served"
    served.mkdir()
    archive = served / "tiny-model-1.2.zip"
    with zipfile.ZipFile(archive, "w") as output:
        output.writestr("tiny-model/config.json", '{"model_type":"tiny"}')
        output.writestr("tiny-model/model.gguf", b"weights")

    def handler(*args: object, **kwargs: object) -> QuietHandler:
        return QuietHandler(*args, directory=served, **kwargs)

    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    settings = Settings(
        storage_root=tmp_path / "storage",
        admin_password_hash=PasswordHasher().hash("secret"),
        write_tokens=("write-token",),
        session_secret="test-session-secret-with-32-bytes-minimum",
    )
    try:
        with TestClient(create_app(settings)) as client:
            assert client.post("/api/v1/auth/login", json={"password": "wrong"}).status_code == 401
            assert client.post("/api/v1/auth/login", json={"password": "secret"}).status_code == 200
            estimate = client.get(
                "/api/v1/providers/http/estimate",
                params={
                    "id": f"http://127.0.0.1:{server.server_port}/{archive.name}",
                    "revision": "content",
                },
            )
            assert estimate.status_code == 200
            assert estimate.json()["totalSize"] == archive.stat().st_size
            assert estimate.json()["fileCount"] == 1
            created = client.post(
                "/api/v1/tasks",
                json={
                    "provider": "http",
                    "id": f"http://127.0.0.1:{server.server_port}/{archive.name}",
                    "revision": "content",
                },
            )
            assert created.status_code == 202
            assert created.json()["disableMirror"] is False
            assert created.json()["disableProxy"] is False
            assert created.json()["totalBytes"] == archive.stat().st_size
            assert "require" in created.json()["warning"]
            task_id = created.json()["id"]
            deadline = time.monotonic() + 10
            task = created.json()
            while time.monotonic() < deadline:
                task = client.get(f"/api/v1/tasks/{task_id}").json()
                if task["status"] in {"awaiting_confirmation", "failed"}:
                    break
                time.sleep(0.05)
            assert task["status"] == "awaiting_confirmation", task
            assert task["resolvedRevision"].startswith("sha256:")
            assert client.get("/api/v1/artifacts").json() == []
            assert not (settings.storage_root / "artifacts/http").exists()

            confirmed = client.post(
                f"/api/v1/tasks/{task_id}/confirm",
                json={"name": "tiny-model", "version": "1.2", "format": "GGUF", "extract": True},
            )
            assert confirmed.status_code == 200, confirmed.text
            assert confirmed.json()["status"] == "completed"
            [artifact_result] = client.get("/api/v1/artifacts").json()
            assert client.get(
                "/api/v1/artifacts", params={"q": "TINY-MODEL", "limit": 1, "offset": 0}
            ).json() == [artifact_result]
            assert client.get(
                "/api/v1/artifacts",
                params={
                    "provider": "http",
                    "sortBy": "size",
                    "sortOrder": "asc",
                },
            ).json() == [artifact_result]
            assert client.get("/api/v1/artifacts", params={"provider": "huggingface"}).json() == []
            assert (
                client.get("/api/v1/artifacts", params={"sortBy": "unsupported"}).status_code == 422
            )
            assert client.get("/api/v1/artifacts", params={"limit": 1, "offset": 1}).json() == []
            detail = client.get(f"/api/v1/artifacts/{artifact_result['artifactId']}").json()
            manifest = detail["manifest"]
            assert manifest["source"]["requestedRevision"] == "content"
            assert manifest["source"]["resolvedRevision"].startswith("sha256:")
            assert {item["path"] for item in manifest["files"]} == {
                "tiny-model/config.json",
                "tiny-model/model.gguf",
            }
    finally:
        server.shutdown()
        thread.join()


def test_provider_search_and_revision_discovery_are_authenticated_and_cached(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = {"models": 0, "revisions": 0, "estimates": 0}

    async def fake_models(
        provider: Provider,
        query: str,
        *,
        github_token: str | None,
        **_network: object,
    ) -> ModelSearch:
        assert github_token is None
        calls["models"] += 1
        return ModelSearch(provider, query, (ModelOption("owner/model", "Model"),))

    async def fake_revisions(
        provider: Provider,
        source_id: str,
        *,
        github_token: str | None,
        **_network: object,
    ) -> RevisionDiscovery:
        assert github_token is None
        calls["revisions"] += 1
        return RevisionDiscovery(
            provider,
            source_id,
            "main",
            (RevisionOption("main", "branch", "a" * 40),),
        )

    async def fake_estimate(
        provider: Provider,
        source_id: str,
        revision: str,
        *,
        github_token: str | None,
        **_network: object,
    ) -> DownloadEstimate:
        assert github_token is None
        calls["estimates"] += 1
        return DownloadEstimate(
            provider,
            source_id,
            revision,
            "a" * 40,
            2_120,
            2,
            "https://hub.example/owner/model",
            (EstimateMetadata("Library", "transformers"),),
            (
                SourceFile("config.json", 120),
                SourceFile("model-Q4_K_M.gguf", 2_000),
            ),
        )

    monkeypatch.setattr(app_module, "search_models", fake_models)
    monkeypatch.setattr(app_module, "discover_revisions", fake_revisions)
    monkeypatch.setattr(app_module, "estimate_download", fake_estimate)
    settings = Settings(
        storage_root=tmp_path / "storage",
        write_tokens=("write-token",),
        session_secret="test-session-secret-with-32-bytes-minimum",
    )
    with TestClient(create_app(settings)) as client:
        assert (
            client.get("/api/v1/providers/huggingface/models", params={"q": "tiny"}).status_code
            == 401
        )
        headers = {"Authorization": "Bearer write-token"}
        for _attempt in range(2):
            models = client.get(
                "/api/v1/providers/huggingface/models",
                params={"q": "tiny"},
                headers=headers,
            )
            assert models.status_code == 200
            assert models.json()["models"][0]["id"] == "owner/model"
            revisions = client.get(
                "/api/v1/providers/huggingface/revisions",
                params={"id": "owner/model"},
                headers=headers,
            )
            assert revisions.status_code == 200
            assert revisions.json()["defaultRevision"] == "main"
            assert revisions.json()["revisions"][0]["resolvedRevision"] == "a" * 40
            estimate = client.get(
                "/api/v1/providers/huggingface/estimate",
                params={"id": "owner/model", "revision": "main"},
                headers=headers,
            )
            assert estimate.status_code == 200
            assert estimate.json()["totalSize"] == 2_120
            assert estimate.json()["hubUrl"] == "https://hub.example/owner/model"
            assert estimate.json()["metadata"] == [{"label": "Library", "value": "transformers"}]
            assert estimate.json()["ggufVariantSelectionAvailable"] is True
            assert estimate.json()["ggufVariants"] == [
                {
                    "label": "model-Q4_K_M.gguf",
                    "paths": ["model-Q4_K_M.gguf"],
                    "fileCount": 1,
                    "totalSize": 2_000,
                }
            ]
            selected = client.get(
                "/api/v1/providers/huggingface/estimate",
                params={
                    "id": "owner/model",
                    "revision": "main",
                    "selectedPath": "model-Q4_K_M.gguf",
                },
                headers=headers,
            )
            assert selected.status_code == 200
            assert selected.json()["selectedPaths"] == ["model-Q4_K_M.gguf"]
            assert selected.json()["totalSize"] == 2_000
            assert selected.json()["fileCount"] == 1
            rejected = client.get(
                "/api/v1/providers/huggingface/estimate",
                params={
                    "id": "owner/model",
                    "revision": "main",
                    "selectedPath": "config.json",
                },
                headers=headers,
            )
            assert rejected.status_code == 422
            assert "recognized GGUF variant" in rejected.json()["detail"]
            rejected_task = client.post(
                "/api/v1/tasks",
                json={
                    "provider": "huggingface",
                    "id": "owner/model",
                    "revision": "main",
                    "selectedPaths": ["config.json"],
                },
                headers=headers,
            )
            assert rejected_task.status_code == 422
            assert "recognized GGUF variant" in rejected_task.json()["detail"]
    assert calls == {"models": 1, "revisions": 1, "estimates": 1}


def test_provider_api_returns_the_original_sanitized_failure_reason(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    secret = "hf_private_token_value"
    monkeypatch.setenv("HF_TOKEN", secret)

    async def failed_search(
        _provider: Provider, _query: str, *, github_token: str | None, **_network: object
    ) -> ModelSearch:
        assert github_token is None
        raise RuntimeError(
            f"DNS lookup failed through https://user:password@proxy.invalid?token={secret}"
        )

    monkeypatch.setattr(app_module, "search_models", failed_search)
    settings = Settings(
        storage_root=tmp_path / "storage",
        write_tokens=("write-token",),
        session_secret="test-session-secret-with-32-bytes-minimum",
    )
    with TestClient(create_app(settings)) as client:
        response = client.get(
            "/api/v1/providers/huggingface/models",
            params={"q": "qwen"},
            headers={"Authorization": "Bearer write-token"},
        )

    assert response.status_code == 502
    detail = response.json()["detail"]
    assert "Hugging Face model search failed (RuntimeError)" in detail
    assert "DNS lookup failed" in detail
    assert secret not in detail
    assert "user:password" not in detail
    assert "[redacted]" in detail


def test_provider_metadata_requests_have_a_bounded_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def slow_provider(*_args: object, **_kwargs: object) -> object:
        import asyncio

        await asyncio.sleep(0.1)
        raise AssertionError("provider operation should outlive the request timeout")

    monkeypatch.setattr(app_module, "search_models", slow_provider)
    monkeypatch.setattr(app_module, "discover_revisions", slow_provider)
    monkeypatch.setattr(app_module, "estimate_download", slow_provider)
    settings = Settings(
        storage_root=tmp_path / "storage",
        write_tokens=("write-token",),
        session_secret="test-session-secret-with-32-bytes-minimum",
        provider_metadata_timeout_seconds=0.01,
    )
    headers = {"Authorization": "Bearer write-token"}
    requests = (
        ("/api/v1/providers/modelscope-cn/models", {"q": "tiny"}, "model search"),
        (
            "/api/v1/providers/modelscope-cn/revisions",
            {"id": "owner/model"},
            "revision discovery",
        ),
        (
            "/api/v1/providers/modelscope-cn/estimate",
            {"id": "owner/model", "revision": "master"},
            "download preflight",
        ),
    )
    with TestClient(create_app(settings)) as client:
        for path, params, operation in requests:
            response = client.get(path, params=params, headers=headers)
            assert response.status_code == 504
            assert response.json()["detail"] == (
                f"ModelScope CN {operation} failed (TimeoutError): "
                "timed out after 0.01 seconds waiting for the provider"
            )
        time.sleep(0.15)


def test_estimate_reports_an_existing_immutable_artifact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    resolved = "b" * 40
    storage_root = tmp_path / "storage"
    catalog = Catalog(storage_root)
    catalog.initialize()
    stage = catalog.staging_path("seed") / "artifact"
    stage.mkdir(parents=True)
    (stage / "model.bin").write_bytes(b"weights")
    manifest = catalog.create_manifest(
        stage,
        name="model",
        version=resolved,
        source=SourceReference(
            provider=Provider.HUGGINGFACE,
            id="owner/model",
            requested_revision="main",
            resolved_revision=resolved,
        ),
    )
    catalog.publish(stage, manifest)

    async def fake_estimate(
        provider: Provider,
        source_id: str,
        revision: str,
        **_network: object,
    ) -> DownloadEstimate:
        return DownloadEstimate(provider, source_id, revision, resolved, 7, 1)

    monkeypatch.setattr(app_module, "estimate_download", fake_estimate)
    settings = Settings(
        storage_root=storage_root,
        write_tokens=("write-token",),
        session_secret="test-session-secret-with-32-bytes-minimum",
    )
    headers = {"Authorization": "Bearer write-token"}
    with TestClient(create_app(settings)) as client:
        estimate = client.get(
            "/api/v1/providers/huggingface/estimate",
            params={"id": "owner/model", "revision": "main"},
            headers=headers,
        )
        assert estimate.status_code == 200
        assert estimate.json()["duplicate"] == {
            "kind": "artifact",
            "artifactId": manifest.artifact_id,
        }

        created = client.post(
            "/api/v1/tasks",
            json={"provider": "huggingface", "id": "owner/model", "revision": "main"},
            headers=headers,
        )
        assert created.status_code == 202
        assert created.json()["status"] == "completed"
        assert created.json()["deduplicated"] is True
        assert created.json()["deduplicationReason"] == "artifact"

        repeated_estimate = client.get(
            "/api/v1/providers/huggingface/estimate",
            params={"id": "owner/model", "revision": "main"},
            headers=headers,
        )
        assert repeated_estimate.json()["duplicate"]["taskId"] == created.json()["id"]


def test_task_creation_rejects_a_failed_download_preflight(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def unavailable(
        provider: Provider,
        source_id: str,
        revision: str,
        *,
        github_token: str | None,
        **_network: object,
    ) -> DownloadEstimate:
        raise ValueError("requested revision was not found")

    monkeypatch.setattr(app_module, "estimate_download", unavailable)
    settings = Settings(
        storage_root=tmp_path / "storage",
        write_tokens=("write-token",),
        session_secret="test-session-secret-with-32-bytes-minimum",
    )
    with TestClient(create_app(settings)) as client:
        response = client.post(
            "/api/v1/tasks",
            json={"provider": "huggingface", "id": "owner/model", "revision": "missing"},
            headers={"Authorization": "Bearer write-token"},
        )
        assert response.status_code == 422
        assert response.json()["detail"] == (
            "Hugging Face download preflight failed: requested revision was not found"
        )
        assert (
            client.get("/api/v1/tasks", headers={"Authorization": "Bearer write-token"}).json()
            == []
        )


def test_filesystem_artifacts_cannot_be_created_as_download_tasks(tmp_path: Path) -> None:
    settings = Settings(
        storage_root=tmp_path / "storage",
        write_tokens=("write-token",),
        session_secret="test-session-secret-with-32-bytes-minimum",
    )
    with TestClient(create_app(settings)) as client:
        response = client.post(
            "/api/v1/tasks",
            json={"provider": "filesystem", "id": "offline/model", "revision": "content"},
            headers={"Authorization": "Bearer write-token"},
        )

    assert response.status_code == 422
    assert response.json()["detail"] == (
        "Filesystem download preflight failed: filesystem artifacts must be created with "
        "modelshelf-server import"
    )


def test_network_configuration_is_reported_without_proxy_credentials_and_forwarded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict[str, object] = {}

    async def fake_estimate(
        provider: Provider,
        source_id: str,
        revision: str,
        **network: object,
    ) -> DownloadEstimate:
        captured.update(network)
        return DownloadEstimate(provider, source_id, revision, "a" * 40, 1_500_000_000, 3)

    proxy = "http://proxy-user:proxy-password@proxy.internal:3128"
    mirror = "https://hf-mirror.internal"
    monkeypatch.setenv("HTTP_PROXY", proxy)
    monkeypatch.setenv("HTTPS_PROXY", proxy)
    monkeypatch.setenv("HF_ENDPOINT", mirror)
    monkeypatch.setattr(app_module, "estimate_download", fake_estimate)
    settings = Settings(
        storage_root=tmp_path / "storage",
        write_tokens=("write-token",),
        session_secret="test-session-secret-with-32-bytes-minimum",
        huggingface_mirror=mirror,
        http_proxy=proxy,
    )
    with TestClient(create_app(settings)) as client:
        info = client.get("/api/v1/info").json()
        assert info["network"] == {
            "mirrors": {"huggingface": mirror},
            "proxyConfigured": True,
            "proxyDisplay": "http://proxy.internal:3128",
        }
        estimate = client.get(
            "/api/v1/providers/huggingface/estimate",
            params={
                "id": "owner/model",
                "revision": "main",
                "disableMirror": True,
                "disableProxy": True,
            },
            headers={"Authorization": "Bearer write-token"},
        )
        assert estimate.status_code == 200
    assert captured["huggingface_mirror"] == mirror
    assert captured["proxy_url"] == proxy
    assert captured["disable_mirror"] is True
    assert captured["disable_proxy"] is True


def test_temporary_mirror_overrides_server_mirror_for_preflight_and_task(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[dict[str, object]] = []

    async def fake_estimate(
        provider: Provider,
        source_id: str,
        revision: str,
        **network: object,
    ) -> DownloadEstimate:
        calls.append(network)
        return DownloadEstimate(provider, source_id, revision, "a" * 40, 100, 1)

    monkeypatch.setattr(app_module, "estimate_download", fake_estimate)
    settings = Settings(
        storage_root=tmp_path / "storage",
        write_tokens=("write-token",),
        session_secret="test-session-secret-with-32-bytes-minimum",
        huggingface_mirror="https://server-mirror.example",
    )
    headers = {"Authorization": "Bearer write-token"}
    temporary_mirror = "https://temporary-mirror.example/"
    with TestClient(create_app(settings)) as client:
        estimate = client.get(
            "/api/v1/providers/huggingface/estimate",
            params={
                "id": "owner/model",
                "revision": "main",
                "mirrorUrl": temporary_mirror,
            },
            headers=headers,
        )
        assert estimate.status_code == 200
        created = client.post(
            "/api/v1/tasks",
            json={
                "provider": "huggingface",
                "id": "owner/model",
                "revision": "main",
                "mirrorUrl": temporary_mirror,
            },
            headers=headers,
        )
        assert created.status_code == 202
        assert created.json()["mirrorUrl"] == "https://temporary-mirror.example"

    assert len(calls) == 1  # The task creation reuses the current preflight cache entry.
    assert calls[0]["huggingface_mirror"] == "https://server-mirror.example"
    assert calls[0]["mirror_url"] == "https://temporary-mirror.example"


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        (
            {"provider": "http", "mirrorUrl": "https://mirror.example"},
            "temporary mirrors are not supported for this source",
        ),
        (
            {
                "provider": "huggingface",
                "mirrorUrl": "https://user:password@mirror.example",
            },
            "temporary mirror address must not contain credentials",
        ),
        (
            {
                "provider": "huggingface",
                "mirrorUrl": "https://mirror.example",
                "disableMirror": True,
            },
            "temporary mirror and mirror bypass cannot be enabled together",
        ),
    ],
)
def test_task_rejects_invalid_temporary_mirror_routes(
    tmp_path: Path, payload: dict[str, object], message: str
) -> None:
    settings = Settings(
        storage_root=tmp_path / "storage",
        write_tokens=("write-token",),
        session_secret="test-session-secret-with-32-bytes-minimum",
    )
    body = {"id": "owner/model", "revision": "main", **payload}
    with TestClient(create_app(settings)) as client:
        response = client.post(
            "/api/v1/tasks",
            json=body,
            headers={"Authorization": "Bearer write-token"},
        )

    assert response.status_code == 422
    assert message in str(response.json()["detail"])


def test_task_can_be_scheduled_and_started_immediately(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    resolved = "7" * 40

    async def fake_estimate(
        provider: Provider, source_id: str, revision: str, **_network: object
    ) -> DownloadEstimate:
        return DownloadEstimate(provider, source_id, revision, resolved, 5, 1)

    async def fake_provider(
        _provider: Provider,
        _source_id: str,
        _revision: str,
        destination: Path,
        _progress: object,
        **_options: object,
    ) -> ProviderResult:
        (destination / "model.bin").write_bytes(b"model")
        return ProviderResult(resolved_revision=resolved)

    monkeypatch.setattr(app_module, "estimate_download", fake_estimate)
    monkeypatch.setattr(task_module, "run_provider", fake_provider)
    settings = Settings(
        storage_root=tmp_path / "storage",
        write_tokens=("write-token",),
        session_secret="test-session-secret-with-32-bytes-minimum",
    )
    headers = {"Authorization": "Bearer write-token"}
    scheduled_at = datetime.now(UTC) + timedelta(hours=1)
    with TestClient(create_app(settings)) as client:
        created = client.post(
            "/api/v1/tasks",
            json={
                "provider": "huggingface",
                "id": "owner/model",
                "revision": "main",
                "scheduledAt": scheduled_at.isoformat(),
            },
            headers=headers,
        )
        assert created.status_code == 202
        assert created.json()["status"] == "scheduled"
        assert created.json()["resolvedRevision"] == resolved
        task_id = created.json()["id"]

        started = client.post(f"/api/v1/tasks/{task_id}/start", headers=headers)
        assert started.status_code == 200
        assert started.json()["status"] == "queued"


def test_task_schedule_requires_an_explicit_timezone(tmp_path: Path) -> None:
    settings = Settings(
        storage_root=tmp_path / "storage",
        write_tokens=("write-token",),
        session_secret="test-session-secret-with-32-bytes-minimum",
    )
    with TestClient(create_app(settings)) as client:
        response = client.post(
            "/api/v1/tasks",
            json={
                "provider": "huggingface",
                "id": "owner/model",
                "revision": "main",
                "scheduledAt": "2026-09-01T12:00:00",
            },
            headers={"Authorization": "Bearer write-token"},
        )

    assert response.status_code == 422
    assert "scheduled start must include a timezone" in str(response.json()["detail"])


def test_paused_task_can_schedule_a_delayed_resume(tmp_path: Path) -> None:
    storage = tmp_path / "storage"
    catalog = Catalog(storage)
    catalog.initialize()
    store = TaskStore(catalog.jobs_root)
    task = store.create(
        Provider.HUGGINGFACE,
        "owner/model",
        "main",
        resolved_revision="4" * 40,
        total_bytes=10,
        disable_mirror=False,
        disable_proxy=False,
    )
    store.update(task.id, {"status": TaskStatus.PAUSED, "resume_from_stage": True})
    settings = Settings(
        storage_root=storage,
        write_tokens=("write-token",),
        session_secret="test-session-secret-with-32-bytes-minimum",
    )
    scheduled_at = datetime.now(UTC) + timedelta(hours=1)
    with TestClient(create_app(settings)) as client:
        response = client.post(
            f"/api/v1/tasks/{task.id}/resume",
            json={"scheduledAt": scheduled_at.isoformat()},
            headers={"Authorization": "Bearer write-token"},
        )

    assert response.status_code == 200
    assert response.json()["status"] == "scheduled"
    assert response.json()["resumeFromStage"] is True
    assert response.json()["scheduledAt"] == scheduled_at.isoformat().replace("+00:00", "Z")
