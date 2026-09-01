"""Reusable resource-ownership dependencies (T6b Step 2).

Distinct from app/middleware/auth.py's get_current_user (identity only): these two
resolve a path-param id INTO the owning row AND verify it belongs to the current
user, in one place, with ONE consistent failure shape -- a resource that doesn't
exist and a resource that exists but belongs to someone else are INDISTINGUISHABLE
to the caller, always 404, never 403. A user must never be able to detect that
another user's project/session exists.

Both routers/routes in this project (and step 3's chat endpoints, which will guard
their session_id path param the exact same way) should depend on these rather than
querying ProjectRepo/SessionRepo directly and checking ownership themselves.
"""
import uuid

from fastapi import Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.middleware.auth import get_current_user
from data.db import get_session
from data.models import Project, Session, User
from data.repository import ProjectRepo, SessionRepo

_NOT_FOUND = HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")


async def get_owned_project(
    project_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
) -> Project:
    project = await ProjectRepo.get_by_id(db, id=project_id, user_id=current_user.id)
    if project is None:
        raise _NOT_FOUND
    return project


async def get_owned_session(
    session_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
) -> Session:
    session_row = await SessionRepo.get_by_id(db, id=session_id)
    if session_row is None:
        raise _NOT_FOUND
    # Session has no direct user_id -- ownership is via its project. Reusing
    # ProjectRepo.get_by_id's own (id, user_id) filter is exactly the same "exists
    # AND is owned" check as get_owned_project, just entered from the session side.
    owning_project = await ProjectRepo.get_by_id(db, id=session_row.project_id, user_id=current_user.id)
    if owning_project is None:
        raise _NOT_FOUND
    return session_row
