from supervisor.state import SupervisorState


async def finalize_turn(state: SupervisorState) -> dict:
    """Minimal turn finalization (Layer-1 rebuild, Step 5): the LAST node before END on
    every normal-completion path (guard breach and plan_review cancellation still end
    directly, unchanged from Steps 1-4). No heavy logic -- long-term memory/learning is
    explicitly out of scope here, a later layer's concern.

    Just makes the turn's outcome unambiguous: done is set (already True from
    top_level_supervisor on every path that reaches here, but confirmed regardless),
    and result falls back to a generic marker if the upstream node somehow left it
    unset. plan_state/results are already in order by the time control reaches here --
    nothing to recompute.
    """
    update: dict = {"done": True}
    if not state.get("result"):
        update["result"] = "turn_finalized"
    return update
