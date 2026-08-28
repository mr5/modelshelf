from __future__ import annotations

import threading
import time
import zipfile
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import modelshelf_server.app as app_module
import pytest
from argon2 import PasswordHasher
from fastapi.testclient import TestClient
from modelshelf_core import Provider
from modelshelf_server.app import create_app
from modelshelf_server.config import Settings
from modelshelf_server.providers import (
    DownloadEstimate,
    EstimateMetadata,
    ModelOption,
    ModelSearch,
    RevisionDiscovery,
    RevisionOption,
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
            assert client.get(
                "/api/v1/artifacts", params={"provider": "huggingface"}
            ).json() == []
            assert client.get(
                "/api/v1/artifacts", params={"sortBy": "unsupported"}
            ).status_code == 422
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
    assert calls == {"models": 1, "revisions": 1, "estimates": 1}


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
        assert response.json()["detail"] == "requested revision was not found"
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
        "filesystem artifacts must be created with modelshelf-server import"
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
