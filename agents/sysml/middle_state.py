import uuid
from typing import TypedDict


class MiddleState(TypedDict, total=False):
    # input
    user_input: str
    messages: list[str]

    # middle supervisor decision
    resolved_intent: str | None
    clarifying_message: str | None

    # the requirement this processing concerns, if any. T5a: passed in directly.
    # T5b adds real resolution logic (e.g. from "which requirement are we discussing").
    target_requirement_id: uuid.UUID | None

    # loop bookkeeping
    processing_counter: int  # how many processings dispatched so far -> drives proc_thread_id
    supervisor_visits: int  # loop guard

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
