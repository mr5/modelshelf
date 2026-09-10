from __future__ import annotations

import asyncio
import hashlib
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import huggingface_hub
import modelshelf_server.providers as providers
import modelshelf_server.tasks as tasks
import pytest
from modelshelf_core import Catalog, Provider, TaskStatus, VerificationError
from modelshelf_core.catalog import clone_artifact_file, remove_staging_tree
from modelshelf_server.providers import ProviderResult
from modelshelf_server.tasks import TaskManager


def test_failed_download_retains_data_and_retries_after_restart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    catalog = Catalog(tmp_path / "storage")
    catalog.initialize()
    attempts = 0

    async def download(*args: object, **kwargs: object) -> ProviderResult:
        nonlocal attempts
        attempts += 1
        destination = args[3]
        assert isinstance(destination, Path)
        assert kwargs["expected_resolved_revision"] == "a" * 40
        payload = destination / "model.bin"
        if attempts == 1:
            payload.write_bytes(b"already downloaded")
            raise RuntimeError("temporary upstream outage")
        assert payload.read_bytes() == b"already downloaded"
        return ProviderResult(resolved_revision="a" * 40)

    monkeypatch.setattr(tasks, "run_provider", download)

    async def exercise() -> None:
        manager = TaskManager(catalog, github_token=None)
        await manager.start()
        task = await manager.create(
            Provider.MODELSCOPE_CN, "owner/model", "master", resolved_revision="a" * 40
        )
        await asyncio.wait_for(manager.queue.join(), 3)
        failed = manager.store.get(task.id)
        assert failed is not None and failed.status is TaskStatus.FAILED
        assert failed.resume_from_stage
        stage = catalog.staging_path(task.id)
        assert (stage / "artifact/model.bin").read_bytes() == b"already downloaded"
        await manager.stop()
        restarted = TaskManager(catalog, github_token=None)
        await restarted.start()
        try:
            assert restarted.store.get(task.id).status is TaskStatus.FAILED
            await restarted.resume(task.id)
            await asyncio.wait_for(restarted.queue.join(), 3)
            assert restarted.store.get(task.id).status is TaskStatus.COMPLETED
            assert not stage.exists()
            published = catalog.artifact_path(Provider.MODELSCOPE_CN, "owner/model", "a" * 40)
            assert (published / "model.bin").read_bytes() == b"already downloaded"
        finally:
            await restarted.stop()

    asyncio.run(exercise())
    assert attempts == 2


def test_verification_failure_keeps_worktree_and_lfs_objects(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    catalog = Catalog(tmp_path)
    catalog.initialize()

    async def download(*args: object, **_kwargs: object) -> ProviderResult:
        destination = args[3]
        assert isinstance(destination, Path)
        (destination / "model.bin").write_bytes(b"wrong")
        objects = destination / ".git/lfs/objects/object"
        objects.parent.mkdir(parents=True)
        objects.write_bytes(b"valuable cached object")
        return ProviderResult(
            resolved_revision="a" * 40,
            expected_sha256={"model.bin": hashlib.sha256(b"right").hexdigest()},
            fetched_paths=["model.bin"],
        )

    monkeypatch.setattr(tasks, "run_provider", download)

    async def exercise() -> None:
        manager = TaskManager(catalog, github_token=None)
        await manager.start()
        try:
            task = await manager.create(
                Provider.MODELSCOPE_CN, "owner/model", "master", resolved_revision="a" * 40
            )
            await asyncio.wait_for(manager.queue.join(), 3)
            assert manager.store.get(task.id).status is TaskStatus.FAILED
            stage = catalog.staging_path(task.id)
            assert (stage / "artifact/model.bin").read_bytes() == b"wrong"
            assert (stage / ".modelscope-git/lfs/objects/object").read_bytes() == (
                b"valuable cached object"
            )
            assert not catalog.list()
        finally:
            await manager.stop()

    asyncio.run(exercise())


def test_modelscope_fetches_old_commit_after_master_moves(tmp_path: Path) -> None:
    remote = tmp_path / "upstream/owner/model.git"
    remote.mkdir(parents=True)

    def git(*args: str) -> str:
        return subprocess.check_output(
            ["git", "-C", str(remote), *args], text=True, stderr=subprocess.PIPE
        ).strip()

    git("init", "-b", "master")
    (remote / "config.json").write_text("old version")
    git("add", ".")
    git("-c", "user.name=Test", "-c", "user.email=test@example.test", "commit", "-m", "old")
    locked = git("rev-parse", "HEAD")
    (remote / "config.json").write_text("new version")
    git("add", ".")
    git("-c", "user.name=Test", "-c", "user.email=test@example.test", "commit", "-m", "new")
    assert git("rev-parse", "HEAD") != locked
    destination = tmp_path / "artifact"
    providers._prepare_modelscope_git_checkout(
        "owner/model", "master", locked, destination,
        (tmp_path / "upstream").as_uri(), None, skip_lfs=True,
    )
    assert (destination / "config.json").read_text() == "old version"


def test_resume_validation_error_never_deletes_existing_data(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    destination = tmp_path / "artifact"
    cached = destination / ".git/lfs/objects/large-object"
    cached.parent.mkdir(parents=True)
    cached.write_bytes(b"previously downloaded bytes")

    def broken_git(*_args: object) -> str:
        raise providers.ProviderRequestError("index.lock exists")

    monkeypatch.setattr(providers, "_checked_modelscope_git", broken_git)
    with pytest.raises(providers.ProviderRequestError, match="staging retained"):
        providers._try_resume_modelscope_git_checkout(
            "owner/model", "a" * 40, destination, "https://modelscope.cn", None
        )
    with pytest.raises(providers.ProviderRequestError, match="staging data retained"):
        providers._prepare_modelscope_git_checkout(
            "owner/model", "master", "a" * 40, destination,
            "https://modelscope.cn", None, skip_lfs=True,
        )
    assert cached.read_bytes() == b"previously downloaded bytes"


def test_cleanup_rejects_root_escape_and_symlink(tmp_path: Path) -> None:
    root = tmp_path / ".staging"
    root.mkdir()
    external = tmp_path / "valuable"
    external.mkdir()
    marker = external / "model"
    marker.write_bytes(b"keep")
    link = root / "link"
    link.symlink_to(external, target_is_directory=True)
    for path in [root, tmp_path, external, link]:
        with pytest.raises(VerificationError):
            remove_staging_tree(path, root, reason="test invalid cleanup")
    assert marker.read_bytes() == b"keep"


def test_clone_does_not_delete_existing_destination(tmp_path: Path) -> None:
    source = tmp_path / "source"
    destination = tmp_path / "destination"
    source.write_bytes(b"new")
    destination.write_bytes(b"keep")
    with pytest.raises(FileExistsError):
        clone_artifact_file(source, destination)
    assert destination.read_bytes() == b"keep"


@pytest.mark.parametrize("provider", [Provider.HUGGINGFACE, Provider.KAGGLE])
def test_sdk_failure_keeps_download_caches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, provider: Provider
) -> None:
    destination = tmp_path / "artifact"
    destination.mkdir()
    cache = tmp_path / (
        ".huggingface-cache" if provider is Provider.HUGGINGFACE else ".kaggle-cache"
    )
    cache.mkdir()
    (cache / "partial").write_bytes(b"cached partial")
    metadata = destination / ".cache/huggingface"
    metadata.mkdir(parents=True)
    (metadata / "download.incomplete").write_bytes(b"incomplete")

    async def fail(*_args: object, **_kwargs: object) -> str:
        raise RuntimeError("network interrupted")

    monkeypatch.setattr(providers, "_blocking_download", fail)
    monkeypatch.setattr(huggingface_hub, "HfApi", lambda **_kwargs: SimpleNamespace(
        model_info=lambda *_args, **_options: SimpleNamespace(
            sha="a" * 40, siblings=[SimpleNamespace(rfilename="model.bin", size=100)]
        )
    ))
    with pytest.raises(RuntimeError, match="network interrupted"):
        if provider is Provider.HUGGINGFACE:
            asyncio.run(providers.download_huggingface(
                "owner/model", "a" * 40, destination,
                lambda *_args: asyncio.sleep(0), "https://huggingface.co"
            ))
        else:
            asyncio.run(providers.download_kaggle(
                "owner/model/framework/variation", "1", destination,
                lambda *_args: asyncio.sleep(0)
            ))
    assert (cache / "partial").read_bytes() == b"cached partial"
    assert (metadata / "download.incomplete").read_bytes() == b"incomplete"


@pytest.mark.skipif(os.name != "posix", reason="POSIX process groups")
@pytest.mark.parametrize("progress_error", [False, True])
def test_isolated_worker_stops_descendants_on_cancel_or_progress_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, progress_error: bool
) -> None:
    marker = tmp_path / "child-writing"
    child_code = (
        "import signal,time; from pathlib import Path; "
        "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
        f"p=Path({str(marker)!r}); "
        "\nwhile True:\n with p.open('ab') as f: f.write(b'x'); f.flush()\n time.sleep(.01)"
    )
    worker_code = (
        "import sys,subprocess,time; from pathlib import Path; sys.stdin.read(); "
        f"subprocess.Popen([sys.executable,'-c',{child_code!r}]); "
        f"p=Path({str(marker)!r})\n"
        "while not p.exists(): time.sleep(.01)\n"
        "print('MODELSHELF_JSON:{\"type\":\"progress\",\"downloaded\":1}',flush=True)\n"
        "time.sleep(30)"
    )
    original_create = asyncio.create_subprocess_exec

    async def create(*_args: object, **kwargs: object) -> asyncio.subprocess.Process:
        assert kwargs["start_new_session"] is True
        return await original_create(sys.executable, "-c", worker_code, **kwargs)

    monkeypatch.setattr(providers.asyncio, "create_subprocess_exec", create)

    async def exercise() -> None:
        started = asyncio.Event()

        async def progress(_downloaded: int, _total: int | None) -> None:
            started.set()
            if progress_error:
                raise RuntimeError("metrics persistence failed")

        task = asyncio.create_task(providers._isolated_download(
            Provider.MODELSCOPE_CN, "owner/model", "master", tmp_path, progress,
            github_token=None, huggingface_mirror=None, modelscope_cn_mirror=None,
            modelscope_ai_mirror=None, disable_mirror=False, mirror_url=None,
            direct=False, expected_resolved_revision="a" * 40, selected_paths=None,
            reusable_artifact_roots=None,
        ))
        try:
            await asyncio.wait_for(started.wait(), 3)
            if progress_error:
                with pytest.raises(RuntimeError, match="metrics persistence failed"):
                    await asyncio.wait_for(task, 7)
            else:
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await asyncio.wait_for(task, 7)
            await asyncio.sleep(.1)
            stopped_size = marker.stat().st_size
            await asyncio.sleep(.15)
            assert marker.stat().st_size == stopped_size
        finally:
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    asyncio.run(exercise())
