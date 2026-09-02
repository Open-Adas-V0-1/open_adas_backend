"""Shared pending-interrupt detection (T6b Step 4), reused by:
  - POST /sessions/{id}/turn's 409 "awaiting_input" guard (Step 3a),
  - GET /sessions/{id}/pending (Step 4),
  - POST /sessions/{id}/resume's 409 "no_pending_interrupt" guard (Step 4).

ONE source of truth: the checkpointer's own pending-task state, via
graph.aget_state(config) -- never a status column. This is the SAME mechanism
app/chat/turn.py uses to detect an interrupt after a turn's own astream_events loop
ends; this module is that detection, factored out so it's written once.
"""
from langgraph.types import Interrupt


async def get_pending_interrupt(graph, config: dict, snapshot=None) -> Interrupt | None:
    """The first pending Interrupt for this thread, or None if the session isn't
    currently suspended on one. A brand-new session (no checkpoint yet) safely
    returns None -- aget_state on an unknown thread_id just comes back empty.

    Pass an already-fetched `snapshot` (a StateSnapshot from graph.aget_state) to
    avoid a redundant checkpointer round-trip when the caller needs BOTH the pending
    interrupt AND the snapshot's other fields (e.g. app/chat/turn.py also reads
    snapshot.values for light_refs) -- otherwise this fetches it itself.
    """
    if snapshot is None:
        snapshot = await graph.aget_state(config)
    for task in snapshot.tasks:
        if task.interrupts:
            return task.interrupts[0]
    return None
