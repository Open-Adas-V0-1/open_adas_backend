from langchain_core.runnables import RunnableConfig
from langgraph.graph import END
from langgraph.types import interrupt

from agents.sysml.graph import build_sysml_graph
from agents.sysml.middle_state import MiddleState
from app.config import get_settings
from app.schemas.confirmations import (
    ConfirmActionPattern,
    ConfirmDiagramTypePattern,
    RequirementOption,
    SelectRequirementPattern,
)
from app.schemas.sysml import Intent, MiddleDecision
from data.db import async_session_factory
from data.repository import RequirementRepo
from harness.guards import checkpoint_durability
from llm.factory import get_llm
from llm.prompts import load_prompt

# Intents that concern an EXISTING requirement (as opposed to creating a fresh one) —
# these are the ones that can be ambiguous when several requirements are active.
_REQUIREMENT_TARGETING_INTENTS = {
    Intent.generate_diagram.value,
    Intent.modify_diagram.value,
    Intent.modify_requirement.value,
}

# Compiled ONCE, WITHOUT its own checkpointer — inherits whatever checkpointer the
# caller's compiled graph carries, exactly like sysml_processing inherits it below.
_sysml_processing_graph = build_sysml_graph()


async def middle_supervisor(state: MiddleState) -> dict:
    visits = (state.get("supervisor_visits") or 0) + 1
    max_visits = get_settings().sysml_middle_max_visits

    # Loop guard: an unbounded middle_supervisor <-> sysml_processing loop would be a bug,
    # not a valid use case. Fail open to END rather than looping forever. Env-driven
    # (SYSML_MIDDLE_MAX_VISITS) rather than hardcoded.
    if visits > max_visits:
        return {
            "supervisor_visits": visits,
            "resolved_intent": None,
            "pending_pattern": None,
            "result": "stopped: max supervisor visits reached",
        }

    async with async_session_factory() as db:
        active_requirements = await RequirementRepo.list_active_for_session(
            db, session_id=state["session_id"]
        )

    candidates_text = (
        "\n".join(f"- id={r.id} summary={r.content[:120]}" for r in active_requirements) or "(none)"
    )

    llm = get_llm("sysml_middle_supervisor").with_structured_output(MiddleDecision)
    prompt = load_prompt(
        "sysml/middle_supervisor.md",
        user_input=state.get("user_input", ""),
        active_requirements=candidates_text,
    )
    decision: MiddleDecision = await llm.ainvoke(prompt)

    if not decision.has_request:
        # No actionable request THIS visit: clear any stale intent from a prior visit so
        # the router doesn't loop back into sysml_processing on old state.
        return {
            "supervisor_visits": visits,
            "resolved_intent": None,
            "pending_pattern": None,
            "clarifying_message": decision.message,
            "result": "no_action",
        }

    resolved_intent = decision.resolved_intent.value if decision.resolved_intent else None
    diagram_type = decision.diagram_type.value if decision.diagram_type else None
    concerns_existing_requirement = resolved_intent in _REQUIREMENT_TARGETING_INTENTS

    if concerns_existing_requirement:
        if decision.named_requirement_id:
            # LLM matched an explicitly-named requirement among the candidates -> unambiguous.
            return {
                "supervisor_visits": visits,
                "resolved_intent": resolved_intent,
                "diagram_type": diagram_type,
                "target_requirement_id": decision.named_requirement_id,
                "pending_pattern": None,
                "clarifying_message": None,
                "processing_counter": (state.get("processing_counter") or 0) + 1,
            }
        if len(active_requirements) == 1:
            # Exactly one active requirement -> unambiguous even without an explicit name.
            return {
                "supervisor_visits": visits,
                "resolved_intent": resolved_intent,
                "diagram_type": diagram_type,
                "target_requirement_id": str(active_requirements[0].id),
                "pending_pattern": None,
                "clarifying_message": None,
                "processing_counter": (state.get("processing_counter") or 0) + 1,
            }
        if len(active_requirements) > 1:
            # >1 active requirement and none named -> genuinely ambiguous.
            return {
                "supervisor_visits": visits,
                "resolved_intent": resolved_intent,
                "diagram_type": diagram_type,
                "pending_pattern": "select_requirement",
                "pending_options_source": [
                    {"id": str(r.id), "summary": r.content[:120]} for r in active_requirements
                ],
                "clarifying_message": None,
            }
        # zero active requirements: nothing to be ambiguous about — let layer-3's own
        # guard_requirement_available handle the "no requirement" path.

    return {
        "supervisor_visits": visits,
        "resolved_intent": resolved_intent,
        "diagram_type": diagram_type,
        "pending_pattern": None,
        "clarifying_message": None,
        "processing_counter": (state.get("processing_counter") or 0) + 1,
    }


def route_from_middle_supervisor(state: MiddleState) -> str:
    if (state.get("supervisor_visits") or 0) > get_settings().sysml_middle_max_visits:
        return END
    if state.get("pending_pattern"):
        return "user_confirm_inputs"
    if state.get("resolved_intent"):
        return "sysml_processing"
    return END


async def user_confirm_inputs(state: MiddleState) -> dict:
    """HITL node: only reached when middle_supervisor found genuine ambiguity. Builds
    a FIXED-STRUCTURE confirmation pattern with deterministic, repository-derived
    options; the LLM phrases only the accompanying question text.
    """
    pattern_name = state.get("pending_pattern")
    options_source = state.get("pending_options_source") or []

    llm = get_llm("sysml_confirm_question")
    prompt = load_prompt(
        "sysml/confirm_question.md",
        pattern=pattern_name or "confirm_action",
        user_input=state.get("user_input", ""),
        options_source=str(options_source),
    )
    question_text = (await llm.ainvoke(prompt)).content

    if pattern_name == "select_requirement":
        payload = SelectRequirementPattern(
            question=question_text,
            options=[RequirementOption(**o) for o in options_source],
        ).model_dump()
    elif pattern_name == "confirm_diagram_type":
        payload = ConfirmDiagramTypePattern(question=question_text).model_dump()
    else:
        payload = ConfirmActionPattern(question=question_text).model_dump()

    # No side effects above this line — this node re-runs from the top on resume.
    decision = interrupt(payload)

    action = decision.get("action") if isinstance(decision, dict) else decision
    selected_id = decision.get("selected_id") if isinstance(decision, dict) else None
    selected_diagram_type = decision.get("selected_diagram_type") if isinstance(decision, dict) else None
    new_user_input = decision.get("user_input") if isinstance(decision, dict) else None

    if action == "confirm":
        update = {
            "confirm_decision": "confirmed",
            "pending_pattern": None,
            "pending_options_source": None,
            "processing_counter": (state.get("processing_counter") or 0) + 1,
        }
        if selected_id:
            update["target_requirement_id"] = selected_id
        if selected_diagram_type:
            update["diagram_type"] = selected_diagram_type
        return update

    if action == "modify":
        return {
            "confirm_decision": "modified",
            "pending_pattern": None,
            "pending_options_source": None,
            "resolved_intent": None,
            "user_input": new_user_input or state.get("user_input"),
        }

    # cancel, or anything unrecognized -> fail-open to the cancel exit rather than
    # looping forever asking the same question.
    return {
        "confirm_decision": "cancelled",
        "pending_pattern": None,
        "pending_options_source": None,
        "resolved_intent": None,
        "result": "cancelled",
    }


def route_from_user_confirm(state: MiddleState) -> str:
    decision = state.get("confirm_decision")
    if decision == "confirmed":
        return "sysml_processing"
    if decision == "modified":
        return "middle_supervisor"
    return END  # cancelled, or unrecognized -> fail-open to END


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
    l3_output = await _sysml_processing_graph.ainvoke(
        l3_input, child_config, durability=checkpoint_durability()
    )

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
