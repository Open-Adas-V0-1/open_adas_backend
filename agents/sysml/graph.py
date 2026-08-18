from langgraph.graph import END, START, StateGraph

from agents.sysml.nodes import (
    generate_requirement,
    promote_requirement,
    requirement_review,
    route_from_review,
    route_from_stockage,
    route_from_supervisor,
    stockage_output,
    sysml_supervisor,
)
from agents.sysml.state import SysmlState


def build_sysml_graph(checkpointer=None):
    """Build the SysML subgraph. Compiled WITHOUT its own checkpointer by default —
    it inherits the parent's when invoked as a node (T5). Tests may pass one in.
    """
    builder = StateGraph(SysmlState)

    builder.add_node("sysml_supervisor", sysml_supervisor)
    builder.add_node("generate_requirement", generate_requirement)
    builder.add_node("requirement_review", requirement_review)
    builder.add_node("stockage_output", stockage_output)
    builder.add_node("promote_requirement", promote_requirement)

    builder.add_edge(START, "sysml_supervisor")

    builder.add_conditional_edges(
        "sysml_supervisor",
        route_from_supervisor,
        {"generate_requirement": "generate_requirement", END: END},
    )

    builder.add_edge("generate_requirement", "requirement_review")

    builder.add_conditional_edges(
        "requirement_review",
        route_from_review,
        {
            "generate_requirement": "generate_requirement",
            "stockage_output": "stockage_output",
            END: END,
        },
    )

    builder.add_conditional_edges(
        "stockage_output",
        route_from_stockage,
        {"promote_requirement": "promote_requirement"},
    )

    builder.add_edge("promote_requirement", END)

    return builder.compile(checkpointer=checkpointer)
