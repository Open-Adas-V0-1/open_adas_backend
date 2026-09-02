"""Runs one chat turn against the compiled Layer-1 graph and produces SSE events
(T6b Step 3a, extended in Step 3b). The Step-3a event contract is FROZEN -- see
app/api/routes/chat.py's docstring for the full list. Step 3b adds `trace` events,
additively, interleaved when a TraceEmitter with enabled=True is passed in; when
disabled (the default), the trace-only branches below are skipped before any payload
is ever built (see each `if trace.enabled:` guard) -- this is the SAME astream_events()
call either way, never a second stream_mode.

Architecture note (client disconnect): run_turn is launched as its own asyncio.Task,
independent of the HTTP response's async generator (_stream_from_queue in
app/api/routes/chat.py). The two communicate ONLY through an asyncio.Queue. If the
client disconnects, Starlette stops iterating the response generator (which just
stops reading from the queue) -- it does NOT cancel run_turn's task, so the graph run
always continues to completion and the checkpointer ends up in the correct final
state regardless of whether anyone was listening.
"""
import json
import time
import uuid

from app.chat import run_lock
from app.chat.node_layers import attribute_layer
from app.chat.trace import ALLOW_FULL_NODES, TraceEmitter, full_disclosure_data, llm_call_metadata, summarize_output
from app.logging import get_logger
from supervisor.streaming_tags import TOKEN_STREAM_TAG

logger = get_logger(__name__)

# Sentinel pushed onto the queue to signal "no more events, close the stream".
STREAM_DONE = None


def _sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"


def _prompt_length(chat_model_start_data: dict) -> int | None:
    """Best-effort SUM of message content lengths from on_chat_model_start's own
    `data.input` -- never the text itself, and never read anywhere else in this
    module (on_chat_model_end's message content is likewise never touched). Returns
    None on any unexpected shape rather than guessing.
    """
    try:
        raw = chat_model_start_data.get("input")
        messages = raw.get("messages") if isinstance(raw, dict) else raw
        if not messages:
            return None
        flat = messages[0] if messages and isinstance(messages[0], list) else messages
        return sum(len(str(getattr(m, "content", ""))) for m in flat)
    except Exception:
        return None


async def run_turn(
    graph, config: dict, session_id: uuid.UUID, turn_id: str, user_input: str, queue, trace: TraceEmitter
) -> None:
    """Drives one graph turn to completion (normal end OR interrupt), pushing
    formatted SSE strings onto `queue`. ALWAYS releases the run_lock and pushes the
    STREAM_DONE sentinel in its finally block, regardless of outcome.
    """
    # run_id -> (start_perf_counter, prompt_length | None); only populated when
    # trace.enabled, and only for run_ids belonging to a recognized node -- see the
    # "if trace.enabled:" guards below. Cleared as each matching end event consumes it.
    node_starts: dict[str, float] = {}
    llm_starts: dict[str, tuple[float, int | None]] = {}

    try:
        await queue.put(_sse("turn_started", {"session_id": str(session_id), "turn_id": turn_id}))

        current_answer_text = ""

        async for event in graph.astream_events(
            {"user_input": user_input, "session_id": session_id}, config, version="v2"
        ):
            kind = event.get("event")

            if kind == "on_parser_stream" and TOKEN_STREAM_TAG in (event.get("tags") or []):
                chunk = (event.get("data") or {}).get("chunk")
                text = getattr(chunk, "response", None) or ""
                if text.startswith(current_answer_text):
                    delta = text[len(current_answer_text):]
                else:
                    # Defensive fallback (not expected in practice for this one-shot
                    # classification call): never emit something that would make the
                    # concatenation of `token` events diverge from the final answer.
                    delta = text
                current_answer_text = text
                if delta:
                    await queue.put(_sse("token", {"text": delta}))
                continue

            if kind == "on_chain_start":
                metadata = event.get("metadata") or {}
                node = metadata.get("langgraph_node")
                ns = metadata.get("checkpoint_ns")
                layer = attribute_layer(node, ns)
                if layer is not None:
                    await queue.put(_sse("status", {"node": node, "layer": layer}))
                    if trace.enabled:
                        run_id = event.get("run_id")
                        if run_id:
                            node_starts[run_id] = time.perf_counter()
                        await trace.emit(layer=layer, node=node, ns=ns, phase="enter")
                continue

            if trace.enabled and kind == "on_chain_end":
                metadata = event.get("metadata") or {}
                node = metadata.get("langgraph_node")
                ns = metadata.get("checkpoint_ns")
                layer = attribute_layer(node, ns)
                if layer is None:
                    continue
                run_id = event.get("run_id")
                start = node_starts.pop(run_id, None) if run_id else None
                duration_ms = (time.perf_counter() - start) * 1000 if start is not None else None
                await trace.emit(layer=layer, node=node, ns=ns, phase="exit", duration_ms=duration_ms)

                output = (event.get("data") or {}).get("output")
                # Only the REAL node-function return is a dict -- LangGraph's own
                # conditional-edge routing functions return a bare next-node-name
                # string, and the nested with_structured_output sub-chain's own
                # on_chain_end returns the parsed Pydantic decision object, not a
                # dict. This filter is what isolates "the node's actual state
                # update", not an artifact of naming.
                if isinstance(output, dict):
                    if (layer, node) in ALLOW_FULL_NODES:
                        data = full_disclosure_data(layer, node, output)
                    else:
                        data = summarize_output(output)
                    await trace.emit(layer=layer, node=node, ns=ns, phase="decision", data=data)
                continue

            if trace.enabled and kind == "on_chat_model_start":
                metadata = event.get("metadata") or {}
                node = metadata.get("langgraph_node")
                ns = metadata.get("checkpoint_ns")
                run_id = event.get("run_id")
                if run_id:
                    llm_starts[run_id] = (time.perf_counter(), _prompt_length(event.get("data") or {}))
                continue

            if trace.enabled and kind == "on_chat_model_end":
                metadata = event.get("metadata") or {}
                node = metadata.get("langgraph_node")
                ns = metadata.get("checkpoint_ns")
                layer = attribute_layer(node, ns)
                run_id = event.get("run_id")
                started = llm_starts.pop(run_id, None) if run_id else None
                start, prompt_length = started if started else (None, None)
                duration_ms = (time.perf_counter() - start) * 1000 if start is not None else None
                message = (event.get("data") or {}).get("output")
                response_metadata = getattr(message, "response_metadata", None)
                data = llm_call_metadata(response_metadata, prompt_length)
                await trace.emit(layer=layer, node=node, ns=ns, phase="llm", duration_ms=duration_ms, data=data)
                continue

        snapshot = await graph.aget_state(config)
        interrupts = [i for task in snapshot.tasks for i in (task.interrupts or ())]
        light_refs = (snapshot.values or {}).get("results") or []

        if interrupts:
            payload = interrupts[0].value
            pattern = payload.get("pattern") or payload.get("type") or "unknown"
            if trace.enabled:
                # Same payload the frozen `interrupt` event carries -- already
                # sanctioned for full disclosure (that event's whole purpose is
                # showing the pending review content), so no new exposure here.
                await trace.emit(layer=None, node="requirement_review", ns=None, phase="interrupt",
                                  data={"pattern": pattern})
            await queue.put(_sse("interrupt", {"pattern": pattern, "payload": payload}))
            await queue.put(_sse("done", {"status": "interrupted", "light_refs": light_refs}))
            logger.info("chat.turn_interrupted", session_id=str(session_id), turn_id=turn_id, pattern=pattern)
        else:
            await queue.put(_sse("done", {"status": "completed", "light_refs": light_refs}))
            logger.info("chat.turn_completed", session_id=str(session_id), turn_id=turn_id)

    except Exception as exc:
        # Generic message only -- never a stack trace or provider detail to the
        # client. error_type (just the class name) is safe/useful server-side.
        logger.error("chat.turn_failed", session_id=str(session_id), turn_id=turn_id, error_type=type(exc).__name__)
        if trace.enabled:
            await trace.emit(layer=None, node=None, ns=None, phase="error", data={"error_type": type(exc).__name__})
        await queue.put(_sse("error", {"message": "An internal error occurred while processing your message."}))
    finally:
        run_lock.release(session_id)
        await queue.put(STREAM_DONE)
