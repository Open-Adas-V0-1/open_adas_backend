from langgraph.graph import END

from app.config import get_settings
from app.schemas.supervisor import HubClassification, HubDecision
from harness.guards import guard_breached
from llm.factory import get_llm
from llm.prompts import load_prompt
from supervisor.memory_ops import is_context_near_full
from supervisor.plan_ops import in_progress_task, next_pending_task, with_task_status
from supervisor.state import SupervisorState
from supervisor.streaming_tags import TOKEN_STREAM_TAG

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
            # plan_state is left populated (all tasks "done") so the final response
            # can show what was accomplished this turn -- unlike plan_review's cancel
            # path, which clears it (nothing was done, nothing to show).
            return {
                "supervisor_visits": visits,
                "done": True,
                "result": "execution_complete",
            }
        return {
            "supervisor_visits": visits,
            "plan_state": with_task_status(plan_state, task["id"], "in_progress"),
        }

    # Tagged + streamed (T6b Step 3a), NOT for a different outcome -- .with_config's
    # tag is the ONLY thing an external astream_events() caller (the chat SSE route)
    # can use to attribute a token to THIS specific call, among every other LLM call
    # the graph makes. Streaming (astream, not ainvoke) is what makes token-level
    # events exist at all; the structured-output parser re-validates into a fresh
    # HubDecision on every chunk (LangChain's own incremental json_mode parsing), so
    # `decision` ends up EXACTLY the object .ainvoke() would have produced -- same
    # prompt, same classification/response, same fallback logic below, unchanged.
    llm = (
        get_llm("top_level_supervisor")
        .with_structured_output(HubDecision)
        .with_config(tags=[TOKEN_STREAM_TAG])
    )
    prompt = load_prompt("supervisor/general_answer.md", user_input=state.get("user_input", ""))
    decision: HubDecision | None = None
    async for chunk in llm.astream(prompt):
        decision = chunk
    if decision is None:
        # Observed, transient real-gateway flake (empty stream, no error raised) --
        # same class of unreliability already handled by callers' retry wrappers for
        # this backend's structured-output calls (see llm/factory.py's json_mode
        # fix). A clear, distinct exception type so a caller can retry the whole
        # node call, exactly as it would for any other transient LLM failure.
        raise RuntimeError("hub decision stream produced no chunks")

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
    # The turn's intent is complete here -- simple_response, unclear (Steps 1-2), or
    # needs_execution with a plan whose tasks are ALL done (Step 3). Step 5: instead of
    # ending directly, route through finalize_turn (and memory_optimization first, when
    # the short-term context is near its configured budget) so every normal completion
    # exits the SAME way.
    if is_context_near_full(state):
        return "memory_optimization"
    return "finalize_turn"
