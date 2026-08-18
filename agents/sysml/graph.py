from langgraph.graph import END, START, StateGraph

from agents.sysml.nodes import (
    apply_published_delta,
    contextual_answer,
    generate_diagram,
    generate_requirement,
    guard_requirement_available,
    message_no_requirement,
    promote_requirement,
    requirement_review,
    route_from_guard,
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
    builder.add_node("guard_requirement_available", guard_requirement_available)
    builder.add_node("generate_diagram", generate_diagram)
    builder.add_node("message_no_requirement", message_no_requirement)
    builder.add_node("apply_published_delta", apply_published_delta)
    builder.add_node("contextual_answer", contextual_answer)
    builder.add_node("requirement_review", requirement_review)
    builder.add_node("stockage_output", stockage_output)
    builder.add_node("promote_requirement", promote_requirement)

    builder.add_edge(START, "sysml_supervisor")

    builder.add_conditional_edges(
        "sysml_supervisor",
        route_from_supervisor,
        {
            "generate_requirement": "generate_requirement",
            "guard_requirement_available": "guard_requirement_available",
            "apply_published_delta": "apply_published_delta",
            END: END,
        },
    )

    builder.add_conditional_edges(
        "guard_requirement_available",
        route_from_guard,
        {"generate_diagram": "generate_diagram", "message_no_requirement": "message_no_requirement"},
    )

    builder.add_edge("generate_requirement", "requirement_review")
    builder.add_edge("generate_diagram", "requirement_review")
    builder.add_edge("apply_published_delta", "requirement_review")
    builder.add_edge("message_no_requirement", END)
    builder.add_edge("contextual_answer", "requirement_review")

    builder.add_conditional_edges(
        "requirement_review",
        route_from_review,
        {
            "generate_requirement": "generate_requirement",
            "generate_diagram": "generate_diagram",
            "apply_published_delta": "apply_published_delta",
            "contextual_answer": "contextual_answer",
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
