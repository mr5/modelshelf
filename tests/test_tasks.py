from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from pathlib import Path

import modelshelf_server.tasks as task_module
import pytest
from modelshelf_core import (
    Catalog,
    FutureSchemaVersionError,
    Provider,
    SourceReference,
    TaskStatus,
)
from modelshelf_server.providers import Progress, ProviderResult
from modelshelf_server.tasks import TaskManager


@pytest.mark.parametrize(
    "terminal_status",
    [TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED],
)
def test_delete_task_only_accepts_terminal_states_and_removes_staging(
    tmp_path: Path, terminal_status: TaskStatus
) -> None:
    catalog = Catalog(tmp_path / "storage")
    catalog.initialize()
    manager = TaskManager(catalog, github_token=None)
    task = manager.store.create(
        Provider.HUGGINGFACE,
        "owner/model",
        "main",
        resolved_revision="a" * 40,
        total_bytes=7,
        disable_mirror=False,
        disable_proxy=False,
    )
    manager.store.update(task.id, {"status": terminal_status})
    stage = catalog.staging_path(task.id)
    stage.mkdir()
    (stage / "partial.bin").write_bytes(b"partial")

    asyncio.run(manager.delete_task(task.id))

    assert manager.store.get(task.id) is None
    assert not stage.exists()


def test_delete_task_rejects_nonterminal_state(tmp_path: Path) -> None:
    catalog = Catalog(tmp_path / "storage")
    catalog.initialize()
    manager = TaskManager(catalog, github_token=None)
    task = manager.store.create(
        Provider.HUGGINGFACE,
        "owner/model",
        "main",
        resolved_revision="a" * 40,
        total_bytes=7,
        disable_mirror=False,
        disable_proxy=False,
    )
    manager.store.update(task.id, {"status": TaskStatus.PAUSED})

    with pytest.raises(ValueError, match="only completed, failed or cancelled"):
        asyncio.run(manager.delete_task(task.id))
    assert manager.store.get(task.id) is not None


def test_delete_completed_task_keeps_its_published_artifact(tmp_path: Path) -> None:
    catalog = Catalog(tmp_path / "storage")
    catalog.initialize()
    stage = catalog.staging_path("published")
    stage.mkdir()
    (stage / "model.bin").write_bytes(b"weights")
    manifest = catalog.create_manifest(
        stage,
        name="model",
        version="1",
        source=SourceReference(
            provider=Provider.HUGGINGFACE,
            id="owner/model",
            requested_revision="main",
            resolved_revision="a" * 40,
        ),
    )
    destination, _ = catalog.publish(stage, manifest)
    manager = TaskManager(catalog, github_token=None)
    task = manager.store.create_completed(
        Provider.HUGGINGFACE,
        "owner/model",
        "main",
        "a" * 40,
        manifest.artifact_id,
        manifest.total_size,
        disable_mirror=False,
        disable_proxy=False,
    )

    asyncio.run(manager.delete_task(task.id))

    assert manager.store.get(task.id) is None
    assert destination.exists()
    assert catalog.find(manifest.artifact_id) is not None


def test_delete_completed_task_can_explicitly_delete_its_artifact(tmp_path: Path) -> None:
    catalog = Catalog(tmp_path / "storage")
    catalog.initialize()
    stage = catalog.staging_path("published")
    stage.mkdir()
    (stage / "model.bin").write_bytes(b"weights")
    manifest = catalog.create_manifest(
        stage,
        name="model",
        version="1",
        source=SourceReference(
            provider=Provider.HUGGINGFACE,
            id="owner/model",
            requested_revision="main",
            resolved_revision="a" * 40,
        ),
    )
    destination, _ = catalog.publish(stage, manifest)
    manager = TaskManager(catalog, github_token=None)
    task = manager.store.create_completed(
        Provider.HUGGINGFACE,
        "owner/model",
        "main",
        "a" * 40,
        manifest.artifact_id,
        manifest.total_size,
        disable_mirror=False,
        disable_proxy=False,
    )

    asyncio.run(manager.delete_task(task.id, delete_artifact=True))

    assert manager.store.get(task.id) is None
    assert not destination.exists()
    assert catalog.find(manifest.artifact_id) is None


def test_create_reuses_task_with_the_same_immutable_identity(tmp_path: Path) -> None:
    catalog = Catalog(tmp_path / "storage")
    catalog.initialize()
    manager = TaskManager(catalog, github_token=None)
    resolved = "a" * 40

    async def exercise() -> None:
        first, second = await asyncio.gather(
            manager.create(
                Provider.HUGGINGFACE,
                "owner/model",
                "main",
                resolved_revision=resolved,
            ),
            manager.create(
                Provider.HUGGINGFACE,
                "owner/model",
                "v1",
                resolved_revision=resolved,
            ),
        )
        assert second.id == first.id
        assert len(manager.store.list()) == 1
        assert manager.queue.qsize() == 1

    asyncio.run(exercise())


def test_pre_release_task_file_is_atomically_upgraded_to_schema_v1(tmp_path: Path) -> None:
    catalog = Catalog(tmp_path / "storage")
    catalog.initialize()
    manager = TaskManager(catalog, github_token=None)

    async def create() -> str:
        task = await manager.create(Provider.HTTP, "https://example.test/model", "content")
        return task.id

    task_id = asyncio.run(create())
    task_path = catalog.jobs_root / f"{task_id}.json"
    document = json.loads(task_path.read_text(encoding="utf-8"))
    document.pop("schemaVersion")
    task_path.write_text(json.dumps(document), encoding="utf-8")

    loaded = manager.store.get(task_id)
    assert loaded is not None
    assert json.loads(task_path.read_text(encoding="utf-8"))["schemaVersion"] == 1

    document = json.loads(task_path.read_text(encoding="utf-8"))
    document["schemaVersion"] = 2
    task_path.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(FutureSchemaVersionError, match="upgrade ModelShelf"):
        manager.store.list()


def test_create_reuses_existing_artifact_without_downloading(tmp_path: Path) -> None:
    catalog = Catalog(tmp_path / "storage")
    catalog.initialize()
    resolved = "b" * 40
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
    manager = TaskManager(catalog, github_token=None)

    async def exercise() -> None:
        before_create = manager.find_duplicate(
            Provider.HUGGINGFACE,
            "owner/model",
            "main",
            resolved_revision=resolved,
        )
        assert before_create is not None
        assert before_create.kind == "artifact"
        assert before_create.artifact_id == manifest.artifact_id
        assert before_create.task is None

        first_result = await manager.create_with_result(
            Provider.HUGGINGFACE,
            "owner/model",
            "main",
            resolved_revision=resolved,
        )
        first = first_result.task
        second = await manager.create(
            Provider.HUGGINGFACE,
            "owner/model",
            "stable",
            resolved_revision=resolved,
        )
        assert first.status.value == "completed"
        assert first.artifact_id == manifest.artifact_id
        assert first.bytes_downloaded == manifest.total_size
        assert first_result.deduplication_reason == "artifact"
        assert second.id == first.id
        assert manager.queue.empty()

    asyncio.run(exercise())


def test_create_does_not_deduplicate_without_resolved_revision(tmp_path: Path) -> None:
    catalog = Catalog(tmp_path / "storage")
    catalog.initialize()
    manager = TaskManager(catalog, github_token=None)

    async def exercise() -> None:
        first = await manager.create(Provider.HTTP, "https://example.test/model", "content")
        second = await manager.create(Provider.HTTP, "https://example.test/model", "content")
        assert second.id != first.id
        assert len(manager.store.list()) == 2
        assert manager.queue.qsize() == 2

    asyncio.run(exercise())


def test_create_keeps_explicit_network_routes_separate(tmp_path: Path) -> None:
    catalog = Catalog(tmp_path / "storage")
    catalog.initialize()
    manager = TaskManager(catalog, github_token=None)
    resolved = "c" * 40

    async def exercise() -> None:
        default_route = await manager.create(
            Provider.HUGGINGFACE,
            "owner/model",
            "main",
            resolved_revision=resolved,
        )
        direct_route = await manager.create(
            Provider.HUGGINGFACE,
            "owner/model",
            "main",
            resolved_revision=resolved,
            disable_mirror=True,
        )
        assert direct_route.id != default_route.id
        assert manager.queue.qsize() == 2

    asyncio.run(exercise())


def test_download_uses_the_preflight_resolved_revision(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured_revision = ""
    resolved = "d" * 40

    async def fake_provider(
        _provider: Provider,
        _source_id: str,
        revision: str,
        destination: Path,
        _progress: Progress,
        **_options: object,
    ) -> ProviderResult:
        nonlocal captured_revision
        captured_revision = revision
        (destination / "model.bin").write_bytes(b"model")
        return ProviderResult(resolved_revision=resolved)

    monkeypatch.setattr(task_module, "run_provider", fake_provider)
    catalog = Catalog(tmp_path / "storage")
    catalog.initialize()
    manager = TaskManager(catalog, github_token=None)

    async def exercise() -> None:
        await manager.start()
        try:
            task = await manager.create(
                Provider.HUGGINGFACE,
                "owner/model",
                "main",
                resolved_revision=resolved,
            )
            await asyncio.wait_for(manager.queue.join(), timeout=2)
            assert manager.store.get(task.id).status.value == "completed"  # type: ignore[union-attr]
        finally:
            await manager.stop()

    asyncio.run(exercise())
    assert captured_revision == resolved


def test_modelscope_download_uses_requested_revision_with_preflight_commit_guard(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict[str, object] = {}
    resolved = "e" * 40

    async def fake_provider(
        _provider: Provider,
        _source_id: str,
        revision: str,
        destination: Path,
        _progress: Progress,
        **options: object,
    ) -> ProviderResult:
        captured["revision"] = revision
        captured["expected"] = options["expected_resolved_revision"]
        (destination / "model.bin").write_bytes(b"model")
        return ProviderResult(resolved_revision=resolved)

    monkeypatch.setattr(task_module, "run_provider", fake_provider)
    catalog = Catalog(tmp_path / "storage")
    catalog.initialize()
    manager = TaskManager(catalog, github_token=None)

    async def exercise() -> None:
        await manager.start()
        try:
            task = await manager.create(
                Provider.MODELSCOPE_CN,
                "owner/model",
                "master",
                resolved_revision=resolved,
            )
            await asyncio.wait_for(manager.queue.join(), timeout=2)
            assert manager.store.get(task.id).status.value == "completed"  # type: ignore[union-attr]
        finally:
            await manager.stop()

    asyncio.run(exercise())
    assert captured == {"revision": "master", "expected": resolved}


def test_download_rejects_content_that_does_not_match_preflight_size(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fake_provider(
        _provider: Provider,
        _source_id: str,
        _revision: str,
        destination: Path,
        _progress: Progress,
        **_options: object,
    ) -> ProviderResult:
        (destination / "model.bin").write_bytes(b"short")
        return ProviderResult(resolved_revision="f" * 40)

    monkeypatch.setattr(task_module, "run_provider", fake_provider)
    catalog = Catalog(tmp_path / "storage")
    catalog.initialize()
    manager = TaskManager(catalog, github_token=None)

    async def exercise() -> None:
        await manager.start()
        try:
            task = await manager.create(
                Provider.MODELSCOPE_CN,
                "owner/model",
                "master",
                resolved_revision="f" * 40,
                total_bytes=10,
            )
            await asyncio.wait_for(manager.queue.join(), timeout=2)
            failed = manager.store.get(task.id)
            assert failed is not None
            assert failed.status.value == "failed"
            assert "expected 10 bytes, got 5" in (failed.error or "")
        finally:
            await manager.stop()

    asyncio.run(exercise())


def test_known_preflight_size_drives_download_progress_and_speed(
    tmp_path: Path,
) -> None:
    moments = iter((100.0, 110.0))
    catalog = Catalog(tmp_path / "storage")
    catalog.initialize()
    manager = TaskManager(catalog, github_token=None, clock=lambda: next(moments))

    async def exercise() -> tuple[int, int, int | None, float, float, int | None]:
        task = await manager.create(
            Provider.MODELSCOPE_CN,
            "owner/model",
            "master",
            total_bytes=1_000,
        )
        manager._start_metrics(task)
        await manager._progress(task.id, 500, task.total_bytes)
        updated = manager.store.get(task.id)
        assert updated is not None
        return (
            updated.progress,
            updated.bytes_downloaded,
            updated.total_bytes,
            updated.instantaneous_bytes_per_second,
            updated.average_bytes_per_second,
            updated.eta_seconds,
        )

    assert asyncio.run(exercise()) == (45, 500, 1_000, 50, 50, 10)


def test_average_speed_uses_only_bytes_measured_after_resume(tmp_path: Path) -> None:
    moments = iter((200.0, 210.0))
    catalog = Catalog(tmp_path / "storage")
    catalog.initialize()
    manager = TaskManager(catalog, github_token=None, clock=lambda: next(moments))

    async def exercise() -> float:
        created = await manager.create(
            Provider.HUGGINGFACE,
            "owner/model",
            "main",
            total_bytes=4_000,
        )
        resumed = manager.store.update(
            created.id,
            {
                "bytes_downloaded": 1_000,
                "download_elapsed_seconds": 10,
                "average_bytes_per_second": 100,
            },
        )
        manager._start_metrics(resumed)
        await manager._progress(resumed.id, 3_000, resumed.total_bytes)
        updated = manager.store.get(resumed.id)
        assert updated is not None
        return updated.average_bytes_per_second

    assert asyncio.run(exercise()) == 150


def test_active_task_can_pause_and_resume_from_preserved_stage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    started = asyncio.Event()
    release_forever = asyncio.Event()
    attempts = 0
    resumed_from_stage = False

    async def fake_provider(
        _provider: Provider,
        _source_id: str,
        _revision: str,
        destination: Path,
        progress: Progress,
        **_options: object,
    ) -> ProviderResult:
        nonlocal attempts, resumed_from_stage
        attempts += 1
        marker = destination.parent / "resume-marker"
        if attempts == 1:
            marker.write_text("keep", encoding="utf-8")
            (destination / "partial.bin").write_bytes(b"partial")
            await progress(100, None)
            started.set()
            await release_forever.wait()
        resumed_from_stage = marker.is_file()
        (destination / "model.bin").write_bytes(b"complete")
        await progress(1_000, None)
        return ProviderResult(resolved_revision="a" * 40)

    monkeypatch.setattr(task_module, "run_provider", fake_provider)
    catalog = Catalog(tmp_path / "storage")
    catalog.initialize()
    manager = TaskManager(catalog, github_token=None)

    async def exercise() -> None:
        await manager.start()
        try:
            task = await manager.create(
                Provider.MODELSCOPE_CN,
                "owner/model",
                "master",
                total_bytes=len(b"partial") + len(b"complete"),
            )
            await asyncio.wait_for(started.wait(), timeout=2)
            paused = await manager.pause(task.id)
            assert paused.status.value == "paused"
            assert catalog.staging_path(task.id).is_dir()
            resumed = await manager.resume(task.id)
            assert resumed.status.value == "queued"
            await asyncio.wait_for(manager.queue.join(), timeout=2)
            completed = manager.store.get(task.id)
            assert completed is not None
            assert completed.status.value == "completed"
        finally:
            await manager.stop()

    asyncio.run(exercise())
    assert attempts == 2
    assert resumed_from_stage is True


def test_cancel_active_task_stops_work_and_removes_stage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    started = asyncio.Event()
    release_forever = asyncio.Event()

    async def fake_provider(
        _provider: Provider,
        _source_id: str,
        _revision: str,
        destination: Path,
        _progress: Progress,
        **_options: object,
    ) -> ProviderResult:
        (destination / "partial.bin").write_bytes(b"partial")
        started.set()
        await release_forever.wait()
        return ProviderResult(resolved_revision="a" * 40)

    monkeypatch.setattr(task_module, "run_provider", fake_provider)
    catalog = Catalog(tmp_path / "storage")
    catalog.initialize()
    manager = TaskManager(catalog, github_token=None)

    async def exercise() -> None:
        await manager.start()
        try:
            task = await manager.create(Provider.HUGGINGFACE, "owner/model", "main")
            await asyncio.wait_for(started.wait(), timeout=2)
            cancelled = await manager.cancel(task.id)
            assert cancelled.status.value == "cancelled"
            assert not catalog.staging_path(task.id).exists()
        finally:
            await manager.stop()

    asyncio.run(exercise())


def test_scheduler_enforces_global_and_per_source_limits_without_head_of_line_blocking(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    started: set[str] = set()
    active: set[str] = set()
    gates: dict[str, asyncio.Event] = {}
    max_active = 0

    async def fake_provider(
        _provider: Provider,
        source_id: str,
        _revision: str,
        destination: Path,
        _progress: Progress,
        **_options: object,
    ) -> ProviderResult:
        nonlocal max_active
        started.add(source_id)
        active.add(source_id)
        max_active = max(max_active, len(active))
        try:
            await gates[source_id].wait()
            (destination / "model.bin").write_bytes(source_id.encode())
            return ProviderResult(resolved_revision="a" * 40)
        finally:
            active.remove(source_id)

    async def wait_until(predicate: Callable[[], bool]) -> None:
        async with asyncio.timeout(2):
            while not predicate():
                await asyncio.sleep(0.01)

    monkeypatch.setattr(task_module, "run_provider", fake_provider)
    catalog = Catalog(tmp_path / "storage")
    catalog.initialize()
    manager = TaskManager(
        catalog,
        github_token=None,
        max_concurrent_downloads=2,
        max_concurrent_downloads_per_source=1,
    )

    async def exercise() -> None:
        for source_id in ("hf-one", "hf-two", "ms-one", "github-one"):
            gates[source_id] = asyncio.Event()
        await manager.start()
        try:
            first = await manager.create(Provider.HUGGINGFACE, "hf-one", "main")
            second = await manager.create(Provider.HUGGINGFACE, "hf-two", "main")
            third = await manager.create(Provider.MODELSCOPE_CN, "ms-one", "master")
            fourth = await manager.create(Provider.GITHUB_RELEASE, "github-one", "latest")

            await wait_until(lambda: {"hf-one", "ms-one"}.issubset(started))
            assert "hf-two" not in started
            assert "github-one" not in started
            assert max_active == 2

            paused = await manager.pause(first.id)
            assert paused.status.value == "paused"
            await wait_until(lambda: "hf-two" in started)
            still_running = manager.store.get(third.id)
            assert still_running is not None
            assert still_running.status.value == "downloading"
            assert max_active == 2

            gates["ms-one"].set()
            await wait_until(lambda: "github-one" in started)
            gates["hf-two"].set()
            gates["github-one"].set()
            await asyncio.wait_for(manager.queue.join(), timeout=2)

            assert manager.store.get(second.id).status.value == "completed"  # type: ignore[union-attr]
            assert manager.store.get(third.id).status.value == "completed"  # type: ignore[union-attr]
            assert manager.store.get(fourth.id).status.value == "completed"  # type: ignore[union-attr]
        finally:
            await manager.stop()

    asyncio.run(exercise())
