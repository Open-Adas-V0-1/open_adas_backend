<<<<<<< Updated upstream
import asyncio
import sys
from contextlib import asynccontextmanager

from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from sqlalchemy import text

# AsyncPostgresSaver (the production LangGraph checkpointer, attached below in
# lifespan) uses psycopg's async mode, which requires SelectorEventLoop -- uvicorn's
# default loop on Windows is ProactorEventLoop, which psycopg explicitly refuses to
# run under. Every smoke test in this project already sets this for the same reason
# (not a new constraint here, just the first time the app process itself needs it,
# since Steps 1-2 never touched the checkpointer). Must happen before uvicorn creates
# its event loop, i.e. at import time -- app.main is always imported before the
# server starts serving, regardless of how uvicorn is launched.
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

from app.api.routes.artifacts import router as artifacts_router
from app.api.routes.auth import router as auth_router
from app.api.routes.chat import router as chat_router
from app.api.routes.projects import router as projects_router
from app.config import get_settings
from data.db import engine
from harness.checkpointer import build_production_checkpointer
from storage.s3 import S3StorageBackend
from supervisor.graph import build_supervisor_graph

settings = get_settings()
storage = S3StorageBackend(settings)


@asynccontextmanager
async def lifespan(app: FastAPI):
    await storage.ensure_bucket()
    # The ONE production checkpointer + the ONE compiled Layer-1 graph, built once
    # for the app's whole lifetime (Layers 2/3 inherit this same checkpointer,
    # exactly as every existing smoke test relies on) -- never rebuilt per request.
    async with build_production_checkpointer() as checkpointer:
        app.state.checkpointer = checkpointer
        app.state.supervisor_graph = build_supervisor_graph(checkpointer=checkpointer)
        yield


app = FastAPI(title=settings.app_name, version=settings.app_version, lifespan=lifespan)
app.include_router(auth_router)
app.include_router(projects_router)
app.include_router(chat_router)
app.include_router(artifacts_router)


@app.get("/health")
async def health():
    result: dict[str, str] = {}

    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        result["db"] = "ok"
    except Exception as exc:
        result["db"] = "error"
        result["db_detail"] = str(exc)

    try:
        key = "health-check/ping.txt"
        await storage.save(key, b"ping", "text/plain")
        await storage.load(key)
        await storage.delete(key)
        result["storage"] = "ok"
    except Exception as exc:
        result["storage"] = "error"
        result["storage_detail"] = str(exc)

    if result.get("db") != "ok" or result.get("storage") != "ok":
        return JSONResponse(status_code=503, content=result)
    return result


_DEV_UI_PATH = Path(__file__).resolve().parent.parent / "static" / "dev_ui.html"


@app.get("/dev/ui", include_in_schema=False)
async def dev_ui():
    """T6b Step 6 -- developer test harness only, never the product frontend. 404
    (never 403, same convention as every ownership dependency) when the env flag is off.
    """
    if not settings.dev_ui_enabled:
        raise HTTPException(status_code=404)
    return FileResponse(_DEV_UI_PATH)
=======
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from sqlalchemy import text

from app.config import get_settings
from data.db import engine
from storage.s3 import S3StorageBackend

settings = get_settings()
storage = S3StorageBackend(settings)


@asynccontextmanager
async def lifespan(app: FastAPI):
    await storage.ensure_bucket()
    yield


app = FastAPI(title=settings.app_name, version=settings.app_version, lifespan=lifespan)


@app.get("/health")
async def health():
    result: dict[str, str] = {}

    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        result["db"] = "ok"
    except Exception as exc:
        result["db"] = "error"
        result["db_detail"] = str(exc)

    try:
        key = "health-check/ping.txt"
        await storage.save(key, b"ping", "text/plain")
        await storage.load(key)
        await storage.delete(key)
        result["storage"] = "ok"
    except Exception as exc:
        result["storage"] = "error"
        result["storage_detail"] = str(exc)

    if result.get("db") != "ok" or result.get("storage") != "ok":
        return JSONResponse(status_code=503, content=result)
    return result
>>>>>>> Stashed changes
