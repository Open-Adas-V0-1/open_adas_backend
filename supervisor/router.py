from langgraph.graph import END

from app.config import get_settings
from app.schemas.supervisor import TopDecision
from harness.guards import guard_breached
from llm.factory import get_llm
from llm.prompts import load_prompt
from supervisor.state import SupervisorState


def _default_plan(state: SupervisorState) -> dict:
    return {
        "goal": state.get("user_input", ""),
        "steps_done": [],
        "next_step": None,
        "complete": False,
    }


async def top_level_supervisor(state: SupervisorState) -> dict:
    """The planner/orchestrator brain. Structured-output decision + router-as-code.
    Maintains a lightweight plan across visits so multi-step work doesn't stop early,
    and evaluates completion itself rather than assuming one dispatch is the whole job.
    """
    visits = (state.get("supervisor_visits") or 0) + 1
    max_visits = get_settings().supervisor_max_visits

    # Loop guard: env-driven (SUPERVISOR_MAX_VISITS), fail-open — a breach ends the
    # turn safely instead of crashing or looping forever.
    if guard_breached(visits, max_visits):
        return {
            "supervisor_visits": visits,
            "active_agent": None,
            "done": True,
            "result": "stopped: max supervisor visits reached",
        }

    plan = state.get("plan") or _default_plan(state)

    llm = get_llm("top_level_supervisor").with_structured_output(TopDecision)
    prompt = load_prompt(
        "supervisor/planning.md",
        user_input=state.get("user_input", ""),
        plan=str(plan),
        sysml_result=str(state.get("sysml_result")),
    )
    decision: TopDecision = await llm.ainvoke(prompt)

    if decision.intent_complete:
        plan = {**plan, "complete": True}
        return {
            "supervisor_visits": visits,
            "plan": plan,
            "active_agent": None,
            "clarifying_message": decision.message,
        }

    if decision.active_agent is None:
        # fail-open: nothing actionable this visit -> end gracefully, don't crash.
        return {
            "supervisor_visits": visits,
            "plan": plan,
            "active_agent": None,
            "clarifying_message": decision.message,
            "result": "no_action",
        }

    plan = {**plan, "next_step": decision.active_agent.value}
    return {
        "supervisor_visits": visits,
        "plan": plan,
        "active_agent": decision.active_agent.value,
        "processing_index": (state.get("processing_index") or 0) + 1,
        "clarifying_message": None,
    }


def route_from_top_supervisor(state: SupervisorState) -> str:
    if guard_breached(state.get("supervisor_visits") or 0, get_settings().supervisor_max_visits):
        return END
    plan = state.get("plan") or {}
    if plan.get("complete"):
        return "finalize_turn"
    if state.get("active_agent") == "sysml":
        return "sysml_middle_node"
    return END
