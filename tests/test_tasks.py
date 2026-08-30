from __future__ import annotations

import asyncio
import hashlib
import json
import threading
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

import modelshelf_server.tasks as task_module
import pytest
from modelshelf_core import (
    Catalog,
    FileEntry,
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


def test_old_task_files_are_atomically_upgraded_to_current_schema(tmp_path: Path) -> None:
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
    assert loaded.mirror_url is None
    assert json.loads(task_path.read_text(encoding="utf-8"))["schemaVersion"] == 6

    document = json.loads(task_path.read_text(encoding="utf-8"))
    document["schemaVersion"] = 1
    document.pop("mirrorUrl", None)
    task_path.write_text(json.dumps(document), encoding="utf-8")
    migrated = manager.store.get(task_id)
    assert migrated is not None
    assert migrated.mirror_url is None
    assert migrated.scheduled_at is None
    assert json.loads(task_path.read_text(encoding="utf-8"))["schemaVersion"] == 6

    document = json.loads(task_path.read_text(encoding="utf-8"))
    document["schemaVersion"] = 4
    document.pop("verificationBytesCompleted", None)
    document.pop("verificationDetail", None)
    task_path.write_text(json.dumps(document), encoding="utf-8")
    migrated = manager.store.get(task_id)
    assert migrated is not None
    assert migrated.verification_bytes_completed == 0
    assert migrated.verification_detail is None
    assert migrated.artifact_alias is None
    assert json.loads(task_path.read_text(encoding="utf-8"))["schemaVersion"] == 6

    document = json.loads(task_path.read_text(encoding="utf-8"))
    document["schemaVersion"] = 7
    task_path.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(FutureSchemaVersionError, match="upgrade ModelShelf"):
        manager.store.list()


def test_reordered_queue_is_persisted_and_controls_download_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    execution_order: list[str] = []

    async def fake_provider(
        _provider: Provider,
        source_id: str,
        _revision: str,
        destination: Path,
        _progress: Progress,
        **_options: object,
    ) -> ProviderResult:
        execution_order.append(source_id)
        (destination / "model.bin").write_bytes(b"model")
        suffix = source_id.rsplit("-", 1)[-1]
        return ProviderResult(resolved_revision=suffix * 40)

    monkeypatch.setattr(task_module, "run_provider", fake_provider)
    catalog = Catalog(tmp_path / "storage")
    catalog.initialize()
    seed = TaskManager(catalog, github_token=None)

    async def create_and_reorder() -> list[str]:
        tasks = [
            await seed.create(
                Provider.HUGGINGFACE,
                f"owner/model-{suffix}",
                "main",
                resolved_revision=suffix * 40,
            )
            for suffix in ("a", "b", "c")
        ]
        ordered_ids = [tasks[2].id, tasks[0].id, tasks[1].id]
        reordered = seed.reorder_queued(ordered_ids)
        assert [task.id for task in reordered] == ordered_ids
        assert [task.queue_position for task in reordered] == [0, 1, 2]
        return ordered_ids

    ordered_ids = asyncio.run(create_and_reorder())
    restarted = TaskManager(
        catalog,
        github_token=None,
        max_concurrent_downloads=1,
        max_concurrent_downloads_per_source=1,
    )

    async def execute() -> None:
        await restarted.start()
        try:
            await asyncio.wait_for(restarted.queue.join(), timeout=2)
        finally:
            await restarted.stop()

    asyncio.run(execute())

    assert execution_order == ["owner/model-c", "owner/model-a", "owner/model-b"]
    assert all(restarted.store.get(task_id) is not None for task_id in ordered_ids)


def test_reorder_rejects_a_stale_or_partial_queue(tmp_path: Path) -> None:
    catalog = Catalog(tmp_path / "storage")
    catalog.initialize()
    manager = TaskManager(catalog, github_token=None)

    async def create() -> tuple[str, str]:
        first = await manager.create(Provider.HTTP, "https://example.test/first", "content")
        second = await manager.create(Provider.HTTP, "https://example.test/second", "content")
        return first.id, second.id

    first_id, second_id = asyncio.run(create())

    with pytest.raises(ValueError, match="queue changed"):
        manager.reorder_queued([second_id])
    with pytest.raises(ValueError, match="duplicate"):
        manager.reorder_queued([first_id, first_id])


def test_paused_task_keeps_its_priority_when_reordered_and_resumed(
    tmp_path: Path,
) -> None:
    catalog = Catalog(tmp_path / "storage")
    catalog.initialize()
    manager = TaskManager(catalog, github_token=None)

    async def exercise() -> None:
        first = await manager.create(Provider.HTTP, "https://example.test/first", "content")
        second = await manager.create(Provider.HTTP, "https://example.test/second", "content")

        paused = await manager.pause(second.id)
        assert paused.status is TaskStatus.PAUSED
        assert paused.queue_position == second.queue_position

        reordered = manager.reorder_queued([second.id, first.id])
        assert [task.status for task in reordered] == [
            TaskStatus.PAUSED,
            TaskStatus.QUEUED,
        ]
        assert [task.queue_position for task in reordered] == [0, 1]

        resumed = await manager.resume(second.id)
        assert resumed.status is TaskStatus.QUEUED
        assert resumed.queue_position == 0

    asyncio.run(exercise())


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
            artifact_alias="production-model",
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
        assert first.artifact_alias == "production-model"
        found = catalog.find(manifest.artifact_id)
        assert found is not None and found[0].alias == "production-model"
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


def test_task_creation_reserves_artifact_alias_until_terminal_state(tmp_path: Path) -> None:
    catalog = Catalog(tmp_path / "storage")
    catalog.initialize()
    manager = TaskManager(catalog, github_token=None)

    async def exercise() -> None:
        first = await manager.create(
            Provider.HUGGINGFACE,
            "owner/first",
            "main",
            resolved_revision="a" * 40,
            artifact_alias="shared-alias",
        )
        with pytest.raises(ValueError, match="reserved by task"):
            await manager.create(
                Provider.HUGGINGFACE,
                "owner/second",
                "main",
                resolved_revision="b" * 40,
                artifact_alias="shared-alias",
            )

        await manager.cancel(first.id)
        second = await manager.create(
            Provider.HUGGINGFACE,
            "owner/second",
            "main",
            resolved_revision="b" * 40,
            artifact_alias="shared-alias",
        )
        assert second.artifact_alias == "shared-alias"

    asyncio.run(exercise())


def test_duplicate_task_can_reserve_one_alias_but_not_two(tmp_path: Path) -> None:
    catalog = Catalog(tmp_path / "storage")
    catalog.initialize()
    manager = TaskManager(catalog, github_token=None)

    async def exercise() -> None:
        first = await manager.create(
            Provider.HUGGINGFACE,
            "owner/model",
            "main",
            resolved_revision="a" * 40,
        )
        aliased = await manager.create_with_result(
            Provider.HUGGINGFACE,
            "owner/model",
            "main",
            resolved_revision="a" * 40,
            artifact_alias="first-alias",
        )
        assert aliased.task.id == first.id
        assert aliased.task.artifact_alias == "first-alias"
        with pytest.raises(ValueError, match="already reserves"):
            await manager.create(
                Provider.HUGGINGFACE,
                "owner/model",
                "main",
                resolved_revision="a" * 40,
                artifact_alias="second-alias",
            )

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
        temporary_mirror_route = await manager.create(
            Provider.HUGGINGFACE,
            "owner/model",
            "main",
            resolved_revision=resolved,
            mirror_url="https://temporary-mirror.example",
        )
        assert direct_route.id != default_route.id
        assert temporary_mirror_route.id not in {default_route.id, direct_route.id}
        assert manager.queue.qsize() == 3

    asyncio.run(exercise())


def test_scheduled_task_does_not_enter_queue_until_its_start_time(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = 0
    resolved = "9" * 40

    async def fake_provider(
        _provider: Provider,
        _source_id: str,
        _revision: str,
        destination: Path,
        _progress: Progress,
        **_options: object,
    ) -> ProviderResult:
        nonlocal calls
        calls += 1
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
                scheduled_at=datetime.now(UTC) + timedelta(milliseconds=200),
                artifact_alias="scheduled-model",
            )
            assert task.status is TaskStatus.SCHEDULED
            assert manager.queue.qsize() == 0
            await asyncio.sleep(0.05)
            assert calls == 0
            assert manager.store.get(task.id).status is TaskStatus.SCHEDULED  # type: ignore[union-attr]

            deadline = asyncio.get_running_loop().time() + 2
            while asyncio.get_running_loop().time() < deadline:
                current = manager.store.get(task.id)
                if current is not None and current.status is TaskStatus.COMPLETED:
                    break
                await asyncio.sleep(0.02)
            assert manager.store.get(task.id).status is TaskStatus.COMPLETED  # type: ignore[union-attr]
            completed = manager.store.get(task.id)
            assert completed is not None and completed.artifact_id is not None
            artifact = catalog.find(completed.artifact_id)
            assert artifact is not None and artifact[0].alias == "scheduled-model"
            assert calls == 1
        finally:
            await manager.stop()

    asyncio.run(exercise())


def test_selected_file_task_publishes_only_the_exact_validated_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    resolved = "7" * 40
    selected = ["config.json", "weights/model.gguf"]

    async def fake_provider(
        _provider: Provider,
        _source_id: str,
        _revision: str,
        destination: Path,
        _progress: Progress,
        **options: object,
    ) -> ProviderResult:
        assert options["selected_paths"] == selected
        (destination / "weights").mkdir()
        (destination / "config.json").write_text("{}", encoding="utf-8")
        (destination / "weights/model.gguf").write_bytes(b"model")
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
                total_bytes=7,
                selected_paths=selected,
            )
            deadline = asyncio.get_running_loop().time() + 2
            while asyncio.get_running_loop().time() < deadline:
                current = manager.store.get(task.id)
                if current is not None and current.status in {
                    TaskStatus.COMPLETED,
                    TaskStatus.FAILED,
                }:
                    break
                await asyncio.sleep(0.02)
            current = manager.store.get(task.id)
            assert current is not None and current.status is TaskStatus.COMPLETED, current
            assert current.artifact_id is not None
            found = catalog.find(current.artifact_id)
            assert found is not None
            summary, manifest = found
            assert summary.selected_paths == selected
            assert manifest.source.selected_paths == selected
            assert {file.path for file in manifest.files} == set(selected)
        finally:
            await manager.stop()

    asyncio.run(exercise())


def test_selected_file_task_rejects_unexpected_provider_output(
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
        (destination / "model.gguf").write_bytes(b"model")
        (destination / "unexpected.txt").write_text("extra", encoding="utf-8")
        return ProviderResult(resolved_revision="8" * 40)

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
                resolved_revision="8" * 40,
                selected_paths=["model.gguf"],
            )
            deadline = asyncio.get_running_loop().time() + 2
            while asyncio.get_running_loop().time() < deadline:
                current = manager.store.get(task.id)
                if current is not None and current.status is TaskStatus.FAILED:
                    break
                await asyncio.sleep(0.02)
            current = manager.store.get(task.id)
            assert current is not None and current.status is TaskStatus.FAILED
            assert current.error is not None and "unexpected unexpected.txt" in current.error
        finally:
            await manager.stop()

    asyncio.run(exercise())


def test_provider_source_hash_is_checked_during_manifest_inventory(
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
        (destination / "model.bin").write_bytes(b"model")
        return ProviderResult(
            resolved_revision="8" * 40,
            expected_sha256={"model.bin": "0" * 64},
        )

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
                resolved_revision="8" * 40,
                total_bytes=len(b"model"),
            )
            await asyncio.wait_for(manager.queue.join(), timeout=2)
            failed = manager.store.get(task.id)
            assert failed is not None and failed.status is TaskStatus.FAILED
            assert "source SHA-256 mismatch: model.bin" in (failed.error or "")
        finally:
            await manager.stop()

    asyncio.run(exercise())


def test_scheduled_task_can_start_immediately(tmp_path: Path) -> None:
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
                resolved_revision="8" * 40,
                scheduled_at=datetime.now(UTC) + timedelta(hours=1),
            )
            queued = await manager.start_now(task.id)
            assert queued.status is TaskStatus.QUEUED
            assert task.id not in manager._scheduled_runs
            assert manager.queue.qsize() == 1
        finally:
            await manager.stop()

    asyncio.run(exercise())


def test_scheduled_task_can_replace_its_start_time(tmp_path: Path) -> None:
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
                resolved_revision="8" * 40,
                scheduled_at=datetime.now(UTC) + timedelta(hours=1),
            )
            previous_waiter = manager._scheduled_runs[task.id]
            replacement = datetime.now(UTC) + timedelta(hours=2)

            updated = await manager.reschedule(task.id, replacement)

            assert updated.status is TaskStatus.SCHEDULED
            assert updated.scheduled_at == replacement
            assert manager._scheduled_runs[task.id] is not previous_waiter
            await asyncio.sleep(0)
            assert previous_waiter.cancelled()
        finally:
            await manager.stop()

    asyncio.run(exercise())


def test_scheduled_task_rejects_a_past_replacement_time(tmp_path: Path) -> None:
    catalog = Catalog(tmp_path / "storage")
    catalog.initialize()
    manager = TaskManager(catalog, github_token=None)

    async def exercise() -> None:
        await manager.start()
        try:
            original = datetime.now(UTC) + timedelta(hours=1)
            task = await manager.create(
                Provider.HUGGINGFACE,
                "owner/model",
                "main",
                resolved_revision="8" * 40,
                scheduled_at=original,
            )
            with pytest.raises(ValueError, match="scheduled start must be in the future"):
                await manager.reschedule(task.id, datetime.now(UTC) - timedelta(seconds=1))
            unchanged = manager.store.get(task.id)
            assert unchanged is not None and unchanged.scheduled_at == original
        finally:
            await manager.stop()

    asyncio.run(exercise())


def test_delayed_resume_preserves_staging_data_across_restart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    resolved = "5" * 40
    resumed_from_stage = False

    async def fake_provider(
        _provider: Provider,
        _source_id: str,
        _revision: str,
        destination: Path,
        _progress: Progress,
        **_options: object,
    ) -> ProviderResult:
        nonlocal resumed_from_stage
        resumed_from_stage = (destination / "resume-marker").is_file()
        (destination / "model.bin").write_bytes(b"model")
        return ProviderResult(resolved_revision=resolved)

    monkeypatch.setattr(task_module, "run_provider", fake_provider)
    catalog = Catalog(tmp_path / "storage")
    catalog.initialize()
    seed = TaskManager(catalog, github_token=None)
    task = seed.store.create(
        Provider.HUGGINGFACE,
        "owner/model",
        "main",
        resolved_revision=resolved,
        total_bytes=None,
        disable_mirror=False,
        disable_proxy=False,
    )
    seed.store.update(task.id, {"status": TaskStatus.PAUSED, "resume_from_stage": True})
    artifact_stage = catalog.staging_path(task.id) / "artifact"
    artifact_stage.mkdir(parents=True)
    (artifact_stage / "resume-marker").write_text("keep", encoding="utf-8")

    async def exercise() -> None:
        await seed.start()
        scheduled = await seed.resume(task.id, scheduled_at=datetime.now(UTC) + timedelta(hours=1))
        assert scheduled.status is TaskStatus.SCHEDULED
        assert scheduled.resume_from_stage is True
        assert seed.queue.qsize() == 0
        await seed.stop()

        seed.store.update(task.id, {"scheduled_at": datetime.now(UTC) - timedelta(seconds=1)})
        manager = TaskManager(catalog, github_token=None)
        await manager.start()
        try:
            deadline = asyncio.get_running_loop().time() + 2
            while asyncio.get_running_loop().time() < deadline:
                current = manager.store.get(task.id)
                if current is not None and current.status is TaskStatus.COMPLETED:
                    break
                await asyncio.sleep(0.02)
            current = manager.store.get(task.id)
            assert current is not None
            assert current.status is TaskStatus.COMPLETED
            assert resumed_from_stage is True
        finally:
            await manager.stop()

    asyncio.run(exercise())


def test_overdue_scheduled_task_is_recovered_after_restart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    resolved = "6" * 40

    async def fake_provider(
        _provider: Provider,
        _source_id: str,
        _revision: str,
        destination: Path,
        _progress: Progress,
        **_options: object,
    ) -> ProviderResult:
        (destination / "model.bin").write_bytes(b"model")
        return ProviderResult(resolved_revision=resolved)

    monkeypatch.setattr(task_module, "run_provider", fake_provider)
    catalog = Catalog(tmp_path / "storage")
    catalog.initialize()
    seed = TaskManager(catalog, github_token=None)
    task = seed.store.create(
        Provider.HUGGINGFACE,
        "owner/model",
        "main",
        resolved_revision=resolved,
        total_bytes=None,
        disable_mirror=False,
        disable_proxy=False,
        scheduled_at=datetime.now(UTC) + timedelta(hours=1),
    )
    seed.store.update(task.id, {"scheduled_at": datetime.now(UTC) - timedelta(minutes=1)})

    manager = TaskManager(catalog, github_token=None)

    async def exercise() -> None:
        await manager.start()
        try:
            deadline = asyncio.get_running_loop().time() + 2
            while asyncio.get_running_loop().time() < deadline:
                current = manager.store.get(task.id)
                if current is not None and current.status is TaskStatus.COMPLETED:
                    break
                await asyncio.sleep(0.02)
            assert manager.store.get(task.id).status is TaskStatus.COMPLETED  # type: ignore[union-attr]
        finally:
            await manager.stop()

    asyncio.run(exercise())


def test_download_uses_the_preflight_resolved_revision(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured_revision = ""
    captured_options: dict[str, object] = {}
    resolved = "d" * 40

    async def fake_provider(
        _provider: Provider,
        _source_id: str,
        revision: str,
        destination: Path,
        _progress: Progress,
        **options: object,
    ) -> ProviderResult:
        nonlocal captured_revision
        captured_revision = revision
        captured_options.update(options)
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
                mirror_url="https://temporary-mirror.example",
            )
            await asyncio.wait_for(manager.queue.join(), timeout=2)
            assert manager.store.get(task.id).status.value == "completed"  # type: ignore[union-attr]
        finally:
            await manager.stop()

    asyncio.run(exercise())
    assert captured_revision == resolved
    assert captured_options["mirror_url"] == "https://temporary-mirror.example"


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


def test_modelscope_download_can_reuse_artifacts_with_a_different_file_selection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    catalog = Catalog(tmp_path / "storage")
    catalog.initialize()
    old_stage = catalog.staging_path("old-partial")
    old_stage.mkdir(parents=True)
    (old_stage / "model.bin").write_bytes(b"old")
    old_manifest = catalog.create_manifest(
        old_stage,
        name="model",
        version="old",
        source=SourceReference(
            provider=Provider.MODELSCOPE_CN,
            id="owner/model",
            requested_revision="master",
            resolved_revision="a" * 40,
            selected_paths=["model.bin"],
        ),
    )
    old_root, _ = catalog.publish(old_stage, old_manifest)
    captured_roots: list[Path] = []

    async def fake_provider(
        _provider: Provider,
        _source_id: str,
        _revision: str,
        destination: Path,
        _progress: Progress,
        **options: object,
    ) -> ProviderResult:
        captured_roots.extend(options["reusable_artifact_roots"])  # type: ignore[arg-type]
        (destination / "model.bin").write_bytes(b"old")
        (destination / "new.bin").write_bytes(b"new")
        return ProviderResult(resolved_revision="b" * 40)

    monkeypatch.setattr(task_module, "run_provider", fake_provider)
    manager = TaskManager(catalog, github_token=None)

    async def exercise() -> None:
        await manager.start()
        try:
            task = await manager.create(
                Provider.MODELSCOPE_CN,
                "owner/model",
                "master",
                resolved_revision="b" * 40,
                selected_paths=["model.bin", "new.bin"],
            )
            await asyncio.wait_for(manager.queue.join(), timeout=2)
            current = manager.store.get(task.id)
            assert current is not None and current.status is TaskStatus.COMPLETED
        finally:
            await manager.stop()

    asyncio.run(exercise())
    assert captured_roots == [old_root]


def test_modelscope_incremental_task_hashes_only_untrusted_content(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    reused_payload = b"already verified weights"
    changed_payload = b"new metadata"
    reused_digest = hashlib.sha256(reused_payload).hexdigest()
    changed_digest = hashlib.sha256(changed_payload).hexdigest()
    observed_trusted_sha256: list[dict[str, str]] = []
    original_inventory = task_module.inventory

    def tracked_inventory(*args: object, **kwargs: object) -> list[FileEntry]:
        trusted = kwargs.get("trusted_sha256")
        observed_trusted_sha256.append(dict(trusted) if isinstance(trusted, dict) else {})
        return original_inventory(*args, **kwargs)  # type: ignore[arg-type]

    async def fake_provider(
        _provider: Provider,
        _source_id: str,
        _revision: str,
        destination: Path,
        progress: Progress,
        **_options: object,
    ) -> ProviderResult:
        (destination / "model.bin").write_bytes(reused_payload)
        (destination / "config.json").write_bytes(changed_payload)
        await progress(len(changed_payload), len(changed_payload))
        return ProviderResult(
            resolved_revision="c" * 40,
            expected_sha256={
                "model.bin": reused_digest,
                "config.json": changed_digest,
            },
            fetched_paths=["config.json"],
        )

    monkeypatch.setattr(task_module, "inventory", tracked_inventory)
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
                resolved_revision="c" * 40,
                total_bytes=len(reused_payload) + len(changed_payload),
            )
            await asyncio.wait_for(manager.queue.join(), timeout=2)
            completed = manager.store.get(task.id)
            assert completed is not None and completed.status is TaskStatus.COMPLETED
            assert completed.verification_total_bytes == len(changed_payload)
            found = catalog.find(completed.artifact_id or "")
            assert found is not None
            _summary, manifest = found
            assert manifest.total_size == len(reused_payload) + len(changed_payload)
            assert {entry.path: entry.sha256 for entry in manifest.files} == {
                "config.json": changed_digest,
                "model.bin": reused_digest,
            }
        finally:
            await manager.stop()

    asyncio.run(exercise())
    assert observed_trusted_sha256 == [{"model.bin": reused_digest}]


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


def test_verification_progress_has_separate_rate_and_eta(tmp_path: Path) -> None:
    moments = iter((100.0, 110.0))
    catalog = Catalog(tmp_path / "storage")
    catalog.initialize()
    manager = TaskManager(catalog, github_token=None, clock=lambda: next(moments))

    async def exercise() -> tuple[int, int, int | None, float, float, int | None]:
        created = await manager.create(
            Provider.HUGGINGFACE,
            "owner/model",
            "main",
            total_bytes=1_000,
        )
        verifying = manager.store.update(
            created.id,
            {
                "status": TaskStatus.VERIFYING,
                "progress": 92,
                "verification_total_bytes": 1_000,
            },
        )
        manager._start_verification_metrics(verifying)
        await manager._verification_progress(created.id, 500, 1_000)
        updated = manager.store.get(created.id)
        assert updated is not None
        return (
            updated.progress,
            updated.verification_bytes_completed,
            updated.verification_total_bytes,
            updated.verification_instantaneous_bytes_per_second,
            updated.verification_average_bytes_per_second,
            updated.verification_eta_seconds,
        )

    assert asyncio.run(exercise()) == (95, 500, 1_000, 50, 50, 10)


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


def test_verification_can_pause_and_resume_without_downloading_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    verification_started = threading.Event()
    provider_attempts = 0
    inventory_attempts = 0
    original_inventory = task_module.inventory

    async def fake_provider(
        _provider: Provider,
        _source_id: str,
        _revision: str,
        destination: Path,
        _progress: Progress,
        **_options: object,
    ) -> ProviderResult:
        nonlocal provider_attempts
        provider_attempts += 1
        (destination / "model.bin").write_bytes(b"complete")
        return ProviderResult(resolved_revision="a" * 40)

    def pausable_inventory(*args: object, **kwargs: object) -> list[object]:
        nonlocal inventory_attempts
        inventory_attempts += 1
        if inventory_attempts == 1:
            verification_started.set()
            cancelled = kwargs["cancelled"]
            assert isinstance(cancelled, threading.Event)
            cancelled.wait(timeout=2)
            raise RuntimeError("verification cancelled")
        return original_inventory(*args, **kwargs)

    monkeypatch.setattr(task_module, "run_provider", fake_provider)
    monkeypatch.setattr(task_module, "inventory", pausable_inventory)
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
                resolved_revision="a" * 40,
                total_bytes=len(b"complete"),
            )
            assert await asyncio.to_thread(verification_started.wait, 2)
            current = manager.store.get(task.id)
            assert current is not None and current.status is TaskStatus.VERIFYING

            paused = await manager.pause(task.id)
            assert paused.status is TaskStatus.PAUSED
            resumed = await manager.resume(task.id)
            assert resumed.status is TaskStatus.QUEUED
            await asyncio.wait_for(manager.queue.join(), timeout=2)
            completed = manager.store.get(task.id)
            assert completed is not None and completed.status is TaskStatus.COMPLETED
        finally:
            await manager.stop()

    asyncio.run(exercise())
    assert provider_attempts == 1
    assert inventory_attempts == 2


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
