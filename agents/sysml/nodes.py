import uuid
from datetime import datetime, timezone

from langgraph.graph import END
from langgraph.types import interrupt

from agents.sysml.state import SysmlState
from app.schemas.sysml import Intent, IntentDecision
from data.db import async_session_factory
from data.models import DiagramType, RequirementLevel, VersionStatus
from data.repository import DiagramRepo, PublishedRequirementRepo, RequirementRepo
from llm.factory import get_llm
from llm.prompts import load_prompt

# Which generator a "regenerate" decision routes back to, keyed by source_node.
SOURCE_NODE_MAP = {
    "requirement": "generate_requirement",
    "diagram": "generate_diagram",
    "delta": "apply_published_delta",
}


def _regeneration_tracking(state: SysmlState) -> dict:
    """Shared by every generator node: bump regeneration_count and remember the
    feedback that produced THIS draft, only when this call was actually a regenerate
    (i.e. feedback was present). Feeds the approved-artifact metadata at stockage time.
    """
    incoming_feedback = state.get("feedback")
    return {
        "last_feedback": incoming_feedback if incoming_feedback else state.get("last_feedback"),
        "regeneration_count": (state.get("regeneration_count") or 0) + (1 if incoming_feedback else 0),
    }


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
    if intent in (Intent.generate_requirement.value, Intent.modify_requirement.value):
        return "generate_requirement"
    if intent in (Intent.generate_diagram.value, Intent.modify_diagram.value):
        return "guard_requirement_available"
    if intent == Intent.apply_published_delta.value:
        return "apply_published_delta"
    # fail-open: no_action and conversation end gracefully instead of crashing the graph.
    return END


async def generate_requirement(state: SysmlState) -> dict:
    llm = get_llm("sysml_generate_requirement")
    prompt = load_prompt(
        "sysml/generate_requirement.md",
        level=state.get("level", "functional"),
        user_input=state.get("user_input", ""),
        previous_draft=state.get("draft_requirement") or "(none)",
        feedback=state.get("feedback") or "(none)",
    )

    response = await llm.ainvoke(prompt)

    return {
        "draft_requirement": response.content,
        "source_node": "requirement",
        "feedback": None,
        **_regeneration_tracking(state),
    }


async def apply_published_delta(state: SysmlState) -> dict:
    published_id = state.get("selected_published_requirement_id")
    if not published_id:
        raise ValueError("apply_published_delta requires state['selected_published_requirement_id']")

    async with async_session_factory() as db:
        published = await PublishedRequirementRepo.get_by_id(
            db, id=uuid.UUID(str(published_id)), session_id=state["session_id"]
        )
    if published is None:
        raise ValueError(
            f"Published requirement {published_id} not found in session {state['session_id']}"
        )

    llm = get_llm("sysml_apply_published_delta")
    prompt = load_prompt(
        "sysml/apply_published_delta.md",
        level=state.get("level", "functional"),
        published_content=published.content,
        current_draft=state.get("draft_requirement") or "(none)",
        feedback=state.get("feedback") or "(none)",
    )

    response = await llm.ainvoke(prompt)

    return {
        "draft_requirement": response.content,
        "source_node": "delta",
        "feedback": None,
        # keep provenance across regenerate loops
        "selected_published_requirement_id": str(published.id),
        **_regeneration_tracking(state),
    }


async def guard_requirement_available(state: SysmlState) -> dict:
    """Router-as-code, not an LLM node: validates the SPECIFIC target requirement this
    processing was given (resolved upstream by the middle layer), not "does any
    requirement exist" in the session.
    """
    target_id = state.get("target_requirement_id")
    if not target_id:
        return {"requirement_valid": False, "resolved_requirement_content": None}

    async with async_session_factory() as db:
        requirement = await RequirementRepo.get_by_id(
            db, id=uuid.UUID(str(target_id)), session_id=state["session_id"]
        )

    if requirement is None or requirement.status == VersionStatus.rejected:
        return {"requirement_valid": False, "resolved_requirement_content": None}

    return {"requirement_valid": True, "resolved_requirement_content": requirement.content}


def route_from_guard(state: SysmlState) -> str:
    return "generate_diagram" if state.get("requirement_valid") else "message_no_requirement"


async def generate_diagram(state: SysmlState) -> dict:
    llm = get_llm("sysml_generate_diagram")
    prompt = load_prompt(
        "sysml/generate_diagram.md",
        diagram_type=state.get("diagram_type") or "use_case",
        requirement_content=state.get("resolved_requirement_content") or "(unknown)",
        previous_draft=state.get("draft_diagram") or "(none)",
        feedback=state.get("feedback") or "(none)",
    )

    response = await llm.ainvoke(prompt)

    return {
        "draft_diagram": response.content,
        "source_node": "diagram",
        "feedback": None,
        **_regeneration_tracking(state),
    }


async def message_no_requirement(state: SysmlState) -> dict:
    """Contextual LLM reply — not a static string."""
    llm = get_llm("sysml_message_no_requirement")
    prompt = load_prompt(
        "sysml/message_no_requirement.md",
        user_input=state.get("user_input", ""),
        diagram_type=state.get("diagram_type") or "unspecified",
    )

    response = await llm.ainvoke(prompt)

    return {"no_requirement_message": response.content, "result": "no_requirement"}


def requirement_review(state: SysmlState) -> dict:
    # No DB writes above this line — this node re-runs from the top on resume.
    source = state.get("source_node")
    draft = state.get("draft_diagram") if source == "diagram" else state.get("draft_requirement")

    decision = interrupt(
        {
            "type": "requirement_review",
            "source_node": source,
            "draft": draft,
            "level": state.get("level", "functional"),
            "diagram_type": state.get("diagram_type"),
        }
    )

    action = decision.get("action") if isinstance(decision, dict) else decision
    feedback = decision.get("feedback") if isinstance(decision, dict) else None
    question = decision.get("question") if isinstance(decision, dict) else None

    return {"review_decision": action, "feedback": feedback, "question": question}


def route_from_review(state: SysmlState) -> str:
    decision = state.get("review_decision")
    if decision == "approve":
        return "stockage_output"
    if decision == "regenerate":
        return SOURCE_NODE_MAP.get(state.get("source_node"), END)
    if decision == "question":
        return "contextual_answer"
    # fail-open: unrecognized/failed decision ends gracefully rather than crashing.
    return END


async def contextual_answer(state: SysmlState) -> dict:
    """Answers a question raised DURING review, using live context. Strictly
    READ-ONLY: no DB writes, no persistence, no side effects. Routes back to review.
    """
    source = state.get("source_node")
    draft = state.get("draft_diagram") if source == "diagram" else state.get("draft_requirement")

    llm = get_llm("sysml_contextual_answer")
    prompt = load_prompt(
        "sysml/contextual_answer.md",
        level=state.get("level", "functional"),
        current_draft=draft or "(none)",
        user_question=state.get("question") or "",
    )

    response = await llm.ainvoke(prompt)

    return {"contextual_answer_text": response.content, "question": None}


def _summarize(text: str, limit: int = 200) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


async def stockage_output(state: SysmlState) -> dict:
    source = state.get("source_node")
    approved_at = datetime.now(timezone.utc).isoformat()
    regeneration_count = state.get("regeneration_count") or 0
    final_feedback = state.get("last_feedback")

    async with async_session_factory() as db:
        if source == "diagram":
            diagram_type_value = state.get("diagram_type") or "use_case"
            metadata = {
                "artifact_type": "diagram",
                "level": state.get("level", "functional"),
                "source_node": source,
                "source_published_id": None,
                "regeneration_count": regeneration_count,
                "final_feedback": final_feedback,
                "summary": f"{diagram_type_value} diagram",
                "approved_at": approved_at,
            }
            diagram = await DiagramRepo.create(
                db,
                session_id=state["session_id"],
                requirement_id=uuid.UUID(str(state["target_requirement_id"])),
                type=DiagramType(diagram_type_value),
                plantuml=state["draft_diagram"],
                metadata=metadata,
            )
            await db.commit()
            return {"persisted_diagram_id": str(diagram.id)}

        provenance_id = None
        source_published_id_str = None
        if source == "delta" and state.get("selected_published_requirement_id"):
            provenance_id = uuid.UUID(str(state["selected_published_requirement_id"]))
            source_published_id_str = str(provenance_id)

        content = state["draft_requirement"]
        metadata = {
            "artifact_type": "requirement",
            "level": state.get("level", "functional"),
            "source_node": source,
            "source_published_id": source_published_id_str,
            "regeneration_count": regeneration_count,
            "final_feedback": final_feedback,
            "summary": _summarize(content),
            "approved_at": approved_at,
        }

        requirement = await RequirementRepo.create(
            db,
            session_id=state["session_id"],
            content=content,
            level=RequirementLevel(state.get("level", "functional")),
            source_published_requirement_id=provenance_id,
            metadata=metadata,
        )
        await db.commit()
        return {"persisted_requirement_id": str(requirement.id)}


def route_from_stockage(state: SysmlState) -> str:
    return "promote_requirement"


async def promote_requirement(state: SysmlState) -> dict:
    source = state.get("source_node")

    async with async_session_factory() as db:
        if source == "diagram":
            promoted = await DiagramRepo.promote(
                db,
                id=uuid.UUID(str(state["persisted_diagram_id"])),
                session_id=state["session_id"],
            )
            await db.commit()
            return {"result": "promoted", "active_diagram_id": str(promoted.id)}

        promoted = await RequirementRepo.promote(
            db,
            id=uuid.UUID(str(state["persisted_requirement_id"])),
            session_id=state["session_id"],
        )
        await db.commit()
        return {"result": "promoted", "active_requirement_id": str(promoted.id)}
