"""Async client for the standard LSP server bundled in daltskin/sysml-v2-lsp.

Content-Length-framed JSON-RPC over stdio — the same protocol VS Code speaks. Targets
Linux (asyncio subprocess streams work the same on POSIX and Windows; unlike the
sync spike client, this never touches the stdlib `select` module, which is what broke
on Windows there).

The server process is started lazily and kept alive across calls (its DFA warms up on
the first parse, ~20s per the spike; subsequent parses in the same process are fast).
"""
from __future__ import annotations

import asyncio
import itertools
import json
from pathlib import Path
from typing import Any

from tools.sysml_v2.diagnostics import Diagnostic
from tools.sysml_v2.paths import resolve_lsp_server_path, resolve_node_bin, resolve_timeout

_uri_counter = itertools.count(1)


class SysmlLspClient:
    def __init__(self):
        self._proc: asyncio.subprocess.Process | None = None
        self._id = 0
        self._lock = asyncio.Lock()

    async def _ensure_started(self) -> None:
        if self._proc is not None and self._proc.returncode is None:
            return
        server_path = resolve_lsp_server_path()
        node_bin = resolve_node_bin()
        self._proc = await asyncio.create_subprocess_exec(
            node_bin, str(server_path), "--stdio",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        await self._initialize()

    async def _send(self, msg: dict[str, Any]) -> None:
        body = json.dumps(msg).encode("utf-8")
        header = f"Content-Length: {len(body)}\r\n\r\n".encode("utf-8")
        self._proc.stdin.write(header + body)
        await self._proc.stdin.drain()

    async def _read_message(self, timeout: float) -> dict[str, Any] | None:
        async def _read() -> dict[str, Any] | None:
            headers: dict[str, str] = {}
            while True:
                line = await self._proc.stdout.readline()
                if not line or line == b"\r\n":
                    break
                key, _, val = line.decode("utf-8").partition(":")
                headers[key.strip().lower()] = val.strip()
            length = int(headers.get("content-length", 0))
            if length == 0:
                return None
            body = await self._proc.stdout.readexactly(length)
            return json.loads(body.decode("utf-8"))

        return await asyncio.wait_for(_read(), timeout=timeout)

    async def _request(self, method: str, params: dict[str, Any] | None, timeout: float) -> dict[str, Any]:
        self._id += 1
        req_id = self._id
        msg: dict[str, Any] = {"jsonrpc": "2.0", "id": req_id, "method": method}
        if params is not None:
            msg["params"] = params
        await self._send(msg)

        while True:
            resp = await self._read_message(timeout)
            if resp is None:
                raise RuntimeError("LSP server closed the connection")
            if resp.get("id") == req_id:
                return resp
            # notifications while waiting are just dropped here; validate() drains
            # publishDiagnostics itself via _wait_for_diagnostics

    async def _notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        msg: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            msg["params"] = params
        await self._send(msg)

    async def _initialize(self) -> None:
        root_uri = Path.cwd().resolve().as_uri()
        await self._request(
            "initialize",
            {
                "processId": None,
                "rootUri": root_uri,
                "capabilities": {
                    "textDocument": {"publishDiagnostics": {"relatedInformation": True}},
                },
                "workspaceFolders": [{"uri": root_uri, "name": "workspace"}],
            },
            timeout=resolve_timeout(),
        )
        await self._notify("initialized")

    async def validate(self, sysml_text: str) -> list[Diagnostic]:
        """Open *sysml_text* as a throwaway document and return its diagnostics."""
        async with self._lock:
            await self._ensure_started()
            uri = f"file:///virtual/verify-{next(_uri_counter)}.sysml"
            await self._notify("textDocument/didOpen", {
                "textDocument": {
                    "uri": uri, "languageId": "sysml", "version": 1, "text": sysml_text,
                },
            })
            diagnostics = await self._wait_for_diagnostics(uri, timeout=resolve_timeout())
            await self._notify("textDocument/didClose", {"textDocument": {"uri": uri}})
            return [Diagnostic.from_lsp(d) for d in diagnostics]

    async def _wait_for_diagnostics(self, uri: str, timeout: float) -> list[dict[str, Any]]:
        async def _wait() -> list[dict[str, Any]]:
            while True:
                headers: dict[str, str] = {}
                while True:
                    line = await self._proc.stdout.readline()
                    if not line or line == b"\r\n":
                        break
                    key, _, val = line.decode("utf-8").partition(":")
                    headers[key.strip().lower()] = val.strip()
                length = int(headers.get("content-length", 0))
                if length == 0:
                    raise RuntimeError("LSP server closed the connection")
                body = await self._proc.stdout.readexactly(length)
                msg = json.loads(body.decode("utf-8"))
                if msg.get("method") == "textDocument/publishDiagnostics":
                    params = msg.get("params", {})
                    if params.get("uri") == uri:
                        return params.get("diagnostics", [])

        return await asyncio.wait_for(_wait(), timeout=timeout)

    async def aclose(self) -> None:
        if self._proc is not None and self._proc.returncode is None:
            try:
                await self._request("shutdown", None, timeout=5.0)
                await self._notify("exit")
            except Exception:
                pass
            self._proc.kill()
            await self._proc.wait()
        self._proc = None


_client: SysmlLspClient | None = None


def get_lsp_client() -> SysmlLspClient:
    global _client
    if _client is None:
        _client = SysmlLspClient()
    return _client


async def validate(sysml_text: str) -> list[Diagnostic]:
    return await get_lsp_client().validate(sysml_text)


async def shutdown_lsp_client() -> None:
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None
