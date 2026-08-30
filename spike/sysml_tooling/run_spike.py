#!/usr/bin/env python3
"""
Spike driver: prove we can drive the free SysML v2 tooling (daltskin/sysml-v2-lsp)
from Python for (1) diagnostics and (2) Mermaid diagram generation.

Two DIFFERENT servers/protocols are involved, both bundled in the npm package:
  - dist/server/server.js    -> standard LSP over stdio, Content-Length framed.
                                 Used for validation/diagnostics.
  - dist/server/mcpServer.js -> Model Context Protocol (MCP) server over stdio,
                                 NEWLINE-delimited JSON (different framing!).
                                 The Mermaid "preview" tool only lives here.

Run: python run_spike.py
"""
from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
LSP_SERVER = HERE / "node_modules" / "sysml-v2-lsp" / "dist" / "server" / "server.js"
MCP_SERVER = HERE / "node_modules" / "sysml-v2-lsp" / "dist" / "server" / "mcpServer.js"
SAMPLES = HERE / "samples"


# ===========================================================================
# Part A — LSP client (Content-Length framed JSON-RPC), for diagnostics
# ===========================================================================

class LspClient:
    def __init__(self, proc: subprocess.Popen):
        self._proc = proc
        self._id = 0
        self._diagnostics: dict[str, list[dict[str, Any]]] = {}

    def _send(self, msg: dict[str, Any]) -> None:
        body = json.dumps(msg)
        header = f"Content-Length: {len(body)}\r\n\r\n"
        self._proc.stdin.write(header.encode("utf-8"))
        self._proc.stdin.write(body.encode("utf-8"))
        self._proc.stdin.flush()

    def request(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        self._id += 1
        msg: dict[str, Any] = {"jsonrpc": "2.0", "id": self._id, "method": method}
        if params is not None:
            msg["params"] = params
        self._send(msg)
        return self._wait_for_response(self._id)

    def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        msg: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            msg["params"] = params
        self._send(msg)

    def _read_headers(self) -> dict[str, str]:
        headers: dict[str, str] = {}
        while True:
            line = self._proc.stdout.readline()
            if not line or line == b"\r\n":
                break
            key, _, val = line.decode("utf-8").partition(":")
            headers[key.strip().lower()] = val.strip()
        return headers

    def _read_message(self) -> dict[str, Any] | None:
        headers = self._read_headers()
        length = int(headers.get("content-length", 0))
        if length == 0:
            return None
        body = self._proc.stdout.read(length)
        return json.loads(body.decode("utf-8"))

    def _wait_for_response(self, req_id: int) -> dict[str, Any]:
        while True:
            msg = self._read_message()
            if msg is None:
                raise RuntimeError("LSP server closed the connection")
            if "id" in msg and msg["id"] == req_id:
                return msg
            if "method" in msg:
                self._handle_notification(msg)

    def drain_until_diagnostics(self, uri: str, timeout: float = 30.0) -> None:
        # NOTE: the reference Python client (clients/python/sysml_lsp_client.py) uses
        # select.select() on self._proc.stdout here, which works on POSIX but raises
        # OSError [WinError 10093] on Windows — select() there only supports sockets,
        # not pipes. Blocking reads work fine since we know a publishDiagnostics
        # notification always follows didOpen; a background timer thread enforces the
        # timeout by killing the process if the server never responds.
        import threading

        timed_out = threading.Event()
        timer = threading.Timer(timeout, lambda: (timed_out.set(), self._proc.kill()))
        timer.start()
        try:
            while not timed_out.is_set():
                msg = self._read_message()
                if msg is None:
                    break
                if "method" in msg:
                    self._handle_notification(msg)
                if msg.get("method") == "textDocument/publishDiagnostics":
                    if msg.get("params", {}).get("uri") == uri:
                        break
        finally:
            timer.cancel()

    def _handle_notification(self, msg: dict[str, Any]) -> None:
        if msg.get("method") == "textDocument/publishDiagnostics":
            uri = msg["params"]["uri"]
            self._diagnostics[uri] = msg["params"]["diagnostics"]

    def get_diagnostics(self, uri: str) -> list[dict[str, Any]]:
        return self._diagnostics.get(uri, [])


SEVERITY = {1: "Error", 2: "Warning", 3: "Info", 4: "Hint"}


def file_uri(path: Path) -> str:
    return path.resolve().as_uri()


def run_lsp_diagnostics(files: list[Path]) -> None:
    print("=" * 72)
    print("PART 1 — Diagnostics via the standard LSP (Content-Length framed)")
    print("=" * 72)
    print(f"Server: {LSP_SERVER.relative_to(HERE)}\n")

    proc = subprocess.Popen(
        ["node", str(LSP_SERVER), "--stdio"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    client = LspClient(proc)

    try:
        init_result = client.request("initialize", {
            "processId": None,
            "rootUri": file_uri(HERE),
            "capabilities": {"textDocument": {"publishDiagnostics": {"relatedInformation": True}}},
            "workspaceFolders": [{"uri": file_uri(HERE), "name": HERE.name}],
        })
        caps = sorted(k for k, v in init_result.get("result", {}).get("capabilities", {}).items() if v)
        print(f"Server capabilities: {', '.join(caps)}\n")
        client.notify("initialized")

        for path in files:
            uri = file_uri(path)
            text = path.read_text(encoding="utf-8")
            client.notify("textDocument/didOpen", {
                "textDocument": {"uri": uri, "languageId": "sysml", "version": 1, "text": text},
            })
            # First file triggers DFA warm-up (~20s); give it room.
            client.drain_until_diagnostics(uri, timeout=30.0)

            diags = client.get_diagnostics(uri)
            print("-" * 72)
            print(f"FILE: {path.name}")
            print("-" * 72)
            if not diags:
                print("  CLEAN — no diagnostics reported.\n")
                continue

            print(f"  {len(diags)} diagnostic(s):\n")
            for d in diags:
                sev = SEVERITY.get(d.get("severity", 1), str(d.get("severity")))
                start = d["range"]["start"]
                end = d["range"]["end"]
                print(f"  [{sev}] line {start['line'] + 1}, col {start['character'] + 1} "
                      f"-> line {end['line'] + 1}, col {end['character'] + 1}")
                print(f"      message : {d.get('message')}")
                print(f"      code    : {d.get('code')}")
                print(f"      source  : {d.get('source')}")
                print(f"      raw     : {json.dumps(d)}")
                print()

        client.request("shutdown")
        client.notify("exit")
    finally:
        proc.wait(timeout=5)


# ===========================================================================
# Part B — MCP client (newline-delimited JSON), for Mermaid diagram derivation
# ===========================================================================

class McpClient:
    def __init__(self, proc: subprocess.Popen):
        self._proc = proc
        self._id = 0

    def request(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        self._id += 1
        msg: dict[str, Any] = {"jsonrpc": "2.0", "id": self._id, "method": method}
        if params is not None:
            msg["params"] = params
        line = (json.dumps(msg) + "\n").encode("utf-8")
        self._proc.stdin.write(line)
        self._proc.stdin.flush()
        while True:
            raw = self._proc.stdout.readline()
            if not raw:
                raise RuntimeError("MCP server closed the connection")
            resp = json.loads(raw.decode("utf-8"))
            if resp.get("id") == self._id:
                return resp
            # else: a notification — ignore for this spike

    def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        msg: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            msg["params"] = params
        line = (json.dumps(msg) + "\n").encode("utf-8")
        self._proc.stdin.write(line)
        self._proc.stdin.flush()


def run_mermaid_preview(valid_file: Path) -> None:
    print("=" * 72)
    print("PART 2 — Mermaid diagram via the MCP server (NEWLINE-delimited JSON)")
    print("=" * 72)
    print(f"Server: {MCP_SERVER.relative_to(HERE)}")
    print("NOTE: this is a DIFFERENT protocol/server than Part 1 — Mermaid generation")
    print("      (mermaidGenerator.ts) is only wired into the MCP 'preview' tool,")
    print("      not exposed as a standard LSP method.\n")

    proc = subprocess.Popen(
        ["node", str(MCP_SERVER)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    client = McpClient(proc)

    try:
        init = client.request("initialize", {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "spike-client", "version": "0.1"},
        })
        server_info = init.get("result", {}).get("serverInfo", {})
        print(f"MCP server: {server_info.get('name')} v{server_info.get('version')}\n")
        client.notify("notifications/initialized")

        code = valid_file.read_text(encoding="utf-8")
        result = client.request("tools/call", {
            "name": "preview",
            "arguments": {"code": code, "uri": valid_file.name},
        })

        if "error" in result:
            print(f"MCP ERROR: {result['error']}")
            return

        content = result.get("result", {}).get("content", [])
        print(f"tools/call returned {len(content)} content block(s).\n")

        data = None
        for block in content:
            print(f"--- content block (type={block.get('type')}) ---")
            text = block.get("text", "")
            print(text[:500] + ("..." if len(text) > 500 else ""))
            print()
            try:
                parsed = json.loads(text)
                if isinstance(parsed, dict) and "mermaidMarkup" in parsed:
                    data = parsed
            except json.JSONDecodeError:
                pass

        if data is None:
            print("Could not find a 'mermaidMarkup' field in any content block.")
            return

        print("=" * 72)
        print(f"MERMAID DIAGRAM (title: {data.get('title')})")
        print("=" * 72)
        print(data["mermaidMarkup"])

    finally:
        proc.kill()
        proc.wait(timeout=5)


def main() -> None:
    if not LSP_SERVER.exists():
        print(f"ERROR: LSP server not found at {LSP_SERVER}")
        print("Run 'npm install sysml-v2-lsp' in this directory first.")
        sys.exit(1)
    if not MCP_SERVER.exists():
        print(f"ERROR: MCP server not found at {MCP_SERVER}")
        sys.exit(1)

    files = [SAMPLES / "valid.sysml", SAMPLES / "invalid.sysml"]
    for f in files:
        if not f.exists():
            print(f"ERROR: sample file missing: {f}")
            sys.exit(1)

    run_lsp_diagnostics(files)
    print()
    run_mermaid_preview(SAMPLES / "valid.sysml")


if __name__ == "__main__":
    main()
