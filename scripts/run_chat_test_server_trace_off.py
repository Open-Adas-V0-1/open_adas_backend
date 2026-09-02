"""Test-only server launcher, identical to scripts/run_chat_test_server.py, EXCEPT
TRACE_ENABLED is forced to false (overriding .env) -- used by
scripts/smoke_test_chat_trace.py's DoD #8 (server-level off-switch wins over the
per-request ?trace=1 query param). A separate process is the only way to exercise
this: TRACE_ENABLED is read once via get_settings() at server startup, same as every
other Settings field in this project.

Run: python -m scripts.run_chat_test_server_trace_off
"""
import asyncio
import os
import sys
from unittest.mock import patch

os.environ["TRACE_ENABLED"] = "false"

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
        uvicorn.run("app.main:app", host="127.0.0.1", port=8126, log_level="info")
