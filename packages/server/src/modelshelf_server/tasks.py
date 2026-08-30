from __future__ import annotations

import asyncio
import contextlib
import json
import math
import shutil
import threading
import time
from collections import deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

from modelshelf_core import (
    TASK_SCHEMA_VERSION,
    ArtifactSummary,
    Catalog,
    DownloadTask,
    FileEntry,
    Provider,
    SourceReference,
    TaskStatus,
)
from modelshelf_core.catalog import atomic_write_json, content_digest, inventory
from modelshelf_core.identity import artifact_identity
from modelshelf_core.schema import load_task_json

from .archive import extract_archive, infer_metadata
from .providers import ProviderResult, provider_failure_detail, run_provider

_PROVIDER_RESULT_SCHEMA_VERSION = 1
_VERIFICATION_WORKERS = 2


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
        mirror_url: str | None = None,
        scheduled_at: datetime | None = None,
        queue_position: int | None = None,
        selected_paths: list[str] | None = None,
        artifact_alias: str | None = None,
    ) -> DownloadTask:
        now = datetime.now(UTC)
        if scheduled_at is not None:
            if scheduled_at.tzinfo is None or scheduled_at.utcoffset() is None:
                raise ValueError("scheduled start must include a timezone")
            scheduled_at = scheduled_at.astimezone(UTC)
        initial_status = (
            TaskStatus.SCHEDULED
            if scheduled_at is not None and scheduled_at > now
            else TaskStatus.QUEUED
        )
        task = DownloadTask(
            schema_version=TASK_SCHEMA_VERSION,
            id=str(uuid4()),
            provider=provider,
            source_id=source_id,
            requested_revision=requested_revision,
            resolved_revision=resolved_revision,
            disable_mirror=disable_mirror,
            mirror_url=mirror_url,
            disable_proxy=disable_proxy,
            scheduled_at=scheduled_at,
            queue_position=queue_position if initial_status is TaskStatus.QUEUED else None,
            selected_paths=selected_paths,
            artifact_alias=artifact_alias,
            status=initial_status,
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
        mirror_url: str | None = None,
        selected_paths: list[str] | None = None,
        artifact_alias: str | None = None,
    ) -> DownloadTask:
        now = datetime.now(UTC)
        task = DownloadTask(
            schema_version=TASK_SCHEMA_VERSION,
            id=str(uuid4()),
            provider=provider,
            source_id=source_id,
            requested_revision=requested_revision,
            resolved_revision=resolved_revision,
            disable_mirror=disable_mirror,
            mirror_url=mirror_url,
            disable_proxy=disable_proxy,
            selected_paths=selected_paths,
            artifact_alias=artifact_alias,
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
        self._scheduled_runs: dict[str, asyncio.Task[None]] = {}
        self._active_by_source: dict[Provider, int] = {}
        self._scheduler_wakeup = asyncio.Event()
        self._resume_from_stage: set[str] = set()
        self._create_lock = asyncio.Lock()
        self._update_lock = asyncio.Lock()
        self._artifact_lock = asyncio.Lock()
        self._verification_slot = asyncio.Lock()
        self._progress_written: dict[str, float] = {}
        self._download_timing: dict[str, tuple[float, float]] = {}
        self._download_baselines: dict[str, tuple[int, float]] = {}
        self._progress_samples: dict[str, tuple[float, int]] = {}
        self._speed_ema: dict[str, float] = {}
        self._verification_written: dict[str, float] = {}
        self._verification_timing: dict[str, tuple[float, float]] = {}
        self._verification_baselines: dict[str, tuple[int, float]] = {}
        self._verification_samples: dict[str, tuple[float, int]] = {}
        self._verification_speed_ema: dict[str, float] = {}
        positions = [
            task.queue_position for task in self.store.list() if task.queue_position is not None
        ]
        self._next_queue_position = max(positions, default=-1) + 1

    def _claim_queue_position(self) -> int:
        position = self._next_queue_position
        self._next_queue_position += 1
        return position

    @staticmethod
    def _queue_order(task: DownloadTask) -> tuple[int, datetime]:
        position = task.queue_position if task.queue_position is not None else 2**63 - 1
        return position, task.created_at

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
        scheduled_tasks = [task for task in stored_tasks if task.status is TaskStatus.SCHEDULED]
        for task in scheduled_tasks:
            self._arm_scheduled_task(task)
        interrupted = sorted(
            (task for task in stored_tasks if task.status in recoverable - {TaskStatus.QUEUED}),
            key=lambda task: task.created_at,
        )
        pending = sorted(
            (
                task
                for task in stored_tasks
                if task.status in {TaskStatus.QUEUED, TaskStatus.PAUSED}
            ),
            key=self._queue_order,
        )
        tasks = [*interrupted, *pending]
        for position, task in enumerate(tasks):
            if task.status in recoverable - {TaskStatus.QUEUED}:
                self.store.update(
                    task.id,
                    {
                        "status": TaskStatus.QUEUED,
                        "progress": 0,
                        "queue_position": position,
                        "resume_from_stage": True,
                    },
                )
                self._resume_from_stage.add(task.id)
            elif task.queue_position != position:
                self.store.update(task.id, {"queue_position": position})
            if task.status is not TaskStatus.PAUSED:
                self.queue.put_nowait(task.id)
        self._next_queue_position = max(self._next_queue_position, len(tasks))
        self.scheduler = asyncio.create_task(
            self._schedule(), name="modelshelf-ingestion-scheduler"
        )
        self._scheduler_wakeup.set()

    async def stop(self) -> None:
        if self.scheduler:
            self.scheduler.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.scheduler
        scheduled = list(self._scheduled_runs.values())
        for task in scheduled:
            task.cancel()
        if scheduled:
            await asyncio.gather(*scheduled, return_exceptions=True)
        self._scheduled_runs.clear()
        active = list(self._active_runs.values())
        for task in active:
            task.cancel()
        if active:
            await asyncio.gather(*active, return_exceptions=True)

    def _arm_scheduled_task(self, task: DownloadTask) -> None:
        if task.scheduled_at is None:
            raise ValueError("scheduled task is missing scheduled_at")
        previous = self._scheduled_runs.pop(task.id, None)
        if previous is not None:
            previous.cancel()
        waiter = asyncio.create_task(
            self._wait_for_scheduled_task(task.id, task.scheduled_at),
            name=f"modelshelf-scheduled-{task.id}",
        )
        self._scheduled_runs[task.id] = waiter

        def finished(completed: asyncio.Task[None]) -> None:
            if self._scheduled_runs.get(task.id) is completed:
                self._scheduled_runs.pop(task.id, None)
            if not completed.cancelled():
                completed.exception()

        waiter.add_done_callback(finished)

    async def _wait_for_scheduled_task(self, task_id: str, scheduled_at: datetime) -> None:
        delay = max(0.0, (scheduled_at - datetime.now(UTC)).total_seconds())
        await asyncio.sleep(delay)
        async with self._update_lock:
            current = self.store.get(task_id)
            if current is None or current.status is not TaskStatus.SCHEDULED:
                return
            self.store.update(
                task_id,
                {
                    "status": TaskStatus.QUEUED,
                    "queue_position": self._claim_queue_position(),
                },
            )
        await self.queue.put(task_id)
        self._scheduler_wakeup.set()

    async def start_now(self, task_id: str) -> DownloadTask:
        queued = await self._transition(
            task_id,
            {TaskStatus.SCHEDULED},
            "started immediately",
            status=TaskStatus.QUEUED,
            queue_position=self._claim_queue_position(),
            scheduled_at=None,
        )
        waiter = self._scheduled_runs.pop(task_id, None)
        if waiter is not None:
            waiter.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await waiter
        await self.queue.put(task_id)
        self._scheduler_wakeup.set()
        return queued

    async def reschedule(self, task_id: str, scheduled_at: datetime) -> DownloadTask:
        if scheduled_at.tzinfo is None or scheduled_at.utcoffset() is None:
            raise ValueError("scheduled start must include a timezone")
        scheduled_at = scheduled_at.astimezone(UTC)
        if scheduled_at <= datetime.now(UTC):
            raise ValueError("scheduled start must be in the future")
        rescheduled = await self._transition(
            task_id,
            {TaskStatus.SCHEDULED},
            "rescheduled",
            scheduled_at=scheduled_at,
            queue_position=None,
        )
        self._arm_scheduled_task(rescheduled)
        return rescheduled

    async def pause(self, task_id: str) -> DownloadTask:
        current = self.store.get(task_id)
        queue_position = (
            current.queue_position
            if current is not None and current.queue_position is not None
            else self._claim_queue_position()
        )
        paused = await self._transition(
            task_id,
            {
                TaskStatus.QUEUED,
                TaskStatus.RESOLVING,
                TaskStatus.DOWNLOADING,
                TaskStatus.VERIFYING,
            },
            "paused",
            status=TaskStatus.PAUSED,
            queue_position=queue_position,
            resume_from_stage=True,
        )
        timing = self._download_timing.get(task_id)
        baseline = self._download_baselines.get(task_id)
        active = self._active_runs.get(task_id)
        if active is not None:
            active.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await active
        self._scheduler_wakeup.set()
        await self._stop_metrics(paused.id, timing=timing, baseline=baseline)
        return await self._stop_verification_metrics(paused.id)

    async def resume(self, task_id: str, *, scheduled_at: datetime | None = None) -> DownloadTask:
        if scheduled_at is not None:
            if scheduled_at.tzinfo is None or scheduled_at.utcoffset() is None:
                raise ValueError("scheduled resume must include a timezone")
            scheduled_at = scheduled_at.astimezone(UTC)
        delayed = scheduled_at is not None and scheduled_at > datetime.now(UTC)
        if not delayed:
            self._resume_from_stage.add(task_id)
        current = self.store.get(task_id)
        queue_position = (
            current.queue_position
            if current is not None and current.queue_position is not None
            else self._claim_queue_position()
        )
        try:
            resumed = await self._transition(
                task_id,
                {TaskStatus.PAUSED},
                "scheduled for resume" if delayed else "resumed",
                status=TaskStatus.SCHEDULED if delayed else TaskStatus.QUEUED,
                scheduled_at=scheduled_at if delayed else None,
                queue_position=None if delayed else queue_position,
                resume_from_stage=True,
                error=None,
            )
        except Exception:
            self._resume_from_stage.discard(task_id)
            raise
        if delayed:
            self._arm_scheduled_task(resumed)
            return resumed
        await self.queue.put(task_id)
        self._scheduler_wakeup.set()
        return resumed

    async def cancel(self, task_id: str) -> DownloadTask:
        cancelled = await self._transition(
            task_id,
            {
                TaskStatus.QUEUED,
                TaskStatus.SCHEDULED,
                TaskStatus.RESOLVING,
                TaskStatus.DOWNLOADING,
                TaskStatus.VERIFYING,
                TaskStatus.PAUSED,
                TaskStatus.AWAITING_CONFIRMATION,
            },
            "cancelled",
            status=TaskStatus.CANCELLED,
            queue_position=None,
        )
        timing = self._download_timing.get(task_id)
        baseline = self._download_baselines.get(task_id)
        active = self._active_runs.get(task_id)
        if active is not None:
            active.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await active
        self._scheduler_wakeup.set()
        scheduled = self._scheduled_runs.pop(task_id, None)
        if scheduled is not None:
            scheduled.cancel()
        self._resume_from_stage.discard(task_id)
        shutil.rmtree(self.catalog.staging_path(task_id), ignore_errors=True)
        await self._stop_metrics(cancelled.id, timing=timing, baseline=baseline, clear_eta=True)
        return await self._stop_verification_metrics(cancelled.id)

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

    async def set_artifact_alias(
        self, artifact_id: str, alias: str | None
    ) -> ArtifactSummary:
        async with self._create_lock:
            self._ensure_alias_available(alias, allow_artifact_id=artifact_id)
            async with self._artifact_lock:
                return self.catalog.set_alias(artifact_id, alias)

    def find_duplicate(
        self,
        provider: Provider,
        source_id: str,
        requested_revision: str,
        *,
        resolved_revision: str | None,
        disable_mirror: bool = False,
        mirror_url: str | None = None,
        disable_proxy: bool = False,
        selected_paths: list[str] | None = None,
    ) -> DuplicateIngestion | None:
        if not resolved_revision:
            return None

        tasks = self.store.list()
        artifact_id = artifact_identity(provider, source_id, resolved_revision, selected_paths)
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
            TaskStatus.SCHEDULED,
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
                and task.mirror_url == mirror_url
                and task.disable_proxy == disable_proxy
                and task.selected_paths == selected_paths
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
        mirror_url: str | None = None,
        disable_proxy: bool = False,
        scheduled_at: datetime | None = None,
        selected_paths: list[str] | None = None,
        artifact_alias: str | None = None,
    ) -> TaskCreationResult:
        async with self._create_lock:
            duplicate = self.find_duplicate(
                provider,
                source_id,
                requested_revision,
                resolved_revision=resolved_revision,
                disable_mirror=disable_mirror,
                mirror_url=mirror_url,
                disable_proxy=disable_proxy,
                selected_paths=selected_paths,
            )
            if duplicate is not None:
                if (
                    duplicate.kind == "artifact"
                    and duplicate.artifact_id is not None
                    and duplicate.artifact_total_size is not None
                    and resolved_revision is not None
                ):
                    found = self.catalog.find(duplicate.artifact_id)
                    if found is None:
                        raise ValueError("the duplicate artifact disappeared during task creation")
                    existing_alias = found[0].alias
                    if artifact_alias is not None and existing_alias not in {None, artifact_alias}:
                        raise ValueError(
                            f"this artifact already has alias {existing_alias!r}"
                        )
                    if artifact_alias is not None and existing_alias is None:
                        self._ensure_alias_available(
                            artifact_alias, allow_artifact_id=duplicate.artifact_id
                        )
                        existing_alias = self.catalog.set_alias(
                            duplicate.artifact_id, artifact_alias
                        ).alias
                    if duplicate.task is not None:
                        completed = duplicate.task
                        if completed.artifact_alias != existing_alias:
                            completed = self.store.update(
                                completed.id, {"artifact_alias": existing_alias}
                            )
                        return TaskCreationResult(completed, "artifact")
                    completed = self.store.create_completed(
                        provider,
                        source_id,
                        requested_revision,
                        resolved_revision,
                        duplicate.artifact_id,
                        duplicate.artifact_total_size,
                        disable_mirror=disable_mirror,
                        mirror_url=mirror_url,
                        disable_proxy=disable_proxy,
                        selected_paths=selected_paths,
                        artifact_alias=existing_alias,
                    )
                    return TaskCreationResult(completed, "artifact")
                if duplicate.task is not None:
                    existing_task = duplicate.task
                    if artifact_alias is None or artifact_alias == existing_task.artifact_alias:
                        return TaskCreationResult(existing_task, duplicate.kind)
                    if existing_task.artifact_alias is not None:
                        raise ValueError(
                            "this download task already reserves artifact alias "
                            f"{existing_task.artifact_alias!r}"
                        )
                    self._ensure_alias_available(
                        artifact_alias, exclude_task_id=existing_task.id
                    )
                    updated = self.store.update(
                        existing_task.id, {"artifact_alias": artifact_alias}
                    )
                    return TaskCreationResult(updated, duplicate.kind)

            self._ensure_alias_available(artifact_alias)
            task = self.store.create(
                provider,
                source_id,
                requested_revision,
                resolved_revision=resolved_revision,
                total_bytes=total_bytes,
                disable_mirror=disable_mirror,
                mirror_url=mirror_url,
                disable_proxy=disable_proxy,
                scheduled_at=scheduled_at,
                queue_position=self._claim_queue_position(),
                selected_paths=selected_paths,
                artifact_alias=artifact_alias,
            )
            if task.status is TaskStatus.SCHEDULED:
                self._arm_scheduled_task(task)
            else:
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
        mirror_url: str | None = None,
        disable_proxy: bool = False,
        scheduled_at: datetime | None = None,
        selected_paths: list[str] | None = None,
        artifact_alias: str | None = None,
    ) -> DownloadTask:
        return (
            await self.create_with_result(
                provider,
                source_id,
                requested_revision,
                resolved_revision=resolved_revision,
                total_bytes=total_bytes,
                disable_mirror=disable_mirror,
                mirror_url=mirror_url,
                disable_proxy=disable_proxy,
                scheduled_at=scheduled_at,
                selected_paths=selected_paths,
                artifact_alias=artifact_alias,
            )
        ).task

    def _ensure_alias_available(
        self,
        alias: str | None,
        *,
        exclude_task_id: str | None = None,
        allow_artifact_id: str | None = None,
    ) -> None:
        if alias is None:
            return
        owner = self.catalog.alias_owner(alias)
        if owner is not None and owner != allow_artifact_id:
            raise ValueError(f"artifact alias {alias!r} is already in use")
        reserving = next(
            (
                task
                for task in self.store.list()
                if task.id != exclude_task_id
                and task.artifact_alias == alias
                and task.status
                not in {TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED}
            ),
            None,
        )
        if reserving is not None:
            raise ValueError(
                f"artifact alias {alias!r} is reserved by task {reserving.id}"
            )

    def reorder_queued(self, ordered_task_ids: list[str]) -> list[DownloadTask]:
        if len(ordered_task_ids) != len(set(ordered_task_ids)):
            raise ValueError("queue order contains duplicate task IDs")
        pending = [
            task
            for task in self.store.list()
            if task.status in {TaskStatus.QUEUED, TaskStatus.PAUSED}
        ]
        pending_by_id = {task.id: task for task in pending}
        if set(ordered_task_ids) != set(pending_by_id):
            raise ValueError("download queue changed; refresh the task list and try again")

        reordered: list[DownloadTask] = []
        for position, task_id in enumerate(ordered_task_ids):
            task = pending_by_id[task_id]
            reordered.append(
                task
                if task.queue_position == position
                else self.store.update(task_id, {"queue_position": position})
            )
        self._scheduler_wakeup.set()
        return reordered

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

    def _start_verification_metrics(self, task: DownloadTask) -> None:
        now = self.clock()
        self._verification_written.pop(task.id, None)
        self._verification_timing[task.id] = (now, task.verification_elapsed_seconds)
        self._verification_baselines[task.id] = (
            task.verification_bytes_completed,
            task.verification_average_bytes_per_second,
        )
        self._verification_samples[task.id] = (now, task.verification_bytes_completed)
        self._verification_speed_ema.pop(task.id, None)

    async def _verification_progress(self, task_id: str, completed: int, total: int) -> None:
        now = self.clock()
        previous = self._verification_written.get(task_id, 0)
        if now - previous < 0.5 and completed < total:
            return
        self._verification_written[task_id] = now
        last_sample = self._verification_samples.get(task_id)
        raw_speed = 0.0
        if last_sample is not None and now > last_sample[0]:
            raw_speed = max(0, completed - last_sample[1]) / (now - last_sample[0])
        previous_speed = self._verification_speed_ema.get(task_id)
        speed = raw_speed if previous_speed is None else previous_speed * 0.7 + raw_speed * 0.3
        self._verification_speed_ema[task_id] = speed
        self._verification_samples[task_id] = (now, completed)
        timing = self._verification_timing.get(task_id)
        elapsed = timing[1] + (now - timing[0]) if timing is not None else 0.0
        baseline = self._verification_baselines.get(task_id)
        if timing is not None and baseline is not None:
            previous_bytes = baseline[1] * timing[1]
            measured_bytes = previous_bytes + max(0, completed - baseline[0])
        else:
            measured_bytes = completed
        average_speed = measured_bytes / elapsed if elapsed > 0 else 0.0
        eta_speed = speed if speed > 0 else average_speed
        eta = (
            math.ceil(max(0, total - completed) / eta_speed)
            if total > completed and eta_speed > 0
            else 0
            if completed >= total
            else None
        )
        progress = min(98, 92 + int(completed / total * 6)) if total else 98
        await self._update(
            task_id,
            progress=progress,
            verification_bytes_completed=completed,
            verification_total_bytes=total,
            verification_instantaneous_bytes_per_second=speed,
            verification_average_bytes_per_second=average_speed,
            verification_eta_seconds=eta,
            verification_elapsed_seconds=elapsed,
        )

    async def _stop_verification_metrics(self, task_id: str) -> DownloadTask:
        timing = self._verification_timing.pop(task_id, None)
        self._verification_baselines.pop(task_id, None)
        self._verification_samples.pop(task_id, None)
        self._verification_speed_ema.pop(task_id, None)
        task = self.store.get(task_id)
        if task is None:
            raise KeyError(f"unknown task {task_id}")
        elapsed = task.verification_elapsed_seconds
        if timing is not None:
            elapsed = max(elapsed, timing[1] + (self.clock() - timing[0]))
        return await self._update(
            task_id,
            verification_instantaneous_bytes_per_second=0,
            verification_eta_seconds=None,
            verification_elapsed_seconds=elapsed,
        )

    async def _verify_inventory(
        self,
        task_id: str,
        root: Path,
        *,
        expected_sha256: Mapping[str, str] | None = None,
    ) -> list[FileEntry]:
        task = self.store.get(task_id)
        detail = task.verification_detail if task is not None else None
        if self._verification_slot.locked():
            await self._update(task_id, verification_detail="Waiting for verification capacity")
        async with self._verification_slot:
            self._raise_if_stopped(task_id)
            if detail is not None:
                await self._update(task_id, verification_detail=detail)
            current = self.store.get(task_id)
            if current is None:
                raise KeyError(f"unknown task {task_id}")
            self._start_verification_metrics(current)
            return await self._verify_inventory_unlocked(
                task_id,
                root,
                expected_sha256=expected_sha256,
            )

    async def _verify_inventory_unlocked(
        self,
        task_id: str,
        root: Path,
        *,
        expected_sha256: Mapping[str, str] | None = None,
    ) -> list[FileEntry]:
        progress_lock = threading.Lock()
        progress_state = (0, 0)
        cancelled = threading.Event()

        def record_progress(completed: int, total: int) -> None:
            nonlocal progress_state
            with progress_lock:
                progress_state = completed, total

        background = asyncio.create_task(
            asyncio.to_thread(
                inventory,
                root,
                workers=_VERIFICATION_WORKERS,
                progress=record_progress,
                expected_sha256=expected_sha256,
                cancelled=cancelled,
            )
        )
        try:
            while True:
                try:
                    files = await asyncio.wait_for(asyncio.shield(background), timeout=0.5)
                    break
                except TimeoutError:
                    with progress_lock:
                        completed, total = progress_state
                    await self._verification_progress(task_id, completed, total)
            with progress_lock:
                completed, total = progress_state
            await self._verification_progress(task_id, completed, total)
            return files
        except asyncio.CancelledError:
            cancelled.set()
            with contextlib.suppress(Exception):
                await asyncio.shield(background)
            raise

    def _raise_if_stopped(self, task_id: str) -> None:
        task = self.store.get(task_id)
        if task is not None and task.status in {TaskStatus.PAUSED, TaskStatus.CANCELLED}:
            raise asyncio.CancelledError

    def _next_schedulable(self) -> tuple[str, Provider] | None:
        selected: tuple[int, DownloadTask] | None = None
        index = 0
        while index < len(self._pending):
            task_id = self._pending[index]
            task = self.store.get(task_id)
            if task is None or task.status is not TaskStatus.QUEUED:
                del self._pending[index]
                self.queue.task_done()
                continue
            if self._active_by_source.get(
                task.provider, 0
            ) < self.max_concurrent_downloads_per_source and (
                selected is None or self._queue_order(task) < self._queue_order(selected[1])
            ):
                selected = index, task
            index += 1
        if selected is None:
            return None
        selected_index, task = selected
        del self._pending[selected_index]
        return task.id, task.provider

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

    @staticmethod
    def _provider_result_path(stage: Path) -> Path:
        return stage / "provider-result.json"

    @staticmethod
    def _inventory_path(stage: Path) -> Path:
        return stage / "verified-inventory.json"

    def _save_provider_result(self, stage: Path, result: ProviderResult) -> None:
        atomic_write_json(
            self._provider_result_path(stage),
            {
                "schemaVersion": _PROVIDER_RESULT_SCHEMA_VERSION,
                "resolvedRevision": result.resolved_revision,
                "sourceUrl": result.source_url,
                "downloadedFile": result.downloaded_file,
                "contentDisposition": result.content_disposition,
                "expectedSha256": result.expected_sha256,
            },
        )

    def _load_provider_result(self, stage: Path) -> ProviderResult | None:
        try:
            value = json.loads(self._provider_result_path(stage).read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        if not isinstance(value, dict) or value.get("schemaVersion") != 1:
            raise RuntimeError("staged provider result has an unsupported schema")
        expected = value.get("expectedSha256")
        if expected is not None and not isinstance(expected, dict):
            raise RuntimeError("staged provider result has invalid source hashes")
        return ProviderResult(
            resolved_revision=str(value.get("resolvedRevision") or ""),
            source_url=value.get("sourceUrl"),
            downloaded_file=value.get("downloadedFile"),
            content_disposition=value.get("contentDisposition"),
            expected_sha256={str(path): str(digest) for path, digest in expected.items()}
            if expected is not None
            else None,
        )

    def _save_verified_inventory(self, stage: Path, files: list[FileEntry]) -> None:
        atomic_write_json(
            self._inventory_path(stage),
            {
                "schemaVersion": 1,
                "files": [file.model_dump(mode="json", by_alias=True) for file in files],
            },
        )

    def _load_verified_inventory(self, stage: Path) -> list[FileEntry] | None:
        try:
            value = json.loads(self._inventory_path(stage).read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        if not isinstance(value, dict) or value.get("schemaVersion") != 1:
            raise RuntimeError("staged verified inventory has an unsupported schema")
        raw_files = value.get("files")
        if not isinstance(raw_files, list):
            raise RuntimeError("staged verified inventory is invalid")
        return [FileEntry.model_validate(file) for file in raw_files]

    async def _run(self, task_id: str) -> None:
        task = self.store.get(task_id)
        if task is None:
            return
        stage = self.catalog.staging_path(task_id)
        resume_from_stage = task.resume_from_stage or task_id in self._resume_from_stage
        self._resume_from_stage.discard(task_id)
        try:
            if not resume_from_stage:
                shutil.rmtree(stage, ignore_errors=True)
            elif task.resume_from_stage:
                task = self.store.update(task_id, {"resume_from_stage": False})
            stage.mkdir(parents=True, exist_ok=True)
            download_root = (
                stage / "download" if task.provider is Provider.HTTP else stage / "artifact"
            )
            download_root.mkdir(parents=True, exist_ok=True)
            result = self._load_provider_result(stage) if resume_from_stage else None
            if result is None:
                await self._update(
                    task_id,
                    status=TaskStatus.RESOLVING,
                    progress=1,
                    queue_position=None,
                    error=None,
                )
                await self._update(task_id, status=TaskStatus.DOWNLOADING, progress=2)
                self._start_metrics(task)

                async def report_progress(downloaded: int, reported_total: int | None) -> None:
                    total = task.total_bytes if task.total_bytes is not None else reported_total
                    transferred = min(downloaded, total) if total is not None else downloaded
                    await self._progress(task_id, transferred, total)

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
                    report_progress,
                    github_token=self.github_token,
                    huggingface_mirror=self.huggingface_mirror,
                    modelscope_cn_mirror=self.modelscope_cn_mirror,
                    modelscope_ai_mirror=self.modelscope_ai_mirror,
                    proxy_url=self.proxy_url,
                    disable_mirror=task.disable_mirror,
                    mirror_url=task.mirror_url,
                    disable_proxy=task.disable_proxy,
                    expected_resolved_revision=task.resolved_revision,
                    selected_paths=task.selected_paths,
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
            self._save_provider_result(stage, result)
            await self._stop_metrics(task_id, clear_eta=True)
            files = self._load_verified_inventory(stage) if resume_from_stage else None
            if files is None:
                await self._update(
                    task_id,
                    status=TaskStatus.VERIFYING,
                    progress=92,
                    queue_position=None,
                    error=None,
                    instantaneous_bytes_per_second=0,
                    eta_seconds=None,
                    verification_bytes_completed=0,
                    verification_total_bytes=task.total_bytes,
                    verification_instantaneous_bytes_per_second=0,
                    verification_average_bytes_per_second=0,
                    verification_eta_seconds=None,
                    verification_elapsed_seconds=0,
                    verification_detail=(
                        "Hashing files and checking source integrity"
                        if result.expected_sha256
                        else "Hashing files for the artifact manifest"
                    ),
                )
                files = await self._verify_inventory(
                    task_id,
                    download_root,
                    expected_sha256=result.expected_sha256,
                )
                self._save_verified_inventory(stage, files)
            else:
                verified_size = sum(file.size for file in files)
                await self._update(
                    task_id,
                    status=TaskStatus.VERIFYING,
                    progress=98,
                    queue_position=None,
                    error=None,
                    instantaneous_bytes_per_second=0,
                    eta_seconds=None,
                    verification_bytes_completed=verified_size,
                    verification_total_bytes=verified_size,
                    verification_instantaneous_bytes_per_second=0,
                    verification_eta_seconds=0,
                    verification_detail="Verification complete",
                )
            self._raise_if_stopped(task_id)
            await self._stop_verification_metrics(task_id)
            if not files:
                raise RuntimeError("provider returned no files")
            if task.selected_paths is not None:
                expected_paths = set(task.selected_paths)
                actual_paths = {file.path for file in files}
                missing = sorted(expected_paths - actual_paths)
                unexpected = sorted(actual_paths - expected_paths)
                if missing or unexpected:
                    details: list[str] = []
                    if missing:
                        details.append("missing " + ", ".join(missing[:3]))
                    if unexpected:
                        details.append("unexpected " + ", ".join(unexpected[:3]))
                    raise RuntimeError(
                        "downloaded files do not exactly match the selected files: "
                        + "; ".join(details)
                    )
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
                    eta_seconds=None,
                    verification_instantaneous_bytes_per_second=0,
                    verification_eta_seconds=None,
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
                    selected_paths=task.selected_paths,
                ),
            )
            await self._update(
                task_id,
                status=TaskStatus.PUBLISHING,
                progress=99,
                resolved_revision=resolved_revision,
                verification_instantaneous_bytes_per_second=0,
                verification_eta_seconds=None,
            )
            self._raise_if_stopped(task_id)
            async with self._artifact_lock:
                self.catalog.publish(download_root, manifest)
                if task.artifact_alias is not None:
                    self.catalog.set_alias(manifest.artifact_id, task.artifact_alias)
            shutil.rmtree(stage, ignore_errors=True)
            await self._update(
                task_id,
                status=TaskStatus.COMPLETED,
                progress=100,
                artifact_id=manifest.artifact_id,
                bytes_downloaded=manifest.total_size,
                total_bytes=manifest.total_size,
                instantaneous_bytes_per_second=0,
                eta_seconds=0,
                verification_instantaneous_bytes_per_second=0,
                verification_eta_seconds=0,
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
                verification_instantaneous_bytes_per_second=0,
                verification_eta_seconds=None,
            )
        finally:
            self._download_timing.pop(task_id, None)
            self._download_baselines.pop(task_id, None)
            self._progress_samples.pop(task_id, None)
            self._speed_ema.pop(task_id, None)
            self._verification_written.pop(task_id, None)
            self._verification_timing.pop(task_id, None)
            self._verification_baselines.pop(task_id, None)
            self._verification_samples.pop(task_id, None)
            self._verification_speed_ema.pop(task_id, None)

    async def confirm_http(
        self,
        task_id: str,
        *,
        name: str,
        version: str,
        format: str | None,
        extract: bool,
    ) -> DownloadTask:
        operation = asyncio.current_task()
        existing = self._active_runs.get(task_id)
        if existing is not None and existing is not operation:
            raise ValueError("task confirmation is already running")
        if operation is not None:
            self._active_runs[task_id] = operation
        try:
            return await self._confirm_http(
                task_id,
                name=name,
                version=version,
                format=format,
                extract=extract,
            )
        finally:
            if self._active_runs.get(task_id) is operation:
                self._active_runs.pop(task_id, None)
            self._scheduler_wakeup.set()

    async def _confirm_http(
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
            instantaneous_bytes_per_second=0,
            eta_seconds=None,
            verification_instantaneous_bytes_per_second=0,
            verification_eta_seconds=None,
            verification_detail="Preparing the staged download",
        )
        assert task.resolved_revision is not None
        stage = self.catalog.staging_path(task_id)
        download_root = stage / "download"
        files = self._load_verified_inventory(stage)
        if files is None:
            await self._update(
                task_id,
                verification_bytes_completed=0,
                verification_total_bytes=task.total_bytes,
                verification_average_bytes_per_second=0,
                verification_elapsed_seconds=0,
                verification_detail="Hashing files for the artifact manifest",
            )
            files = await self._verify_inventory(task_id, download_root)
            await self._stop_verification_metrics(task_id)
            self._save_verified_inventory(stage, files)
        if not files:
            raise RuntimeError("staged HTTP content is missing")
        publish_root = download_root
        if extract:
            if len(files) != 1:
                raise ValueError("automatic extraction requires exactly one downloaded archive")
            publish_root = stage / "publish"
            shutil.rmtree(publish_root, ignore_errors=True)
            await self._update(task_id, verification_detail="Extracting the archive")
            await asyncio.to_thread(extract_archive, download_root / files[0].path, publish_root)
            await self._update(
                task_id,
                verification_bytes_completed=0,
                verification_total_bytes=None,
                verification_instantaneous_bytes_per_second=0,
                verification_average_bytes_per_second=0,
                verification_eta_seconds=None,
                verification_elapsed_seconds=0,
                verification_detail="Hashing extracted files for the artifact manifest",
            )
            files = await self._verify_inventory(task_id, publish_root)
            await self._stop_verification_metrics(task_id)
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
            if task.artifact_alias is not None:
                self.catalog.set_alias(manifest.artifact_id, task.artifact_alias)
        shutil.rmtree(stage, ignore_errors=True)
        return await self._update(
            task_id,
            status=TaskStatus.COMPLETED,
            progress=100,
            artifact_id=manifest.artifact_id,
        )
