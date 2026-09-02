import uuid
from datetime import datetime

from pydantic import BaseModel

from data.models import DiagramType, RequirementLevel


class RequirementOut(BaseModel):
    id: uuid.UUID
    root_id: uuid.UUID
    version: int
    level: RequirementLevel
    content: str
    parent_id: uuid.UUID | None
    # BEST-EFFORT, read-only heuristic -- see RequirementRepo.find_likely_derivation_source.
    # Distinct from parent_id (a real, stored FK for VERSION lineage v1->v2 of the SAME
    # requirement). None for operational (top of chain) or when ambiguous/missing.
    derived_from_requirement_id: uuid.UUID | None
    is_active: bool
    created_at: datetime


class RequirementVersionOut(BaseModel):
    id: uuid.UUID
    version: int
    is_active: bool
    status: str
    created_at: datetime


class DiagramOut(BaseModel):
    id: uuid.UUID
    root_id: uuid.UUID
    version: int
    type: DiagramType
    sysml_text: str
    mermaid: str | None
    # Always a single-element list today: Diagram.requirement_id is one FK, not a join
    # table (see this step's chat report -- a genuine, pre-existing schema limitation
    # for the multi-select-diagram case, not something this read-only step changes).
    requirement_ids: list[uuid.UUID]
    is_active: bool
    created_at: datetime


class DiagramSummaryOut(BaseModel):
    id: uuid.UUID
    type: DiagramType
    is_active: bool
    created_at: datetime


class ArtifactSummaryItem(BaseModel):
    artifact_type: str  # "requirement" | "diagram"
    id: uuid.UUID
    level: RequirementLevel | None = None
    diagram_type: DiagramType | None = None
    summary: str
    created_at: datetime
