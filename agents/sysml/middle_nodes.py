from langchain_core.runnables import RunnableConfig
from langgraph.graph import END
from langgraph.types import interrupt

from agents.sysml.graph import build_sysml_graph
from agents.sysml.middle_state import MiddleState
from app.config import get_settings
from app.schemas.confirmations import (
    ClarifyRequestPattern,
    ConfirmActionPattern,
    ConfirmDiagramTypePattern,
    RequirementOption,
    SelectRequirementPattern,
    SelectRequirementsForDiagramPattern,
)
from app.schemas.processing_input import ProcessingInput
from app.schemas.sysml import Intent, MiddleDecision
from data.db import async_session_factory
from data.models import DiagramType, RequirementLevel
from data.repository import RequirementRepo, SessionRepo
from harness.guards import checkpoint_durability
from harness.thread_ttl import touch_thread
from llm.factory import get_llm
from llm.prompts import load_prompt

# Intents that concern an EXISTING requirement (as opposed to creating a fresh one) —
# these are the ones that can be ambiguous when several requirements are active.
_REQUIREMENT_TARGETING_INTENTS = {
    Intent.generate_diagram.value,
    Intent.modify_diagram.value,
    Intent.modify_requirement.value,
}

# Intents that create/modify a requirement text itself — these are the ones subject to
# forward-only level ordering (operational -> functional -> physical).
_LEVEL_BEARING_INTENTS = {
    Intent.generate_requirement.value,
    Intent.modify_requirement.value,
}

# The full set of intents validate_inputs considers "actionable" — everything else
# (conversation, apply_published_delta [not built], no_action, None/unrecognized) fails
# validate_inputs's intent check rather than being silently dispatched or dropped.
_ACTIONABLE_INTENTS = _LEVEL_BEARING_INTENTS | _REQUIREMENT_TARGETING_INTENTS

# Each level's immediate higher-level source, per the forward-only chain. Operational
# has no source — it's the top of the chain.
_SOURCE_LEVEL_FOR = {"functional": "operational", "physical": "functional"}

# Compiled ONCE, WITHOUT its own checkpointer — inherits whatever checkpointer the
# caller's compiled graph carries, exactly like sysml_processing inherits it below.
_sysml_processing_graph = build_sysml_graph()


def _target_fulfilled(task_target: dict | None, processing_input: ProcessingInput) -> bool:
    """Did the processing that JUST finished (sysml_processing, deterministic from the
    already-resolved ProcessingInput -- no LLM call) satisfy the locked task's target,
    or was it the resolve_level-triggered MISSING-PREREQUISITE step? Diagrams have no
    prerequisite chain (never go through resolve_level) -- any finalized diagram
    fulfills a diagram target. Requirements are fulfilled only once the level actually
    processed matches the ORIGINALLY requested level; a lower level processed along the
    way is, by construction, the bounded prerequisite step, not the target itself.
    """
    if not task_target:
        return False
    if processing_input.intent == "generate_diagram":
        return task_target.get("intent") in (Intent.generate_diagram.value, Intent.modify_diagram.value)
    target_level = task_target.get("requested_level") or "functional"
    return processing_input.level.value == target_level


def _lock_target(
    state: MiddleState, resolved_intent: str | None, diagram_type: str | None, requested_level: str | None
) -> dict:
    """Fixes the delegated task's completion target the FIRST (and only) time
    middle_supervisor makes a real, dispatch-worthy judgement call for it. Once locked,
    middle_supervisor's own re-entries (after a processing loops back) stop
    re-judging "is there still work?" against the same static task text -- they
    deterministically check whether THIS target was fulfilled instead (see
    _target_fulfilled / sysml_processing). Reset only by an explicit user
    modification (user_confirm_inputs's "modified" branch) -- a genuinely new ask.

    ONLY applies when single_task_dispatch is set -- i.e. this middle graph invocation
    came from Layer-1's sysml_middle_node, which always delegates exactly ONE atomic
    TODO task per invocation (see supervisor/graph.py). Layer-2's OWN standalone
    contract -- a single free-form message legitimately describing SEVERAL distinct
    asks (e.g. "define the operational need, then the function") -- is unaffected:
    without this flag, middle_supervisor keeps its original, already-tested open-ended
    re-judgement, exactly as scripts/smoke_test_layer2_integration.py exercises it.
    """
    if not state.get("single_task_dispatch"):
        return {}
    return {
        "task_locked": True,
        "task_target": {
            "intent": resolved_intent,
            "diagram_type": diagram_type,
            "requested_level": requested_level,
        },
    }


async def middle_supervisor(state: MiddleState, config: RunnableConfig) -> dict:
    visits = (state.get("supervisor_visits") or 0) + 1
    max_visits = get_settings().sysml_middle_max_visits

    # Loop guard: an unbounded middle_supervisor <-> sysml_processing loop would be a bug,
    # not a valid use case. Fail open to END rather than looping forever. Env-driven
    # (SYSML_MIDDLE_MAX_VISITS) rather than hardcoded. Kept as a BACKSTOP only -- the
    # completion condition below is what actually stops the loop in the normal case.
    if visits > max_visits:
        return {
            "supervisor_visits": visits,
            "resolved_intent": None,
            "pending_pattern": None,
            "target_requirement_ids": None,
            "result": "stopped: max supervisor visits reached",
        }

    # Deterministic completion condition (no LLM call): once a task's target is locked,
    # re-entering middle_supervisor (control only returns here after a processing loops
    # back) means one processing just finished. Check whether THAT processing fulfilled
    # the locked target -- never re-open the "is there still work?" judgement on the
    # same static task text, which is what let a real model re-dispatch an
    # already-satisfied request. If not yet fulfilled, the processing that just ran was
    # the (bounded, purposeful) MISSING-PREREQUISITE step resolve_level triggered --
    # resume deterministically toward the ORIGINAL locked target rather than asking the
    # LLM again.
    if state.get("task_locked"):
        if state.get("target_fulfilled"):
            return {
                "supervisor_visits": visits,
                "resolved_intent": None,
                "pending_pattern": None,
                "target_requirement_ids": None,
                "result": "task_complete",
            }
        target = state["task_target"]
        return {
            "supervisor_visits": visits,
            "resolved_intent": target["intent"],
            "diagram_type": target.get("diagram_type"),
            "requested_level": target.get("requested_level"),
            "target_requirement_ids": None,
            "pending_pattern": None,
            "clarifying_message": None,
            "processing_counter": (state.get("processing_counter") or 0) + 1,
        }

    async with async_session_factory() as db:
        active_requirements = await RequirementRepo.list_active_for_session(
            db, session_id=state["session_id"]
        )
        # last_accessed touch for the outer thread — every visit here counts as an
        # access, per the TTL policy (harness/thread_ttl.py). The outer thread_id lives
        # in config (the checkpointer key), not in state.
        outer_thread_id = config["configurable"]["thread_id"]
        await touch_thread(db, thread_id=outer_thread_id, session_id=state["session_id"])
        await db.commit()

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
            "target_requirement_ids": None,
            "clarifying_message": decision.message,
            "result": "no_action",
        }

    resolved_intent = decision.resolved_intent.value if decision.resolved_intent else None
    diagram_type = decision.diagram_type.value if decision.diagram_type else None
    concerns_existing_requirement = resolved_intent in _REQUIREMENT_TARGETING_INTENTS
    is_diagram = resolved_intent in (Intent.generate_diagram.value, Intent.modify_diagram.value)

    if concerns_existing_requirement:
        if decision.named_requirement_id:
            # LLM matched an explicitly-named requirement among the candidates -> unambiguous.
            return {
                "supervisor_visits": visits,
                "resolved_intent": resolved_intent,
                "diagram_type": diagram_type,
                "target_requirement_id": decision.named_requirement_id,
                "target_requirement_ids": None,
                "pending_pattern": None,
                "clarifying_message": None,
                "processing_counter": (state.get("processing_counter") or 0) + 1,
                **_lock_target(state, resolved_intent, diagram_type, None),
            }
        if len(active_requirements) == 1:
            # Exactly one active requirement -> unambiguous even without an explicit name.
            return {
                "supervisor_visits": visits,
                "resolved_intent": resolved_intent,
                "diagram_type": diagram_type,
                "target_requirement_id": str(active_requirements[0].id),
                "target_requirement_ids": None,
                "pending_pattern": None,
                "clarifying_message": None,
                "processing_counter": (state.get("processing_counter") or 0) + 1,
                **_lock_target(state, resolved_intent, diagram_type, None),
            }
        if len(active_requirements) > 1:
            # >1 active requirement and none named -> genuinely ambiguous. The TARGET
            # (intent/diagram_type) is already fixed here -- only WHICH requirement(s)
            # is still open, resolved next via user_confirm_inputs -- so lock now.
            options = [{"id": str(r.id), "summary": r.content[:120]} for r in active_requirements]
            if is_diagram:
                # A diagram may represent SEVERAL requirements at once -> multi-select,
                # not a single pick. modify_requirement (below) still targets exactly one.
                return {
                    "supervisor_visits": visits,
                    "resolved_intent": resolved_intent,
                    "diagram_type": diagram_type,
                    "target_requirement_ids": None,
                    "pending_pattern": "select_requirements_for_diagram",
                    "pending_options_source": options,
                    "pending_action_context": "select_diagram_targets",
                    "clarifying_message": None,
                    **_lock_target(state, resolved_intent, diagram_type, None),
                }
            return {
                "supervisor_visits": visits,
                "resolved_intent": resolved_intent,
                "diagram_type": diagram_type,
                "target_requirement_ids": None,
                "pending_pattern": "select_requirement",
                "pending_options_source": options,
                "clarifying_message": None,
                **_lock_target(state, resolved_intent, diagram_type, None),
            }
        # zero active requirements: nothing to be ambiguous about — let layer-3's own
        # guard_requirement_available handle the "no requirement" path.

    requested_level = decision.level.value if decision.level else None
    return {
        "supervisor_visits": visits,
        "resolved_intent": resolved_intent,
        "diagram_type": diagram_type,
        "requested_level": requested_level,
        "target_requirement_ids": None,
        "pending_pattern": None,
        "clarifying_message": None,
        "processing_counter": (state.get("processing_counter") or 0) + 1,
        **_lock_target(state, resolved_intent, diagram_type, requested_level),
    }


def route_from_middle_supervisor(state: MiddleState) -> str:
    if (state.get("supervisor_visits") or 0) > get_settings().sysml_middle_max_visits:
        return END
    if state.get("pending_pattern"):
        return "user_confirm_inputs"
    if state.get("resolved_intent"):
        return "validate_inputs"
    return END


async def validate_inputs(state: MiddleState) -> dict:
    """Deterministic (router-as-code) gate run BEFORE resolve_level: is this input
    processable at all? No LLM call here — it re-reads middle_supervisor's
    already-structured decision rather than re-classifying anything.

    Checks:
      1. intent validity — resolved_intent must be one of the known actionable intents.
      2. context validity — session/project context must be coherent (session exists).
      3. TODO(file-validity): once file-backed inputs exist (e.g. uploaded reference
         docs), validate them here too. Inert placeholder for now — text-only path.

    On failure: routes to user_confirm_inputs via interrupt() (never calls Layer 1
    directly), asking the user to clarify/rephrase. Fails open — never crashes.
    """
    resolved_intent = state.get("resolved_intent")

    if resolved_intent not in _ACTIONABLE_INTENTS:
        return {
            "input_valid": False,
            "invalid_reason": f"unrecognized or unsupported intent: {resolved_intent!r}",
            "pending_pattern": "clarify_request",
            "pending_options_source": [],
            "pending_action_context": "invalid_intent",
            "clarifying_message": (
                "I couldn't tell what you'd like me to do — could you rephrase your request?"
            ),
        }

    session_id = state.get("session_id")
    async with async_session_factory() as db:
        session = await SessionRepo.get_by_id(db, session_id) if session_id else None

    if session is None:
        return {
            "input_valid": False,
            "invalid_reason": f"no session found for session_id={session_id!r}",
            "pending_pattern": "clarify_request",
            "pending_options_source": [],
            "pending_action_context": "invalid_context",
            "clarifying_message": (
                "I'm having trouble locating your session — could you restart or rephrase your request?"
            ),
        }

    # TODO(file-validity): validate any file-backed inputs attached to this request
    # once that path exists. Inert on the current text-only path.

    return {
        "input_valid": True,
        "invalid_reason": None,
        "pending_pattern": None,
    }


def route_from_validate_inputs(state: MiddleState) -> str:
    if state.get("input_valid") is False:
        return "user_confirm_inputs"
    if state.get("resolved_intent") in _LEVEL_BEARING_INTENTS:
        return "resolve_level"
    return "build_structured_format"


async def resolve_level(state: MiddleState) -> dict:
    """Deterministic (router-as-code) level resolution: enforces the forward-only
    operational -> functional -> physical ordering within THIS thread (session_id
    doubles as the thread), and resolves the SOURCE artifact a derived level reads
    from. No LLM call here — the requested level itself already came from
    middle_supervisor's structured decision; this just applies the ordering rule and
    reads the repository for what already exists in this thread.
    """
    requested_level = state.get("requested_level") or "functional"
    session_id = state["session_id"]

    async with async_session_factory() as db:
        level_progress = await RequirementRepo.level_progress(db, session_id=session_id)

        source_level = _SOURCE_LEVEL_FOR.get(requested_level)
        if source_level is None:
            # operational: top of the chain, no source required.
            return {
                "requested_level": requested_level,
                "resolved_source_id": None,
                "level_progress": level_progress,
                "pending_pattern": None,
            }

        candidates = await RequirementRepo.list_by_session_and_level(
            db, session_id=session_id, level=RequirementLevel(source_level)
        )

    if len(candidates) == 1:
        # Exactly one candidate source in this thread -> unambiguous, proceed.
        return {
            "requested_level": requested_level,
            "resolved_source_id": str(candidates[0].id),
            "level_progress": level_progress,
            "pending_pattern": None,
        }

    if len(candidates) == 0:
        # Forward-only ordering violated: asked for {requested_level} but this thread
        # has no {source_level} yet. Ask the user rather than silently skipping ahead —
        # via interrupt (user_confirm_inputs), never a direct call up to Layer 1.
        return {
            "requested_level": requested_level,
            "resolved_source_id": None,
            "level_progress": level_progress,
            "pending_pattern": "confirm_action",
            "pending_options_source": [],
            "pending_action_context": "missing_level_source",
            "clarifying_message": (
                f"This thread doesn't have a {source_level} requirement yet, and a "
                f"{requested_level} one is meant to derive from one — create the "
                f"{source_level} requirement first?"
            ),
        }

    # >1 candidate sources at the required level -> ambiguous WHICH one to derive from.
    return {
        "requested_level": requested_level,
        "resolved_source_id": None,
        "level_progress": level_progress,
        "pending_pattern": "select_requirement",
        "pending_options_source": [{"id": str(r.id), "summary": r.content[:120]} for r in candidates],
        "pending_action_context": "select_level_source",
        "clarifying_message": (
            f"Several {source_level} requirements exist in this thread — which one "
            f"should this {requested_level} requirement derive from?"
        ),
    }


def route_from_resolve_level(state: MiddleState) -> str:
    if state.get("pending_pattern"):
        return "user_confirm_inputs"
    return "build_structured_format"


async def build_structured_format(state: MiddleState) -> dict:
    """Assembles the unified Layer-2 -> Layer-3 contract (ProcessingInput) from the
    fields already resolved by validate_inputs (Step 2), resolve_level (Step 1), and
    middle_supervisor / user_confirm_inputs's ambiguity resolution. No LLM call, no DB
    read — purely deterministic reshaping of state already in hand.

    Layer-3 always receives this SAME shape regardless of where the request came from
    (text today; a file-derived extraction later — see ProcessingInput's extension
    spot). References only (source_id, target_requirement_ids) — sysml_processing
    (and, through it, layer-3) reads content from the repository when it needs it.
    """
    resolved_intent = state.get("resolved_intent")
    is_diagram = resolved_intent in (Intent.generate_diagram.value, Intent.modify_diagram.value)
    intent = "generate_diagram" if is_diagram else "generate_requirement"

    target_requirement_id = state.get("target_requirement_id")
    multi_targets = state.get("target_requirement_ids")
    if is_diagram:
        # A multi-select resolution (Step 4) takes precedence when present; the single
        # target_requirement_id (named/sole-candidate unambiguous path) is the fallback.
        target_ids = list(multi_targets) if multi_targets else ([target_requirement_id] if target_requirement_id else [])
    else:
        target_ids = []

    # For a diagram, target_requirement_id IS the source (the requirement it
    # represents). For a requirement, the source is either an existing requirement
    # being modified (also carried in target_requirement_id) or the higher-level
    # artifact resolve_level derived (resolved_source_id) — mirrors the fallback the
    # wrapper used to apply itself before this contract existed.
    source_id = None if is_diagram else (target_requirement_id or state.get("resolved_source_id"))

    processing_input = ProcessingInput(
        intent=intent,
        level=RequirementLevel(state.get("requested_level") or "functional"),
        source_id=source_id,
        target_requirement_ids=target_ids,
        diagram_type=DiagramType(state["diagram_type"]) if state.get("diagram_type") else None,
        user_input=state.get("user_input", ""),
        session_id=state["session_id"],
    )

    return {"processing_input": processing_input.model_dump(mode="json")}


def _build_confirm_payload(pattern_name: str | None, question_text: str, options_source: list[dict]) -> dict:
    if pattern_name == "select_requirement":
        return SelectRequirementPattern(
            question=question_text,
            options=[RequirementOption(**o) for o in options_source],
        ).model_dump()
    if pattern_name == "select_requirements_for_diagram":
        return SelectRequirementsForDiagramPattern(
            question=question_text,
            options=[RequirementOption(**o) for o in options_source],
        ).model_dump()
    if pattern_name == "confirm_diagram_type":
        return ConfirmDiagramTypePattern(question=question_text).model_dump()
    if pattern_name == "clarify_request":
        return ClarifyRequestPattern(question=question_text).model_dump()
    return ConfirmActionPattern(question=question_text).model_dump()


def _extract_selected_ids(decision, options_source: list[dict]) -> list[str]:
    if not isinstance(decision, dict):
        return []
    if decision.get("select_all"):
        return [o["id"] for o in options_source]
    return list(decision.get("selected_ids") or [])


async def user_confirm_inputs(state: MiddleState) -> dict:
    """HITL node: only reached when middle_supervisor, validate_inputs, or resolve_level
    found something genuinely needing user input. Builds a FIXED-STRUCTURE confirmation
    pattern with deterministic, repository-derived options; the LLM phrases only the
    accompanying question text. ALL user interaction happens here, via interrupt() —
    this pauses the graph and bubbles the payload up to whoever invoked the middle
    graph; there is no direct call to Layer 1 from anywhere in this subgraph.
    """
    pattern_name = state.get("pending_pattern")
    options_source = state.get("pending_options_source") or []

    llm = get_llm("sysml_confirm_question")
    prompt = load_prompt(
        "sysml/confirm_question.md",
        pattern=pattern_name or "confirm_action",
        user_input=state.get("user_input", ""),
        options_source=str(options_source),
        reason=state.get("clarifying_message") or "",
    )
    question_text = (await llm.ainvoke(prompt)).content
    payload = _build_confirm_payload(pattern_name, question_text, options_source)

    # No side effects above this line — this node re-runs from the top on resume.
    decision = interrupt(payload)

    if pattern_name == "select_requirements_for_diagram":
        # Enforce min_selected=1: re-interrupt (ask again) rather than silently
        # accepting an empty selection or crashing. Bounded retries — fail-open to
        # cancel rather than looping forever against a persistently broken client.
        min_selected = payload.get("min_selected", 1)
        attempts = 0
        while True:
            action = decision.get("action") if isinstance(decision, dict) else decision
            if action != "confirm" or _extract_selected_ids(decision, options_source):
                break
            attempts += 1
            if attempts >= 3:
                decision = {"action": "cancel"}
                break
            decision = interrupt({
                **payload,
                "error": f"Select at least {min_selected} requirement(s) before confirming.",
            })

    action = decision.get("action") if isinstance(decision, dict) else decision
    selected_id = decision.get("selected_id") if isinstance(decision, dict) else None
    selected_diagram_type = decision.get("selected_diagram_type") if isinstance(decision, dict) else None
    new_user_input = decision.get("user_input") if isinstance(decision, dict) else None

    if action == "confirm":
        action_context = state.get("pending_action_context")
        base_update = {
            "pending_pattern": None,
            "pending_options_source": None,
            "pending_action_context": None,
            "target_requirement_ids": None,
        }

        if action_context == "missing_level_source":
            # Pivot: the user agreed to create the MISSING source level first, instead
            # of the originally-requested (higher) level. Route back to resolve_level
            # so it re-evaluates with the new (lower) requested_level — operational
            # needs no source at all, so it proceeds straight through.
            original_level = state.get("requested_level") or "functional"
            source_level = _SOURCE_LEVEL_FOR.get(original_level) or "operational"
            return {
                **base_update,
                "confirm_decision": "confirmed_pivot_source",
                "resolved_intent": Intent.generate_requirement.value,
                "requested_level": source_level,
            }

        if action_context == "select_level_source":
            update = {**base_update, "confirm_decision": "confirmed",
                      "processing_counter": (state.get("processing_counter") or 0) + 1}
            if selected_id:
                update["resolved_source_id"] = selected_id
            return update

        if action_context == "select_diagram_targets":
            # Multi-select: the requirements this diagram should represent.
            selected_ids = _extract_selected_ids(decision, options_source)
            return {
                **base_update,
                "confirm_decision": "confirmed",
                "processing_counter": (state.get("processing_counter") or 0) + 1,
                "target_requirement_ids": selected_ids,
            }

        # default (existing behavior): selecting/confirming a single TARGET requirement.
        update = {**base_update, "confirm_decision": "confirmed",
                  "processing_counter": (state.get("processing_counter") or 0) + 1}
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
            "pending_action_context": None,
            "resolved_intent": None,
            "input_valid": None,
            "invalid_reason": None,
            "target_requirement_ids": None,
            "user_input": new_user_input or state.get("user_input"),
            # The user explicitly changed the ask -- this is a genuinely NEW judgement,
            # not a continuation of the old (now-stale) locked target.
            "task_locked": False,
            "task_target": None,
            "target_fulfilled": None,
        }

    # cancel, or anything unrecognized -> fail-open to the cancel exit rather than
    # looping forever asking the same question.
    return {
        "confirm_decision": "cancelled",
        "pending_pattern": None,
        "pending_options_source": None,
        "pending_action_context": None,
        "resolved_intent": None,
        "target_requirement_ids": None,
        "result": "cancelled",
    }


def route_from_user_confirm(state: MiddleState) -> str:
    decision = state.get("confirm_decision")
    if decision == "confirmed_pivot_source":
        return "resolve_level"
    if decision == "confirmed":
        return "build_structured_format"
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

    async with async_session_factory() as db:
        # last_accessed touch for this proc thread — a fresh proc thread and a resumed
        # one both count as an access.
        await touch_thread(db, thread_id=proc_thread_id, session_id=session_id)
        await db.commit()

    child_config = {
        **config,
        "configurable": {**config["configurable"], "thread_id": proc_thread_id},
    }

    # Translate the unified Layer-2 -> Layer-3 contract (ProcessingInput, built by
    # build_structured_format) onto exactly what the rebuilt layer-3 graph expects at
    # its entry (agents/sysml/state.py: SysmlState). This is the "inside a node"
    # boundary — translation is its job, so layer-3 itself stays contract-agnostic.
    processing_input = ProcessingInput.model_validate(state["processing_input"])

    # target_requirement_ids (diagram's represented requirement(s)) takes precedence
    # when set; source_id (either an existing requirement being modified, or
    # resolve_level's higher-level source to derive from) is the fallback — layer-3's
    # plan_node reads whichever ends up here as its source text, via its single
    # target_requirement_id (the DB's Diagram.requirement_id FK is likewise singular).
    target_ids = processing_input.target_requirement_ids
    target_requirement_id = (
        str(target_ids[0]) if target_ids else (str(processing_input.source_id) if processing_input.source_id else None)
    )

    l3_input = {
        "user_input": processing_input.user_input,
        "session_id": processing_input.session_id,
        "target_requirement_id": target_requirement_id,
        "level": processing_input.level.value,
        "diagram_type": processing_input.diagram_type.value if processing_input.diagram_type else None,
    }

    if len(target_ids) > 1:
        # A multi-select diagram: layer-3's own entry state only carries ONE source id
        # (matching the DB's single-FK Diagram.requirement_id), but plan_node prefers a
        # pre-set source_requirement_content over its own single-id fetch — so feed it
        # the CONCATENATED content of every selected requirement here, at the
        # translation boundary, without touching layer-3's node code.
        async with async_session_factory() as db:
            requirements = [
                r for rid in target_ids
                if (r := await RequirementRepo.get_by_id(db, id=rid, session_id=processing_input.session_id))
            ]
        if requirements:
            l3_input["source_requirement_content"] = "\n\n---\n\n".join(r.content for r in requirements)

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
        "target_fulfilled": _target_fulfilled(state.get("task_target"), processing_input),
    }
