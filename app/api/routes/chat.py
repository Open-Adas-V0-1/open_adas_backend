"""Chat turn/resume endpoints (T6b Step 3a, extended in 3b and 4) -- SSE streams over
the existing, UNCHANGED Layer-1/2/3 LangGraph system. These routes wrap the graph;
they do not alter its behavior (the one graph touch Step 3a made -- tagging
top_level_supervisor's hub-answer call for token attribution -- is behavior-
preserving, verified against the full real-model e2e suite).

Event contract (frozen since Step 3a -- the frontend is built on this, and Step 4's
resume endpoint reuses it EXACTLY, no new event types):
  turn_started -> {session_id, turn_id}
  token        -> {text}                          incremental user-facing answer text
  status       -> {node, layer}                    coarse progress, node name only
  interrupt    -> {pattern, payload}                full confirmation pattern; stream ENDS after this
  done         -> {status: "completed"|"interrupted", light_refs: [...]}
  error        -> {message}                         generic message only

Step 3b adds ONE more event, additive only, never altering the above:
  trace        -> {seq, ts, layer, node, ns, phase, duration_ms?, data}
Only emitted when BOTH settings.trace_enabled (a hard server-side off-switch) AND the
`?trace=1` query param are set -- see app/chat/trace.py for the full content-safety
rule (an explicit node allow-list; artifact-producing nodes get metadata only, never
their generated text).

Token streaming is an ALLOW-LIST (supervisor/streaming_tags.TOKEN_STREAM_TAG),
never a deny-list -- see app/chat/turn.py. If a token can't be reliably attributed to
that one tagged call, it is never emitted.

Concurrency: a session already streaming a turn/resume, or already sitting on an
unresolved interrupt, rejects a new turn with 409 (see app/chat/run_lock.py for
"running"; "awaiting_input"/"no_pending_interrupt" are read fresh from the
checkpointer's own pending-interrupt state on every request, via
app/chat/pending.py's get_pending_interrupt -- ONE source of truth, never a status
column, shared by /turn, /pending, and /resume rather than re-detected three times).

Client disconnect: the graph run is a separate asyncio.Task from the HTTP response
stream (see app/chat/turn.py's module docstring) -- disconnecting the client never
cancels it. Same for /resume.
"""
import asyncio
import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import StreamingResponse
from langgraph.types import Command

from app.api.deps import get_owned_session, get_supervisor_graph
from app.chat import run_lock
from app.chat.pending import get_pending_interrupt
from app.chat.trace import QueueTraceSink, TraceEmitter
from app.chat.turn import STREAM_DONE, _sse, run_turn
from app.config import get_settings
from app.logging import get_logger
from app.schemas.chat import TurnRequest
from app.schemas.resume import ResumeValidationError, validate_resume_payload
from data.models import Session
from supervisor.graph import build_supervisor_config

logger = get_logger(__name__)

router = APIRouter(tags=["chat"])


async def _stream_from_queue(queue: asyncio.Queue):
    while True:
        item = await queue.get()
        if item is STREAM_DONE:
            return
        yield item


def _log_task_result(task: asyncio.Task) -> None:
    """Safety-net logging only -- run_turn already catches everything internally and
    reports via an `error` SSE event. This only fires if something escaped that
    (a bug in run_turn itself), so it should never actually log in practice.
    """
    exc = task.exception() if not task.cancelled() else None
    if exc is not None:
        logger.error("chat.turn_task_uncaught_exception", error_type=type(exc).__name__)


def _make_trace_emitter(queue: asyncio.Queue, trace_query_param: int) -> TraceEmitter:
    trace_on = bool(trace_query_param) and get_settings().trace_enabled
    return TraceEmitter(sinks=[QueueTraceSink(queue, _sse)] if trace_on else [], enabled=trace_on)


def _launch_run(graph, config: dict, session_id: uuid.UUID, turn_id: str, graph_input, trace_query_param: int):
    queue: asyncio.Queue = asyncio.Queue()
    trace_emitter = _make_trace_emitter(queue, trace_query_param)
    task = asyncio.create_task(run_turn(graph, config, session_id, turn_id, graph_input, queue, trace_emitter))
    task.add_done_callback(_log_task_result)
    return StreamingResponse(_stream_from_queue(queue), media_type="text/event-stream")


@router.post("/sessions/{session_id}/turn")
async def create_turn(
    payload: TurnRequest,
    trace: int = 0,
    session_row: Session = Depends(get_owned_session),
    graph=Depends(get_supervisor_graph),
) -> StreamingResponse:
    session_id: uuid.UUID = session_row.id
    config = build_supervisor_config(str(session_id))

    # "awaiting_input" is derived FRESH from the checkpointer, every request -- the
    # ONE source of truth (never a status column). Checked BEFORE touching the
    # running-lock: an interrupted session was never "running" to begin with.
    if await get_pending_interrupt(graph, config) is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"status": "awaiting_input", "message": "This session is awaiting a reply to a pending confirmation."},
        )

    if not run_lock.try_acquire(session_id):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"status": "running", "message": "A turn is already running for this session."},
        )

    # SupervisorState has no reducers (plain TypedDict, total=False -- see
    # supervisor/state.py), but that alone doesn't clear stale state: values passed in
    # astream_events' input dict do NOT overwrite already-persisted channel values on a
    # resumed thread_id -- LangGraph only applies that input to the nodes it runs, and a
    # fresh turn's first node (top_level_supervisor) reads the PERSISTED plan_state/done
    # before the new input is considered, so the previous turn's completed state survives
    # and short-circuits classification. The fix is an explicit aupdate_state() write
    # BEFORE the run, which does land as the thread's latest checkpoint.
    # /turn ONLY -- /resume must never do this: it would wipe an in-flight multi-task
    # plan_state out from under an interrupted turn and break resume/interrupt bubbling.
    await graph.aupdate_state(config, {
        "plan_state": None,
        "done": False,
        "result": None,
        "classification": None,
        "supervisor_visits": 0,
        "plan_review_decision": None,
        "results": None,
    })

    turn_id = str(uuid.uuid4())
    logger.info("chat.turn_started", session_id=str(session_id), turn_id=turn_id)
    graph_input = {"user_input": payload.message, "session_id": session_id}
    return _launch_run(graph, config, session_id, turn_id, graph_input, trace)


@router.get("/sessions/{session_id}/pending")
async def get_pending(
    session_row: Session = Depends(get_owned_session),
    graph=Depends(get_supervisor_graph),
) -> dict:
    """Lets a client that reloaded the page recover its state: is this session
    currently suspended on an interrupt, and if so, on what? SAME detection as the
    409 guards below -- read fresh from the checkpointer, never a status column.
    """
    config = build_supervisor_config(str(session_row.id))
    pending = await get_pending_interrupt(graph, config)
    if pending is None:
        return {"pending": False}
    payload = pending.value
    pattern = payload.get("pattern") or payload.get("type") or "unknown"
    return {"pending": True, "pattern": pattern, "payload": payload}


@router.post("/sessions/{session_id}/resume")
async def resume_turn(
    raw_body: dict,
    trace: int = 0,
    session_row: Session = Depends(get_owned_session),
    graph=Depends(get_supervisor_graph),
) -> StreamingResponse:
    session_id: uuid.UUID = session_row.id
    config = build_supervisor_config(str(session_id))

    pending = await get_pending_interrupt(graph, config)
    if pending is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"status": "no_pending_interrupt", "message": "This session has no pending confirmation to resume."},
        )

    pending_payload = pending.value
    pattern = pending_payload.get("pattern") or pending_payload.get("type") or "unknown"

    # Validated against the pattern that is ACTUALLY pending (never client-declared),
    # and the graph is NEVER invoked on a validation failure -- see app/schemas/resume.py.
    try:
        resume_payload = validate_resume_payload(pattern, raw_body, pending_payload)
    except ResumeValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"pattern": pattern, "message": exc.message},
        ) from exc

    if not run_lock.try_acquire(session_id):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"status": "running", "message": "A turn is already running for this session."},
        )

    turn_id = str(uuid.uuid4())
    logger.info("chat.resume_started", session_id=str(session_id), turn_id=turn_id, pattern=pattern)
    return _launch_run(graph, config, session_id, turn_id, Command(resume=resume_payload), trace)
