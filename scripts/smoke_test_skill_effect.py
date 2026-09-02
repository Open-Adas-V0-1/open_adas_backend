"""Integration check: does wiring the SysML v2 skill into plan_node/generate_node
actually reduce verify/regenerate rounds?

Unlike the other Layer-3 tests, this one uses the REAL LLM factory (get_llm) against
whatever backend is configured in .env — sysml_plan and sysml_generate are NOT stubbed,
because the whole point is to observe how the ACTUAL model's output quality changes with
vs without the skill's selectively-loaded guidance. sysml_supervisor IS stubbed (fixed
intent/level) so the experiment isolates generation quality, not intent classification.

This is a single real-model run per condition, not an averaged multi-trial experiment —
LLM output is non-deterministic, so treat the numbers as one honest data point, not a
proof. The finding is reported as observed, whichever way it goes.

Run: python -m scripts.smoke_test_skill_effect
"""
import asyncio
import uuid
from unittest.mock import patch

from langgraph.checkpoint.memory import MemorySaver
from langgraph.types import Command

import agents.sysml.nodes as nodes
from agents.sysml.graph import build_sysml_graph
from app.schemas.sysml import Intent, IntentDecision
from data.db import async_session_factory
from data.repository import ProjectRepo, RequirementRepo, SessionRepo, UserRepo
from tools.sysml_v2.lsp_client import shutdown_lsp_client

# Deliberately tricky: multiple attributes, multiple obligations, an interface mention —
# stresses whether the model produces correct requirement/subject/constraint STRUCTURE
# (which daltskin's LSP does enforce), not just plausible English.
TRICKY_REQUEST = (
    "Create a physical-level requirement: the brake caliper assembly, consisting of a "
    "caliper body and two mounting bolts, shall have a combined mass no greater than "
    "2.5 kilograms, and shall interface with a hydraulic port rated for at least "
    "10 megapascals of pressure."
)


class FakeStructuredLLM:
    def __init__(self, decision):
        self.decision = decision

    async def ainvoke(self, prompt):
        return self.decision


class FakeStructuredWrapperLLM:
    def __init__(self, decision):
        self._structured = FakeStructuredLLM(decision)

    def with_structured_output(self, schema):
        return self._structured


async def setup_user_project_session(label: str):
    async with async_session_factory() as db:
        user = await UserRepo.create(db, email=f"skilleffect-{label}-{uuid.uuid4()}@test.dev", password_hash="hashed")
        project = await ProjectRepo.create(db, user_id=user.id, name=f"SkillEffect {label}")
        session = await SessionRepo.create(
            db, project_id=project.id, thread_id=str(uuid.uuid4()), title=f"SkillEffect {label}"
        )
        await db.commit()
        return user, session


async def cleanup_user(user):
    async with async_session_factory() as db:
        db_user = await UserRepo.get_by_id(db, user.id)
        await db.delete(db_user)
        await db.commit()


async def run_condition(label: str, disable_skill: bool) -> dict:
    print(f"\n--- Condition: {label} (skill {'DISABLED' if disable_skill else 'ENABLED'}) ---")
    user, session = await setup_user_project_session(label)

    supervisor_llm = FakeStructuredWrapperLLM(
        IntentDecision(intent=Intent.generate_requirement, level=None)
    )

    def fake_get_llm(node_name=None):
        if node_name == "sysml_supervisor":
            return supervisor_llm
        # sysml_plan / sysml_generate: NOT stubbed, real model via the real factory.
        from llm.factory import get_llm as real_get_llm
        return real_get_llm(node_name)

    patches = [patch("agents.sysml.nodes.get_llm", side_effect=fake_get_llm)]
    if disable_skill:
        patches.append(patch("agents.sysml.nodes._skill_guidance", return_value=""))
        patches.append(patch("agents.sysml.nodes.get_error_help", return_value=""))

    try:
        for p in patches:
            p.start()

        checkpointer = MemorySaver()
        graph = build_sysml_graph(checkpointer=checkpointer)
        config = {"configurable": {"thread_id": str(session.id)}}

        result = await graph.ainvoke(
            {"user_input": TRICKY_REQUEST, "session_id": session.id, "level": "physical"}, config
        )

        # drive to completion regardless of outcome (clean review pause, or fail-open
        # warning review pause) — either way the loop has already finished by the time
        # we reach requirement_review, so we just read the counters off state.
        rounds = result.get("verify_visits")
        clean = result.get("verify_clean")
        diagnostics = result.get("__interrupt__", [None])[0]
        diag_count = len((diagnostics.value or {}).get("verify_diagnostics") or []) if diagnostics else None

        print(f"rounds (verify_visits) = {rounds}")
        print(f"exited clean = {clean}")
        print(f"remaining diagnostics at handoff = {diag_count}")
        if diagnostics:
            draft = diagnostics.value.get("draft", "") or ""
            safe_draft = draft.encode("ascii", errors="replace").decode("ascii")
            print(f"final draft:\n{safe_draft}")

        # resume with reject/cancel-equivalent so we don't leave the graph mid-run;
        # we don't care about finalize here, just the round count, so just approve to
        # close it out cleanly (avoids leaving stray "pending review" state around).
        await graph.ainvoke(Command(resume={"action": "approve"}), config)

    finally:
        for p in reversed(patches):
            p.stop()

    await cleanup_user(user)
    return {"rounds": rounds, "clean": clean, "diagnostics_remaining": diag_count}


async def main() -> None:
    with_skill = await run_condition("with-skill", disable_skill=False)
    without_skill = await run_condition("without-skill", disable_skill=True)

    await shutdown_lsp_client()

    print("\n" + "=" * 70)
    print("SKILL EFFECT — SUMMARY (single real-model run per condition)")
    print("=" * 70)
    print(f"WITH skill wired in:    rounds={with_skill['rounds']} "
          f"clean={with_skill['clean']} remaining_diagnostics={with_skill['diagnostics_remaining']}")
    print(f"WITHOUT skill wired in: rounds={without_skill['rounds']} "
          f"clean={without_skill['clean']} remaining_diagnostics={without_skill['diagnostics_remaining']}")

    if with_skill["rounds"] <= without_skill["rounds"]:
        print("\n=> the skill did NOT increase verify/regenerate rounds on this run "
              f"({with_skill['rounds']} <= {without_skill['rounds']}).")
    else:
        print("\n=> on THIS run, the skill-enabled condition took MORE rounds "
              f"({with_skill['rounds']} > {without_skill['rounds']}) — reported as observed, "
              "not smoothed over. Single non-deterministic LLM run; see caveats in the module docstring.")


if __name__ == "__main__":
    asyncio.run(main())
