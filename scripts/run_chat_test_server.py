"""Test-only server launcher for scripts/smoke_test_chat_turn.py.

WHY THIS EXISTS (a real, pre-existing Windows constraint, hit for the first time by
the actual running app in T6b Step 3a, not introduced by it): AsyncPostgresSaver (the
production checkpointer, attached in app/main.py's lifespan) needs psycopg's async
mode, which REQUIRES SelectorEventLoop on Windows. agents.sysml.nodes.validate /
.to_mermaid spawn the REAL SysML v2 LSP/MCP tooling via asyncio.create_subprocess_exec,
which REQUIRES ProactorEventLoop on Windows. A single Windows process cannot run
under both loop types at once -- this is a hard asyncio/Windows limitation, not a bug
in this project. Every smoke test in this repo already navigates this by stubbing
validate/to_mermaid whenever it needs a REAL AsyncPostgresSaver (see e.g.
scripts/smoke_test_level_resolution.py's docstring) -- this script does the SAME
thing, just applied at server-PROCESS startup (patching the module-level functions
BEFORE importing/starting the app) rather than per-test-function, since here the
"caller" is a long-running HTTP server, not a single test function.

This is NOT how app.main:app is run in production/Linux-Docker (where a single event
loop supports both subprocesses and async Postgres, so no such stubbing is needed or
present anywhere in app/ or agents/ or tools/) -- production code is completely
unmodified. Real tool integration (the thing being stubbed here) is exercised
elsewhere, off a MemorySaver, per scripts/smoke_test_layer3_rebuild.py.

Run: python scripts/run_chat_test_server.py
"""
import asyncio
import sys
from unittest.mock import patch

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

import uvicorn  # noqa: E402


async def _fake_validate(text: str):
    return []


async def _fake_to_mermaid(text: str, uri: str = "preview.sysml") -> str:
    return "graph TD; A-->B;"


if __name__ == "__main__":
    with patch("agents.sysml.nodes.validate", side_effect=_fake_validate), \
         patch("agents.sysml.nodes.to_mermaid", side_effect=_fake_to_mermaid):
        uvicorn.run("app.main:app", host="127.0.0.1", port=8125, log_level="info")
