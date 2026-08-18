from langchain_core.runnables import RunnableConfig
from langgraph.graph import END

from agents.sysml.graph import build_sysml_graph
from agents.sysml.middle_state import MiddleState
from app.schemas.sysml import MiddleDecision
from llm.factory import get_llm
from llm.prompts import load_prompt

MAX_SUPERVISOR_VISITS = 5

# Compiled ONCE, WITHOUT its own checkpointer — inherits whatever checkpointer the
# caller's compiled graph carries, exactly like sysml_processing inherits it below.
_sysml_processing_graph = build_sysml_graph()


async def middle_supervisor(state: MiddleState) -> dict:
    visits = (state.get("supervisor_visits") or 0) + 1

    # Loop guard: an unbounded middle_supervisor <-> sysml_processing loop would be a bug,
    # not a valid use case. Fail open to END rather than looping forever.
    if visits > MAX_SUPERVISOR_VISITS:
        return {
            "supervisor_visits": visits,
            "resolved_intent": None,
            "result": "stopped: max supervisor visits reached",
        }

    llm = get_llm("sysml_middle_supervisor").with_structured_output(MiddleDecision)
    prompt = load_prompt("sysml/middle_supervisor.md", user_input=state.get("user_input", ""))
    decision: MiddleDecision = await llm.ainvoke(prompt)

    if decision.has_request:
        return {
            "supervisor_visits": visits,
            "resolved_intent": decision.resolved_intent.value if decision.resolved_intent else None,
            "clarifying_message": None,
            "processing_counter": (state.get("processing_counter") or 0) + 1,
        }

    # No actionable request THIS visit: clear any stale intent from a prior visit so
    # the router doesn't loop back into sysml_processing on old state.
    return {
        "supervisor_visits": visits,
        "resolved_intent": None,
        "clarifying_message": decision.message,
        "result": "no_action",
    }


def route_from_middle_supervisor(state: MiddleState) -> str:
    if (state.get("supervisor_visits") or 0) > MAX_SUPERVISOR_VISITS:
        return END
    if state.get("resolved_intent"):
        return "sysml_processing"
    return END


async def sysml_processing(state: MiddleState, config: RunnableConfig) -> dict:
    """The 'inside a node' wrapper (spike-validated pattern) for the middle -> layer-3
    boundary. Derives a DETERMINISTIC per-processing thread_id from state that was
    already fixed BEFORE this node started (session_id + processing_counter) — not a
    random uuid, since this node re-runs from the top if layer-3 pauses and resumes.
    """
    proc_counter = state.get("processing_counter") or 1
    session_id = state["session_id"]
    proc_thread_id = f"{session_id}:proc:{proc_counter}"

    child_config = {
        **config,
        "configurable": {**config["configurable"], "thread_id": proc_thread_id},
    }

    l3_input = {
        "user_input": state.get("user_input", ""),
        "session_id": session_id,
        "target_requirement_id": state.get("target_requirement_id"),
    }

    # If layer-3 pauses here (interrupt), this raises internally and propagates all the
    # way up through this node, through the middle graph's runner, to whoever invoked
    # THIS (middle) graph — exactly the two-level bubbling validated in the spike.
    # Async invoke: required by AsyncPostgresSaver (a sync .invoke() call against an
    # async checkpointer raises InvalidStateError outside the checkpointer's own thread).
    l3_output = await _sysml_processing_graph.ainvoke(l3_input, child_config)

    artifact_type = "diagram" if l3_output.get("active_diagram_id") else "requirement"
    artifact_id = l3_output.get("active_diagram_id") or l3_output.get("active_requirement_id")

    # LIGHT reference only — ids + a summary, never the full artifact content.
    light_ref = {
        "processing_id": proc_counter,
        "thread_id": proc_thread_id,
        "artifact_type": artifact_type,
        "artifact_id": artifact_id,
        "summary": l3_output.get("result"),
    }

    return {
        "proc_id": str(proc_counter),
        "proc_thread_id": proc_thread_id,
        "processing_result": light_ref,
        "resolved_intent": None,  # consumed; middle_supervisor decides fresh next visit
        "result": "processed",
    }
