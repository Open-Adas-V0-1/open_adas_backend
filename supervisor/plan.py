from langgraph.types import interrupt

from app.schemas.supervisor import PlanDecision, PlanState, TodoItem, TodoStatus
from llm.factory import get_llm
from llm.prompts import load_prompt
from supervisor.state import SupervisorState

_FALLBACK_CLARIFY = "Could you give me a bit more detail so I can plan this out?"


async def plan_node(state: SupervisorState) -> dict:
    """Reached ONLY for needs_execution (never for simple_response/unclear -- those end
    at the hub). Decomposes the request into an ORDERED TODO list (plan_state) via
    structured output + router-as-code -- no work is dispatched here, just planning.

    Also performs the PLAN-level input-sufficiency check: if the request is too vague to
    decompose into concrete tasks, this asks the user via interrupt() (fail-open, never
    fabricates a plan) rather than guessing. This is distinct from Step 4's later
    plan_review HITL (which reviews an ALREADY-built plan); this is upstream of that,
    about whether a plan can be built at all.

    Resume contract: this node re-runs FROM THE TOP on resume (no side effects above
    interrupt()), and the interrupt is only reached when the LLM's OWN fresh verdict is
    "insufficient" again -- so a caller resuming with a clarified request must patch
    state directly via Command(update={"user_input": clarified_text}, resume=...),
    not rely on this node reading the new text out of the resume value alone (which
    would only apply if the LLM re-decomposing the STALE user_input still judges it
    insufficient, i.e. is never reliably reached once the clarification is actually
    good enough to resolve the original ambiguity).
    """
    llm = get_llm("plan_node").with_structured_output(PlanDecision)
    prompt = load_prompt("supervisor/plan_decompose.md", user_input=state.get("user_input", ""))
    decision: PlanDecision = await llm.ainvoke(prompt)

    if not decision.sufficient or not decision.tasks:
        # No side effects above this line -- this node re-runs from the top on resume.
        resume = interrupt({
            "type": "plan_clarify",
            "question": decision.clarifying_message or _FALLBACK_CLARIFY,
        })
        new_user_input = resume.get("user_input") if isinstance(resume, dict) else None
        return {
            "plan_state": None,
            # force fresh re-classification of the clarified request at the hub,
            # rather than re-entering planning on stale/no context.
            "classification": None,
            "user_input": new_user_input or state.get("user_input"),
        }

    tasks = [
        TodoItem(
            id=f"task-{i}",
            description=t.description,
            intent=t.intent,
            level=t.level,
            depends_on=f"task-{t.depends_on_task_number}" if t.depends_on_task_number else None,
            status=TodoStatus.pending,
            result_ref=None,
        )
        for i, t in enumerate(decision.tasks, start=1)
    ]
    plan_state = PlanState(tasks=tasks, original_request=state.get("user_input", ""))

    return {
        "plan_state": plan_state.model_dump(mode="json"),
        "result": "plan_built",
    }
