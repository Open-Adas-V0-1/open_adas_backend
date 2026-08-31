from langgraph.graph import END

from app.config import get_settings
from app.schemas.supervisor import HubClassification, HubDecision
from harness.guards import guard_breached
from llm.factory import get_llm
from llm.prompts import load_prompt
from supervisor.state import SupervisorState

_FALLBACK_SIMPLE_RESPONSE = "Hi! How can I help with your SysML v2 requirements or diagrams today?"
_FALLBACK_CLARIFY = "Could you clarify what you'd like me to do?"
_NEEDS_EXECUTION_PLACEHOLDER = "I'll work on that."


async def top_level_supervisor(state: SupervisorState) -> dict:
    """The HUB every turn enters first (Layer-1 rebuild, Step 1). Structured-output
    classification + router-as-code: decides whether the message can be answered
    DIRECTLY (simple_response), needs real work (needs_execution -- Steps 2-3 build the
    actual planning/delegation path; this step only detects and marks it with a
    placeholder response), or is too ambiguous to act on (unclear -- ask, fail-open).

    No planning happens here. Deep, artifact-specific explanations are explicitly OUT
    of scope for this node -- those belong to a later context/QA path.
    """
    visits = (state.get("supervisor_visits") or 0) + 1
    max_visits = get_settings().supervisor_max_visits

    # Loop guard: env-driven (SUPERVISOR_MAX_VISITS), fail-open — kept wired even
    # though this step's loop is trivial (one visit per turn), for Steps 2-5's
    # multi-visit planning loop to rely on unchanged.
    if guard_breached(visits, max_visits):
        return {
            "supervisor_visits": visits,
            "done": True,
            "result": "stopped: max supervisor visits reached",
        }

    llm = get_llm("top_level_supervisor").with_structured_output(HubDecision)
    prompt = load_prompt("supervisor/general_answer.md", user_input=state.get("user_input", ""))
    decision: HubDecision = await llm.ainvoke(prompt)

    classification = decision.classification.value if decision.classification else None

    if classification == HubClassification.needs_execution.value:
        # Steps 2-3 build the real planning/delegation path off this classification.
        # For now: detect + mark it, with a short placeholder response so the
        # classification itself is testable end to end.
        return {
            "supervisor_visits": visits,
            "classification": classification,
            "response": _NEEDS_EXECUTION_PLACEHOLDER,
            "done": True,
            "result": "needs_execution",
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
    # Step 1: every classification ends the turn here (simple_response and unclear
    # answer directly; needs_execution returns its placeholder). Steps 2-5 add
    # plan_node / sysml_middle_node / memory_optimization / finalize_turn as NEW
    # conditional targets keyed off `classification`, without restructuring this hub.
    return END
