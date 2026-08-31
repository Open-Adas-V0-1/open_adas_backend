from langgraph.graph import END

from app.config import get_settings
from app.schemas.supervisor import HubClassification, HubDecision
from harness.guards import guard_breached
from llm.factory import get_llm
from llm.prompts import load_prompt
from supervisor.state import SupervisorState

_FALLBACK_SIMPLE_RESPONSE = "Hi! How can I help with your SysML v2 requirements or diagrams today?"
_FALLBACK_CLARIFY = "Could you clarify what you'd like me to do?"


async def top_level_supervisor(state: SupervisorState) -> dict:
    """The HUB every turn enters first (Layer-1 rebuild). Structured-output
    classification + router-as-code: decides whether the message can be answered
    DIRECTLY (simple_response), needs real work (needs_execution -- routes to plan_node,
    Step 2; delegation/execution is Step 3), or is too ambiguous to act on (unclear --
    ask, fail-open).

    No planning happens here — that's plan_node's job. Deep, artifact-specific
    explanations are explicitly OUT of scope for this node -- those belong to a later
    context/QA path.

    This node is ALSO the return point after plan_node builds a plan (the unconditional
    plan_node -> top_level_supervisor edge). When plan_state is already populated
    (i.e. this is that return visit, not a fresh turn), classification is skipped
    entirely -- the plan is simply handed back, and the turn ends here for now (Step 3
    wires the execution loop onward from a ready plan).
    """
    visits = (state.get("supervisor_visits") or 0) + 1
    max_visits = get_settings().supervisor_max_visits

    # Loop guard: env-driven (SUPERVISOR_MAX_VISITS), fail-open — a breach ends the
    # turn safely instead of crashing or looping forever.
    if guard_breached(visits, max_visits):
        return {
            "supervisor_visits": visits,
            "done": True,
            "result": "stopped: max supervisor visits reached",
        }

    if state.get("plan_state"):
        # Returning from plan_node with an already-built plan -- nothing to classify
        # again this visit. Step 3 will drive execution from here instead of ending.
        return {
            "supervisor_visits": visits,
            "done": True,
            "result": "plan_ready",
        }

    llm = get_llm("top_level_supervisor").with_structured_output(HubDecision)
    prompt = load_prompt("supervisor/general_answer.md", user_input=state.get("user_input", ""))
    decision: HubDecision = await llm.ainvoke(prompt)

    classification = decision.classification.value if decision.classification else None

    if classification == HubClassification.needs_execution.value:
        # Real work: hand off to plan_node (Step 2) to decompose into a TODO list.
        # No response/placeholder here anymore -- the plan itself IS the next step.
        return {
            "supervisor_visits": visits,
            "classification": classification,
            "response": None,
        }

    if classification == HubClassification.unclear.value:
        return {
            "supervisor_visits": visits,
            "classification": classification,
            "response": decision.response or _FALLBACK_CLARIFY,
            "done": True,
            "result": "unclear",
        }

    # simple_response, or any unrecognized classification -> fail-open to a direct
    # answer rather than crashing or leaving the turn hanging.
    return {
        "supervisor_visits": visits,
        "classification": classification or HubClassification.simple_response.value,
        "response": decision.response or _FALLBACK_SIMPLE_RESPONSE,
        "done": True,
        "result": "simple_response",
    }


def route_from_top_supervisor(state: SupervisorState) -> str:
    if guard_breached(state.get("supervisor_visits") or 0, get_settings().supervisor_max_visits):
        return END
    if state.get("classification") == HubClassification.needs_execution.value and not state.get("plan_state"):
        # needs_execution, no plan built yet this turn -> decompose it.
        return "plan_node"
    # simple_response and unclear answer directly and end here (unchanged from Step 1).
    # needs_execution WITH a plan_state already built also ends here for now -- Step 3
    # adds sysml_middle_node as a new conditional target off this same classification
    # check, without restructuring this hub.
    return END
