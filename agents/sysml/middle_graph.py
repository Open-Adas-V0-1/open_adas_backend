from langgraph.graph import END, START, StateGraph

from agents.sysml.middle_nodes import (
    middle_supervisor,
    route_from_middle_supervisor,
    route_from_user_confirm,
    sysml_processing,
    user_confirm_inputs,
)
from agents.sysml.middle_state import MiddleState


def build_middle_graph(checkpointer=None):
    """Build the SysML middle (layer-2) subgraph. Compiled WITHOUT its own
    checkpointer by default — inherits the caller's, same as layer-3. Tests (and
    eventually the top-level layer-1 graph) may pass one in.
    """
    builder = StateGraph(MiddleState)

    builder.add_node("middle_supervisor", middle_supervisor)
    builder.add_node("user_confirm_inputs", user_confirm_inputs)
    builder.add_node("sysml_processing", sysml_processing)

    builder.add_edge(START, "middle_supervisor")

    builder.add_conditional_edges(
        "middle_supervisor",
        route_from_middle_supervisor,
        {
            "sysml_processing": "sysml_processing",
            "user_confirm_inputs": "user_confirm_inputs",
            END: END,
        },
    )

    builder.add_conditional_edges(
        "user_confirm_inputs",
        route_from_user_confirm,
        {
            "sysml_processing": "sysml_processing",
            "middle_supervisor": "middle_supervisor",
            END: END,
        },
    )

    builder.add_edge("sysml_processing", "middle_supervisor")

    return builder.compile(checkpointer=checkpointer)
