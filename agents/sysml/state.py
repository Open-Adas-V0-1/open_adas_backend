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

    # generation / review loop
    draft_requirement: str | None
    source_node: str | None  # which node produced the current draft ("requirement" in T4a)
    feedback: str | None  # reviewer feedback, consumed on regeneration
    review_decision: str | None  # "approve" | "regenerate"

    # ownership / checkpoint context
    session_id: uuid.UUID
    thread_id: str

    # result
    persisted_requirement_id: str | None
    active_requirement_id: str | None
    result: str | None
