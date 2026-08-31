from langgraph.graph import END, START, StateGraph

from app.config import get_settings
from supervisor.plan import plan_node
from supervisor.router import route_from_top_supervisor, top_level_supervisor
from supervisor.state import SupervisorState


def build_supervisor_graph(checkpointer=None):
    """Build the top-level supervisor graph -- the head of the whole harness. Owns the
    SINGLE production checkpointer (passed in by the caller, encrypted + serialized as
    in T6a); Layer 2 and Layer 3 will inherit it through a wrapper node again once
    execution/delegation is rebuilt (Step 3), and never build their own.

    Step 1: the hub (top_level_supervisor) was the only node -- every classification
    ended the turn directly.
    Step 2 (this build): planning becomes a CONDITIONAL path. needs_execution now
    routes to plan_node, which decomposes the request into an ordered TODO list
    (plan_state) and hands control back to the hub; simple_response and unclear are
    UNCHANGED from Step 1 and never reach plan_node. Steps 3-5 add sysml_middle_node /
    memory_optimization / finalize_turn as further NEW conditional targets off
    route_from_top_supervisor, without needing to restructure this shape.
    """
    builder = StateGraph(SupervisorState)

    builder.add_node("top_level_supervisor", top_level_supervisor)
    builder.add_node("plan_node", plan_node)

    builder.add_edge(START, "top_level_supervisor")

    builder.add_conditional_edges(
        "top_level_supervisor",
        route_from_top_supervisor,
        {
            "plan_node": "plan_node",
            END: END,
        },
    )

    builder.add_edge("plan_node", "top_level_supervisor")

    return builder.compile(checkpointer=checkpointer)


def build_supervisor_config(thread_id: str, **extra_configurable) -> dict:
    """Build the RunnableConfig callers should use to invoke the top-level graph, with
    the env-driven step-count guard (SUPERVISOR_RECURSION_LIMIT) applied.
    """
    return {
        "configurable": {"thread_id": thread_id, **extra_configurable},
        "recursion_limit": get_settings().supervisor_recursion_limit,
    }
