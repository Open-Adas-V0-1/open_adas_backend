from langgraph.graph import END, START, StateGraph

from agents.sysml.middle_nodes import (
    build_structured_format,
    middle_supervisor,
    resolve_level,
    route_from_middle_supervisor,
    route_from_resolve_level,
    route_from_user_confirm,
    route_from_validate_inputs,
    sysml_processing,
    user_confirm_inputs,
    validate_inputs,
)
from agents.sysml.middle_state import MiddleState
from app.config import get_settings


def build_middle_config(thread_id: str, **extra_configurable) -> dict:
    """Build the RunnableConfig callers should use to invoke the middle graph, with
    the env-driven step-count guard (SYSML_MIDDLE_RECURSION_LIMIT) applied. This is
    additive to LangGraph's own recursion_limit config key, not a new mechanism.
    """
    return {
        "configurable": {"thread_id": thread_id, **extra_configurable},
        "recursion_limit": get_settings().sysml_middle_recursion_limit,
    }


def build_middle_graph(checkpointer=None):
    """Build the SysML middle (layer-2) subgraph. Compiled WITHOUT its own
    checkpointer by default — inherits the caller's, same as layer-3. Tests (and
    eventually the top-level layer-1 graph) may pass one in.
    """
    builder = StateGraph(MiddleState)

    builder.add_node("middle_supervisor", middle_supervisor)
    builder.add_node("validate_inputs", validate_inputs)
    builder.add_node("resolve_level", resolve_level)
    builder.add_node("build_structured_format", build_structured_format)
    builder.add_node("user_confirm_inputs", user_confirm_inputs)
    builder.add_node("sysml_processing", sysml_processing)

    builder.add_edge(START, "middle_supervisor")

    builder.add_conditional_edges(
        "middle_supervisor",
        route_from_middle_supervisor,
        {
            "validate_inputs": "validate_inputs",
            "user_confirm_inputs": "user_confirm_inputs",
            END: END,
        },
    )

    builder.add_conditional_edges(
        "validate_inputs",
        route_from_validate_inputs,
        {
            "resolve_level": "resolve_level",
            "build_structured_format": "build_structured_format",
            "user_confirm_inputs": "user_confirm_inputs",
        },
    )

    builder.add_conditional_edges(
        "resolve_level",
        route_from_resolve_level,
        {
            "build_structured_format": "build_structured_format",
            "user_confirm_inputs": "user_confirm_inputs",
        },
    )

    builder.add_conditional_edges(
        "user_confirm_inputs",
        route_from_user_confirm,
        {
            "resolve_level": "resolve_level",
            "build_structured_format": "build_structured_format",
            "middle_supervisor": "middle_supervisor",
            END: END,
        },
    )

    builder.add_edge("build_structured_format", "sysml_processing")
    builder.add_edge("sysml_processing", "middle_supervisor")

    return builder.compile(checkpointer=checkpointer)
