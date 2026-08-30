from __future__ import annotations

import os
import re
from importlib.metadata import PackageNotFoundError, version


def _display_version(distribution_version: str) -> str:
    beta = re.fullmatch(r"(\d+\.\d+\.\d+)b(\d+)", distribution_version)
    if beta is not None:
        return f"{beta.group(1)}-beta.{beta.group(2)}"
    return distribution_version


if build_version := os.getenv("MODELSHELF_VERSION"):
    __version__ = build_version
else:
    try:
        __version__ = _display_version(version("modelshelf-server"))
    except PackageNotFoundError:
        __version__ = "dev"
