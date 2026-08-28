from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Any

from modelshelf_core import Provider

from .providers import (
    _WORKER_PREFIX,
    ProviderRequestError,
    ProviderUnavailable,
    estimate_download,
    run_provider,
    safe_error_message,
)


def _emit(payload: dict[str, Any]) -> None:
    print(f"{_WORKER_PREFIX}{json.dumps(payload, separators=(',', ':'))}", flush=True)


async def _main(payload: dict[str, Any]) -> None:
    provider = Provider(payload["provider"])
    common: dict[str, Any] = {
        "github_token": payload.get("githubToken"),
        "huggingface_mirror": payload.get("huggingfaceMirror"),
        "modelscope_cn_mirror": payload.get("modelscopeCnMirror"),
        "modelscope_ai_mirror": payload.get("modelscopeAiMirror"),
        "proxy_url": None,
        "disable_mirror": bool(payload.get("disableMirror")),
        "disable_proxy": False,
        "_isolated": True,
    }
    if payload["operation"] == "estimate":
        estimate = await estimate_download(
            provider,
            payload["sourceId"],
            payload["revision"],
            **common,
        )
        _emit({"type": "result", "estimate": estimate.as_dict()})
        return

    async def progress(downloaded: int, total: int | None) -> None:
        _emit({"type": "progress", "downloaded": downloaded, "total": total})

    result = await run_provider(
        provider,
        payload["sourceId"],
        payload["revision"],
        Path(payload["destination"]),
        progress,
        **common,
    )
    _emit(
        {
            "type": "result",
            "result": {
                "resolvedRevision": result.resolved_revision,
                "sourceUrl": result.source_url,
                "downloadedFile": result.downloaded_file,
                "contentDisposition": result.content_disposition,
            },
        }
    )


def main() -> None:
    payload: dict[str, Any] = {}
    try:
        decoded = json.loads(sys.stdin.read())
        if not isinstance(decoded, dict):
            raise ValueError("provider worker input must be a JSON object")
        payload = decoded
        asyncio.run(_main(payload))
    except Exception as error:
        status_code = getattr(error, "status_code", None)
        if status_code is None:
            status_code = getattr(getattr(error, "response", None), "status_code", None)
        kind = (
            "ValueError"
            if isinstance(error, ValueError)
            else "ProviderUnavailable"
            if isinstance(error, ProviderUnavailable)
            else "ProviderRequestError"
            if isinstance(error, ProviderRequestError)
            else type(error).__name__
        )
        _emit(
            {
                "type": "error",
                "errorKind": kind,
                "message": safe_error_message(
                    error,
                    secrets=(
                        str(payload.get("githubToken") or ""),
                        str(payload.get("proxyUrl") or ""),
                    ),
                ),
                "statusCode": status_code,
            }
        )
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
