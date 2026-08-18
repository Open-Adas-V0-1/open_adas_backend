import uuid

from langgraph.graph import END
from langgraph.types import interrupt

from agents.sysml.state import SysmlState
from app.schemas.sysml import Intent, IntentDecision
from data.db import async_session_factory
from data.models import RequirementLevel
from data.repository import RequirementRepo
from llm.factory import get_llm
from llm.prompts import load_prompt

# T4a only wires "requirement" back to generate_requirement; T4b/T4c add "diagram" etc.
SOURCE_NODE_MAP = {"requirement": "generate_requirement"}


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
    # fail-open: no_action, conversation, and (in T4a) any not-yet-built intent all end
    # gracefully instead of crashing the graph.
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
    }


def requirement_review(state: SysmlState) -> dict:
    # No DB writes above this line — this node re-runs from the top on resume.
    decision = interrupt(
        {
            "type": "requirement_review",
            "draft": state["draft_requirement"],
            "level": state.get("level", "functional"),
        }
    )

    action = decision.get("action") if isinstance(decision, dict) else decision
    feedback = decision.get("feedback") if isinstance(decision, dict) else None

    return {"review_decision": action, "feedback": feedback}


def route_from_review(state: SysmlState) -> str:
    decision = state.get("review_decision")
    if decision == "approve":
        return "stockage_output"
    if decision == "regenerate":
        return SOURCE_NODE_MAP.get(state.get("source_node"), END)
    # fail-open: unrecognized/failed decision ends gracefully rather than crashing.
    return END


async def stockage_output(state: SysmlState) -> dict:
    async with async_session_factory() as db:
        requirement = await RequirementRepo.create(
            db,
            session_id=state["session_id"],
            content=state["draft_requirement"],
            level=RequirementLevel(state.get("level", "functional")),
        )
        await db.commit()

    return {"persisted_requirement_id": str(requirement.id)}


def route_from_stockage(state: SysmlState) -> str:
    return "promote_requirement"


async def promote_requirement(state: SysmlState) -> dict:
    async with async_session_factory() as db:
        promoted = await RequirementRepo.promote(
            db,
            id=uuid.UUID(str(state["persisted_requirement_id"])),
            session_id=state["session_id"],
        )
        await db.commit()

    return {"result": "promoted", "active_requirement_id": str(promoted.id)}
