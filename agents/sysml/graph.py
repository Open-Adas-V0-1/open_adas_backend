from langgraph.graph import END, START, StateGraph

from agents.sysml.nodes import (
    contextual_answer,
    finalize,
    generate_node,
    plan_node,
    requirement_review,
    route_from_review,
    route_from_supervisor,
    route_from_verify,
    sysml_supervisor,
    verify_node,
)
from agents.sysml.state import SysmlState


def build_sysml_graph(checkpointer=None):
    """Build the SysML single-processing (Layer 3) subgraph: plan -> generate -> verify,
    iterating to a clean result before human review. Compiled WITHOUT its own
    checkpointer by default — it inherits the parent's when invoked as a node (Layer 2).
    Tests may pass one in.
    """
    builder = StateGraph(SysmlState)

    builder.add_node("sysml_supervisor", sysml_supervisor)
    builder.add_node("plan_node", plan_node)
    builder.add_node("generate_node", generate_node)
    builder.add_node("verify_node", verify_node)
    builder.add_node("requirement_review", requirement_review)
    builder.add_node("contextual_answer", contextual_answer)
    builder.add_node("finalize", finalize)

    builder.add_edge(START, "sysml_supervisor")

    builder.add_conditional_edges(
        "sysml_supervisor",
        route_from_supervisor,
        {
            "plan_node": "plan_node",
            "contextual_answer": "contextual_answer",
            END: END,
        },
    )

    builder.add_edge("plan_node", "generate_node")
    builder.add_edge("generate_node", "verify_node")

    builder.add_conditional_edges(
        "verify_node",
        route_from_verify,
        {
            "generate_node": "generate_node",
            "requirement_review": "requirement_review",
        },
    )

    builder.add_conditional_edges(
        "requirement_review",
        route_from_review,
        {
            "plan_node": "plan_node",
            "contextual_answer": "contextual_answer",
            "finalize": "finalize",
            END: END,
        },
    )

    builder.add_edge("contextual_answer", "requirement_review")
    builder.add_edge("finalize", END)

    return builder.compile(checkpointer=checkpointer)
