import uuid
from typing import TypedDict


class SupervisorState(TypedDict, total=False):
    # input
    user_input: str
    messages: list[str]

    # hub classification (Layer-1 rebuild, Step 1): the FIRST-pass routing decision
    # made every turn by top_level_supervisor. "response" is the direct answer for
    # simple_response, the clarifying question for unclear, or the placeholder for
    # needs_execution (until Steps 2-3 build real dispatch).
    classification: str | None  # "simple_response" | "needs_execution" | "unclear"
    response: str | None

    # planner state -- a placeholder shape here (unused in Step 1); Step 2's
    # conditional planning path fills this in when classification == needs_execution.
    # Kept forward-compatible so later steps extend it rather than reshaping it.
    plan_state: dict | None

    # LIGHT references from delegated tasks -- never full artifact content. A
    # placeholder list here (unused in Step 1); Step 3's delegation path appends to it.
    results: list[dict] | None

    # dispatched-to-SysML context (forwarded to the middle layer as-is once dispatch is
    # rebuilt in Step 2; unused in Step 1)
    target_requirement_id: uuid.UUID | str | None

    # loop bookkeeping
    processing_index: int  # how many agent dispatches so far -> drives child thread ids
    supervisor_visits: int  # loop guard

    # ownership / checkpoint context
    session_id: uuid.UUID
    thread_id: str

    done: bool
    result: str | None
