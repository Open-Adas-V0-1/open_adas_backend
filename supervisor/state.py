import uuid
from typing import TypedDict


class SupervisorState(TypedDict, total=False):
    # input
    user_input: str
    messages: list[str]

    # planner state: lightweight — what was asked, what's done, what's next, whether
    # the overall intent is complete. Lets the orchestrator run multi-step work without
    # stopping early. {"goal": str, "steps_done": list[str], "next_step": str | None,
    # "complete": bool}
    plan: dict | None

    # routing
    active_agent: str | None  # "sysml" for MVP_1; more agents later
    clarifying_message: str | None

    # dispatched-to-SysML context (forwarded to the middle layer as-is; the middle
    # layer resolves any ambiguity itself)
    target_requirement_id: uuid.UUID | str | None

    # loop bookkeeping
    processing_index: int  # how many agent dispatches so far -> drives child thread ids
    supervisor_visits: int  # loop guard

    # LIGHT reference returned from the middle layer — never the full artifact content
    sysml_result: dict | None

    # ownership / checkpoint context
    session_id: uuid.UUID
    thread_id: str

    done: bool
    result: str | None
