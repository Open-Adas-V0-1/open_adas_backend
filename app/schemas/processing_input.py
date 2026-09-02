import uuid
from typing import Literal

from pydantic import BaseModel

from data.models import DiagramType, RequirementLevel


class ProcessingInput(BaseModel):
    """The unified Layer-2 -> Layer-3 contract (assembled by build_structured_format).

    Layer-3 always receives this SAME shape, regardless of where the request came from
    (a text message today; a file-derived extraction later). Carries REFERENCES only
    (source_id, target_requirement_ids) — never full artifact content; Layer-3 reads
    content from the repository via those ids when it needs it.

    "modify" intents fold into their "generate" counterpart here: which existing
    artifact to derive from/represent is already captured by source_id /
    target_requirement_ids, so Layer-3 doesn't need a separate modify/generate
    distinction — its own supervisor node decides the concrete handling from
    user_input. apply_published_delta is not a valid processing intent (Layer-2's
    validate_inputs rejects it before this node is ever reached).
    """

    intent: Literal["generate_requirement", "generate_diagram"]
    level: RequirementLevel
    source_id: uuid.UUID | None = None
    target_requirement_ids: list[uuid.UUID] = []
    diagram_type: DiagramType | None = None
    user_input: str
    session_id: uuid.UUID

    # Extension point: once file-backed requests exist, a reference to the
    # file-extracted requirements (e.g. `extracted_requirements_ref: str | None`)
    # belongs here — deliberately not added yet, kept as a clean, documented spot.
