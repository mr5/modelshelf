from __future__ import annotations

import asyncio
import contextlib
import math
import shutil
import time
from collections import deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

from modelshelf_core import (
    Catalog,
    DownloadTask,
    Provider,
    SourceReference,
    TaskStatus,
)
from modelshelf_core.catalog import atomic_write_json, content_digest, inventory
from modelshelf_core.identity import artifact_identity
from modelshelf_core.schema import load_task_json

from .archive import extract_archive, infer_metadata
from .providers import provider_failure_detail, run_provider


@dataclass(frozen=True)
class DuplicateIngestion:
    kind: Literal["artifact", "task"]
    task: DownloadTask | None = None
    artifact_id: str | None = None
    artifact_total_size: int | None = None

    def as_dict(self) -> dict[str, object]:
        result: dict[str, object] = {"kind": self.kind}
        if self.task is not None:
            result["taskId"] = self.task.id
            result["taskStatus"] = self.task.status.value
        if self.artifact_id is not None:
            result["artifactId"] = self.artifact_id
        return result


@dataclass(frozen=True)
class TaskCreationResult:
    task: DownloadTask
    deduplication_reason: Literal["artifact", "task"] | None = None


class TaskStore:
    def __init__(self, jobs_root: Path) -> None:
        self.jobs_root = jobs_root

    def _path(self, task_id: str) -> Path:
        return self.jobs_root / f"{task_id}.json"

    def create(
        self,
        provider: Provider,
        source_id: str,
        requested_revision: str,
        *,
        resolved_revision: str | None,
        total_bytes: int | None,
        disable_mirror: bool,
        disable_proxy: bool,
    ) -> DownloadTask:
        now = datetime.now(UTC)
        task = DownloadTask(
            schema_version=1,
            id=str(uuid4()),
            provider=provider,
            source_id=source_id,
            requested_revision=requested_revision,
            resolved_revision=resolved_revision,
            disable_mirror=disable_mirror,
            disable_proxy=disable_proxy,
            status=TaskStatus.QUEUED,
            progress=0,
            total_bytes=total_bytes,
            created_at=now,
            updated_at=now,
        )
        self.save(task)
        return task

    def create_completed(
        self,
        provider: Provider,
        source_id: str,
        requested_revision: str,
        resolved_revision: str,
        artifact_id: str,
        total_bytes: int,
        *,
        disable_mirror: bool,
        disable_proxy: bool,
    ) -> DownloadTask:
        now = datetime.now(UTC)
        task = DownloadTask(
            schema_version=1,
            id=str(uuid4()),
            provider=provider,
            source_id=source_id,
            requested_revision=requested_revision,
            resolved_revision=resolved_revision,
            disable_mirror=disable_mirror,
            disable_proxy=disable_proxy,
            status=TaskStatus.COMPLETED,
            progress=100,
            bytes_downloaded=total_bytes,
            total_bytes=total_bytes,
            eta_seconds=0,
            artifact_id=artifact_id,
            created_at=now,
            updated_at=now,
        )
        self.save(task)
        return task

    def save(self, task: DownloadTask) -> None:
        atomic_write_json(
            self._path(task.id), task.model_dump(mode="json", by_alias=True, exclude_none=True)
        )

    def get(self, task_id: str) -> DownloadTask | None:
        try:
            task, migrated = load_task_json(self._path(task_id).read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        if migrated:
            self.save(task)
        return task

    def list(self) -> list[DownloadTask]:
        tasks: list[DownloadTask] = []
        for path in self.jobs_root.glob("*.json"):
            try:
                task = self.get(path.stem)
                if task is not None:
                    tasks.append(task)
            except OSError:
                continue
        return sorted(tasks, key=lambda item: item.created_at, reverse=True)

    def update(self, task_id: str, values: Mapping[str, Any]) -> DownloadTask:
        task = self.get(task_id)
        if task is None:
            raise KeyError(f"unknown task {task_id}")
        data = task.model_dump()
        data.update(values)
        data["id"] = task_id
        data["updated_at"] = datetime.now(UTC)
        updated = DownloadTask.model_validate(data)
        self.save(updated)
        return updated

    def delete(self, task_id: str) -> bool:
        path = self._path(task_id)
        try:
            path.unlink()
        except FileNotFoundError:
            return False
        return True


class TaskManager:
    def __init__(
        self,
        catalog: Catalog,
        *,
        github_token: str | None,
        huggingface_mirror: str | None = None,
        modelscope_cn_mirror: str | None = None,
        modelscope_ai_mirror: str | None = None,
        proxy_url: str | None = None,
        max_concurrent_downloads: int = 1,
        max_concurrent_downloads_per_source: int = 1,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if max_concurrent_downloads < 1:
            raise ValueError("max_concurrent_downloads must be at least 1")
        if max_concurrent_downloads_per_source < 1:
            raise ValueError("max_concurrent_downloads_per_source must be at least 1")
        self.catalog = catalog
        self.store = TaskStore(catalog.jobs_root)
        self.github_token = github_token
        self.huggingface_mirror = huggingface_mirror
        self.modelscope_cn_mirror = modelscope_cn_mirror
        self.modelscope_ai_mirror = modelscope_ai_mirror
        self.proxy_url = proxy_url
        self.max_concurrent_downloads = max_concurrent_downloads
        self.max_concurrent_downloads_per_source = max_concurrent_downloads_per_source
        self.clock = clock
        self.queue: asyncio.Queue[str] = asyncio.Queue()
        self.scheduler: asyncio.Task[None] | None = None
        self._pending: deque[str] = deque()
        self._active_runs: dict[str, asyncio.Task[None]] = {}
        self._active_by_source: dict[Provider, int] = {}
        self._scheduler_wakeup = asyncio.Event()
        self._resume_from_stage: set[str] = set()
        self._create_lock = asyncio.Lock()
        self._update_lock = asyncio.Lock()
        self._artifact_lock = asyncio.Lock()
        self._progress_written: dict[str, float] = {}
        self._download_timing: dict[str, tuple[float, float]] = {}
        self._download_baselines: dict[str, tuple[int, float]] = {}
        self._progress_samples: dict[str, tuple[float, int]] = {}
        self._speed_ema: dict[str, float] = {}

    async def start(self) -> None:
        recoverable = {
            TaskStatus.QUEUED,
            TaskStatus.RESOLVING,
            TaskStatus.DOWNLOADING,
            TaskStatus.VERIFYING,
            TaskStatus.PUBLISHING,
        }
        stored_tasks = self.store.list()
        for task in stored_tasks:
            if task.status is TaskStatus.CANCELLED:
                shutil.rmtree(self.catalog.staging_path(task.id), ignore_errors=True)
        tasks = [task for task in stored_tasks if task.status in recoverable]
        tasks.sort(key=lambda task: (task.status is TaskStatus.QUEUED, task.created_at))
        for task in tasks:
            if task.status is not TaskStatus.QUEUED:
                self.store.update(
                    task.id,
                    {"status": TaskStatus.QUEUED, "progress": 0},
                )
                self._resume_from_stage.add(task.id)
            self.queue.put_nowait(task.id)
        self.scheduler = asyncio.create_task(
            self._schedule(), name="modelshelf-ingestion-scheduler"
        )
        self._scheduler_wakeup.set()

    async def stop(self) -> None:
        if self.scheduler:
            self.scheduler.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.scheduler
        active = list(self._active_runs.values())
        for task in active:
            task.cancel()
        if active:
            await asyncio.gather(*active, return_exceptions=True)

    async def pause(self, task_id: str) -> DownloadTask:
        paused = await self._transition(
            task_id,
            {TaskStatus.QUEUED, TaskStatus.RESOLVING, TaskStatus.DOWNLOADING},
            "paused",
            status=TaskStatus.PAUSED,
        )
        timing = self._download_timing.get(task_id)
        baseline = self._download_baselines.get(task_id)
        active = self._active_runs.get(task_id)
        if active is not None:
            active.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await active
        self._scheduler_wakeup.set()
        return await self._stop_metrics(paused.id, timing=timing, baseline=baseline)

    async def resume(self, task_id: str) -> DownloadTask:
        self._resume_from_stage.add(task_id)
        try:
            resumed = await self._transition(
                task_id,
                {TaskStatus.PAUSED},
                "resumed",
                status=TaskStatus.QUEUED,
                error=None,
            )
        except Exception:
            self._resume_from_stage.discard(task_id)
            raise
        await self.queue.put(task_id)
        self._scheduler_wakeup.set()
        return resumed

    async def cancel(self, task_id: str) -> DownloadTask:
        cancelled = await self._transition(
            task_id,
            {
                TaskStatus.QUEUED,
                TaskStatus.RESOLVING,
                TaskStatus.DOWNLOADING,
                TaskStatus.PAUSED,
                TaskStatus.AWAITING_CONFIRMATION,
            },
            "cancelled",
            status=TaskStatus.CANCELLED,
        )
        timing = self._download_timing.get(task_id)
        baseline = self._download_baselines.get(task_id)
        active = self._active_runs.get(task_id)
        if active is not None:
            active.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await active
        self._scheduler_wakeup.set()
        self._resume_from_stage.discard(task_id)
        shutil.rmtree(self.catalog.staging_path(task_id), ignore_errors=True)
        return await self._stop_metrics(
            cancelled.id, timing=timing, baseline=baseline, clear_eta=True
        )

    async def delete_task(self, task_id: str, *, delete_artifact: bool = False) -> None:
        async with self._update_lock:
            task = self.store.get(task_id)
            if task is None:
                raise KeyError(f"unknown task {task_id}")
            allowed = {TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED}
            if task.status not in allowed:
                raise ValueError("only completed, failed or cancelled tasks can be deleted")
            if delete_artifact and task.status is not TaskStatus.COMPLETED:
                raise ValueError("only completed tasks can delete an associated artifact")
        if delete_artifact and task.artifact_id is not None:
            async with self._artifact_lock:
                self.catalog.delete(task.artifact_id)
        async with self._update_lock:
            shutil.rmtree(self.catalog.staging_path(task_id), ignore_errors=True)
            self.store.delete(task_id)

    async def delete_artifact(self, artifact_id: str) -> bool:
        async with self._artifact_lock:
            return self.catalog.delete(artifact_id)

    def find_duplicate(
        self,
        provider: Provider,
        source_id: str,
        requested_revision: str,
        *,
        resolved_revision: str | None,
        disable_mirror: bool = False,
        disable_proxy: bool = False,
    ) -> DuplicateIngestion | None:
        if not resolved_revision:
            return None

        tasks = self.store.list()
        artifact_id = artifact_identity(provider, source_id, resolved_revision)
        existing_artifact = self.catalog.find(artifact_id)
        if existing_artifact is not None:
            completed = next(
                (
                    task
                    for task in tasks
                    if task.status is TaskStatus.COMPLETED and task.artifact_id == artifact_id
                ),
                None,
            )
            summary, _manifest = existing_artifact
            return DuplicateIngestion(
                kind="artifact",
                task=completed,
                artifact_id=artifact_id,
                artifact_total_size=summary.total_size,
            )

        reusable_statuses = {
            TaskStatus.QUEUED,
            TaskStatus.RESOLVING,
            TaskStatus.DOWNLOADING,
            TaskStatus.VERIFYING,
            TaskStatus.AWAITING_CONFIRMATION,
            TaskStatus.PUBLISHING,
            TaskStatus.PAUSED,
        }
        reusable = next(
            (
                task
                for task in tasks
                if task.status in reusable_statuses
                and task.provider is provider
                and task.source_id == source_id
                and task.resolved_revision == resolved_revision
                and task.disable_mirror == disable_mirror
                and task.disable_proxy == disable_proxy
            ),
            None,
        )
        if reusable is None:
            return None
        return DuplicateIngestion(kind="task", task=reusable)

    async def create_with_result(
        self,
        provider: Provider,
        source_id: str,
        requested_revision: str,
        *,
        resolved_revision: str | None = None,
        total_bytes: int | None = None,
        disable_mirror: bool = False,
        disable_proxy: bool = False,
    ) -> TaskCreationResult:
        async with self._create_lock:
            duplicate = self.find_duplicate(
                provider,
                source_id,
                requested_revision,
                resolved_revision=resolved_revision,
                disable_mirror=disable_mirror,
                disable_proxy=disable_proxy,
            )
            if duplicate is not None:
                if duplicate.task is not None:
                    return TaskCreationResult(duplicate.task, duplicate.kind)
                if (
                    duplicate.kind == "artifact"
                    and duplicate.artifact_id is not None
                    and duplicate.artifact_total_size is not None
                    and resolved_revision is not None
                ):
                    completed = self.store.create_completed(
                        provider,
                        source_id,
                        requested_revision,
                        resolved_revision,
                        duplicate.artifact_id,
                        duplicate.artifact_total_size,
                        disable_mirror=disable_mirror,
                        disable_proxy=disable_proxy,
                    )
                    return TaskCreationResult(completed, "artifact")

            task = self.store.create(
                provider,
                source_id,
                requested_revision,
                resolved_revision=resolved_revision,
                total_bytes=total_bytes,
                disable_mirror=disable_mirror,
                disable_proxy=disable_proxy,
            )
            await self.queue.put(task.id)
            self._scheduler_wakeup.set()
            return TaskCreationResult(task)

    async def create(
        self,
        provider: Provider,
        source_id: str,
        requested_revision: str,
        *,
        resolved_revision: str | None = None,
        total_bytes: int | None = None,
        disable_mirror: bool = False,
        disable_proxy: bool = False,
    ) -> DownloadTask:
        return (
            await self.create_with_result(
                provider,
                source_id,
                requested_revision,
                resolved_revision=resolved_revision,
                total_bytes=total_bytes,
                disable_mirror=disable_mirror,
                disable_proxy=disable_proxy,
            )
        ).task

    async def _update(self, task_id: str, **values: Any) -> DownloadTask:
        async with self._update_lock:
            return self.store.update(task_id, values)

    async def _transition(
        self,
        task_id: str,
        allowed: set[TaskStatus],
        action: str,
        **values: Any,
    ) -> DownloadTask:
        async with self._update_lock:
            task = self.store.get(task_id)
            if task is None:
                raise KeyError(f"unknown task {task_id}")
            if task.status not in allowed:
                raise ValueError(f"task cannot be {action} while {task.status.value}")
            return self.store.update(task_id, values)

    async def _progress(self, task_id: str, downloaded: int, total: int | None) -> None:
        now = self.clock()
        previous = self._progress_written.get(task_id, 0)
        if now - previous < 0.5 and (total is None or downloaded < total):
            return
        self._progress_written[task_id] = now
        progress = min(89, 2 + int(downloaded / total * 87)) if total else 20
        last_sample = self._progress_samples.get(task_id)
        raw_speed = 0.0
        if last_sample is not None and now > last_sample[0]:
            raw_speed = max(0, downloaded - last_sample[1]) / (now - last_sample[0])
        previous_speed = self._speed_ema.get(task_id)
        speed = raw_speed if previous_speed is None else previous_speed * 0.7 + raw_speed * 0.3
        self._speed_ema[task_id] = speed
        self._progress_samples[task_id] = (now, downloaded)
        timing = self._download_timing.get(task_id)
        elapsed = timing[1] + (now - timing[0]) if timing is not None else 0.0
        baseline = self._download_baselines.get(task_id)
        if timing is not None and baseline is not None:
            previous_bytes = baseline[1] * timing[1]
            measured_bytes = previous_bytes + max(0, downloaded - baseline[0])
        else:
            measured_bytes = downloaded
        average_speed = measured_bytes / elapsed if elapsed > 0 else 0.0
        eta_speed = speed if speed > 0 else average_speed
        eta = (
            math.ceil(max(0, total - downloaded) / eta_speed)
            if total is not None and eta_speed > 0
            else None
        )
        await self._update(
            task_id,
            bytes_downloaded=downloaded,
            total_bytes=total,
            progress=progress,
            instantaneous_bytes_per_second=speed,
            average_bytes_per_second=average_speed,
            eta_seconds=eta,
            download_elapsed_seconds=elapsed,
        )

    def _start_metrics(self, task: DownloadTask) -> None:
        now = self.clock()
        self._progress_written.pop(task.id, None)
        self._download_timing[task.id] = (now, task.download_elapsed_seconds)
        self._download_baselines[task.id] = (
            task.bytes_downloaded,
            task.average_bytes_per_second,
        )
        self._progress_samples[task.id] = (now, task.bytes_downloaded)
        self._speed_ema.pop(task.id, None)

    async def _stop_metrics(
        self,
        task_id: str,
        *,
        timing: tuple[float, float] | None = None,
        baseline: tuple[int, float] | None = None,
        clear_eta: bool = False,
    ) -> DownloadTask:
        timing = timing or self._download_timing.pop(task_id, None)
        baseline = baseline or self._download_baselines.pop(task_id, None)
        self._download_baselines.pop(task_id, None)
        self._progress_samples.pop(task_id, None)
        self._speed_ema.pop(task_id, None)
        task = self.store.get(task_id)
        if task is None:
            raise KeyError(f"unknown task {task_id}")
        elapsed = task.download_elapsed_seconds
        if timing is not None:
            elapsed = max(elapsed, timing[1] + (self.clock() - timing[0]))
        if timing is not None and baseline is not None:
            previous_bytes = baseline[1] * timing[1]
            measured_bytes = previous_bytes + max(0, task.bytes_downloaded - baseline[0])
            average = measured_bytes / elapsed if elapsed > 0 else 0.0
        else:
            average = task.average_bytes_per_second
        return await self._update(
            task_id,
            instantaneous_bytes_per_second=0,
            average_bytes_per_second=average,
            eta_seconds=None if clear_eta else task.eta_seconds,
            download_elapsed_seconds=elapsed,
        )

    def _raise_if_stopped(self, task_id: str) -> None:
        task = self.store.get(task_id)
        if task is not None and task.status in {TaskStatus.PAUSED, TaskStatus.CANCELLED}:
            raise asyncio.CancelledError

    def _next_schedulable(self) -> tuple[str, Provider] | None:
        index = 0
        while index < len(self._pending):
            task_id = self._pending[index]
            task = self.store.get(task_id)
            if task is None or task.status is not TaskStatus.QUEUED:
                del self._pending[index]
                self.queue.task_done()
                continue
            if (
                self._active_by_source.get(task.provider, 0)
                < self.max_concurrent_downloads_per_source
            ):
                del self._pending[index]
                return task_id, task.provider
            index += 1
        return None

    async def _execute_queued(self, task_id: str, provider: Provider) -> None:
        try:
            await self._run(task_id)
        except asyncio.CancelledError:
            pass
        finally:
            self._active_runs.pop(task_id, None)
            remaining = self._active_by_source.get(provider, 1) - 1
            if remaining > 0:
                self._active_by_source[provider] = remaining
            else:
                self._active_by_source.pop(provider, None)
            self.queue.task_done()
            self._scheduler_wakeup.set()

    async def _schedule(self) -> None:
        while True:
            await self._scheduler_wakeup.wait()
            self._scheduler_wakeup.clear()
            while True:
                try:
                    self._pending.append(self.queue.get_nowait())
                except asyncio.QueueEmpty:
                    break
            while len(self._active_runs) < self.max_concurrent_downloads:
                candidate = self._next_schedulable()
                if candidate is None:
                    break
                task_id, provider = candidate
                self._active_by_source[provider] = self._active_by_source.get(provider, 0) + 1
                active = asyncio.create_task(
                    self._execute_queued(task_id, provider),
                    name=f"modelshelf-task-{task_id}",
                )
                self._active_runs[task_id] = active

    async def _run(self, task_id: str) -> None:
        task = self.store.get(task_id)
        if task is None:
            return
        stage = self.catalog.staging_path(task_id)
        resume_from_stage = task_id in self._resume_from_stage
        self._resume_from_stage.discard(task_id)
        try:
            if not resume_from_stage:
                shutil.rmtree(stage, ignore_errors=True)
            stage.mkdir(parents=True, exist_ok=True)
            await self._update(task_id, status=TaskStatus.RESOLVING, progress=1, error=None)
            download_root = (
                stage / "download" if task.provider is Provider.HTTP else stage / "artifact"
            )
            download_root.mkdir(parents=True, exist_ok=True)
            await self._update(task_id, status=TaskStatus.DOWNLOADING, progress=2)
            self._start_metrics(task)
            download_revision = task.requested_revision
            if task.resolved_revision and task.provider is Provider.HUGGINGFACE:
                download_revision = task.resolved_revision
            elif (
                task.resolved_revision
                and task.provider is Provider.KAGGLE
                and task.resolved_revision.startswith("version:")
            ):
                download_revision = task.resolved_revision.removeprefix("version:")
            result = await run_provider(
                task.provider,
                task.source_id,
                download_revision,
                download_root,
                lambda downloaded, total: self._progress(
                    task_id, downloaded, total if total is not None else task.total_bytes
                ),
                github_token=self.github_token,
                huggingface_mirror=self.huggingface_mirror,
                modelscope_cn_mirror=self.modelscope_cn_mirror,
                modelscope_ai_mirror=self.modelscope_ai_mirror,
                proxy_url=self.proxy_url,
                disable_mirror=task.disable_mirror,
                disable_proxy=task.disable_proxy,
                expected_resolved_revision=task.resolved_revision,
            )
            self._raise_if_stopped(task_id)
            if (
                task.resolved_revision
                and result.resolved_revision
                and result.resolved_revision != task.resolved_revision
            ):
                raise RuntimeError(
                    "provider returned a different resolved revision than the validated preflight"
                )
            await self._update(task_id, status=TaskStatus.VERIFYING, progress=92)
            files = inventory(download_root)
            if not files:
                raise RuntimeError("provider returned no files")
            downloaded_size = sum(file.size for file in files)
            if task.total_bytes is not None and downloaded_size != task.total_bytes:
                raise RuntimeError(
                    "downloaded content size does not match the validated preflight: "
                    f"expected {task.total_bytes} bytes, got {downloaded_size}"
                )
            digest = content_digest(files)
            resolved_revision = result.resolved_revision or f"sha256:{digest}"
            if task.provider is Provider.GITHUB_RELEASE:
                resolved_revision = f"{resolved_revision}:sha256:{digest}"
            if task.provider is Provider.HTTP:
                filename = result.downloaded_file or files[0].path
                inferred = infer_metadata(
                    download_root / filename, result.source_url or task.source_id
                )
                await self._update(
                    task_id,
                    status=TaskStatus.AWAITING_CONFIRMATION,
                    progress=95,
                    resolved_revision=resolved_revision,
                    inferred_metadata=inferred,
                    instantaneous_bytes_per_second=0,
                    eta_seconds=0,
                )
                return
            source_name = task.source_id.rstrip("/").split("/")[-1]
            manifest = self.catalog.create_manifest(
                download_root,
                name=source_name,
                version=resolved_revision,
                files=files,
                source=SourceReference(
                    provider=task.provider,
                    id=task.source_id,
                    requested_revision=task.requested_revision,
                    resolved_revision=resolved_revision,
                    url=result.source_url,
                ),
            )
            await self._update(
                task_id,
                status=TaskStatus.PUBLISHING,
                progress=99,
                resolved_revision=resolved_revision,
            )
            self._raise_if_stopped(task_id)
            async with self._artifact_lock:
                self.catalog.publish(download_root, manifest)
            shutil.rmtree(stage, ignore_errors=True)
            await self._update(
                task_id,
                status=TaskStatus.COMPLETED,
                progress=100,
                artifact_id=manifest.artifact_id,
                instantaneous_bytes_per_second=0,
                eta_seconds=0,
            )
        except asyncio.CancelledError:
            raise
        except Exception as error:  # worker failures must be durable and visible
            shutil.rmtree(stage, ignore_errors=True)
            await self._update(
                task_id,
                status=TaskStatus.FAILED,
                error=provider_failure_detail(task.provider, "download task", error),
                instantaneous_bytes_per_second=0,
                eta_seconds=None,
            )
        finally:
            self._download_timing.pop(task_id, None)
            self._download_baselines.pop(task_id, None)
            self._progress_samples.pop(task_id, None)
            self._speed_ema.pop(task_id, None)

    async def confirm_http(
        self,
        task_id: str,
        *,
        name: str,
        version: str,
        format: str | None,
        extract: bool,
    ) -> DownloadTask:
        task = self.store.get(task_id)
        if task is None or task.provider is not Provider.HTTP:
            raise KeyError("unknown HTTP task")
        if task.status is not TaskStatus.AWAITING_CONFIRMATION or not task.resolved_revision:
            raise ValueError("task is not awaiting confirmation")
        task = await self._transition(
            task_id,
            {TaskStatus.AWAITING_CONFIRMATION},
            "confirmed",
            status=TaskStatus.VERIFYING,
            progress=96,
        )
        assert task.resolved_revision is not None
        stage = self.catalog.staging_path(task_id)
        download_root = stage / "download"
        files = inventory(download_root)
        if not files:
            raise RuntimeError("staged HTTP content is missing")
        publish_root = download_root
        if extract:
            if len(files) != 1:
                raise ValueError("automatic extraction requires exactly one downloaded archive")
            publish_root = stage / "publish"
            shutil.rmtree(publish_root, ignore_errors=True)
            extract_archive(download_root / files[0].path, publish_root)
            files = inventory(publish_root)
        manifest = self.catalog.create_manifest(
            publish_root,
            name=name,
            version=version,
            format=format,
            files=files,
            source=SourceReference(
                provider=Provider.HTTP,
                id=task.source_id,
                requested_revision=task.requested_revision,
                resolved_revision=task.resolved_revision,
                url=task.source_id,
            ),
        )
        await self._update(task_id, status=TaskStatus.PUBLISHING, progress=99)
        async with self._artifact_lock:
            self.catalog.publish(publish_root, manifest)
        shutil.rmtree(stage, ignore_errors=True)
        return await self._update(
            task_id,
            status=TaskStatus.COMPLETED,
            progress=100,
            artifact_id=manifest.artifact_id,
        )
