from langchain_core.runnables import RunnableConfig

from data.db import async_session_factory
from data.repository import DiagramRepo, RequirementRepo
from supervisor.state import SupervisorState

_LEVEL_KEYWORDS = ("operational", "functional", "physical")


def _short_summary(content: str, limit: int = 80) -> str:
    collapsed = " ".join(content.split())
    return collapsed if len(collapsed) <= limit else collapsed[: limit - 1] + "…"


async def resolve_revisit(state: SupervisorState, config: RunnableConfig) -> dict:
    """Step 2c, piece 1: deterministically resolves WHICH existing generation the user
    means by a revisit_generation message, and writes its permanent gen_id handle to
    state. Classification already happened DB-free in top_level_supervisor -- this is
    the ONLY node with DB access on this path (same pattern as Layer-2's own nodes).

    NO time-travel, NO thread re-opening, NO re-generation here -- piece 3 acts on the
    resolved gen_id. On success, control routes straight to finalize_turn for now so the
    resolved target is observable; on failure to resolve, the turn ends by asking the
    user to clarify (fail-open, never a crash, never a fabricated gen_id).
    """
    session_id = state["session_id"]
    user_input_lower = (state.get("user_input") or "").lower()

    async with async_session_factory() as db:
        requirements = await RequirementRepo.list_revisitable_for_session(db, session_id=session_id)
        diagrams = await DiagramRepo.list_revisitable_for_session(db, session_id=session_id)

    # Deterministic index: [{gen_id, level, artifact_type, short_summary}] -- rows with
    # no gen_id (pre-dating that column) are already excluded by the repo helpers above.
    rows = [
        {
            "gen_id": r.gen_id,
            "level": r.level.value,
            "artifact_type": "requirement",
            "short_summary": _short_summary(r.content),
        }
        for r in requirements
    ] + [
        {
            "gen_id": d.gen_id,
            "level": d.level.value if d.level else None,
            "artifact_type": "diagram",
            "short_summary": f"{d.type.value} diagram",
        }
        for d in diagrams
    ]

    if not rows:
        return {
            "done": True,
            "result": "revisit_no_target",
            "response": (
                "There's nothing generated yet in this session to modify -- want me to "
                "generate something new instead?"
            ),
        }

    matched_level = next((lvl for lvl in _LEVEL_KEYWORDS if lvl in user_input_lower), None)
    candidates = [row for row in rows if row["level"] == matched_level] if matched_level else rows

    if len(candidates) == 1:
        target = candidates[0]
        return {
            "revisit_gen_id": target["gen_id"],
            "revisit_target_summary": target["short_summary"],
            "supervisor_visits": state.get("supervisor_visits") or 0,
        }

    if not candidates:
        # matched_level was set (else `rows` being non-empty would make candidates ==
        # rows, non-empty) but no row at that level exists.
        response = f"I don't have a {matched_level} generation in this session to modify yet."
    elif matched_level:
        response = f"There are several {matched_level} generations -- which one did you mean?"
    else:
        options = "; ".join(f"{row['artifact_type']} ({row['level'] or 'unspecified level'})" for row in candidates)
        response = f"Which one did you mean? I found: {options}."

    return {
        "done": True,
        "result": "revisit_no_target",
        "response": response,
    }
