"""Execution trace channel (T6b Step 3b) -- a debug-only overlay on top of the
frozen Step-3a event contract. Everything here is derived from the SAME
astream_events() stream app/chat/turn.py already consumes -- no new stream_mode, no
node touched, no prompt changed. See app/chat/turn.py for how trace events are
interleaved with the frozen ones.

--- THE CONTENT RULE (the safety core of this step) ---

Two classes of node:

(a) Routing/planning/validation nodes -- their output (LLM-derived OR fully
    deterministic) is a small structured decision, never an artifact body.
    -> emitted IN FULL: see ALLOW_FULL_NODES below, an explicit (layer, node)
       allow-list. A node NOT on this list defaults to metadata-only -- adding a new
       node to the graph in the future is silent by default, never leaky by default.

(b) Artifact-producing nodes (generation; Layer-3's own plan_node, whose free-text
    "plan" is itself a substantial description of the artifact; contextual_answer,
    which can quote/paraphrase the draft) -- NEVER emit their generated text, not
    truncated, not previewed, not hashed. Only structural metadata: which fields were
    present and how long they were, never their content.

Verification diagnostics are the ONE named exception within metadata-only mode,
explicitly sanctioned by the task: they are tool-generated error codes/messages
ABOUT the code, not the code itself, and verify_node's own dict output is NOT fully
allow-listed (it can carry draft_mermaid on a clean diagram pass) -- so this
exception is scoped to exactly these field names, wherever they appear, not to the
whole node.

Redaction is achieved by CONSTRUCTION, not by scrubbing after the fact: this module
never reads on_chat_model_*'s `data.input` (the rendered prompt/messages), so prompt
text and anything embedded in it (API keys, base URLs, credentials) never enters the
trace pipeline in the first place. Prompt LENGTH is computed (sum of message content
lengths) since that IS available on the stream without reading the text meaningfully;
prompt NAME/path is NOT available anywhere on the stream without a node touch, so it
is omitted -- see this step's chat report for that disclosure.
"""
import time
import uuid
from enum import Enum
from typing import Protocol

# (layer, node) pairs whose ENTIRE state-update dict may be emitted as trace `data`.
# Every one of these has been read at the source and confirmed to never carry
# LLM-generated artifact content in its return value:
#   - top_level_supervisor: classification/response (response is the SAME text
#     already sent via `token` events -- no new exposure) / done / result.
#   - plan_node (Layer 1, supervisor/plan.py): plan_state (the TODO list: task
#     descriptions/intents/levels/dependencies) / result. LLM-authored but a
#     decomposition decision, not the artifact itself. plan_state.original_request
#     is stripped by _sanitize_full (see below) -- it's a verbatim copy of the
#     user's OWN message, which foreseeably overlaps with whatever gets generated
#     FROM it (that's the point of generating it), so a raw echo of it doesn't
#     belong in a channel whose whole guarantee is "generated text never appears".
#   - middle_supervisor: resolved_intent/diagram_type/requested_level/... (MiddleDecision).
#   - validate_inputs, resolve_level: 100% deterministic, no LLM call, no artifact
#     reference beyond ids/booleans/short strings.
#   - sysml_supervisor: intent/level/diagram_type/clarifying_message (IntentDecision).
#   - user_confirm_inputs: pending_pattern/options (already-repository-derived
#     summaries, same ones the frozen `interrupt` event itself would show)/decision flags.
#   - sysml_middle_node, sysml_processing: their own dict output is a LIGHT reference
#     only ({processing_id, thread_id, artifact_type, artifact_id, summary}) -- the
#     same shape already documented project-wide as safe, never full content.
#   - plan_review, memory_optimization, finalize_turn, finalize: bookkeeping/ids only.
#
# NOT allow-listed: build_structured_format. Its ProcessingInput.user_input is,
# again, a verbatim (lightly-annotated) copy of the user's own message -- same
# overlap-with-the-artifact reasoning as plan_node's original_request above, and
# nothing in this step's DoD needs it visible, so it simply defaults to
# metadata-only (summarize_output) rather than needing a special-cased strip.
ALLOW_FULL_NODES: set[tuple[int, str]] = {
    (1, "top_level_supervisor"),
    (1, "plan_node"),
    (1, "plan_review"),
    (1, "sysml_middle_node"),
    (1, "memory_optimization"),
    (1, "finalize_turn"),
    (2, "middle_supervisor"),
    (2, "validate_inputs"),
    (2, "resolve_level"),
    (2, "user_confirm_inputs"),
    (2, "sysml_processing"),
    (3, "sysml_supervisor"),
    (3, "finalize"),
}

# The ONE named exception inside metadata-only mode (see module docstring). Values
# pass through as-is wherever these exact keys appear, even on a non-allow-listed
# node's output.
_SAFE_DIAGNOSTIC_FIELDS = {"verify_diagnostics", "verify_coverage_gaps", "verify_clean", "verify_warning"}

# Any string this short cannot possibly contain a >=40-char verbatim artifact
# substring (the leak check this whole step is built to satisfy) -- safe to pass
# through as-is for a non-allow-listed node (status markers like "task_processed",
# "finalized", enum values -- never generated content, which is always far longer).
_SHORT_STRING_LIMIT = 40


def json_safe(value):
    """Defense in depth: every field this module has traced back to source is
    already a JSON-primitive by the time a node returns it (enums/UUIDs are
    .value/str()'d before being put in state, across this whole codebase's
    convention) -- this is a belt-and-suspenders cast, not the primary safety
    mechanism, so a serialization surprise degrades to a string rather than
    crashing the turn.
    """
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, dict):
        return {k: json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_safe(v) for v in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def _sanitize_plan_state(plan_state: dict) -> dict:
    """plan_state.original_request (a verbatim copy of the user's message) and each
    task's free-text `description` are length-only -- NOT because they're artifacts
    themselves, but because a SysML "doc" comment inside the eventual generated
    artifact routinely RESTATES the request/task description near-verbatim (that's
    what a doc comment is FOR), so keeping them in full would make this channel's own
    "generated text never appears" guarantee impossible to keep in general. Task
    STRUCTURE (id/intent/level/depends_on/status) stays fully visible -- that's what
    DoD #4's "plan/TODO is visible" is actually about.

    plan_state appears in TWO allow-listed nodes' output, not just its creator: both
    plan_node (which builds it) and top_level_supervisor (the execution-loop driver,
    which re-emits it -- with_task_status -- every time it picks the next task) --
    this sanitizer is applied wherever the key `plan_state` appears in an
    allow-listed node's dict, not hardcoded to one node.
    """
    sanitized = dict(plan_state)

    original_request = sanitized.pop("original_request", None)
    if isinstance(original_request, str):
        sanitized["original_request_length"] = len(original_request)

    def _sanitize_task(task):
        if not (isinstance(task, dict) and isinstance(task.get("description"), str)):
            return task
        sanitized_task = {k: v for k, v in task.items() if k != "description"}
        sanitized_task["description_length"] = len(task["description"])
        return sanitized_task

    tasks = sanitized.get("tasks")
    if isinstance(tasks, list):
        sanitized["tasks"] = [_sanitize_task(t) for t in tasks]

    return sanitized


def full_disclosure_data(layer: int, node: str, output: dict) -> dict:
    """The `data` for an ALLOW_FULL_NODES member -- the whole dict, except plan_state
    is passed through _sanitize_plan_state wherever it appears (see there for why).
    """
    if isinstance(output.get("plan_state"), dict):
        output = {**output, "plan_state": _sanitize_plan_state(output["plan_state"])}
    return output


def summarize_output(output: dict) -> dict:
    """The metadata-only extraction for a NON-allow-listed node's own state-update
    dict. Never a per-field DENY-list -- a generic, value-SHAPE-based rule: small
    scalars, None, short strings, and the named diagnostic fields pass through
    as-is; every other string/list/dict is reduced to `{key}_length` (its len()),
    never its content.
    """
    safe: dict = {}
    for key, value in output.items():
        if key in _SAFE_DIAGNOSTIC_FIELDS:
            safe[key] = value
        elif value is None or isinstance(value, (bool, int, float)):
            safe[key] = value
        elif isinstance(value, str) and len(value) <= _SHORT_STRING_LIMIT:
            safe[key] = value
        else:
            safe[f"{key}_length"] = len(str(value))
    return safe


def llm_call_metadata(response_metadata: dict | None, prompt_length: int | None) -> dict:
    """Model id + token counts from an on_chat_model_end AIMessage's response_metadata
    -- never the message content itself (not read here, not passed in).
    """
    response_metadata = response_metadata or {}
    token_usage = response_metadata.get("token_usage") or {}
    data: dict = {}
    model = response_metadata.get("model_name") or response_metadata.get("model")
    if model:
        data["model"] = model
    if token_usage:
        data["prompt_tokens"] = token_usage.get("prompt_tokens")
        data["completion_tokens"] = token_usage.get("completion_tokens")
        data["total_tokens"] = token_usage.get("total_tokens")
    if prompt_length is not None:
        data["prompt_length"] = prompt_length
    return data


class TraceSink(Protocol):
    async def emit(self, event: dict) -> None: ...


class QueueTraceSink:
    """The ONE sink shipped in this step -- pushes a formatted `trace` SSE string
    onto the same queue app/chat/turn.py already streams from. A future persistent
    sink (e.g. a DB writer) is just another class implementing `emit` and getting
    appended to TraceEmitter's sink list -- zero changes at any call site.
    """

    def __init__(self, queue, sse_formatter):
        self._queue = queue
        self._sse = sse_formatter

    async def emit(self, event: dict) -> None:
        await self._queue.put(self._sse("trace", event))


class TraceEmitter:
    """Guards at the SOURCE: when disabled, `enabled` is False and every call site in
    app/chat/turn.py skips its own trace-payload construction entirely BEFORE ever
    calling emit() -- this class doesn't just drop events after building them, there
    is nothing to build.
    """

    def __init__(self, sinks: list[TraceSink], enabled: bool):
        self._sinks = sinks
        self.enabled = enabled
        self._seq = 0

    async def emit(self, *, layer: int | None, node: str | None, ns: str | None, phase: str,
                    duration_ms: float | None = None, data: dict | None = None) -> None:
        if not self.enabled:
            return
        self._seq += 1
        event: dict = {
            "seq": self._seq,
            "ts": time.time(),
            "layer": layer,
            "node": node,
            "ns": ns,
            "phase": phase,
        }
        if duration_ms is not None:
            event["duration_ms"] = round(duration_ms, 2)
        event["data"] = json_safe(data) if data is not None else {}
        for sink in self._sinks:
            await sink.emit(event)
