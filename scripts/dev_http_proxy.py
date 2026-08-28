from __future__ import annotations

import argparse
import asyncio
from contextlib import suppress
from urllib.parse import urlsplit


async def copy_stream(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        while data := await reader.read(64 * 1024):
            writer.write(data)
            await writer.drain()
    finally:
        with suppress(Exception):
            writer.close()
            await writer.wait_closed()


async def handle_client(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        request_line = await reader.readline()
        if not request_line:
            return
        method, target, version = request_line.decode("latin-1").rstrip("\r\n").split(" ", 2)
        headers: list[bytes] = []
        host_header = ""
        while True:
            line = await reader.readline()
            if line in {b"", b"\r\n"}:
                break
            name, _, value = line.decode("latin-1").partition(":")
            if name.casefold() == "host":
                host_header = value.strip()
            if name.casefold() != "proxy-connection":
                headers.append(line)

        if method.upper() == "CONNECT":
            host, _, port_text = target.rpartition(":")
            upstream_reader, upstream_writer = await asyncio.open_connection(
                host, int(port_text or "443")
            )
            writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            await writer.drain()
            await asyncio.gather(
                copy_stream(reader, upstream_writer),
                copy_stream(upstream_reader, writer),
            )
            return

        parsed = urlsplit(target)
        host = parsed.hostname or host_header.split(":", 1)[0]
        port = parsed.port or 80
        path = parsed.path or "/"
        if parsed.query:
            path += f"?{parsed.query}"
        upstream_reader, upstream_writer = await asyncio.open_connection(host, port)
        upstream_writer.write(f"{method} {path} {version}\r\n".encode("latin-1"))
        for header in headers:
            upstream_writer.write(header)
        upstream_writer.write(b"\r\n")
        await upstream_writer.drain()
        await asyncio.gather(
            copy_stream(reader, upstream_writer),
            copy_stream(upstream_reader, writer),
        )
    except Exception:
        with suppress(Exception):
            writer.write(b"HTTP/1.1 502 Bad Gateway\r\nContent-Length: 0\r\n\r\n")
            await writer.drain()
    finally:
        with suppress(Exception):
            writer.close()
            await writer.wait_closed()


async def main() -> None:
    parser = argparse.ArgumentParser(description="Local development HTTP CONNECT proxy")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=18080)
    args = parser.parse_args()
    server = await asyncio.start_server(handle_client, args.host, args.port)
    print(f"Development proxy listening on http://{args.host}:{args.port}", flush=True)
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    asyncio.run(main())
