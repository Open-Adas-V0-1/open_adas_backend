import uuid
from datetime import datetime, timezone

from langgraph.graph import END
from langgraph.types import interrupt

from agents.sysml.state import SysmlState
from agents.sysml.tools import to_mermaid, validate
from app.config import get_settings
from app.schemas.sysml import Intent, IntentDecision
from data.db import async_session_factory
from data.models import DiagramType, RequirementLevel
from data.repository import DiagramRepo, RequirementRepo
from llm.factory import get_llm
from llm.prompts import load_prompt
from skills.loader import get_error_help, get_patterns, get_syntax, match

_GENERATE_TARGETING_INTENTS = {
    Intent.generate_requirement.value,
    Intent.modify_requirement.value,
    Intent.generate_diagram.value,
    Intent.modify_diagram.value,
}
_DIAGRAM_INTENTS = {Intent.generate_diagram.value, Intent.modify_diagram.value}


def _coverage_gaps(text: str, source_node: str | None, diagram_type: str | None) -> list[str]:
    """Deterministic structural check: did generation actually produce the elements the
    request needed, distinct from the tool's syntax/semantic diagnostics.
    """
    gaps: list[str] = []
    if source_node == "requirement":
        if "requirement def" not in text:
            gaps.append("Missing a 'requirement def' block.")
        if "subject" not in text:
            gaps.append("Missing a 'subject' declaration.")
        if "require constraint" not in text:
            gaps.append("Missing at least one 'require constraint'.")
    elif source_node == "diagram":
        keyword_map = {
            "use_case": ["part def", "part "],
            "state_machine": ["state def", "state "],
            "sequence": ["part def", "action def"],
        }
        keywords = keyword_map.get(diagram_type, ["part def"])
        if not any(k in text for k in keywords):
            gaps.append(
                f"Missing expected structural elements for diagram_type={diagram_type!r} "
                f"(looked for any of {keywords})."
            )
    return gaps


def _format_diagnostics(diagnostics: list[dict]) -> str:
    if not diagnostics:
        return "(none)"
    return "\n".join(
        f"- [{d.get('severity')}] line {d.get('line')}, col {d.get('column')}: {d.get('message')}"
        for d in diagnostics
    )


def _skill_match_query(state: SysmlState) -> str:
    """Query used at Level 2 (match against name+description) to find which
    procedural-memory skill(s) are relevant to this generation task.
    """
    source_node = state.get("source_node") or "requirement"
    level = state.get("level", "functional")
    diagram_type = (state.get("diagram_type") or "").replace("_", " ")
    parts = ["SysML v2 systems modeling", level, source_node, diagram_type, state.get("user_input", "")]
    return " ".join(p for p in parts if p)


def _skill_reference_query(state: SysmlState) -> str:
    """Query used at Level 3 (section-level retrieval) — narrower than the Level 2
    match query, aimed at the specific construct being generated.
    """
    source_node = state.get("source_node") or "requirement"
    if source_node == "diagram":
        diagram_type = (state.get("diagram_type") or "part").replace("_", " ")
        return f"{diagram_type} definition"
    return "requirement subject constraint"


def _skill_guidance(state: SysmlState) -> str:
    """Progressive disclosure in action: Level 2 match() picks the relevant skill(s),
    then Level 3 get_syntax()/get_patterns() pull ONLY the section(s) relevant to what
    is being generated right now — never a whole reference file.
    """
    matched = match(_skill_match_query(state), max_skills=2)
    if not matched:
        return ""

    ref_query = _skill_reference_query(state)
    blocks: list[str] = []
    for skill in matched:
        syntax = get_syntax(ref_query, skill_name=skill.meta.name)
        patterns = get_patterns(ref_query, skill_name=skill.meta.name)
        blocks.extend(b for b in (syntax, patterns) if b)
    return "\n\n".join(blocks)


async def sysml_supervisor(state: SysmlState) -> dict:
    llm = get_llm("sysml_supervisor").with_structured_output(IntentDecision)
    prompt = load_prompt("sysml/supervisor.md", user_input=state.get("user_input", ""))

    decision: IntentDecision = await llm.ainvoke(prompt)

    return {
        "intent": decision.intent.value,
        "level": decision.level.value if decision.level else state.get("level", "functional"),
        "diagram_type": decision.diagram_type.value if decision.diagram_type else None,
        "clarifying_message": decision.message,
    }


def route_from_supervisor(state: SysmlState) -> str:
    intent = state.get("intent")
    if intent in _GENERATE_TARGETING_INTENTS:
        return "plan_node"
    if intent == Intent.conversation.value:
        return "contextual_answer"
    # fail-open: no_action, apply_published_delta (not built in this layer), and any
    # unrecognized intent end gracefully instead of crashing the graph.
    return END


async def plan_node(state: SysmlState) -> dict:
    """Plans the structure BEFORE generating (never writes SysML syntax itself)."""
    level = state.get("level", "functional")
    is_diagram = state.get("intent") in _DIAGRAM_INTENTS
    source_node = "diagram" if is_diagram else "requirement"

    source_text = state.get("source_requirement_content")
    target_id = state.get("target_requirement_id")
    if source_text is None and target_id:
        async with async_session_factory() as db:
            requirement = await RequirementRepo.get_by_id(
                db, id=uuid.UUID(str(target_id)), session_id=state["session_id"]
            )
        if requirement is None:
            raise ValueError(
                f"target_requirement_id {target_id} not found in session {state['session_id']}"
            )
        source_text = requirement.content

    if is_diagram and target_id is None:
        raise ValueError(
            "A diagram must target an existing requirement (target_requirement_id); "
            "the middle layer is responsible for resolving it before dispatching here."
        )

    # plan_node runs before generate_node's source_node/diagram_type would normally be
    # set for _skill_guidance's lookup, so build the query from what we already know.
    skill_guidance = _skill_guidance({**state, "source_node": source_node})

    llm = get_llm("sysml_plan")
    prompt = load_prompt(
        "sysml/plan.md",
        level=level,
        target=source_node,
        diagram_type=state.get("diagram_type") or "n/a",
        user_input=state.get("user_input", ""),
        source_text=source_text or "(none)",
        skill_guidance=skill_guidance or "(no matching skill guidance found)",
    )
    response = await llm.ainvoke(prompt)

    return {
        "plan": {"structure": response.content, "level": level, "target": source_node},
        "source_node": source_node,
        "source_requirement_content": source_text,
        "verify_visits": 0,  # fresh generate/verify round starts here
        "verify_warning": None,
    }


async def generate_node(state: SysmlState) -> dict:
    level = state.get("level", "functional")
    if level not in ("operational", "functional", "physical"):
        level = "functional"

    diagnostics = state.get("verify_diagnostics") or []
    coverage_gaps = state.get("verify_coverage_gaps") or []
    feedback = state.get("feedback")

    verify_feedback_text = "(none)"
    if diagnostics or coverage_gaps or feedback:
        # Skill-sourced fix guidance for exactly the diagnostics we got back — this is
        # what should shorten the verify loop: regeneration applies the documented fix
        # instead of guessing again.
        skill_error_help = get_error_help(diagnostics) if diagnostics else ""
        verify_feedback_text = load_prompt(
            "sysml/verify_feedback.md",
            diagnostics=_format_diagnostics(diagnostics),
            coverage_gaps="\n".join(f"- {g}" for g in coverage_gaps) or "(none)",
            human_feedback=feedback or "(none)",
            skill_error_help=skill_error_help or "(no matching documented fix found)",
        )

    skill_guidance = _skill_guidance(state)

    llm = get_llm("sysml_generate")
    prompt = load_prompt(
        f"sysml/generate_{level}.md",
        target=state.get("source_node", "requirement"),
        diagram_type=state.get("diagram_type") or "n/a",
        user_input=state.get("user_input", ""),
        plan=(state.get("plan") or {}).get("structure", ""),
        source_text=state.get("source_requirement_content") or "(none)",
        previous_draft=state.get("draft_sysml") or "(none)",
        verify_feedback=verify_feedback_text,
        skill_guidance=skill_guidance or "(no matching skill guidance found)",
    )
    response = await llm.ainvoke(prompt)

    return {
        "draft_sysml": response.content,
        "verify_visits": (state.get("verify_visits") or 0) + 1,
        "feedback": None,
    }


async def verify_node(state: SysmlState) -> dict:
    """Automatic verification: LSP diagnostics, coverage analysis, and (for diagrams)
    Mermaid derivation. Iterates the caller toward CLEAN — see route_from_verify.
    """
    text = state.get("draft_sysml") or ""
    source_node = state.get("source_node")

    diagnostics = await validate(text)
    diagnostic_dicts = [d.to_dict() for d in diagnostics]

    coverage_gaps = _coverage_gaps(text, source_node, state.get("diagram_type"))

    mermaid = None
    if source_node == "diagram" and not diagnostic_dicts:
        # Only attempt Mermaid derivation once the model text is at least syntactically
        # sound — deriving from broken SysML text isn't a meaningful signal either way.
        try:
            mermaid = await to_mermaid(text)
        except Exception as exc:
            coverage_gaps = [*coverage_gaps, f"Mermaid generation failed: {exc}"]

    clean = not diagnostic_dicts and not coverage_gaps

    result: dict = {
        "verify_diagnostics": diagnostic_dicts,
        "verify_coverage_gaps": coverage_gaps,
        "verify_clean": clean,
    }
    if mermaid is not None:
        result["draft_mermaid"] = mermaid

    visits = state.get("verify_visits") or 0
    max_visits = get_settings().sysml_proc_max_visits
    if not clean and visits >= max_visits:
        result["verify_warning"] = (
            f"Automatic verification did not reach a clean result after {visits} attempt(s); "
            "handing to human review with the remaining diagnostics below."
        )
    else:
        result["verify_warning"] = None

    return result


def route_from_verify(state: SysmlState) -> str:
    if state.get("verify_clean"):
        return "requirement_review"
    visits = state.get("verify_visits") or 0
    if visits >= get_settings().sysml_proc_max_visits:
        # fail-open: verify_node has already attached verify_warning to state.
        return "requirement_review"
    return "generate_node"


def requirement_review(state: SysmlState) -> dict:
    # No DB writes above this line — this node re-runs from the top on resume.
    decision = interrupt(
        {
            "type": "requirement_review",
            "source_node": state.get("source_node"),
            "draft": state.get("draft_sysml"),
            "mermaid": state.get("draft_mermaid"),
            "level": state.get("level", "functional"),
            "diagram_type": state.get("diagram_type"),
            "verify_clean": state.get("verify_clean"),
            "verify_diagnostics": state.get("verify_diagnostics"),
            "verify_warning": state.get("verify_warning"),
            "contextual_answer_text": state.get("contextual_answer_text"),
        }
    )

    action = decision.get("action") if isinstance(decision, dict) else decision
    feedback = decision.get("feedback") if isinstance(decision, dict) else None
    question = decision.get("question") if isinstance(decision, dict) else None

    return {"review_decision": action, "feedback": feedback, "question": question}


def route_from_review(state: SysmlState) -> str:
    decision = state.get("review_decision")
    if decision == "approve":
        return "finalize"
    if decision == "regenerate":
        return "plan_node"
    if decision == "question":
        return "contextual_answer"
    # fail-open: unrecognized/failed decision ends gracefully rather than crashing.
    return END


async def contextual_answer(state: SysmlState) -> dict:
    """Answers a question raised during (or in place of) review, using live context.
    Strictly READ-ONLY: no DB writes, no persistence, no side effects. Routes to review.
    """
    llm = get_llm("sysml_contextual_answer")
    prompt = load_prompt(
        "sysml/contextual_answer.md",
        level=state.get("level", "functional"),
        current_draft=state.get("draft_sysml") or "(none)",
        user_question=state.get("question") or state.get("user_input", ""),
    )
    response = await llm.ainvoke(prompt)
    return {"contextual_answer_text": response.content, "question": None}


async def finalize(state: SysmlState) -> dict:
    """Persists the APPROVED artifact keyed by (session_id == thread_id, level). No
    active/superseded semantics — levels accumulate rather than superseding each other.
    """
    source_node = state.get("source_node")
    level = RequirementLevel(state.get("level", "functional"))
    approved_at = datetime.now(timezone.utc).isoformat()

    metadata = {
        "artifact_type": "diagram" if source_node == "diagram" else "requirement",
        "level": level.value,
        "verify_clean_at_approval": state.get("verify_clean"),
        "verify_diagnostics_at_approval": state.get("verify_diagnostics") or [],
        "regeneration_rounds": state.get("verify_visits") or 0,
        "final_feedback": state.get("feedback"),
        "approved_at": approved_at,
    }

    async with async_session_factory() as db:
        if source_node == "diagram":
            diagram = await DiagramRepo.finalize(
                db,
                session_id=state["session_id"],
                requirement_id=uuid.UUID(str(state["target_requirement_id"])),
                type=DiagramType(state.get("diagram_type") or "use_case"),
                sysml_text=state["draft_sysml"],
                mermaid=state.get("draft_mermaid"),
                metadata=metadata,
            )
            await db.commit()
            # active_diagram_id is a compatibility alias: the middle layer's wrapper
            # (Layer 2, out of scope here) reads this key name to build its light
            # reference. finalize() has no active/superseded semantics of its own —
            # this is just the id of the row just written, which is always active.
            return {
                "persisted_diagram_id": str(diagram.id),
                "active_diagram_id": str(diagram.id),
                "result": "finalized",
            }

        requirement = await RequirementRepo.finalize(
            db,
            session_id=state["session_id"],
            content=state["draft_sysml"],
            level=level,
            metadata=metadata,
        )
        await db.commit()
        return {
            "persisted_requirement_id": str(requirement.id),
            "active_requirement_id": str(requirement.id),
            "result": "finalized",
        }
