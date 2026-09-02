"""Artifacts read API (T6b Step 5) -- the missing piece after resume (Step 4): graph
state only ever carries LIGHT references (processing_id, thread_id, artifact_type,
artifact_id, summary); this router is how a client turns an artifact_id from a
light_ref into the actual requirement/diagram content.

READ ONLY by design: no POST/PATCH/PUT/DELETE here. Modifying or deleting an artifact
must go through the graph (the `modify` intent), which is what enforces versioning,
lineage, and human review -- a second write path here would bypass all of that.

Ownership: every endpoint resolves the artifact's OWN session and checks it against
the current user (app/api/deps.py's get_owned_requirement/get_owned_diagram/
get_owned_session) -- never trusts a bare id. Always 404 for anything not owned,
never 403, matching every other router in this project.
"""
from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_owned_diagram, get_owned_requirement, get_owned_session
from app.schemas.artifacts import (
    ArtifactSummaryItem,
    DiagramOut,
    DiagramSummaryOut,
    RequirementOut,
    RequirementVersionOut,
)
from data.db import get_session as get_db_session
from data.models import Diagram, Requirement, RequirementLevel, Session, VersionStatus
from data.repository import DiagramRepo, RequirementRepo

router = APIRouter(tags=["artifacts"])

# Pagination: a sane default, and a HARD max a client cannot raise above (a long
# session can accumulate many versions -- this bounds any single response).
_DEFAULT_LIMIT = 50
_MAX_LIMIT = 200


def _summary(text: str, length: int = 120) -> str:
    return text[:length]


async def _requirement_out(db: AsyncSession, requirement: Requirement) -> RequirementOut:
    derived_from = await RequirementRepo.find_likely_derivation_source(db, requirement)
    return RequirementOut(
        id=requirement.id,
        root_id=requirement.root_id,
        version=requirement.version,
        level=requirement.level,
        content=requirement.content,
        parent_id=requirement.parent_id,
        derived_from_requirement_id=derived_from.id if derived_from else None,
        is_active=requirement.status == VersionStatus.active,
        created_at=requirement.created_at,
    )


def _diagram_out(diagram: Diagram) -> DiagramOut:
    return DiagramOut(
        id=diagram.id,
        root_id=diagram.root_id,
        version=diagram.version,
        type=diagram.type,
        sysml_text=diagram.sysml_text,
        mermaid=diagram.mermaid,
        requirement_ids=[diagram.requirement_id],
        is_active=diagram.status == VersionStatus.active,
        created_at=diagram.created_at,
    )


@router.get("/sessions/{session_id}/requirements", response_model=list[RequirementOut])
async def list_requirements(
    active_only: bool = True,
    level: RequirementLevel | None = None,
    limit: int = Query(default=_DEFAULT_LIMIT, ge=1, le=_MAX_LIMIT),
    offset: int = Query(default=0, ge=0),
    session_row: Session = Depends(get_owned_session),
    db: AsyncSession = Depends(get_db_session),
) -> list[RequirementOut]:
    rows = await RequirementRepo.list_by_session_filtered(
        db, session_row.id, active_only=active_only, level=level, limit=limit, offset=offset
    )
    return [await _requirement_out(db, r) for r in rows]


@router.get("/requirements/{requirement_id}", response_model=RequirementOut)
async def get_requirement(
    requirement: Requirement = Depends(get_owned_requirement),
    db: AsyncSession = Depends(get_db_session),
) -> RequirementOut:
    return await _requirement_out(db, requirement)


@router.get("/requirements/{requirement_id}/versions", response_model=list[RequirementVersionOut])
async def list_requirement_versions(
    requirement: Requirement = Depends(get_owned_requirement),
    db: AsyncSession = Depends(get_db_session),
) -> list[RequirementVersionOut]:
    versions = await RequirementRepo.list_versions_by_root(db, requirement.root_id, requirement.session_id)
    return [
        RequirementVersionOut(
            id=v.id, version=v.version, is_active=v.status == VersionStatus.active,
            status=v.status.value, created_at=v.created_at,
        )
        for v in versions
    ]


@router.get("/sessions/{session_id}/diagrams", response_model=list[DiagramSummaryOut])
async def list_diagrams(
    active_only: bool = True,
    limit: int = Query(default=_DEFAULT_LIMIT, ge=1, le=_MAX_LIMIT),
    offset: int = Query(default=0, ge=0),
    session_row: Session = Depends(get_owned_session),
    db: AsyncSession = Depends(get_db_session),
) -> list[DiagramSummaryOut]:
    rows = await DiagramRepo.list_by_session_filtered(
        db, session_row.id, active_only=active_only, limit=limit, offset=offset
    )
    return [
        DiagramSummaryOut(id=d.id, type=d.type, is_active=d.status == VersionStatus.active, created_at=d.created_at)
        for d in rows
    ]


@router.get("/diagrams/{diagram_id}", response_model=DiagramOut)
async def get_diagram(diagram: Diagram = Depends(get_owned_diagram)) -> DiagramOut:
    return _diagram_out(diagram)


@router.get("/sessions/{session_id}/artifacts", response_model=list[ArtifactSummaryItem])
async def list_session_artifacts(
    limit: int = Query(default=_DEFAULT_LIMIT, ge=1, le=_MAX_LIMIT),
    offset: int = Query(default=0, ge=0),
    session_row: Session = Depends(get_owned_session),
    db: AsyncSession = Depends(get_db_session),
) -> list[ArtifactSummaryItem]:
    """A single, chronologically-ordered view of the session's ACTIVE artifacts --
    what a client calls after `done` to render a turn's light_refs into real content
    (the artifact_id in a light_ref is exactly the `id` returned here).
    """
    requirements = await RequirementRepo.list_active_for_session(db, session_row.id)
    diagrams = await DiagramRepo.list_active_for_session(db, session_row.id)

    items = [
        ArtifactSummaryItem(
            artifact_type="requirement", id=r.id, level=r.level,
            summary=_summary(r.content), created_at=r.created_at,
        )
        for r in requirements
    ] + [
        ArtifactSummaryItem(
            artifact_type="diagram", id=d.id, diagram_type=d.type,
            summary=_summary(d.sysml_text), created_at=d.created_at,
        )
        for d in diagrams
    ]
    items.sort(key=lambda item: item.created_at)
    return items[offset: offset + limit]
