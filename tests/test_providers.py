from __future__ import annotations

import asyncio
import hashlib
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import huggingface_hub
import kagglehub  # type: ignore[import-untyped]
import modelshelf_server.providers as provider_module
import pytest
from modelshelf_core import Catalog, Provider, SourceReference
from modelshelf_server.providers import (
    ProviderResult,
    _parse_ls_remote,
    discover_revisions,
    download_github_release,
    download_kaggle,
    estimate_download,
    run_provider,
    search_models,
)


def test_modelscope_ls_remote_prefers_peeled_tag_then_branch() -> None:
    output = "\n".join(
        [
            f"{'1' * 40}\trefs/tags/v1",
            f"{'2' * 40}\trefs/tags/v1^{{}}",
            f"{'3' * 40}\trefs/heads/v1",
        ]
    )
    assert _parse_ls_remote(output, "v1") == "2" * 40
    assert _parse_ls_remote(f"{'4' * 40}\trefs/heads/master\n", "master") == "4" * 40
    with pytest.raises(RuntimeError, match="did not resolve"):
        _parse_ls_remote("", "missing")


def test_modelscope_lfs_include_anchors_and_escapes_literal_paths() -> None:
    assert (
        provider_module._modelscope_lfs_include(["weights/model[1]*.bin", "root file.bin"])
        == "/weights/model\\[1\\]\\*.bin,/root file.bin"
    )
    assert provider_module._modelscope_lfs_include(["weights/model,1.bin"]) is None


def test_modelscope_sites_use_independent_endpoints_and_tokens(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MODELSCOPE_CN_API_TOKEN", "cn-token")
    monkeypatch.setenv("MODELSCOPE_AI_API_TOKEN", "ai-token")

    assert provider_module._provider_endpoint(Provider.MODELSCOPE_CN, None, False) == (
        "https://modelscope.cn"
    )
    assert provider_module._provider_endpoint(Provider.MODELSCOPE_AI, None, False) == (
        "https://modelscope.ai"
    )
    assert provider_module._modelscope_token(Provider.MODELSCOPE_CN) == "cn-token"
    assert provider_module._modelscope_token(Provider.MODELSCOPE_AI) == "ai-token"


def test_empty_huggingface_token_is_treated_as_anonymous(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HF_TOKEN", "")
    assert provider_module._huggingface_token() is None

    monkeypatch.setenv("HF_TOKEN", "hf_test_token")
    assert provider_module._huggingface_token() == "hf_test_token"


@pytest.mark.parametrize(
    ("configured_mirror", "temporary_mirror", "disable_mirror", "expected"),
    [
        ("https://mirror.example", None, False, "1"),
        (None, "https://task-mirror.example", False, "1"),
        ("https://mirror.example", None, True, None),
        (None, None, False, None),
    ],
)
def test_huggingface_mirror_download_disables_xet_in_worker(
    monkeypatch: pytest.MonkeyPatch,
    configured_mirror: str | None,
    temporary_mirror: str | None,
    disable_mirror: bool,
    expected: str | None,
) -> None:
    monkeypatch.delenv("HF_HUB_DISABLE_XET", raising=False)

    environment = provider_module._download_worker_environment(
        Provider.HUGGINGFACE,
        direct=False,
        huggingface_mirror=configured_mirror,
        disable_mirror=disable_mirror,
        mirror_url=temporary_mirror,
    )

    assert environment.get("HF_HUB_DISABLE_XET") == expected


def test_huggingface_direct_mirror_download_disables_xet_and_proxy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.example")
    monkeypatch.delenv("HF_HUB_DISABLE_XET", raising=False)

    environment = provider_module._download_worker_environment(
        Provider.HUGGINGFACE,
        direct=True,
        huggingface_mirror="https://mirror.example",
        disable_mirror=False,
        mirror_url=None,
    )

    assert environment["HF_HUB_DISABLE_XET"] == "1"
    assert "HTTPS_PROXY" not in environment
    assert environment["NO_PROXY"] == "*"


def test_huggingface_selected_download_passes_exact_allow_patterns(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict[str, object] = {}
    progress_updates: list[tuple[int, int | None]] = []

    def snapshot_download(**kwargs: object) -> str:
        captured.update(kwargs)
        destination = Path(str(kwargs["local_dir"]))
        destination.mkdir(parents=True, exist_ok=True)
        (destination / "model.gguf").write_bytes(b"model")
        cache = destination / ".cache" / "huggingface"
        cache.mkdir(parents=True)
        (cache / "temporary.incomplete").write_bytes(b"temporary cache")
        return str(destination)

    async def progress(downloaded: int, total: int | None) -> None:
        progress_updates.append((downloaded, total))

    monkeypatch.setattr(huggingface_hub, "snapshot_download", snapshot_download)
    monkeypatch.setattr(
        huggingface_hub,
        "HfApi",
        lambda **_kwargs: SimpleNamespace(
            model_info=lambda *_args, **_kwargs: SimpleNamespace(
                sha="a" * 40,
                siblings=[SimpleNamespace(rfilename="model.gguf", size=5)],
            )
        ),
    )
    result = asyncio.run(
        provider_module.download_huggingface(
            "owner/model",
            "a" * 40,
            tmp_path / "artifact",
            progress,
            "https://huggingface.co",
            ["model.gguf"],
        )
    )

    assert result.resolved_revision == "a" * 40
    assert captured["allow_patterns"] == ["model.gguf"]
    assert progress_updates[-1] == (5, 5)
    assert not (tmp_path / "artifact" / ".cache" / "huggingface").exists()


def test_huggingface_estimate_falls_back_to_expanded_tree_sizes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, object]] = []

    class Api:
        def model_info(self, *_args: object, **_kwargs: object) -> SimpleNamespace:
            return SimpleNamespace(
                sha="b" * 40,
                siblings=[
                    SimpleNamespace(rfilename="config.json", size=None),
                    SimpleNamespace(rfilename="model.safetensors", size=None),
                ],
                pipeline_tag="text-generation",
                library_name="transformers",
                last_modified=None,
            )

        def list_repo_tree(self, *_args: object, **kwargs: object) -> list[SimpleNamespace]:
            calls.append(kwargs)
            return [
                SimpleNamespace(path="config.json", size=7),
                SimpleNamespace(path="model.safetensors", size=1_000),
            ]

    monkeypatch.setattr(huggingface_hub, "HfApi", lambda **_kwargs: Api())

    estimate = asyncio.run(
        provider_module._estimate_huggingface(
            "owner/model",
            "main",
            "https://mirror.example",
        )
    )

    assert estimate.total_size == 1_007
    assert estimate.file_count == 2
    assert [(file.path, file.size) for file in estimate.files] == [
        ("config.json", 7),
        ("model.safetensors", 1_000),
    ]
    assert calls == [{"revision": "b" * 40, "recursive": True, "expand": True}]


def test_modelscope_authentication_error_names_the_selected_site_token() -> None:
    class AuthenticationError(RuntimeError):
        pass

    translated = provider_module._translate_modelscope_error(
        AuthenticationError("secret upstream response"),
        provider=Provider.MODELSCOPE_CN,
        endpoint="https://modelscope.cn",
    )

    assert translated is not None
    assert "modelscope.cn" in str(translated)
    assert "MODELSCOPE_CN_API_TOKEN" in str(translated)
    assert "secret upstream response" not in str(translated)


def test_modelscope_revision_resolution_prefers_anonymous_git(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, str]] = []

    class Process:
        returncode = 0

        async def communicate(self) -> tuple[bytes, bytes]:
            return f"{'4' * 40}\trefs/heads/master\n".encode(), b""

    async def create_process(*_args: object, **kwargs: object) -> Process:
        calls.append(kwargs["env"])  # type: ignore[arg-type]
        return Process()

    monkeypatch.setattr(provider_module.asyncio, "create_subprocess_exec", create_process)
    resolved = asyncio.run(
        provider_module._resolve_modelscope_revision(
            Provider.MODELSCOPE_CN,
            "owner/model",
            "master",
            endpoint="https://modelscope.cn",
            token="stale-token",
        )
    )

    assert resolved == "4" * 40
    assert len(calls) == 1
    assert "GIT_CONFIG_VALUE_0" not in calls[0]


def test_modelscope_revision_resolution_uses_token_after_anonymous_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[tuple[object, ...], dict[str, str]]] = []

    class Process:
        def __init__(self, returncode: int) -> None:
            self.returncode = returncode

        async def communicate(self) -> tuple[bytes, bytes]:
            if self.returncode:
                return b"", b"authentication required"
            return f"{'5' * 40}\trefs/heads/private\n".encode(), b""

    async def create_process(*args: object, **kwargs: object) -> Process:
        environment = kwargs["env"]  # type: ignore[assignment]
        calls.append((args, environment))
        return Process(1 if len(calls) == 1 else 0)

    monkeypatch.setattr(provider_module.asyncio, "create_subprocess_exec", create_process)
    resolved = asyncio.run(
        provider_module._resolve_modelscope_revision(
            Provider.MODELSCOPE_CN,
            "owner/private-model",
            "private",
            endpoint="https://modelscope.cn",
            token="private-token",
        )
    )

    assert resolved == "5" * 40
    assert len(calls) == 2
    assert "GIT_CONFIG_VALUE_0" not in calls[0][1]
    assert calls[1][1]["GIT_CONFIG_KEY_0"] == "http.extraHeader"
    assert calls[1][1]["GIT_CONFIG_VALUE_0"].startswith("Authorization: Basic ")
    assert "private-token" not in " ".join(str(argument) for argument in calls[1][0])


def test_modelscope_download_uses_requested_revision_and_verifies_immutable_commit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    resolved = "e823e888ae179eb3be02c1a48899c4f828371376"
    captured: dict[str, object] = {}

    async def git_download(*args: object, **kwargs: object) -> tuple[dict[str, str], list[str]]:
        captured["source_id"] = args[0]
        captured["revision"] = args[1]
        captured["resolved"] = args[2]
        destination = args[3]
        assert isinstance(destination, Path)
        destination.mkdir(parents=True, exist_ok=True)
        (destination / "config.json").write_text("{}", encoding="utf-8")
        assert kwargs == {"selected_paths": None, "reusable_artifact_roots": None}
        return {}, []

    monkeypatch.setattr(provider_module, "_download_modelscope_git", git_download)

    async def resolve(*_args: object, **_kwargs: object) -> str:
        return resolved

    monkeypatch.setattr(provider_module, "_resolve_modelscope_revision", resolve)

    result = asyncio.run(
        provider_module.download_modelscope(
            Provider.MODELSCOPE_CN,
            "Qwen/Qwen3.8-27B",
            "master",
            tmp_path / "artifact",
            lambda _downloaded, _total: asyncio.sleep(0),
            "https://modelscope.cn",
            None,
            resolved,
        )
    )

    assert result.resolved_revision == resolved
    assert captured == {
        "source_id": "Qwen/Qwen3.8-27B",
        "revision": "master",
        "resolved": resolved,
    }


def test_modelscope_download_rejects_a_revision_that_moves_during_download(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    before = "a" * 40
    after = "b" * 40

    async def resolve(*_args: object, **_kwargs: object) -> str:
        return after

    monkeypatch.setattr(provider_module, "_resolve_modelscope_revision", resolve)

    with pytest.raises(RuntimeError, match="changed after preflight"):
        asyncio.run(
            provider_module.download_modelscope(
                Provider.MODELSCOPE_CN,
                "owner/model",
                "master",
                tmp_path / "artifact",
                lambda _downloaded, _total: asyncio.sleep(0),
                "https://modelscope.cn",
                None,
                before,
            )
        )


def test_modelscope_selected_download_uses_requested_revision_and_checks_it_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    resolved = "c" * 40
    resolutions = 0
    captured: dict[str, object] = {}

    async def resolve(*_args: object, **_kwargs: object) -> str:
        nonlocal resolutions
        resolutions += 1
        return resolved

    async def selected_download(*args: object) -> None:
        captured["revision"] = args[1]
        captured["selected_paths"] = args[-1]
        destination = args[2]
        assert isinstance(destination, Path)
        destination.mkdir(parents=True, exist_ok=True)
        (destination / "model.gguf").write_bytes(b"model")

    monkeypatch.setattr(provider_module, "_resolve_modelscope_revision", resolve)
    monkeypatch.setattr(provider_module, "_download_modelscope_selected", selected_download)

    result = asyncio.run(
        provider_module.download_modelscope(
            Provider.MODELSCOPE_CN,
            "owner/model",
            "master",
            tmp_path / "artifact",
            lambda _downloaded, _total: asyncio.sleep(0),
            "https://modelscope.cn",
            None,
            resolved,
            ["model.gguf"],
        )
    )

    assert result.resolved_revision == resolved
    assert resolutions == 2
    assert captured == {"revision": "master", "selected_paths": ["model.gguf"]}


def test_modelscope_git_estimate_uses_lfs_object_sizes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def prepare(
        _source_id: str,
        _revision: str,
        _resolved_revision: str,
        destination: Path,
        _endpoint: str,
        _token: str | None,
        *,
        skip_lfs: bool,
    ) -> dict[str, str]:
        assert skip_lfs is True
        destination.mkdir(parents=True)
        (destination / ".gitattributes").write_text("*.bin filter=lfs\n", encoding="utf-8")
        (destination / "config.json").write_text("{}", encoding="utf-8")
        (destination / "model.bin").write_text(
            f"version https://git-lfs.github.com/spec/v1\noid sha256:{'a' * 64}\nsize 1048576\n",
            encoding="utf-8",
        )
        git = destination / ".git"
        git.mkdir()
        (git / "index").write_bytes(b"ignored")
        return {}

    monkeypatch.setattr(provider_module, "_prepare_modelscope_git_checkout", prepare)

    total_size, files = provider_module._estimate_modelscope_git(
        "owner/model",
        "master",
        "a" * 40,
        "https://modelscope.cn",
        None,
    )

    assert total_size == 1048576 + len("*.bin filter=lfs\n") + len("{}")
    assert len(files) == 3
    assert {file.path for file in files} == {".gitattributes", "config.json", "model.bin"}
    by_path = {file.path: file for file in files}
    assert by_path["model.bin"].sha256 == "a" * 64
    assert by_path["config.json"].sha256 is None


def test_modelscope_estimate_resolves_revision_before_reading_git_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resolved = "d" * 40
    calls: list[tuple[object, ...]] = []

    async def resolve(*args: object, **_kwargs: object) -> str:
        calls.append(args)
        return resolved

    def estimate(*args: object) -> tuple[int, tuple[provider_module.SourceFile, ...]]:
        calls.append(args)
        return 1234, tuple(provider_module.SourceFile(f"file-{index}.bin", 1) for index in range(5))

    monkeypatch.setattr(provider_module, "_resolve_modelscope_revision", resolve)
    monkeypatch.setattr(provider_module, "_estimate_modelscope_git", estimate)

    result = asyncio.run(
        provider_module._estimate_modelscope(
            Provider.MODELSCOPE_CN,
            "owner/model",
            "master",
            "https://modelscope.cn",
            None,
        )
    )

    assert result.resolved_revision == resolved
    assert result.total_size == 1234
    assert result.file_count == 5
    assert calls[1][1:3] == ("master", resolved)


def test_discovers_only_complete_unambiguous_gguf_variants() -> None:
    files = (
        provider_module.SourceFile("README.md", 10),
        provider_module.SourceFile("Q4/model-Q4-00002-of-00002.gguf", 20),
        provider_module.SourceFile("Q4/model-Q4-00001-of-00002.gguf", 15),
        provider_module.SourceFile("Q8/model-Q8.gguf", 40),
    )

    variants = provider_module.discover_gguf_variants(files)

    assert [variant.label for variant in variants] == [
        "Q4/model-Q4.gguf",
        "Q8/model-Q8.gguf",
    ]
    assert variants[0].paths == (
        "Q4/model-Q4-00001-of-00002.gguf",
        "Q4/model-Q4-00002-of-00002.gguf",
    )
    assert variants[0].total_size == 35


@pytest.mark.parametrize(
    "files",
    [
        (
            provider_module.SourceFile("model-00001-of-00003.gguf", 1),
            provider_module.SourceFile("model-00003-of-00003.gguf", 1),
        ),
        (
            provider_module.SourceFile("model.gguf", 1),
            provider_module.SourceFile("model.safetensors", 1),
        ),
    ],
)
def test_rejects_ambiguous_or_dependency_bearing_gguf_repositories(
    files: tuple[provider_module.SourceFile, ...],
) -> None:
    assert provider_module.discover_gguf_variants(files) == ()


def test_auxiliary_gguf_files_do_not_hide_primary_variants() -> None:
    files = (
        provider_module.SourceFile("model.gguf", 10),
        provider_module.SourceFile("mmproj-BF16.gguf", 2),
    )

    variants = provider_module.discover_gguf_variants(files)

    assert len(variants) == 1
    assert variants[0].paths == ("model.gguf",)

    estimate = provider_module.DownloadEstimate(
        Provider.HUGGINGFACE,
        "owner/model",
        "main",
        "a" * 40,
        12,
        2,
        files=files,
    ).as_dict()
    assert estimate["ggufAuxiliaryFiles"] == [{"path": "mmproj-BF16.gguf", "size": 2}]


def test_non_gguf_estimate_exposes_files_for_manual_selection() -> None:
    files = (
        provider_module.SourceFile("README.md", 10),
        provider_module.SourceFile("ple_layer_quant.py", 20),
        provider_module.SourceFile("ples_fp8/META.json", 40),
        provider_module.SourceFile("ples_fp8/shard_0.safetensors", 100),
    )
    estimate = provider_module.DownloadEstimate(
        Provider.HUGGINGFACE,
        "primitive-ai/Qwen3.8-Flash-Next-PLE-quant",
        "main",
        "a" * 40,
        170,
        len(files),
        files=files,
    ).as_dict()

    assert estimate["fileSelectionAvailable"] is True
    assert estimate["ggufVariantSelectionAvailable"] is False
    assert estimate["selectableFiles"] == [
        {"path": "ple_layer_quant.py", "size": 20},
        {"path": "ples_fp8/META.json", "size": 40},
        {"path": "ples_fp8/shard_0.safetensors", "size": 100},
        {"path": "README.md", "size": 10},
    ]


def test_provider_error_detail_keeps_the_cause_and_redacts_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HF_TOKEN", "hf_private_token_value")
    error = RuntimeError(
        "connection failed for https://user:password@proxy.example/path"
        "?access_token=hf_private_token_value"
    )

    detail = provider_module.provider_failure_detail(Provider.HUGGINGFACE, "model search", error)

    assert "RuntimeError" in detail
    assert "connection failed" in detail
    assert "hf_private_token_value" not in detail
    assert "user:password" not in detail
    assert "[redacted]" in detail


def test_worker_process_error_includes_stderr() -> None:
    error = provider_module._worker_process_error(
        "preflight", 2, b"ImportError: provider dependency is broken"
    )

    assert "exit code 2" in str(error)
    assert "ImportError: provider dependency is broken" in str(error)


def test_sdk_downloads_use_a_supervised_worker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict[str, object] = {}

    async def fake_isolated(*args: object, **kwargs: object) -> ProviderResult:
        captured["provider"] = args[0]
        captured["direct"] = kwargs["direct"]
        captured["mirror_url"] = kwargs["mirror_url"]
        return ProviderResult(resolved_revision="a" * 40)

    monkeypatch.setattr(provider_module, "_isolated_download", fake_isolated)

    async def exercise() -> ProviderResult:
        return await run_provider(
            Provider.MODELSCOPE_CN,
            "owner/model",
            "master",
            tmp_path / "artifact",
            lambda _downloaded, _total: asyncio.sleep(0),
            github_token=None,
            mirror_url="https://temporary-modelscope.example",
        )

    result = asyncio.run(exercise())
    assert result.resolved_revision == "a" * 40
    assert captured == {
        "provider": Provider.MODELSCOPE_CN,
        "direct": False,
        "mirror_url": "https://temporary-modelscope.example",
    }


def test_modelscope_git_download_defers_lfs_integrity_to_manifest_hashing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    destination = tmp_path / "artifact"
    payload = b"model weights"
    oid = hashlib.sha256(payload).hexdigest()
    commands: list[list[str]] = []

    monkeypatch.setattr(
        provider_module.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(returncode=0),
    )

    def fake_checkout(*_args: object, **_kwargs: object) -> dict[str, str]:
        destination.mkdir(parents=True)
        (destination / ".git/lfs/objects").mkdir(parents=True)
        (destination / "model.bin").write_text(
            f"version https://git-lfs.github.com/spec/v1\noid sha256:{oid}\nsize {len(payload)}\n",
            encoding="utf-8",
        )
        return {"GIT_LFS_SKIP_SMUDGE": "1"}

    def fake_git(command: list[str], _environment: dict[str, str]) -> str:
        commands.append(command)
        assert command[-2:] == ["lfs", "pull"]
        (destination / "model.bin").write_bytes(payload)
        return ""

    monkeypatch.setattr(provider_module, "_prepare_modelscope_git_checkout", fake_checkout)
    monkeypatch.setattr(provider_module, "_checked_modelscope_git", fake_git)

    git_result = asyncio.run(
        provider_module._download_modelscope_git(
            "owner/model",
            "master",
            "a" * 40,
            destination,
            lambda _downloaded, _total: asyncio.sleep(0),
            "https://modelscope.test",
            None,
        )
    )
    expected, fetched = git_result

    assert expected == {"model.bin": oid}
    assert fetched == ["model.bin"]
    assert len(commands) == 1
    assert not (destination / ".git").exists()


def test_modelscope_git_download_reuses_matching_lfs_files_from_any_prior_selection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    old_payload = b"unchanged model weights"
    new_payload = b"new scale metadata"
    old_oid = hashlib.sha256(old_payload).hexdigest()
    new_oid = hashlib.sha256(new_payload).hexdigest()
    catalog = Catalog(tmp_path / "storage")
    catalog.initialize()
    old_stage = catalog.staging_path("old-selection")
    old_stage.mkdir(parents=True)
    (old_stage / "model.bin").write_bytes(old_payload)
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
    destination = tmp_path / "incremental"
    commands: list[list[str]] = []

    monkeypatch.setattr(
        provider_module.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(returncode=0),
    )

    def fake_checkout(*_args: object, **_kwargs: object) -> dict[str, str]:
        destination.mkdir(parents=True)
        (destination / ".git/lfs/objects").mkdir(parents=True)
        (destination / "model.bin").write_text(
            f"version https://git-lfs.github.com/spec/v1\noid sha256:{old_oid}\n"
            f"size {len(old_payload)}\n",
            encoding="utf-8",
        )
        (destination / "scales.bin").write_text(
            f"version https://git-lfs.github.com/spec/v1\noid sha256:{new_oid}\n"
            f"size {len(new_payload)}\n",
            encoding="utf-8",
        )
        return {"GIT_LFS_SKIP_SMUDGE": "1"}

    def fake_git(command: list[str], _environment: dict[str, str]) -> str:
        commands.append(command)
        if command[4] == "fetch":
            assert command[5:] == ["--include=/scales.bin", "origin", "b" * 40]
        elif command[4] == "checkout":
            assert command[5:] == ["scales.bin"]
            (destination / "scales.bin").write_bytes(new_payload)
        else:
            pytest.fail(f"unexpected Git LFS command: {command}")
        return ""

    clone_methods: list[tuple[Path, Path]] = []

    def fake_clone(
        source_path: Path,
        destination_path: Path,
        *,
        expected_sha256: str | None = None,
    ) -> str:
        assert expected_sha256 == old_oid
        clone_methods.append((source_path, destination_path))
        destination_path.write_bytes(source_path.read_bytes())
        return "reflink"

    monkeypatch.setattr(provider_module, "_prepare_modelscope_git_checkout", fake_checkout)
    monkeypatch.setattr(provider_module, "_checked_modelscope_git", fake_git)
    monkeypatch.setattr(provider_module, "clone_artifact_file", fake_clone)

    git_result = asyncio.run(
        provider_module._download_modelscope_git(
            "owner/model",
            "master",
            "b" * 40,
            destination,
            lambda _downloaded, _total: asyncio.sleep(0),
            "https://modelscope.test",
            None,
            reusable_artifact_roots=[old_root],
        )
    )
    expected, fetched = git_result

    assert expected == {"model.bin": old_oid, "scales.bin": new_oid}
    assert fetched == ["scales.bin"]
    assert (destination / "model.bin").read_bytes() == old_payload
    assert (destination / "scales.bin").read_bytes() == new_payload
    assert [source_path for source_path, _destination_path in clone_methods] == [
        old_root / "model.bin"
    ]
    assert len(commands) == 2
    assert git_result.reuse_stats is not None
    assert git_result.reuse_stats.reflink_file_count == 1
    assert git_result.reuse_stats.reflink_bytes == len(old_payload)
    assert git_result.reuse_stats.source_artifact_ids == (old_manifest.artifact_id,)


def test_kaggle_latest_is_resolved_from_official_cache_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cached = tmp_path / "cache/models/owner/model/framework/variation/versions/7"
    cached.mkdir(parents=True)
    (cached / "model.bin").write_bytes(b"weights")

    def fake_download(handle: str, *, force_download: bool) -> str:
        assert handle == "owner/model/framework/variation"
        assert force_download
        return str(cached)

    monkeypatch.setattr(kagglehub, "model_download", fake_download)
    destination = tmp_path / "stage/content"

    async def run() -> None:
        result = await download_kaggle(
            "owner/model/framework/variation",
            "latest",
            destination,
            lambda _downloaded, _total: asyncio.sleep(0),
        )
        assert result.resolved_revision == "version:7"

    asyncio.run(run())
    assert (destination / "model.bin").read_bytes() == b"weights"


def test_github_release_resolves_release_id_and_downloads_assets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    payload = b"release-asset"

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            if self.path == "/repos/owner/repo/releases/latest":
                body = json.dumps(
                    {
                        "id": 42,
                        "tag_name": "v1.2.3",
                        "html_url": "https://github.test/owner/repo/releases/tag/v1.2.3",
                        "assets": [
                            {
                                "name": "model.gguf",
                                "size": len(payload),
                                "browser_download_url": (
                                    f"http://127.0.0.1:{self.server.server_port}/asset"
                                ),
                            }
                        ],
                    }
                ).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            if self.path == "/asset":
                self.send_response(200)
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
                return
            self.send_error(404)

        def log_message(self, format: str, *args: object) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setenv("GITHUB_API_URL", f"http://127.0.0.1:{server.server_port}")
    progress: list[tuple[int, int | None]] = []

    async def run() -> None:
        result = await download_github_release(
            "owner/repo",
            "latest",
            tmp_path / "destination",
            lambda downloaded, total: _record_progress(progress, downloaded, total),
            None,
        )
        assert result.resolved_revision == "release:42:v1.2.3"

    try:
        asyncio.run(run())
    finally:
        server.shutdown()
        thread.join()
    assert (tmp_path / "destination/model.gguf").read_bytes() == payload
    assert progress[-1] == (len(payload), len(payload))


async def _record_progress(
    progress: list[tuple[int, int | None]], downloaded: int, total: int | None
) -> None:
    progress.append((downloaded, total))


def test_huggingface_model_search_and_revision_discovery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    estimate_endpoints: list[str] = []

    def fake_model_info(
        api: huggingface_hub.HfApi, _source_id: str, **_kwargs: object
    ) -> SimpleNamespace:
        estimate_endpoints.append(str(api.endpoint))
        return SimpleNamespace(
            sha="2" * 40,
            siblings=[
                SimpleNamespace(rfilename="config.json", size=120),
                SimpleNamespace(rfilename="model.safetensors", size=2_000),
            ],
            pipeline_tag="text-generation",
            library_name="transformers",
            last_modified=None,
        )

    monkeypatch.setattr(
        huggingface_hub.HfApi,
        "list_models",
        lambda _self, **_kwargs: [
            SimpleNamespace(id="owner/tiny-model", downloads=42, pipeline_tag="text-generation")
        ],
    )
    monkeypatch.setattr(
        huggingface_hub.HfApi,
        "list_repo_refs",
        lambda _self, _source_id, **_kwargs: SimpleNamespace(
            branches=[
                SimpleNamespace(name="dev", target_commit="1" * 40),
                SimpleNamespace(name="main", target_commit="2" * 40),
            ],
            tags=[SimpleNamespace(name="v1", target_commit="3" * 40)],
        ),
    )
    monkeypatch.setattr(
        huggingface_hub.HfApi,
        "model_info",
        fake_model_info,
    )

    async def run() -> None:
        models = await search_models(Provider.HUGGINGFACE, "tiny", github_token=None)
        assert models.models[0].id == "owner/tiny-model"
        assert models.models[0].detail == "text-generation · 42 downloads"
        revisions = await discover_revisions(
            Provider.HUGGINGFACE, "owner/tiny-model", github_token=None
        )
        assert revisions.default_revision == "main"
        assert [revision.name for revision in revisions.revisions] == ["main", "dev", "v1"]
        assert revisions.revisions[0].resolved_revision == "2" * 40
        estimate = await estimate_download(
            Provider.HUGGINGFACE,
            "owner/tiny-model",
            "main",
            github_token=None,
        )
        assert estimate.resolved_revision == "2" * 40
        assert estimate.total_size == 2_120
        assert estimate.file_count == 2
        assert estimate.as_dict()["downloadable"] is True
        mirrored = await estimate_download(
            Provider.HUGGINGFACE,
            "owner/tiny-model",
            "main",
            github_token=None,
            huggingface_mirror="https://mirror.example",
        )
        direct = await estimate_download(
            Provider.HUGGINGFACE,
            "owner/tiny-model",
            "main",
            github_token=None,
            huggingface_mirror="https://mirror.example",
            disable_mirror=True,
        )
        temporary = await estimate_download(
            Provider.HUGGINGFACE,
            "owner/tiny-model",
            "main",
            github_token=None,
            huggingface_mirror="https://mirror.example",
            mirror_url="https://temporary-mirror.example",
        )
        assert mirrored.hub_url == "https://huggingface.co/owner/tiny-model/tree/main"
        assert direct.hub_url == mirrored.hub_url
        assert temporary.hub_url == mirrored.hub_url

    asyncio.run(run())
    assert estimate_endpoints == [
        "https://huggingface.co",
        "https://mirror.example",
        "https://huggingface.co",
        "https://temporary-mirror.example",
    ]


def test_generic_http_discovery_is_explicitly_unsupported() -> None:
    async def run() -> None:
        models = await search_models(Provider.HTTP, "https://example.test", github_token=None)
        revisions = await discover_revisions(
            Provider.HTTP, "https://example.test/model.tar", github_token=None
        )
        assert not models.supports_search
        assert not revisions.supports_discovery
        assert revisions.default_revision == "content"

    asyncio.run(run())


def test_sdk_proxy_bypass_uses_an_isolated_provider_process(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    async def fake_isolated(
        provider: Provider,
        source_id: str,
        revision: str,
        **network: object,
    ) -> provider_module.DownloadEstimate:
        captured.update(network)
        return provider_module.DownloadEstimate(provider, source_id, revision, "a" * 40, 100, 1)

    monkeypatch.setattr(provider_module, "_isolated_estimate", fake_isolated)

    async def run() -> None:
        result = await estimate_download(
            Provider.HUGGINGFACE,
            "owner/model",
            "main",
            github_token=None,
            proxy_url="http://proxy.internal:3128",
            disable_proxy=True,
        )
        assert result.total_size == 100

    asyncio.run(run())
    assert captured["disable_mirror"] is False
