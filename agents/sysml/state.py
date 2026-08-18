import uuid
from typing import TypedDict


class SysmlState(TypedDict, total=False):
    # input
    user_input: str
    messages: list[str]

    # supervisor decision
    intent: str | None
    clarifying_message: str | None

    # requirement context
    level: str  # RequirementLevel value, default "functional"
    diagram_type: str | None

    # the requirement this processing targets — resolved/passed in by the middle layer
    # (T5); guard_requirement_available validates THIS one, not "does any requirement exist"
    target_requirement_id: uuid.UUID | None
    requirement_valid: bool
    resolved_requirement_content: str | None

    # generation / review loop
    draft_requirement: str | None
    draft_diagram: str | None  # pending PlantUML text
    source_node: str | None  # which node produced the current draft: "requirement" | "diagram" | "delta"
    feedback: str | None  # reviewer feedback, consumed on regeneration
    review_decision: str | None  # "approve" | "regenerate" | "question"

    # metadata bookkeeping, for the approved-artifact summary written at stockage time
    last_feedback: str | None  # last feedback that actually produced the approved draft
    regeneration_count: int

    # apply_published_delta
    selected_published_requirement_id: uuid.UUID | str | None

    # contextual question raised during review (read-only turn, doesn't leave review)
    question: str | None
    contextual_answer_text: str | None

    # ownership / checkpoint context
    session_id: uuid.UUID
    thread_id: str

    # result
    persisted_requirement_id: str | None
    active_requirement_id: str | None
    persisted_diagram_id: str | None
    active_diagram_id: str | None
    no_requirement_message: str | None
    result: str | None
