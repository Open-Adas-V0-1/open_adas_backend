from langgraph.graph import END

from app.config import get_settings
from app.schemas.supervisor import HubClassification, HubDecision
from harness.guards import guard_breached
from llm.factory import get_llm
from llm.prompts import load_prompt
from supervisor.plan_ops import in_progress_task, next_pending_task, with_task_status
from supervisor.state import SupervisorState

_FALLBACK_SIMPLE_RESPONSE = "Hi! How can I help with your SysML v2 requirements or diagrams today?"
_FALLBACK_CLARIFY = "Could you clarify what you'd like me to do?"


async def top_level_supervisor(state: SupervisorState) -> dict:
    """The HUB every turn enters first (Layer-1 rebuild). Structured-output
    classification + router-as-code: decides whether the message can be answered
    DIRECTLY (simple_response), needs real work (needs_execution -- routes to plan_node),
    or is too ambiguous to act on (unclear -- ask, fail-open).

    No planning happens here — that's plan_node's job. Deep, artifact-specific
    explanations are explicitly OUT of scope for this node -- those belong to a later
    context/QA path.

    This node is ALSO the EXECUTION LOOP driver (Step 3): every time control returns
    here with a plan_state present (from plan_node, or from sysml_middle_node having
    just finished a task), it picks the next eligible pending task (respecting
    dependency order), marks it in_progress, and hands off to sysml_middle_node --
    without stopping in between tasks. When no pending task remains, the turn ends here
    (memory/finalize is Step 5).
    """
    visits = (state.get("supervisor_visits") or 0) + 1
    max_visits = get_settings().supervisor_max_visits

    # Loop guard: env-driven (SUPERVISOR_MAX_VISITS), fail-open — a breach ends the
    # turn safely instead of crashing or looping forever, even mid multi-task execution
    # (whatever tasks already completed keep their recorded result_ref; nothing crashes).
    if guard_breached(visits, max_visits):
        return {
            "supervisor_visits": visits,
            "done": True,
            "result": "stopped: max supervisor visits reached",
        }

    plan_state = state.get("plan_state")
    if plan_state:
        # Returning here with a plan means EITHER plan_node just finished (all tasks
        # still pending) OR sysml_middle_node just finished one task -- either way, pick
        # the next eligible task deterministically (router-as-code, no LLM call).
        task = next_pending_task(plan_state)
        if task is None:
            # No pending task left (and none in_progress, since sysml_middle_node
            # always marks its task done before returning) -> execution complete.
            return {
                "supervisor_visits": visits,
                "done": True,
                "result": "execution_complete",
            }
        return {
            "supervisor_visits": visits,
            "plan_state": with_task_status(plan_state, task["id"], "in_progress"),
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
    plan_state = state.get("plan_state")
    if plan_state and in_progress_task(plan_state) is not None:
        # A task was just marked in_progress this visit -> delegate it to Layer-2.
        return "sysml_middle_node"
    # simple_response and unclear answer directly and end here (unchanged from Steps
    # 1-2). needs_execution with a plan whose tasks are ALL done also ends here for now
    # -- Step 5 adds memory_optimization/finalize_turn as a further conditional target
    # off this same shape, without restructuring this hub.
    return END
