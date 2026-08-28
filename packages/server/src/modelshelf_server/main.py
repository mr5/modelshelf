from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from getpass import getpass
from pathlib import Path

import uvicorn
from modelshelf_core import Catalog, VerificationError

from .config import Settings
from .filesystem_import import allowed_import_roots, import_filesystem
from .password_hash import generate_password_hash


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="modelshelf-server")
    commands = parser.add_subparsers(dest="command")
    commands.add_parser("serve", help="run the ModelShelf HTTP server")
    import_parser = commands.add_parser(
        "import", help="copy a server-local file or directory into the immutable shelf"
    )
    import_parser.add_argument("source", type=Path)
    import_parser.add_argument("--name", help="display name; inferred from the source by default")
    import_parser.add_argument("--version", help="human version; inferred when possible")
    import_parser.add_argument("--format", dest="format_name", help="model format override")
    import_parser.add_argument(
        "--id", dest="source_id", help="stable filesystem source ID; defaults to the model name"
    )
    import_parser.add_argument(
        "--extract", action="store_true", help="safely extract a supported zip/tar archive"
    )
    password_parser = commands.add_parser(
        "hash-password", help="generate an Argon2id hash for the ModelShelf web password"
    )
    password_parser.add_argument(
        "--stdin",
        action="store_true",
        dest="read_stdin",
        help="read one password line from stdin without confirmation (for automation)",
    )
    return parser


def run_import(arguments: argparse.Namespace, settings: Settings) -> dict[str, object]:
    storage_root = settings.storage_root.resolve()
    catalog = Catalog(storage_root)
    catalog.initialize()
    roots = allowed_import_roots(catalog, settings.import_roots)
    result = import_filesystem(
        catalog,
        arguments.source,
        roots=roots,
        name=arguments.name,
        version=arguments.version,
        format_name=arguments.format_name,
        source_id=arguments.source_id,
        extract=arguments.extract,
    )
    return {
        "artifactId": result.manifest.artifact_id,
        "contentSha256": result.manifest.content_sha256,
        "resolvedRevision": result.manifest.source.resolved_revision,
        "fileCount": result.manifest.file_count,
        "totalSize": result.manifest.total_size,
        "relativePath": result.destination.relative_to(catalog.artifacts_root).as_posix(),
        "deduplicated": result.deduplicated,
    }


def read_password(*, read_stdin: bool) -> str:
    if read_stdin:
        return sys.stdin.readline().removesuffix("\n").removesuffix("\r")
    if not sys.stdin.isatty():
        raise ValueError("stdin is not a terminal; use --stdin to read one password line")
    first = getpass("Password: ")
    second = getpass("Confirm password: ")
    if not first:
        raise ValueError("password must not be empty")
    if first != second:
        raise ValueError("passwords do not match")
    return first


def run(argv: Sequence[str] | None = None) -> None:
    parser = build_parser()
    arguments = parser.parse_args(argv)
    if arguments.command == "hash-password":
        try:
            password = read_password(read_stdin=arguments.read_stdin)
            print(generate_password_hash(password))
        except ValueError as error:
            parser.error(str(error))
        return
    settings = Settings()
    if arguments.command == "import":
        try:
            print(f"Importing {arguments.source} into staging…", file=sys.stderr, flush=True)
            result = run_import(arguments, settings)
        except (OSError, ValueError, VerificationError) as error:
            parser.error(str(error))
        print(json.dumps(result, indent=2))
        return
    uvicorn.run(
        "modelshelf_server.app:create_app",
        factory=True,
        host=settings.host,
        port=settings.port,
    )


if __name__ == "__main__":
    run()
