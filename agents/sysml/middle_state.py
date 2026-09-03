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

    # ONE delegated task -> ONE completion target (fix for real-model re-dispatch of an
    # already-satisfied task): task_locked/task_target are set ONCE, the first time
    # middle_supervisor makes a real dispatch-worthy judgement, and fixed until an
    # explicit user modification. target_fulfilled is set by sysml_processing after
    # each processing, deterministically, from the already-resolved ProcessingInput --
    # no LLM re-judgement. See agents/sysml/middle_nodes.py's _lock_target /
    # _target_fulfilled / middle_supervisor's completion-condition branch.
    task_locked: bool
    task_target: dict | None  # {"intent", "diagram_type", "requested_level"}
    target_fulfilled: bool | None
    # Set by Layer-1's sysml_middle_node (supervisor/graph.py) on every dispatch --
    # marks this invocation as delegating exactly ONE atomic TODO task, which is what
    # enables the task_locked/task_target completion condition above. Unset (None) when
    # the middle graph is driven directly/standalone (its own established contract: a
    # single free-form message may legitimately describe several distinct asks).
    single_task_dispatch: bool | None
    # Set alongside single_task_dispatch by Layer-1's sysml_middle_node -- the
    # delegated task's permanent gen_id, used to derive Layer-3's thread id
    # deterministically (see sysml_processing). None on the standalone Layer-2 path,
    # where sysml_processing falls back to its own processing_counter-based id.
    gen_id: str | None

    # the requirement this processing concerns, if any. Resolved either directly by
    # middle_supervisor (named or sole active requirement) or via user_confirm_inputs
    # when genuinely ambiguous.
    target_requirement_id: uuid.UUID | str | None
    # MULTIPLE requirements a diagram represents, when resolved via the multi-select
    # select_requirements_for_diagram confirm (Step 4) rather than a single target.
    # Reset to None at the start of every middle_supervisor visit; only set by
    # user_confirm_inputs's multi-select branch, consumed by build_structured_format.
    target_requirement_ids: list[str] | None

    # validate_inputs (runs BEFORE resolve_level): is this input processable at all?
    # (known/actionable intent, coherent session/project context). File validity is a
    # placeholder for a later step — inert on the text-only path.
    input_valid: bool | None
    invalid_reason: str | None

    # level resolution (resolve_level): the requested level, the higher-level artifact
    # it derives from (if any — operational has none), and the thread's forward-only
    # Op->Func->Phys progress snapshot at the time of resolution.
    requested_level: str | None  # "operational" | "functional" | "physical"
    resolved_source_id: uuid.UUID | str | None
    level_progress: list[str] | None

    # build_structured_format: the unified Layer-2 -> Layer-3 contract (a
    # ProcessingInput.model_dump()), assembled from the fields above once validity
    # (Step 2) and level/source resolution (Step 1) are settled. sysml_processing
    # consumes this directly rather than re-reading the individual fields above.
    processing_input: dict | None

    # loop bookkeeping
    processing_counter: int  # how many processings dispatched so far -> drives proc_thread_id
    supervisor_visits: int  # loop guard

    # conditional confirmation (only when genuinely ambiguous)
    pending_pattern: str | None  # "select_requirement" | "confirm_diagram_type" | "confirm_action" | "clarify_request"
    pending_options_source: list[dict] | None  # deterministic, repository-derived options
    # disambiguates WHY a "confirm_action" pattern was raised, so user_confirm_inputs
    # knows how to interpret "confirm" (e.g. pivot to creating the missing source level).
    pending_action_context: str | None  # e.g. "missing_level_source"
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
