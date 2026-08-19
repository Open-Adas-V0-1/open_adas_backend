from langchain_core.runnables import RunnableConfig
from langgraph.graph import END, START, StateGraph

from agents.sysml.middle_graph import build_middle_graph
from app.config import get_settings
from harness.guards import checkpoint_durability
from supervisor.finalize import finalize_turn
from supervisor.router import route_from_top_supervisor, top_level_supervisor
from supervisor.state import SupervisorState

# Compiled ONCE, WITHOUT its own checkpointer — inherits whatever checkpointer the
# caller's compiled graph carries (the production checkpointer, owned by the top level).
_middle_graph = build_middle_graph()


async def sysml_middle_node(state: SupervisorState, config: RunnableConfig) -> dict:
    """The 'inside a node' wrapper (spike-validated pattern, same shape as T5a's
    sysml_processing) for the top -> middle boundary. Deterministic per-dispatch
    thread_id derived from state that was already fixed BEFORE this node ran
    (session_id + processing_index) — not random, since this node re-runs from the
    top if anything beneath it pauses and resumes.
    """
    proc_index = state.get("processing_index") or 1
    session_id = state["session_id"]
    middle_thread_id = f"{session_id}:middle:{proc_index}"

    child_config = {
        **config,
        "configurable": {**config["configurable"], "thread_id": middle_thread_id},
        "recursion_limit": get_settings().sysml_middle_recursion_limit,
    }

    middle_input = {
        "user_input": state.get("user_input", ""),
        "session_id": session_id,
        "target_requirement_id": state.get("target_requirement_id"),
    }

    # If the middle layer (or layer-3 beneath it) pauses here, this raises internally
    # and propagates all the way up through this node to whoever invoked the TOP graph
    # — the two-level bubbling validated by the spike and T5a, now reachable three
    # levels deep (L3 -> L2 -> L1 caller) through this boundary plus that one.
    middle_output = await _middle_graph.ainvoke(
        middle_input, child_config, durability=checkpoint_durability()
    )

    light_ref = middle_output.get("processing_result")
    plan = state.get("plan") or {}
    steps_done = [*plan.get("steps_done", []), "sysml"]

    return {
        "sysml_result": light_ref,
        "plan": {**plan, "steps_done": steps_done},
        "active_agent": None,
        "result": "processed",
    }


def build_supervisor_graph(checkpointer=None):
    """Build the top-level supervisor graph — the head of the whole harness. Owns the
    SINGLE production checkpointer (passed in by the caller); layers 2 and 3 inherit it
    through the wrapper nodes above/below, and never build their own.
    """
    builder = StateGraph(SupervisorState)

    builder.add_node("top_level_supervisor", top_level_supervisor)
    builder.add_node("sysml_middle_node", sysml_middle_node)
    builder.add_node("finalize_turn", finalize_turn)

    builder.add_edge(START, "top_level_supervisor")

    builder.add_conditional_edges(
        "top_level_supervisor",
        route_from_top_supervisor,
        {
            "sysml_middle_node": "sysml_middle_node",
            "finalize_turn": "finalize_turn",
            END: END,
        },
    )

    builder.add_edge("sysml_middle_node", "top_level_supervisor")
    builder.add_edge("finalize_turn", END)

    return builder.compile(checkpointer=checkpointer)


def build_supervisor_config(thread_id: str, **extra_configurable) -> dict:
    """Build the RunnableConfig callers should use to invoke the top-level graph, with
    the env-driven step-count guard (SUPERVISOR_RECURSION_LIMIT) applied.
    """
    return {
        "configurable": {"thread_id": thread_id, **extra_configurable},
        "recursion_limit": get_settings().supervisor_recursion_limit,
    }
