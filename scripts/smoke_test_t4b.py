"""Standalone tests for T4b: diagram generation + guard, and the repo lineage fix.

LLM call sites are stubbed (same rationale as T4a's smoke test: local Ollama models don't
reliably support LangChain structured-output tool-calling in this environment). Everything
else — graph wiring, interrupt/resume mechanics, guard DB read, and persistence/promotion
via the T2 repository — is real.

Run: python -m scripts.smoke_test_t4b
"""
import asyncio
import uuid
from unittest.mock import patch

from langgraph.checkpoint.memory import MemorySaver
from langgraph.types import Command

from agents.sysml.graph import build_sysml_graph
from app.schemas.sysml import Intent, IntentDecision
from data.db import async_session_factory
from data.models import DiagramType, RequirementLevel, VersionStatus
from data.repository import DiagramRepo, ProjectRepo, RequirementRepo, SessionRepo, UserRepo


class FakeMessage:
    def __init__(self, content):
        self.content = content


class FakeStructuredLLM:
    def __init__(self, decision):
        self._decision = decision

    async def ainvoke(self, prompt):
        return self._decision


class FakeSupervisorLLM:
    def __init__(self, decision):
        self._decision = decision

    def with_structured_output(self, schema):
        return FakeStructuredLLM(self._decision)


class FakeSequenceLLM:
    """Returns drafts[0], drafts[1], ... on successive calls (clamped to the last)."""

    def __init__(self, drafts):
        self._drafts = drafts
        self.calls = 0

    async def ainvoke(self, prompt):
        draft = self._drafts[min(self.calls, len(self._drafts) - 1)]
        self.calls += 1
        return FakeMessage(draft)


async def setup_user_project_session():
    async with async_session_factory() as db:
        user = await UserRepo.create(db, email=f"t4b-{uuid.uuid4()}@test.dev", password_hash="hashed")
        project = await ProjectRepo.create(db, user_id=user.id, name="T4b Project")
        session = await SessionRepo.create(
            db, project_id=project.id, thread_id=str(uuid.uuid4()), title="T4b Session"
        )
        await db.commit()
        return user, session


async def cleanup_user(user):
    async with async_session_factory() as db:
        db_user = await UserRepo.get_by_id(db, user.id)
        await db.delete(db_user)
        await db.commit()


# ---------------------------------------------------------------------------
# Scenario 2: diagram happy path
# ---------------------------------------------------------------------------
async def test_diagram_happy_path():
    print("\n--- Scenario: diagram happy path ---")
    user, session = await setup_user_project_session()

    async with async_session_factory() as db:
        base_requirement = await RequirementRepo.create(
            db, session_id=session.id, content="The system shall stop within 50 meters.",
            level=RequirementLevel.functional,
        )
        base_requirement = await RequirementRepo.promote(db, id=base_requirement.id, session_id=session.id)
        await db.commit()
        requirement_id = base_requirement.id

    draft_1 = "@startuml\nstate Braking\n[*] --> Braking\n@enduml"
    draft_2 = "@startuml\nstate Braking\nstate Stopped\n[*] --> Braking\nBraking --> Stopped : distance <= 50m\n@enduml"

    supervisor_llm = FakeSupervisorLLM(
        IntentDecision(intent=Intent.generate_diagram, diagram_type=DiagramType.state_machine)
    )
    diagram_llm = FakeSequenceLLM([draft_1, draft_2])

    def fake_get_llm(node_name=None):
        if node_name == "sysml_supervisor":
            return supervisor_llm
        if node_name == "sysml_generate_diagram":
            return diagram_llm
        raise AssertionError(f"unexpected node_name in diagram happy path: {node_name}")

    with patch("agents.sysml.nodes.get_llm", side_effect=fake_get_llm):
        checkpointer = MemorySaver()
        graph = build_sysml_graph(checkpointer=checkpointer)
        config = {"configurable": {"thread_id": str(session.id)}}

        result_1 = await graph.ainvoke(
            {
                "user_input": "Give me a state machine diagram for the braking requirement",
                "session_id": session.id,
                "target_requirement_id": requirement_id,
            },
            config,
        )
        assert result_1.get("__interrupt__"), "expected pause at requirement_review"
        payload = result_1["__interrupt__"][0].value
        assert payload["source_node"] == "diagram"
        print(f"RUN 1: paused. source_node={payload['source_node']} draft={payload['draft'][:40]}...")
        assert diagram_llm.calls == 1

        async with async_session_factory() as db:
            rows = await DiagramRepo.get_by_requirement(db, requirement_id=requirement_id, session_id=session.id)
            assert rows == [], "no DB write before approval"
        print("assert OK: no diagram row exists before approval")

        result_2 = await graph.ainvoke(
            Command(resume={"action": "regenerate", "feedback": "add a Stopped state"}), config
        )
        assert result_2.get("__interrupt__"), "expected second pause"
        assert diagram_llm.calls == 2
        print(f"RUN 2: paused again. calls={diagram_llm.calls}")

        async with async_session_factory() as db:
            rows = await DiagramRepo.get_by_requirement(db, requirement_id=requirement_id, session_id=session.id)
            assert rows == [], "still no DB write before approval"
        print("assert OK: still no diagram row after regenerate pause")

        result_3 = await graph.ainvoke(Command(resume={"action": "approve"}), config)
        assert result_3.get("result") == "promoted"
        print(f"RUN 3: completed. result={result_3['result']!r} active_diagram_id={result_3.get('active_diagram_id')}")

        async with async_session_factory() as db:
            rows = await DiagramRepo.get_by_requirement(db, requirement_id=requirement_id, session_id=session.id)
            assert len(rows) == 1, f"expected exactly 1 persisted diagram, got {len(rows)}"
            diagram = rows[0]
            assert diagram.plantuml == draft_2
            assert diagram.status == VersionStatus.active
            assert diagram.requirement_id == requirement_id
            assert diagram.type == DiagramType.state_machine
            print(
                f"assert OK: diagram persisted content=(v2) status={diagram.status} "
                f"linked requirement_id={diagram.requirement_id} type={diagram.type}"
            )

    await cleanup_user(user)
    print("Scenario PASSED: diagram happy path")


# ---------------------------------------------------------------------------
# Scenario 3: guard "no" path
# ---------------------------------------------------------------------------
async def test_guard_no_path():
    print("\n--- Scenario: guard 'no' path (no valid base requirement) ---")
    user, session = await setup_user_project_session()

    canned_reply = (
        "I don't see a requirement yet to base that use case diagram on — "
        "let's create the requirement first, then I can generate the diagram from it."
    )

    supervisor_llm = FakeSupervisorLLM(
        IntentDecision(intent=Intent.generate_diagram, diagram_type=DiagramType.use_case)
    )
    message_llm = FakeSequenceLLM([canned_reply])

    def fake_get_llm(node_name=None):
        if node_name == "sysml_supervisor":
            return supervisor_llm
        if node_name == "sysml_message_no_requirement":
            return message_llm
        raise AssertionError(f"unexpected node_name in guard-no path: {node_name}")

    with patch("agents.sysml.nodes.get_llm", side_effect=fake_get_llm):
        checkpointer = MemorySaver()
        graph = build_sysml_graph(checkpointer=checkpointer)
        config = {"configurable": {"thread_id": str(session.id)}}

        result = await graph.ainvoke(
            {
                "user_input": "Give me a use case diagram",
                "session_id": session.id,
                "target_requirement_id": uuid.uuid4(),  # doesn't exist
            },
            config,
        )

        assert not result.get("__interrupt__"), "guard-no path must not pause"
        assert result.get("result") == "no_requirement"
        assert result.get("no_requirement_message") == canned_reply
        print(f"assert OK: routed straight to message_no_requirement -> END, message={result['no_requirement_message'][:60]!r}...")

        async with async_session_factory() as db:
            rows = await RequirementRepo.list_active_for_session(db, session_id=session.id)
            assert rows == []
        print("assert OK: no requirement exists (as expected) and no diagram was persisted")

    await cleanup_user(user)
    print("Scenario PASSED: guard 'no' path")


# ---------------------------------------------------------------------------
# Scenario 1: repository lineage fix (two distinct active requirements)
# ---------------------------------------------------------------------------
async def test_repo_lineage():
    print("\n--- Scenario: repo lineage fix ---")
    user, session = await setup_user_project_session()

    async with async_session_factory() as db:
        req_a_v1 = await RequirementRepo.create(db, session_id=session.id, content="A shall v1")
        req_a_v1 = await RequirementRepo.promote(db, id=req_a_v1.id, session_id=session.id)

        req_b_v1 = await RequirementRepo.create(db, session_id=session.id, content="B shall v1")
        req_b_v1 = await RequirementRepo.promote(db, id=req_b_v1.id, session_id=session.id)

        active = await RequirementRepo.list_active_for_session(db, session_id=session.id)
        assert {r.id for r in active} == {req_a_v1.id, req_b_v1.id}
        print(f"assert OK: two distinct requirements both active: {[r.id for r in active]}")

        req_a_v2 = await RequirementRepo.supersede_and_create_version(
            db, old_id=req_a_v1.id, new_content="A shall v2", session_id=session.id
        )
        req_a_v2 = await RequirementRepo.promote(db, id=req_a_v2.id, session_id=session.id)

        req_a_v1_reloaded = await RequirementRepo.get_by_id(db, id=req_a_v1.id, session_id=session.id)
        req_b_v1_reloaded = await RequirementRepo.get_by_id(db, id=req_b_v1.id, session_id=session.id)
        assert req_a_v1_reloaded.status == VersionStatus.superseded
        assert req_b_v1_reloaded.status == VersionStatus.active, "unrelated lineage must stay active"
        print(
            f"assert OK: A's v2 supersedes only A's v1 ({req_a_v1_reloaded.status}); "
            f"B's v1 untouched ({req_b_v1_reloaded.status})"
        )

        await db.commit()

    await cleanup_user(user)
    print("Scenario PASSED: repo lineage fix")


async def main() -> None:
    await test_repo_lineage()
    await test_diagram_happy_path()
    await test_guard_no_path()
    print("\n=== T4B TEST SUITE PASSED ===")


if __name__ == "__main__":
    asyncio.run(main())
