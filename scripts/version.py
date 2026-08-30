#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DISPLAY_VERSION = re.compile(r"\d+\.\d+\.\d+-beta\.\d+")
PYTHON_PROJECTS = (
    ROOT / "pyproject.toml",
    ROOT / "packages/core/pyproject.toml",
    ROOT / "packages/server/pyproject.toml",
)
NODE_PROJECTS = (ROOT / "package.json", ROOT / "packages/ui/package.json")


def python_version(display_version: str) -> str:
    match = re.fullmatch(r"(\d+\.\d+\.\d+)-beta\.(\d+)", display_version)
    if match is None:
        raise ValueError(f"unsupported version: {display_version}")
    return f"{match.group(1)}b{match.group(2)}"


def replace_once(path: Path, pattern: str, replacement: str) -> None:
    content = path.read_text(encoding="utf-8")
    updated, count = re.subn(pattern, replacement, content, count=1, flags=re.MULTILINE)
    if count != 1:
        raise RuntimeError(f"could not update version in {path.relative_to(ROOT)}")
    path.write_text(updated, encoding="utf-8")


def set_version(display_version: str) -> None:
    pep440_version = python_version(display_version)
    (ROOT / "VERSION").write_text(f"{display_version}\n", encoding="utf-8")
    for path in PYTHON_PROJECTS:
        replace_once(path, r'^version = "[^"]+"$', f'version = "{pep440_version}"')
    for path in NODE_PROJECTS:
        replace_once(path, r'^  "version": "[^"]+",$', f'  "version": "{display_version}",')
    replace_once(
        ROOT / "Dockerfile",
        r"^ARG MODELSHELF_VERSION=[^\s]+$",
        f"ARG MODELSHELF_VERSION={display_version}",
    )
    subprocess.run(["uv", "lock"], cwd=ROOT, check=True)


def check_version(expected_tag: str | None) -> None:
    display_version = (ROOT / "VERSION").read_text(encoding="utf-8").strip()
    if DISPLAY_VERSION.fullmatch(display_version) is None:
        raise RuntimeError(f"VERSION has unsupported value: {display_version}")
    pep440_version = python_version(display_version)
    errors: list[str] = []

    for path in PYTHON_PROJECTS:
        actual = tomllib.loads(path.read_text(encoding="utf-8"))["project"]["version"]
        if actual != pep440_version:
            errors.append(f"{path.relative_to(ROOT)}: {actual} != {pep440_version}")
    for path in NODE_PROJECTS:
        actual = json.loads(path.read_text(encoding="utf-8"))["version"]
        if actual != display_version:
            errors.append(f"{path.relative_to(ROOT)}: {actual} != {display_version}")

    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    docker_match = re.search(r"^ARG MODELSHELF_VERSION=([^\s]+)$", dockerfile, re.MULTILINE)
    docker_version = docker_match.group(1) if docker_match is not None else None
    if docker_version != display_version:
        errors.append(f"Dockerfile: {docker_version} != {display_version}")

    lock_packages = tomllib.loads((ROOT / "uv.lock").read_text(encoding="utf-8"))["package"]
    workspace_names = {"modelshelf-workspace", "modelshelf-core", "modelshelf-server"}
    locked = {
        package["name"]: package["version"]
        for package in lock_packages
        if package["name"] in workspace_names
    }
    for name in sorted(workspace_names):
        if locked.get(name) != pep440_version:
            errors.append(f"uv.lock {name}: {locked.get(name)} != {pep440_version}")

    if expected_tag is not None and expected_tag.removeprefix("v") != display_version:
        errors.append(f"release tag: {expected_tag} != v{display_version}")

    if errors:
        raise RuntimeError("version mismatch:\n- " + "\n- ".join(errors))
    print(f"version consistency check passed: {display_version}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Set or verify the ModelShelf release version")
    parser.add_argument("--set", dest="new_version", help="set every generated version field")
    parser.add_argument("--tag", help="also require this release tag to match VERSION")
    args = parser.parse_args()
    try:
        if args.new_version is not None:
            set_version(args.new_version)
        check_version(args.tag)
    except (KeyError, OSError, RuntimeError, subprocess.CalledProcessError, ValueError) as error:
        print(error, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
