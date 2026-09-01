"""Runs one chat turn against the compiled Layer-1 graph and produces SSE events
(T6b Step 3a). The event contract is frozen -- see app/api/routes/chat.py's
docstring for the full list.

Architecture note (client disconnect): _run_turn is launched as its own
asyncio.Task, independent of the HTTP response's async generator (_stream_from_queue
in app/api/routes/chat.py). The two communicate ONLY through an asyncio.Queue. If the
client disconnects, Starlette stops iterating the response generator (which just
stops reading from the queue) -- it does NOT cancel _run_turn's task, so the graph
run always continues to completion and the checkpointer ends up in the correct
final state regardless of whether anyone was listening.
"""
import json
import uuid

from app.chat import run_lock
from app.chat.node_layers import attribute_layer
from app.logging import get_logger
from supervisor.streaming_tags import TOKEN_STREAM_TAG

logger = get_logger(__name__)

# Sentinel pushed onto the queue to signal "no more events, close the stream".
STREAM_DONE = None


def _sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"


async def run_turn(graph, config: dict, session_id: uuid.UUID, turn_id: str, user_input: str, queue) -> None:
    """Drives one graph turn to completion (normal end OR interrupt), pushing
    formatted SSE strings onto `queue`. ALWAYS releases the run_lock and pushes the
    STREAM_DONE sentinel in its finally block, regardless of outcome.
    """
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
                layer = attribute_layer(node, metadata.get("checkpoint_ns"))
                if layer is not None:
                    await queue.put(_sse("status", {"node": node, "layer": layer}))
                continue

        snapshot = await graph.aget_state(config)
        interrupts = [i for task in snapshot.tasks for i in (task.interrupts or ())]
        light_refs = (snapshot.values or {}).get("results") or []

        if interrupts:
            payload = interrupts[0].value
            pattern = payload.get("pattern") or payload.get("type") or "unknown"
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
        await queue.put(_sse("error", {"message": "An internal error occurred while processing your message."}))
    finally:
        run_lock.release(session_id)
        await queue.put(STREAM_DONE)
