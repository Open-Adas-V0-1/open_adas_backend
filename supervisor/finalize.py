from supervisor.state import SupervisorState


def finalize_turn(state: SupervisorState) -> dict:
    """Minimal pass-through that ends the turn. Memory consolidation/summarization is
    deferred — not built here.
    """
    return {"done": True, "result": "done"}
