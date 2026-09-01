from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from sqlalchemy import text

from app.api.routes.auth import router as auth_router
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
app.include_router(auth_router)


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
