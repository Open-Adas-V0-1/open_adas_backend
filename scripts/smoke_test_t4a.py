"""Standalone test for T4a: SysML core requirement flow (interrupt/resume/persist/promote).

The graph's LLM call sites are stubbed so the test is deterministic and independent of
which LLM backend/model is configured — local Ollama models don't reliably support
LangChain's structured-output tool-calling in this environment (verified separately;
T3's factory itself works fine against a real provider). Everything else — the graph
wiring, the interrupt/resume mechanics, and the DB writes via the T2 repository — is real.

Run: python -m scripts.smoke_test_t4a
"""
import asyncio
import uuid
from unittest.mock import patch

from langgraph.checkpoint.memory import MemorySaver
from langgraph.types import Command

from agents.sysml.graph import build_sysml_graph
from app.schemas.sysml import Intent, IntentDecision
from data.db import async_session_factory
from data.models import RequirementLevel, VersionStatus
from data.repository import ProjectRepo, RequirementRepo, SessionRepo, UserRepo

DRAFT_1 = "The system shall stop within 50 meters."
DRAFT_2 = "The system shall bring the vehicle to a complete stop within 30 meters when braking at 60 km/h."

generate_calls = 0


class FakeMessage:
    def __init__(self, content):
        self.content = content


class FakeStructuredLLM:
    def __init__(self, decision):
        self._decision = decision

    async def ainvoke(self, prompt):
        return self._decision


class FakeSupervisorLLM:
    def with_structured_output(self, schema):
        return FakeStructuredLLM(IntentDecision(intent=Intent.generate_requirement))


class FakeGenerateLLM:
    async def ainvoke(self, prompt):
        global generate_calls
        generate_calls += 1
        draft = DRAFT_1 if generate_calls == 1 else DRAFT_2
        return FakeMessage(draft)


def fake_get_llm(node_name: str | None = None):
    if node_name == "sysml_supervisor":
        return FakeSupervisorLLM()
    return FakeGenerateLLM()


async def main() -> None:
    async with async_session_factory() as db:
        user = await UserRepo.create(db, email=f"t4a-{uuid.uuid4()}@test.dev", password_hash="hashed")
        project = await ProjectRepo.create(db, user_id=user.id, name="T4a Project")
        session = await SessionRepo.create(
            db, project_id=project.id, thread_id=str(uuid.uuid4()), title="T4a Session"
        )
        await db.commit()
        session_id = session.id

    with patch("agents.sysml.nodes.get_llm", side_effect=fake_get_llm):
        checkpointer = MemorySaver()
        graph = build_sysml_graph(checkpointer=checkpointer)
        config = {"configurable": {"thread_id": str(session_id)}}

        # --- Run 1: pause at first review ---
        result_1 = await graph.ainvoke(
            {"user_input": "I need a requirement about braking distance", "session_id": session_id},
            config,
        )
        assert result_1.get("__interrupt__"), "expected graph to pause at requirement_review"
        print(f"RUN 1: paused. interrupt payload = {result_1['__interrupt__'][0].value}")
        assert generate_calls == 1, f"expected generate_requirement to run once, ran {generate_calls}"

        async with async_session_factory() as db:
            rows = await RequirementRepo.list_by_session(db, session_id=session_id)
            assert rows == [], "no DB write must happen before approval"
        print("assert OK: no DB row exists before approval (after first pause)")

        # --- Run 2: resume with regenerate + feedback ---
        result_2 = await graph.ainvoke(
            Command(resume={"action": "regenerate", "feedback": "Be more specific: add speed and distance."}),
            config,
        )
        assert result_2.get("__interrupt__"), "expected graph to pause again at requirement_review"
        print(f"RUN 2: paused again. interrupt payload = {result_2['__interrupt__'][0].value}")
        assert generate_calls == 2, f"expected generate_requirement to run twice, ran {generate_calls}"

        async with async_session_factory() as db:
            rows = await RequirementRepo.list_by_session(db, session_id=session_id)
            assert rows == [], "still no DB write must happen before approval"
        print("assert OK: still no DB row exists before approval (after regenerate pause)")

        # --- Run 3: resume with approve ---
        result_3 = await graph.ainvoke(Command(resume={"action": "approve"}), config)
        assert result_3.get("result") == "promoted"
        print(f"RUN 3: completed. final state result={result_3['result']!r}")

        async with async_session_factory() as db:
            rows = await RequirementRepo.list_by_session(db, session_id=session_id)
            assert len(rows) == 1, f"expected exactly 1 persisted requirement, got {len(rows)}"
            requirement = rows[0]
            assert requirement.content == DRAFT_2, "persisted content should be the regenerated (2nd) draft"
            assert requirement.status == VersionStatus.active, "requirement must be promoted to active"
            assert requirement.level == RequirementLevel.functional
            print(
                f"assert OK: 1 requirement persisted, content={requirement.content!r} "
                f"status={requirement.status} version={requirement.version}"
            )

    # cleanup: cascade-delete the test user (-> project -> session -> requirement)
    async with async_session_factory() as db:
        db_user = await UserRepo.get_by_id(db, user.id)
        await db.delete(db_user)
        await db.commit()

    print(f"\ngenerate_requirement total calls: {generate_calls} (expected 2)")
    print("=== T4A TEST PASSED ===")


if __name__ == "__main__":
    asyncio.run(main())
