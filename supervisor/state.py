import uuid
from typing import TypedDict


class SupervisorState(TypedDict, total=False):
    # input
    user_input: str
    messages: list[str]

    # hub classification (Layer-1 rebuild, Step 1): the FIRST-pass routing decision
    # made every turn by top_level_supervisor. "response" is the direct answer for
    # simple_response, or the clarifying question for unclear. Left None while
    # needs_execution is being planned/executed (Steps 2-3).
    classification: str | None  # "simple_response" | "needs_execution" | "unclear"
    response: str | None

    # the TODO-list plan (Step 2): a PlanState.model_dump() -- {tasks: [...], "
    # original_request": str}, built by plan_node ONLY for needs_execution. None until
    # a plan is built; None again once a fresh turn starts. Step 3's execution loop
    # consumes this to drive delegation.
    plan_state: dict | None

    # plan-level HITL (Step 4): only set while a plan is under review. "approved" and
    # "modified" both proceed to execution (Step 3) with plan_state as-is/edited;
    # "cancelled" ends the turn. None for simple (single-task) plans, which skip
    # plan_review entirely.
    plan_review_decision: str | None  # "approved" | "modified" | "cancelled"

    # LIGHT references from delegated tasks -- never full artifact content. A
    # placeholder list here (unused until Step 3); Step 3's delegation path appends to it.
    results: list[dict] | None

    # dispatched-to-SysML context (forwarded to the middle layer as-is once dispatch is
    # rebuilt in Step 3; unused in Steps 1-2)
    target_requirement_id: uuid.UUID | str | None

    # loop bookkeeping
    processing_index: int  # how many agent dispatches so far -> drives child thread ids
    supervisor_visits: int  # loop guard

    # ownership / checkpoint context
    session_id: uuid.UUID
    thread_id: str

    done: bool
    result: str | None
