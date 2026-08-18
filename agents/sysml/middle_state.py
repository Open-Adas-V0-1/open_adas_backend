import uuid
from typing import TypedDict


class MiddleState(TypedDict, total=False):
    # input
    user_input: str
    messages: list[str]

    # middle supervisor decision
    resolved_intent: str | None
    diagram_type: str | None
    clarifying_message: str | None

    # the requirement this processing concerns, if any. Resolved either directly by
    # middle_supervisor (named or sole active requirement) or via user_confirm_inputs
    # when genuinely ambiguous.
    target_requirement_id: uuid.UUID | str | None

    # loop bookkeeping
    processing_counter: int  # how many processings dispatched so far -> drives proc_thread_id
    supervisor_visits: int  # loop guard

    # conditional confirmation (only when genuinely ambiguous)
    pending_pattern: str | None  # "select_requirement" | "confirm_diagram_type" | "confirm_action"
    pending_options_source: list[dict] | None  # deterministic, repository-derived options
    confirm_decision: str | None  # "confirmed" | "modified" | "cancelled"

    # layer-3 dispatch
    proc_id: str | None
    proc_thread_id: str | None
    # LIGHT reference only: {processing_id, thread_id, artifact_type, artifact_id, summary}.
    # The full artifact content stays in Postgres via the T2 repository — never copied here.
    processing_result: dict | None

    # ownership / checkpoint context
    session_id: uuid.UUID
    thread_id: str

    result: str | None
