"""Standalone tests for T4c: apply_published_delta + contextual_answer.

LLM call sites are stubbed (same rationale as T4a/T4b's smoke tests: local Ollama models
don't reliably support LangChain structured-output tool-calling in this environment).
Everything else — graph wiring, interrupt/resume mechanics, and persistence/promotion via
the T2 repository — is real.

Run: python -m scripts.smoke_test_t4c
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
from data.repository import (
    ProjectRepo,
    PublishedRequirementRepo,
    RequirementRepo,
    SessionRepo,
    UserRepo,
)


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
    def __init__(self, drafts):
        self._drafts = drafts
        self.calls = 0

    async def ainvoke(self, prompt):
        draft = self._drafts[min(self.calls, len(self._drafts) - 1)]
        self.calls += 1
        return FakeMessage(draft)


async def setup_user_project_session():
    async with async_session_factory() as db:
        user = await UserRepo.create(db, email=f"t4c-{uuid.uuid4()}@test.dev", password_hash="hashed")
        project = await ProjectRepo.create(db, user_id=user.id, name="T4c Project")
        session = await SessionRepo.create(
            db, project_id=project.id, thread_id=str(uuid.uuid4()), title="T4c Session"
        )
        await db.commit()
        return user, session


async def cleanup_user(user):
    async with async_session_factory() as db:
        db_user = await UserRepo.get_by_id(db, user.id)
        await db.delete(db_user)
        await db.commit()


# ---------------------------------------------------------------------------
# Scenarios 1 + 2 combined: apply_delta happy path, with a regenerate step that
# must route back to apply_published_delta (not generate_requirement).
# ---------------------------------------------------------------------------
async def test_apply_delta_and_regenerate():
    print("\n--- Scenario: apply_published_delta happy path + regenerate-through-delta ---")
    user, session = await setup_user_project_session()

    async with async_session_factory() as db:
        published = await PublishedRequirementRepo.create(
            db, session_id=session.id, content="The system shall log all sensor faults.",
            level=RequirementLevel.functional,
        )
        await db.commit()
        published_id = published.id

    draft_1 = "The system shall log all braking sensor faults within 100ms."
    draft_2 = "The system shall log all braking sensor faults within 100ms, including timestamp and sensor id."

    supervisor_llm = FakeSupervisorLLM(IntentDecision(intent=Intent.apply_published_delta))
    delta_llm = FakeSequenceLLM([draft_1, draft_2])

    def fake_get_llm(node_name=None):
        if node_name == "sysml_supervisor":
            return supervisor_llm
        if node_name == "sysml_apply_published_delta":
            return delta_llm
        raise AssertionError(f"unexpected node_name in apply_delta path: {node_name}")

    with patch("agents.sysml.nodes.get_llm", side_effect=fake_get_llm):
        checkpointer = MemorySaver()
        graph = build_sysml_graph(checkpointer=checkpointer)
        config = {"configurable": {"thread_id": str(session.id)}}

        result_1 = await graph.ainvoke(
            {
                "user_input": "Apply the sensor-fault-logging published requirement here",
                "session_id": session.id,
                "selected_published_requirement_id": published_id,
            },
            config,
        )
        assert result_1.get("__interrupt__"), "expected pause at requirement_review"
        payload = result_1["__interrupt__"][0].value
        assert payload["source_node"] == "delta"
        print(f"RUN 1: paused. source_node={payload['source_node']} draft={payload['draft']!r}")
        assert delta_llm.calls == 1

        async with async_session_factory() as db:
            rows = await RequirementRepo.list_by_session(db, session_id=session.id)
            assert rows == [], "no DB write before approval"
        print("assert OK: no DB row exists before approval")

        # regenerate: must route back to apply_published_delta, NOT generate_requirement
        # (fake_get_llm raises if generate_requirement's node_name is ever requested)
        result_2 = await graph.ainvoke(
            Command(resume={"action": "regenerate", "feedback": "add timestamp and sensor id"}), config
        )
        assert result_2.get("__interrupt__"), "expected second pause"
        assert delta_llm.calls == 2, "regenerate must route back to apply_published_delta"
        print(f"RUN 2: paused again via apply_published_delta (calls={delta_llm.calls}), "
              f"draft={result_2['__interrupt__'][0].value['draft']!r}")

        result_3 = await graph.ainvoke(Command(resume={"action": "approve"}), config)
        assert result_3.get("result") == "promoted"
        print(f"RUN 3: completed. result={result_3['result']!r}")

        async with async_session_factory() as db:
            rows = await RequirementRepo.list_by_session(db, session_id=session.id)
            assert len(rows) == 1, f"expected exactly 1 persisted requirement, got {len(rows)}"
            requirement = rows[0]
            assert requirement.content == draft_2
            assert requirement.status == VersionStatus.active
            assert requirement.source_published_requirement_id == published_id, (
                "provenance link to the published requirement must be kept"
            )
            print(
                f"assert OK: requirement persisted content={requirement.content!r} "
                f"status={requirement.status} source_published_requirement_id="
                f"{requirement.source_published_requirement_id} (== published: "
                f"{requirement.source_published_requirement_id == published_id})"
            )

    await cleanup_user(user)
    print("Scenario PASSED: apply_delta + regenerate-through-delta")


# ---------------------------------------------------------------------------
# Scenario 3: contextual question during review — read-only, routes back to review.
# ---------------------------------------------------------------------------
async def test_contextual_question_during_review():
    print("\n--- Scenario: contextual question during review ---")
    user, session = await setup_user_project_session()

    draft = "The system shall stop within 50 meters."
    answer_text = (
        "This requirement is written as a single obligation on stopping distance — "
        "I kept it separate from any speed constraint so each requirement stays testable "
        "on its own."
    )

    supervisor_llm = FakeSupervisorLLM(IntentDecision(intent=Intent.generate_requirement))
    generate_llm = FakeSequenceLLM([draft])
    answer_llm = FakeSequenceLLM([answer_text])

    def fake_get_llm(node_name=None):
        if node_name == "sysml_supervisor":
            return supervisor_llm
        if node_name == "sysml_generate_requirement":
            return generate_llm
        if node_name == "sysml_contextual_answer":
            return answer_llm
        raise AssertionError(f"unexpected node_name in contextual-question path: {node_name}")

    with patch("agents.sysml.nodes.get_llm", side_effect=fake_get_llm):
        checkpointer = MemorySaver()
        graph = build_sysml_graph(checkpointer=checkpointer)
        config = {"configurable": {"thread_id": str(session.id)}}

        result_1 = await graph.ainvoke(
            {"user_input": "I need a requirement about braking distance", "session_id": session.id}, config
        )
        assert result_1.get("__interrupt__")
        print(f"RUN 1: paused for review. draft={result_1['__interrupt__'][0].value['draft']!r}")

        async with async_session_factory() as db:
            rows = await RequirementRepo.list_by_session(db, session_id=session.id)
            assert rows == []
        print("assert OK: no DB row before the question")

        # ask a contextual question instead of approving/regenerating
        result_2 = await graph.ainvoke(
            Command(resume={"action": "question", "question": "Why did you split this requirement?"}), config
        )
        assert result_2.get("__interrupt__"), "must route back to review, still paused"
        payload_2 = result_2["__interrupt__"][0].value
        assert payload_2["draft"] == draft, "draft must be unchanged by a question"
        print(f"RUN 2: contextual_answer ran, routed back to review. draft unchanged={payload_2['draft'] == draft}")
        assert answer_llm.calls == 1
        assert generate_llm.calls == 1, "asking a question must NOT trigger regeneration"

        async with async_session_factory() as db:
            rows = await RequirementRepo.list_by_session(db, session_id=session.id)
            assert rows == [], "contextual_answer must write NOTHING to the DB"
        print("assert OK: contextual_answer wrote nothing to the DB")

        # now actually approve
        result_3 = await graph.ainvoke(Command(resume={"action": "approve"}), config)
        assert result_3.get("result") == "promoted"

        async with async_session_factory() as db:
            rows = await RequirementRepo.list_by_session(db, session_id=session.id)
            assert len(rows) == 1, f"expected exactly 1 persisted requirement, got {len(rows)}"
            assert rows[0].content == draft
            assert rows[0].status == VersionStatus.active
            print(
                f"assert OK: final approval persisted exactly once. content={rows[0].content!r} "
                f"status={rows[0].status}"
            )

    await cleanup_user(user)
    print("Scenario PASSED: contextual question during review")


async def main() -> None:
    await test_apply_delta_and_regenerate()
    await test_contextual_question_during_review()
    print("\n=== T4C TEST SUITE PASSED ===")


if __name__ == "__main__":
    asyncio.run(main())
