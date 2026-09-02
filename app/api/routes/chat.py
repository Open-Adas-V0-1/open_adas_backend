"""Chat turn endpoint (T6b Step 3a, extended in Step 3b) -- POST
/sessions/{session_id}/turn, an SSE stream over the existing, UNCHANGED Layer-1/2/3
LangGraph system. This route wraps the graph; it does not alter its behavior (the one
graph touch Step 3a made -- tagging top_level_supervisor's hub-answer call for token
attribution -- is behavior-preserving, verified against the full real-model e2e suite).

Event contract (frozen -- the frontend is built on this):
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

Concurrency: a session already streaming a turn, or already sitting on an unresolved
interrupt, rejects a new turn with 409 (see app/chat/run_lock.py for "running";
"awaiting_input" is read fresh from the checkpointer's own pending-interrupt state on
every request -- ONE source of truth, not a status column).

Client disconnect: the graph run is a separate asyncio.Task from the HTTP response
stream (see app/chat/turn.py's module docstring) -- disconnecting the client never
cancels it.
"""
import asyncio
import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import StreamingResponse

from app.api.deps import get_owned_session, get_supervisor_graph
from app.chat import run_lock
from app.chat.trace import QueueTraceSink, TraceEmitter
from app.chat.turn import STREAM_DONE, _sse, run_turn
from app.config import get_settings
from app.logging import get_logger
from app.schemas.chat import TurnRequest
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
    snapshot = await graph.aget_state(config)
    if any(task.interrupts for task in snapshot.tasks):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"status": "awaiting_input", "message": "This session is awaiting a reply to a pending confirmation."},
        )

    if not run_lock.try_acquire(session_id):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"status": "running", "message": "A turn is already running for this session."},
        )

    turn_id = str(uuid.uuid4())
    queue: asyncio.Queue = asyncio.Queue()

    trace_on = bool(trace) and get_settings().trace_enabled
    trace_emitter = TraceEmitter(
        sinks=[QueueTraceSink(queue, _sse)] if trace_on else [], enabled=trace_on
    )

    task = asyncio.create_task(run_turn(graph, config, session_id, turn_id, payload.message, queue, trace_emitter))
    task.add_done_callback(_log_task_result)

    logger.info("chat.turn_started", session_id=str(session_id), turn_id=turn_id)
    return StreamingResponse(_stream_from_queue(queue), media_type="text/event-stream")
