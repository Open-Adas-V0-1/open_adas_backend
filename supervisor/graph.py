from langgraph.graph import END, START, StateGraph

from app.config import get_settings
from supervisor.router import route_from_top_supervisor, top_level_supervisor
from supervisor.state import SupervisorState


def build_supervisor_graph(checkpointer=None):
    """Build the top-level supervisor graph -- the head of the whole harness. Owns the
    SINGLE production checkpointer (passed in by the caller, encrypted + serialized as
    in T6a); Layers 2 and 3 will inherit it through wrapper nodes again once dispatch is
    rebuilt (Step 2+), and never build their own.

    Step 1 (this build): the hub (top_level_supervisor) is the ONLY node. Every turn
    enters it directly (NOT a plan node) and every classification ends the turn here --
    simple_response is answered directly, unclear asks for clarification, and
    needs_execution returns a placeholder pending Steps 2-3. Steps 2-5 add plan_node /
    sysml_middle_node / memory_optimization / finalize_turn as NEW nodes and conditional
    targets off route_from_top_supervisor, without needing to restructure this shape.
    """
    builder = StateGraph(SupervisorState)

    builder.add_node("top_level_supervisor", top_level_supervisor)

    builder.add_edge(START, "top_level_supervisor")

    builder.add_conditional_edges(
        "top_level_supervisor",
        route_from_top_supervisor,
        {END: END},
    )

    return builder.compile(checkpointer=checkpointer)


def build_supervisor_config(thread_id: str, **extra_configurable) -> dict:
    """Build the RunnableConfig callers should use to invoke the top-level graph, with
    the env-driven step-count guard (SUPERVISOR_RECURSION_LIMIT) applied.
    """
    return {
        "configurable": {"thread_id": thread_id, **extra_configurable},
        "recursion_limit": get_settings().supervisor_recursion_limit,
    }
