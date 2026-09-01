"""Programmatic uvicorn launcher (T6b Step 3a).

Exists ONLY to set the Windows event loop policy BEFORE uvicorn creates its event
loop -- `uvicorn app.main:app` (the CLI) imports the app module AFTER its own loop
already exists via asyncio.run(), so setting the policy inside app/main.py at import
time is too late on Windows. AsyncPostgresSaver (the production checkpointer,
attached in app/main.py's lifespan) needs SelectorEventLoop; uvicorn's default on
Windows is ProactorEventLoop. Not needed on Linux/Docker (the deployment target) --
this only matters for local Windows dev, same constraint as every smoke test script.

Run: python run.py
"""
import asyncio
import sys

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

import uvicorn

if __name__ == "__main__":
    uvicorn.run("app.main:app", host="0.0.0.0", port=8000, reload=True)
