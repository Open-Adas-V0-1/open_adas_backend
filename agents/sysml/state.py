import uuid
from typing import TypedDict


class SysmlState(TypedDict, total=False):
    # input
    user_input: str
    messages: list[str]

    # supervisor decision
    intent: str | None
    clarifying_message: str | None

    # requirement/diagram context
    level: str  # operational|functional|physical, default "functional"
    diagram_type: str | None

    # the requirement this processing targets (diagrams model an existing requirement;
    # also used by plan_node to read a higher-level source to derive from). Resolved
    # upstream by the middle layer (T5), passed straight through here.
    target_requirement_id: uuid.UUID | str | None
    source_requirement_content: str | None

    # plan (plan_node output, read by generate_node)
    plan: dict | None

    # generation / verify loop
    draft_sysml: str | None  # SysML v2 textual notation — requirement OR diagram model
    draft_mermaid: str | None  # Mermaid, DERIVED by verify_node for diagram targets only
    source_node: str | None  # "requirement" | "diagram"
    feedback: str | None  # human reviewer feedback, consumed on regenerate
    review_decision: str | None  # "approve" | "regenerate" | "question"
    question: str | None

    # verify loop state
    verify_diagnostics: list[dict] | None  # [{message, line, column, severity}, ...]
    verify_coverage_gaps: list[str] | None
    verify_clean: bool
    verify_visits: int  # generate/verify round counter within the current plan cycle
    verify_warning: str | None  # set only on fail-open (limit reached, still not clean)

    # ownership / checkpoint context
    session_id: uuid.UUID
    thread_id: str

    # result
    persisted_requirement_id: str | None
    persisted_diagram_id: str | None
    # compatibility aliases (same value as the persisted_* id above): Layer 2's wrapper
    # (agents/sysml/middle_nodes.py, out of scope for this rebuild) reads these key
    # names to build its light reference — finalize() has no active/superseded
    # semantics of its own, this is just naming compatibility with that contract.
    active_requirement_id: str | None
    active_diagram_id: str | None
    contextual_answer_text: str | None
    result: str | None
