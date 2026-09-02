"""Lazy TTL expiry for SysML processing threads' CHECKPOINTER state.

Scope, deliberately narrow: this touches ONLY the checkpointer's own tables
(checkpoints/checkpoint_blobs/checkpoint_writes, via AsyncPostgresSaver.adelete_thread)
for one thread_id. It NEVER touches requirements/diagrams in Postgres — those are
permanent once approved, regardless of how stale a thread's checkpointer state gets.

The checkpointer instance is owned by whoever compiles the top-level graph (Layer 1,
per harness/checkpointer.py) — this module doesn't hold one itself, so `expire_if_stale`
takes it as a parameter. `touch_thread` only needs a DB session and is called from
inside Layer 2's nodes on every access, independent of who owns the checkpointer.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from data.repository import ThreadActivityRepo


async def touch_thread(db: AsyncSession, thread_id: str, session_id: uuid.UUID) -> None:
    """Update (or create) last_accessed = now() for a checkpointer thread_id. Call on
    every read/resume/modify of that thread — this is what makes expiry 'lazy' rather
    than needing a background scanner to know activity.
    """
    await ThreadActivityRepo.touch(db, thread_id=thread_id, session_id=session_id)


async def is_expired(db: AsyncSession, thread_id: str, ttl_days: int | None = None) -> bool:
    """True if thread_id's last_accessed is older than the TTL. A thread that was never
    touched is NOT considered expired (nothing to expire — it's simply unknown/new).
    """
    ttl_days = ttl_days if ttl_days is not None else get_settings().sysml_thread_ttl_days
    last_accessed = await ThreadActivityRepo.get_last_accessed(db, thread_id)
    if last_accessed is None:
        return False
    if last_accessed.tzinfo is None:
        last_accessed = last_accessed.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - last_accessed) > timedelta(days=ttl_days)


async def expire_if_stale(db: AsyncSession, checkpointer, thread_id: str, ttl_days: int | None = None) -> bool:
    """Lazy expiry entry point: if thread_id is expired, delete its checkpointer state
    (adelete_thread — checkpointer tables only) and return True. Otherwise return False
    and leave it untouched. Callers that proceed to use the thread afterward should
    still call touch_thread() themselves once they've actually accessed it.
    """
    if await is_expired(db, thread_id, ttl_days):
        await checkpointer.adelete_thread(thread_id)
        return True
    return False
