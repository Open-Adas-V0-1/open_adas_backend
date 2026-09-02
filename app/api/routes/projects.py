import uuid

from fastapi import APIRouter, Depends, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_owned_project, get_owned_session
from app.logging import get_logger
from app.middleware.auth import get_current_user
from app.schemas.projects import ProjectCreate, ProjectOut, SessionCreate, SessionOut, SessionRename
from data.db import get_session
from data.models import Project, Session, User
from data.repository import CheckpointRepo, ProjectRepo, SessionRepo

logger = get_logger(__name__)

router = APIRouter(tags=["projects"])


@router.post("/projects", response_model=ProjectOut, status_code=status.HTTP_201_CREATED)
async def create_project(
    payload: ProjectCreate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
) -> Project:
    project = await ProjectRepo.create(
        db, user_id=current_user.id, name=payload.name, description=payload.description
    )
    await db.commit()
    logger.info("projects.create", project_id=str(project.id), user_id=str(current_user.id))
    return project


@router.get("/projects", response_model=list[ProjectOut])
async def list_projects(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
) -> list[Project]:
    return await ProjectRepo.list_by_user(db, user_id=current_user.id)


@router.get("/projects/{project_id}", response_model=ProjectOut)
async def get_project(project: Project = Depends(get_owned_project)) -> Project:
    return project


@router.delete("/projects/{project_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_project(
    project: Project = Depends(get_owned_project),
    db: AsyncSession = Depends(get_session),
) -> None:
    # Checkpoints have no FK to sessions -- purge each session's checkpoint tree
    # BEFORE the project row goes away (DB-level ondelete=CASCADE handles the
    # project -> sessions -> {requirements, diagrams, files, ...} rows on its own).
    sessions = await SessionRepo.list_by_project(db, project.id)
    for session_row in sessions:
        await CheckpointRepo.purge_thread_tree(db, session_row.id)
    await ProjectRepo.delete(db, project)
    await db.commit()
    logger.info("projects.delete", project_id=str(project.id), sessions_purged=len(sessions))


@router.post(
    "/projects/{project_id}/sessions", response_model=SessionOut, status_code=status.HTTP_201_CREATED
)
async def create_session(
    payload: SessionCreate,
    project: Project = Depends(get_owned_project),
    db: AsyncSession = Depends(get_session),
) -> Session:
    # thread_id: kept only to satisfy the existing (unique, NOT NULL) column -- never
    # read anywhere in the graph-invocation path. The session's OWN id is the real
    # LangGraph thread_id from step 3 onward; this endpoint never creates a thread.
    session_row = await SessionRepo.create(
        db, project_id=project.id, thread_id=str(uuid.uuid4()), title=payload.title
    )
    await db.commit()
    logger.info("sessions.create", session_id=str(session_row.id), project_id=str(project.id))
    return session_row


@router.get("/projects/{project_id}/sessions", response_model=list[SessionOut])
async def list_sessions(
    project: Project = Depends(get_owned_project),
    db: AsyncSession = Depends(get_session),
) -> list[Session]:
    return await SessionRepo.list_by_project(db, project.id)


@router.get("/sessions/{session_id}", response_model=SessionOut)
async def get_session_route(session_row: Session = Depends(get_owned_session)) -> Session:
    return session_row


@router.patch("/sessions/{session_id}", response_model=SessionOut)
async def rename_session(
    payload: SessionRename,
    session_row: Session = Depends(get_owned_session),
    db: AsyncSession = Depends(get_session),
) -> Session:
    session_row = await SessionRepo.rename(db, session_row, payload.title)
    await db.commit()
    logger.info("sessions.rename", session_id=str(session_row.id))
    return session_row


@router.delete("/sessions/{session_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_session(
    session_row: Session = Depends(get_owned_session),
    db: AsyncSession = Depends(get_session),
) -> None:
    await CheckpointRepo.purge_thread_tree(db, session_row.id)
    await SessionRepo.delete(db, session_row)
    await db.commit()
    logger.info("sessions.delete", session_id=str(session_row.id))
