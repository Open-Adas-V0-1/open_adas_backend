from langgraph.graph import END
from langgraph.types import interrupt

from app.schemas.supervisor import PlannedTask, PlanReviewPattern, PlanReviewTaskSummary
from supervisor.plan_ops import build_todo_items, is_complex_plan
from supervisor.state import SupervisorState

_REVIEW_QUESTION = "Here's the plan I've put together for your request -- approve it, edit it, or cancel."


async def plan_review(state: SupervisorState) -> dict:
    """Plan-level HITL (Layer-1 rebuild, Step 4): reached ONLY for a COMPLEX plan
    (more than one task -- see plan_ops.is_complex_plan). Presents the ordered TODO
    list for approval BEFORE any execution begins. A single-task plan skips this
    entirely (route_from_plan_node) to keep simple turns frictionless.

    No side effects above interrupt() -- this node re-runs from the top on resume.
    """
    plan_state = state["plan_state"]
    payload = PlanReviewPattern(
        question=_REVIEW_QUESTION,
        tasks=[
            PlanReviewTaskSummary(
                id=t["id"], description=t["description"], intent=t["intent"],
                level=t.get("level"), depends_on=t.get("depends_on"),
            )
            for t in plan_state["tasks"]
        ],
    ).model_dump(mode="json")

    decision = interrupt(payload)
    action = decision.get("action") if isinstance(decision, dict) else decision

    if action == "approve":
        return {"plan_review_decision": "approved"}

    if action == "modify":
        edited = decision.get("tasks") if isinstance(decision, dict) else None
        if not edited:
            # fail-open: an empty/missing edit is treated as a cancel rather than
            # silently executing a stale or fabricated plan.
            return {"plan_review_decision": "cancelled", "plan_state": None, "classification": None}
        # Light re-validation path (preferred over re-running plan_node's LLM
        # decomposition, which would discard the user's edits): the SAME deterministic
        # construction plan_node itself uses, fed by the user's edited task list
        # instead of the LLM's -- ids and dependencies are always freshly re-derived
        # from CURRENT order, so a dropped/reordered/added task never leaves a dangling
        # reference.
        planned_tasks = [PlannedTask.model_validate(t) for t in edited]
        new_tasks = build_todo_items(planned_tasks)
        updated_plan_state = {
            **plan_state,
            "tasks": [t.model_dump(mode="json") for t in new_tasks],
        }
        return {"plan_review_decision": "modified", "plan_state": updated_plan_state}

    # cancel, or anything unrecognized -> fail-open to cancel rather than looping or
    # crashing. Clears plan_state so a later turn on this SAME thread starts fresh
    # (classifies the next message) instead of finding a stale, never-run plan.
    return {"plan_review_decision": "cancelled", "plan_state": None, "classification": None}


def route_from_plan_node(state: SupervisorState) -> str:
    plan_state = state.get("plan_state")
    if not plan_state:
        # plan_node's own insufficiency interrupt/resume already handled -- re-classify
        # the (now clarified) request at the hub.
        return "top_level_supervisor"
    if is_complex_plan(plan_state):
        return "plan_review"
    # simple (single-task) plan -- straight to execution, no friction.
    return "top_level_supervisor"


def route_from_plan_review(state: SupervisorState) -> str:
    decision = state.get("plan_review_decision")
    if decision in ("approved", "modified"):
        return "top_level_supervisor"
    return END  # cancelled, or unrecognized -> fail-open to END
