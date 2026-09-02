"""Reusable resource-ownership dependencies (T6b Step 2, extended in Step 5).

Distinct from app/middleware/auth.py's get_current_user (identity only): these
resolve a path-param id INTO the owning row AND verify it belongs to the current
user, in one place, with ONE consistent failure shape -- a resource that doesn't
exist and a resource that exists but belongs to someone else are INDISTINGUISHABLE
to the caller, always 404, never 403. A user must never be able to detect that
another user's project/session/artifact exists.

Every router in this project (chat's session_id guard, Step 5's artifact endpoints)
depends on these rather than querying the repositories and checking ownership itself.
"""
import uuid

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.middleware.auth import get_current_user
from data.db import get_session
from data.models import Diagram, Project, Requirement, Session, User
from data.repository import DiagramRepo, ProjectRepo, RequirementRepo, SessionRepo

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


async def _verify_session_ownership(db: AsyncSession, session_id: uuid.UUID, user_id: uuid.UUID) -> Session | None:
    """Session has no direct user_id -- ownership is via its project. Shared by
    get_owned_session AND the artifact-ownership dependencies below (an artifact's
    session must pass this SAME check), so there is exactly one place this logic lives.
    """
    session_row = await SessionRepo.get_by_id(db, id=session_id)
    if session_row is None:
        return None
    owning_project = await ProjectRepo.get_by_id(db, id=session_row.project_id, user_id=user_id)
    if owning_project is None:
        return None
    return session_row


async def get_owned_session(
    session_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
) -> Session:
    session_row = await _verify_session_ownership(db, session_id, current_user.id)
    if session_row is None:
        raise _NOT_FOUND
    return session_row


async def get_owned_requirement(
    requirement_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
) -> Requirement:
    """T6b Step 5: resolves a BARE requirement id (the client only ever has the id,
    e.g. from a light_ref) to its row, THEN verifies its session is owned by the
    current user -- never trusts the id alone. 404 either way, same as every other
    ownership dependency here.
    """
    requirement = await RequirementRepo.get_by_id_any_session(db, requirement_id)
    if requirement is None:
        raise _NOT_FOUND
    if await _verify_session_ownership(db, requirement.session_id, current_user.id) is None:
        raise _NOT_FOUND
    return requirement


async def get_owned_diagram(
    diagram_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
) -> Diagram:
    """T6b Step 5: same shape as get_owned_requirement, for diagrams."""
    diagram = await DiagramRepo.get_by_id_any_session(db, diagram_id)
    if diagram is None:
        raise _NOT_FOUND
    if await _verify_session_ownership(db, diagram.session_id, current_user.id) is None:
        raise _NOT_FOUND
    return diagram


def get_supervisor_graph(request: Request):
    """The ONE compiled Layer-1 graph, built once at app startup (see app/main.py's
    lifespan) with the ONE production checkpointer attached -- Layers 2/3 inherit it
    exactly as every existing smoke test relies on. Never rebuilt per request.
    """
    return request.app.state.supervisor_graph
