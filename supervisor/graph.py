from langchain_core.runnables import RunnableConfig
from langgraph.graph import END, START, StateGraph

from agents.sysml.middle_graph import build_middle_graph
from app.config import get_settings
from harness.guards import checkpoint_durability
from supervisor.finalize import finalize_turn
from supervisor.memory import memory_optimization
from supervisor.plan import plan_node
from supervisor.plan_ops import in_progress_task, with_task_status
from supervisor.plan_review import plan_review, route_from_plan_node, route_from_plan_review
from supervisor.router import route_from_top_supervisor, top_level_supervisor
from supervisor.state import SupervisorState

# Compiled ONCE, WITHOUT its own checkpointer — inherits whatever checkpointer the
# caller's compiled graph carries (the production checkpointer, owned by the top level).
_middle_graph = build_middle_graph()


async def sysml_middle_node(state: SupervisorState, config: RunnableConfig) -> dict:
    """The 'inside a node' wrapper (spike-validated pattern, same shape as Layer-2's
    own sysml_processing) for the Layer-1 -> Layer-2 boundary. Executes exactly the ONE
    task top_level_supervisor just marked in_progress.

    Deterministic per-processing thread id, derived from state fixed BEFORE this node
    ran (session_id + the task's own id, not a random uuid) -- this node re-runs from
    the top if Layer-2 (or Layer-3 beneath it) pauses and resumes, and must reconstruct
    the SAME child thread id every time for the checkpointer to resume correctly. Each
    task in a plan gets its OWN middle_thread_id -- distinct rows in Postgres, verified
    per task.

    Known characteristic (not a bug to fix here): Layer-2's OWN sysml_processing derives
    ITS child (layer-3) thread id as f"{session_id}:proc:{processing_counter}" --
    session_id-scoped, not middle_thread_id-scoped, and processing_counter resets to 1
    on every FRESH middle-graph invocation. Since each task here gets a fresh
    middle_thread_id (by design, above), two DIFFERENT tasks' layer-3 sub-threads can
    end up with the SAME computed string. This is harmless in practice under Layer-1's
    strictly-sequential execution (verified: each task's artifact still finalizes
    correctly, with distinct content, since a fresh dict invoke always starts a new run
    from START regardless of prior history under that thread id) -- but changing Layer-2's
    formula to avoid it would break its own regression suite (smoke_test_t5a.py hardcodes
    the current f"{session.id}:proc:1" shape), which is explicitly out of scope here.
    """
    plan_state = state["plan_state"]
    task = in_progress_task(plan_state)
    session_id = state["session_id"]
    middle_thread_id = f"{session_id}:middle:{task['id']}"

    child_config = {
        **config,
        "configurable": {**config["configurable"], "thread_id": middle_thread_id},
        "recursion_limit": get_settings().sysml_middle_recursion_limit,
    }

    # Map the TodoItem onto Layer-2's entry (MiddleState): Layer-2's own
    # middle_supervisor re-derives intent/level via its OWN structured-output call from
    # user_input text (its established, already-tested contract -- unchanged here), so
    # the task's already-known level is folded into the text as a hint rather than
    # inventing a new Layer-2 entry field. When this task DEPENDS on another (e.g. a
    # diagram depending on the requirement it represents), pass that dependency's
    # finalized artifact_id through as target_requirement_id too -- Layer-2's CURRENT
    # middle_supervisor doesn't yet consume an incoming target (it always derives its
    # own via named/sole-active-requirement heuristics, unchanged here per "don't touch
    # Layer-2"), so this is presently inert but forward-compatible; it resolves
    # correctly today anyway because the dependency's requirement is the sole active
    # one in the session by the time the dependent task runs.
    level_hint = f" (level: {task['level']})" if task.get("level") else ""
    middle_user_input = f"{task['description']}{level_hint}"

    target_requirement_id = None
    depends_on = task.get("depends_on")
    if depends_on:
        dependency = next((t for t in plan_state["tasks"] if t["id"] == depends_on), None)
        if dependency and dependency.get("result_ref"):
            target_requirement_id = dependency["result_ref"].get("artifact_id")

    middle_input = {
        "user_input": middle_user_input,
        "session_id": session_id,
        "target_requirement_id": target_requirement_id,
    }

    # If Layer-2 (or Layer-3 beneath it) pauses here, this raises internally and
    # propagates all the way up through this node, through the top graph's runner, to
    # whoever invoked THIS (top) graph -- the two/three-level bubbling validated by the
    # original spike and T5a/T6a, now reached via the rebuilt Layer-1 hub + execution
    # loop instead of the old always-dispatching planner.
    middle_output = await _middle_graph.ainvoke(
        middle_input, child_config, durability=checkpoint_durability()
    )

    light_ref = middle_output.get("processing_result")

    return {
        "plan_state": with_task_status(plan_state, task["id"], "done", result_ref=light_ref),
        "results": [*(state.get("results") or []), light_ref],
        "result": "task_processed",
    }


def build_supervisor_graph(checkpointer=None):
    """Build the top-level supervisor graph -- the head of the whole harness. Owns the
    SINGLE production checkpointer (passed in by the caller, encrypted + serialized as
    in T6a); Layer 2 and Layer 3 inherit it through this wrapper node (and Layer-2's own
    wrapper down to Layer 3), and never build their own.

    Step 1: the hub was the only node -- every classification ended the turn directly.
    Step 2: needs_execution routes through plan_node, which decomposes the request into
    an ordered TODO list (plan_state) and hands control back to the hub; simple_response
    and unclear are unchanged and never reach plan_node.
    Step 3: the hub becomes the EXECUTION LOOP driver too -- with a plan_state
    present, it picks the next eligible pending task (dependency order respected) and
    delegates it to sysml_middle_node; sysml_middle_node records the task's light
    reference and hands control back, looping WITHOUT stopping until every task is done.
    Step 4 (this build): a COMPLEX plan (more than one task) is routed through
    plan_review for HITL approval/editing/cancellation BEFORE execution starts; a
    SIMPLE (single-task) plan skips it entirely, straight to execution, no friction.
    Step 5 (this build): every turn now ends through finalize_turn instead of
    route_from_top_supervisor returning END directly. memory_optimization is a further
    CONDITIONAL target off that same routing decision -- only entered when the
    short-term context is near its configured budget (memory_ops.is_context_near_full);
    most turns skip it, straight to finalize_turn. Summarization itself is DEFERRED
    (memory_optimization is a pass-through node for now); only the routing is real.
    """
    builder = StateGraph(SupervisorState)

    builder.add_node("top_level_supervisor", top_level_supervisor)
    builder.add_node("plan_node", plan_node)
    builder.add_node("plan_review", plan_review)
    builder.add_node("sysml_middle_node", sysml_middle_node)
    builder.add_node("memory_optimization", memory_optimization)
    builder.add_node("finalize_turn", finalize_turn)

    builder.add_edge(START, "top_level_supervisor")

    builder.add_conditional_edges(
        "top_level_supervisor",
        route_from_top_supervisor,
        {
            "plan_node": "plan_node",
            "sysml_middle_node": "sysml_middle_node",
            "memory_optimization": "memory_optimization",
            "finalize_turn": "finalize_turn",
            END: END,
        },
    )

    builder.add_conditional_edges(
        "plan_node",
        route_from_plan_node,
        {
            "plan_review": "plan_review",
            "top_level_supervisor": "top_level_supervisor",
        },
    )

    builder.add_conditional_edges(
        "plan_review",
        route_from_plan_review,
        {
            "top_level_supervisor": "top_level_supervisor",
            END: END,
        },
    )

    builder.add_edge("sysml_middle_node", "top_level_supervisor")

    builder.add_edge("memory_optimization", "finalize_turn")
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
