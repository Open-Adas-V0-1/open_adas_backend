"""Async client for the MCP server bundled in daltskin/sysml-v2-lsp.

NEWLINE-delimited JSON-RPC over stdio — a DIFFERENT framing than the LSP client's
Content-Length headers, and a different server process/protocol entirely (confirmed by
the spike). Calls the `preview` tool to derive a Mermaid diagram from SysML v2 text.

The `preview` tool's response is written for an LLM caller: its first content block
tells the model to call a `renderMermaidDiagram` tool next. We ignore that — the second
content block already contains the Mermaid markup as JSON (`mermaidMarkup`), which is
all this client reads.
"""
from __future__ import annotations

import asyncio
import json
from typing import Any

from tools.sysml_v2.paths import resolve_mcp_server_path, resolve_node_bin, resolve_timeout


class SysmlMcpClient:
    def __init__(self):
        self._proc: asyncio.subprocess.Process | None = None
        self._id = 0
        self._lock = asyncio.Lock()

    async def _ensure_started(self) -> None:
        if self._proc is not None and self._proc.returncode is None:
            return
        server_path = resolve_mcp_server_path()
        node_bin = resolve_node_bin()
        self._proc = await asyncio.create_subprocess_exec(
            node_bin, str(server_path),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        await self._initialize()

    async def _request(self, method: str, params: dict[str, Any] | None, timeout: float) -> dict[str, Any]:
        self._id += 1
        req_id = self._id
        msg: dict[str, Any] = {"jsonrpc": "2.0", "id": req_id, "method": method}
        if params is not None:
            msg["params"] = params
        line = (json.dumps(msg) + "\n").encode("utf-8")
        self._proc.stdin.write(line)
        await self._proc.stdin.drain()

        async def _wait() -> dict[str, Any]:
            while True:
                raw = await self._proc.stdout.readline()
                if not raw:
                    raise RuntimeError("MCP server closed the connection")
                resp = json.loads(raw.decode("utf-8"))
                if resp.get("id") == req_id:
                    return resp

        return await asyncio.wait_for(_wait(), timeout=timeout)

    async def _notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        msg: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            msg["params"] = params
        line = (json.dumps(msg) + "\n").encode("utf-8")
        self._proc.stdin.write(line)
        await self._proc.stdin.drain()

    async def _initialize(self) -> None:
        await self._request(
            "initialize",
            {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "open-adas-backend", "version": "0.1"},
            },
            timeout=resolve_timeout(),
        )
        await self._notify("notifications/initialized")

    async def to_mermaid(self, sysml_text: str, uri: str = "preview.sysml") -> str:
        """Call the 'preview' MCP tool and return the derived Mermaid markup."""
        async with self._lock:
            await self._ensure_started()
            resp = await self._request(
                "tools/call",
                {"name": "preview", "arguments": {"code": sysml_text, "uri": uri}},
                timeout=resolve_timeout(),
            )
            if "error" in resp:
                raise RuntimeError(f"MCP preview tool error: {resp['error']}")

            content = resp.get("result", {}).get("content", [])
            for block in content:
                text = block.get("text", "")
                try:
                    parsed = json.loads(text)
                except json.JSONDecodeError:
                    continue
                if isinstance(parsed, dict) and "mermaidMarkup" in parsed:
                    return parsed["mermaidMarkup"]

            raise RuntimeError(
                "MCP preview tool response contained no 'mermaidMarkup' field "
                f"(got {len(content)} content block(s))"
            )

    async def aclose(self) -> None:
        if self._proc is not None and self._proc.returncode is None:
            self._proc.kill()
            await self._proc.wait()
        self._proc = None


_client: SysmlMcpClient | None = None


def get_mcp_client() -> SysmlMcpClient:
    global _client
    if _client is None:
        _client = SysmlMcpClient()
    return _client


async def to_mermaid(sysml_text: str, uri: str = "preview.sysml") -> str:
    return await get_mcp_client().to_mermaid(sysml_text, uri=uri)


async def shutdown_mcp_client() -> None:
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None
