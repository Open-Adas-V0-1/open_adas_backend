<<<<<<< Updated upstream
"""Standalone tests for T5b: conditional user_confirm_inputs in the SysML middle layer,
under a REAL Postgres checkpointer (same as T5a).

LLM call sites are stubbed (same rationale as T4/T5a: local Ollama models don't reliably
support LangChain structured-output tool-calling in this environment). Everything else —
graph wiring, ambiguity routing (router-as-code, reads Postgres), interrupt/resume, and
persistence via the T2 repository — runs for real.

Run: python -m scripts.smoke_test_t5b
"""
import asyncio
import sys
import uuid
from unittest.mock import patch

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
# NOTE: this policy is required for AsyncPostgresSaver/psycopg on Windows, but is
# incompatible with asyncio subprocesses (needed by Layer 3's real SysML v2 tooling).
# This test's job is ambiguity routing/confirm patterns, not the tool integration
# (covered for real by scripts/smoke_test_layer3_rebuild.py), so verify_node's tool
# call is stubbed below rather than fighting that Windows-only conflict.

from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver  # noqa: E402
from langgraph.types import Command  # noqa: E402
from sqlalchemy import text  # noqa: E402

from agents.sysml.middle_graph import build_middle_config, build_middle_graph  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.schemas.sysml import Intent, IntentDecision, MiddleDecision  # noqa: E402
from data.db import async_session_factory  # noqa: E402
from data.models import DiagramType, VersionStatus  # noqa: E402
from data.repository import DiagramRepo, ProjectRepo, RequirementRepo, SessionRepo, UserRepo  # noqa: E402

VALID_DIAGRAM_MODEL = (
    "package BrakeStates {\n"
    "    part def BrakingController {\n"
    "        state def Idle;\n"
    "        state def Braking;\n"
    "    }\n"
    "    part controller : BrakingController;\n"
    "}\n"
)


class FakeMessage:
    def __init__(self, content):
        self.content = content


class FakeStructuredLLM:
    def __init__(self, decisions):
        self._decisions = decisions if isinstance(decisions, list) else [decisions]
        self.calls = 0

    async def ainvoke(self, prompt):
        decision = self._decisions[min(self.calls, len(self._decisions) - 1)]
        self.calls += 1
        return decision

    async def astream(self, prompt):
        yield await self.ainvoke(prompt)

    def with_config(self, **kwargs):
        return self


class FakeStructuredWrapperLLM:
    def __init__(self, decisions):
        self._structured = FakeStructuredLLM(decisions)

    def with_structured_output(self, schema):
        return self._structured


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
        user = await UserRepo.create(db, email=f"t5b-{uuid.uuid4()}@test.dev", password_hash="hashed")
        project = await ProjectRepo.create(db, user_id=user.id, name="T5b Project")
        session = await SessionRepo.create(
            db, project_id=project.id, thread_id=str(uuid.uuid4()), title="T5b Session"
        )
        await db.commit()
        return user, session


async def cleanup_user(user):
    async with async_session_factory() as db:
        db_user = await UserRepo.get_by_id(db, user.id)
        await db.delete(db_user)
        await db.commit()


async def clear_checkpoints():
    async with async_session_factory() as db:
        await db.execute(text("DELETE FROM checkpoint_writes"))
        await db.execute(text("DELETE FROM checkpoint_blobs"))
        await db.execute(text("DELETE FROM checkpoints"))
        await db.commit()


# ---------------------------------------------------------------------------
# Scenario 1: unambiguous (exactly one active requirement) -> direct to processing,
# user_confirm_inputs SKIPPED.
# ---------------------------------------------------------------------------
async def test_unambiguous_direct_route():
    print("\n--- Scenario: unambiguous (1 active requirement) -> direct route, confirm SKIPPED ---")
    user, session = await setup_user_project_session()

    async with async_session_factory() as db:
        req = await RequirementRepo.create(db, session_id=session.id, content="The system shall stop within 50 meters.")
        req = await RequirementRepo.promote(db, id=req.id, session_id=session.id)
        await db.commit()
        requirement_id = req.id

    middle_llm = FakeStructuredWrapperLLM(
        MiddleDecision(has_request=True, resolved_intent=Intent.generate_diagram, diagram_type=DiagramType.state_machine)
    )
    layer3_supervisor_llm = FakeStructuredWrapperLLM(
        IntentDecision(intent=Intent.generate_diagram, diagram_type=DiagramType.state_machine)
    )
    plan_llm = FakeSequenceLLM(["State machine with Idle and Braking states."])
    diagram_llm = FakeSequenceLLM([VALID_DIAGRAM_MODEL])

    def fake_middle_get_llm(node_name=None):
        if node_name == "sysml_middle_supervisor":
            return middle_llm
        if node_name == "sysml_confirm_question":
            raise AssertionError("user_confirm_inputs must NOT run on the unambiguous path")
        raise AssertionError(f"unexpected node_name in middle_nodes: {node_name}")

    def fake_layer3_get_llm(node_name=None):
        if node_name == "sysml_supervisor":
            return layer3_supervisor_llm
        if node_name == "sysml_plan":
            return plan_llm
        if node_name == "sysml_generate":
            return diagram_llm
        raise AssertionError(f"unexpected node_name in layer-3 nodes: {node_name}")

    outer_thread_id = f"outer-{uuid.uuid4()}"
    settings = get_settings()

    async with AsyncPostgresSaver.from_conn_string(settings.checkpointer_database_url) as checkpointer:
        await checkpointer.setup()
        with patch("agents.sysml.middle_nodes.get_llm", side_effect=fake_middle_get_llm), \
             patch("agents.sysml.nodes.get_llm", side_effect=fake_layer3_get_llm), \
             patch("agents.sysml.nodes.validate", return_value=[]):

            middle_graph = build_middle_graph(checkpointer=checkpointer)
            config = build_middle_config(outer_thread_id)

            result_1 = await middle_graph.ainvoke(
                {"user_input": "give me a state machine diagram", "session_id": session.id}, config
            )
            assert result_1.get("__interrupt__"), "expected layer-3 to pause at requirement_review"
            payload = result_1["__interrupt__"][0].value
            assert payload["source_node"] == "diagram"
            print(f"RUN 1: paused DIRECTLY at layer-3 review (no confirm step). draft={payload['draft'][:30]}...")

            result_2 = await middle_graph.ainvoke(Command(resume={"action": "approve"}), config)
            light_ref = result_2.get("processing_result")
            print(f"RUN 2: completed. processing_result={light_ref}")

    async with async_session_factory() as db:
        diagrams = await DiagramRepo.get_by_requirement(db, requirement_id=requirement_id, session_id=session.id)
        assert len(diagrams) == 1
        assert diagrams[0].status == VersionStatus.active
        assert diagrams[0].requirement_id == requirement_id
        print(f"assert OK: diagram persisted and linked to the sole active requirement {requirement_id}")

    await cleanup_user(user)
    print("Scenario PASSED: unambiguous direct route (confirm skipped)")


# ---------------------------------------------------------------------------
# Scenario 2: ambiguous (2 active requirements, none named) -> pauses with the
# select_requirements_for_diagram MULTI-SELECT pattern (Step 4) -> resume selecting
# ONE of the two -> processing targets the chosen one.
# ---------------------------------------------------------------------------
async def test_ambiguous_select_requirement():
    print("\n--- Scenario: ambiguous (2 active requirements) -> select_requirements_for_diagram -> resume selection ---")
    user, session = await setup_user_project_session()

    async with async_session_factory() as db:
        req_a = await RequirementRepo.create(db, session_id=session.id, content="The system shall stop within 50 meters.")
        req_a = await RequirementRepo.promote(db, id=req_a.id, session_id=session.id)
        req_b = await RequirementRepo.create(db, session_id=session.id, content="The system shall log all sensor faults.")
        req_b = await RequirementRepo.promote(db, id=req_b.id, session_id=session.id)
        await db.commit()

    # named_requirement_id left unset -> genuinely ambiguous with 2 active requirements
    middle_llm = FakeStructuredWrapperLLM(
        MiddleDecision(has_request=True, resolved_intent=Intent.generate_diagram, diagram_type=DiagramType.state_machine)
    )
    confirm_question_llm = FakeSequenceLLM(["Which requirement should this diagram be for?"])
    layer3_supervisor_llm = FakeStructuredWrapperLLM(
        IntentDecision(intent=Intent.generate_diagram, diagram_type=DiagramType.state_machine)
    )
    plan_llm = FakeSequenceLLM(["State machine with Idle and Braking states."])
    diagram_llm = FakeSequenceLLM([VALID_DIAGRAM_MODEL])

    def fake_middle_get_llm(node_name=None):
        if node_name == "sysml_middle_supervisor":
            return middle_llm
        if node_name == "sysml_confirm_question":
            return confirm_question_llm
        raise AssertionError(f"unexpected node_name in middle_nodes: {node_name}")

    def fake_layer3_get_llm(node_name=None):
        if node_name == "sysml_supervisor":
            return layer3_supervisor_llm
        if node_name == "sysml_plan":
            return plan_llm
        if node_name == "sysml_generate":
            return diagram_llm
        raise AssertionError(f"unexpected node_name in layer-3 nodes: {node_name}")

    outer_thread_id = f"outer-{uuid.uuid4()}"
    settings = get_settings()

    async with AsyncPostgresSaver.from_conn_string(settings.checkpointer_database_url) as checkpointer:
        await checkpointer.setup()
        with patch("agents.sysml.middle_nodes.get_llm", side_effect=fake_middle_get_llm), \
             patch("agents.sysml.nodes.get_llm", side_effect=fake_layer3_get_llm), \
             patch("agents.sysml.nodes.validate", return_value=[]):

            middle_graph = build_middle_graph(checkpointer=checkpointer)
            config = build_middle_config(outer_thread_id)

            result_1 = await middle_graph.ainvoke(
                {"user_input": "give me a state machine diagram", "session_id": session.id}, config
            )
            assert result_1.get("__interrupt__"), "expected pause at user_confirm_inputs"
            payload = result_1["__interrupt__"][0].value
            assert payload["pattern"] == "select_requirements_for_diagram"
            assert payload["multi_select"] is True and payload["min_selected"] == 1 and payload["allow_all"] is True
            option_ids = {o["id"] for o in payload["options"]}
            assert option_ids == {str(req_a.id), str(req_b.id)}, "options must list BOTH active requirements"
            print(f"RUN 1: paused at user_confirm_inputs. pattern={payload['pattern']!r} "
                  f"question={payload['question']!r} options={payload['options']}")

            result_2 = await middle_graph.ainvoke(
                Command(resume={"action": "confirm", "selected_ids": [str(req_b.id)]}), config
            )
            assert result_2.get("__interrupt__"), "expected layer-3 to now pause at requirement_review"
            payload_2 = result_2["__interrupt__"][0].value
            assert payload_2["source_node"] == "diagram"
            print(f"RUN 2: resumed with selection={req_b.id}, layer-3 now paused at review. "
                  f"draft={payload_2['draft'][:30]}...")

            result_3 = await middle_graph.ainvoke(Command(resume={"action": "approve"}), config)
            light_ref = result_3.get("processing_result")
            print(f"RUN 3: completed. processing_result={light_ref}")
            assert light_ref["artifact_type"] == "diagram"

    async with async_session_factory() as db:
        diagrams_b = await DiagramRepo.get_by_requirement(db, requirement_id=req_b.id, session_id=session.id)
        diagrams_a = await DiagramRepo.get_by_requirement(db, requirement_id=req_a.id, session_id=session.id)
        assert len(diagrams_b) == 1 and diagrams_b[0].status == VersionStatus.active
        assert len(diagrams_a) == 0, "the diagram must be linked to the SELECTED requirement, not the other one"
        print(f"assert OK: diagram persisted against the CHOSEN requirement ({req_b.id}), "
              f"none created for the other ({req_a.id})")

    await cleanup_user(user)
    print("Scenario PASSED: ambiguous -> select_requirement -> resume selection")


# ---------------------------------------------------------------------------
# Scenario 3: cancel at user_confirm_inputs -> END, nothing processed.
# ---------------------------------------------------------------------------
async def test_cancel_path():
    print("\n--- Scenario: cancel at user_confirm_inputs -> END, nothing processed ---")
    user, session = await setup_user_project_session()

    async with async_session_factory() as db:
        req_a = await RequirementRepo.create(db, session_id=session.id, content="The system shall stop within 50 meters.")
        req_a = await RequirementRepo.promote(db, id=req_a.id, session_id=session.id)
        req_b = await RequirementRepo.create(db, session_id=session.id, content="The system shall log all sensor faults.")
        req_b = await RequirementRepo.promote(db, id=req_b.id, session_id=session.id)
        await db.commit()

    middle_llm = FakeStructuredWrapperLLM(
        MiddleDecision(has_request=True, resolved_intent=Intent.generate_diagram, diagram_type=DiagramType.use_case)
    )
    confirm_question_llm = FakeSequenceLLM(["Which requirement should this diagram be for?"])

    def fake_middle_get_llm(node_name=None):
        if node_name == "sysml_middle_supervisor":
            return middle_llm
        if node_name == "sysml_confirm_question":
            return confirm_question_llm
        raise AssertionError(f"unexpected node_name in middle_nodes: {node_name}")

    def fake_layer3_get_llm(node_name=None):
        raise AssertionError(f"layer-3 must NEVER run on the cancel path (node_name={node_name})")

    outer_thread_id = f"outer-{uuid.uuid4()}"
    settings = get_settings()

    async with AsyncPostgresSaver.from_conn_string(settings.checkpointer_database_url) as checkpointer:
        await checkpointer.setup()
        with patch("agents.sysml.middle_nodes.get_llm", side_effect=fake_middle_get_llm), \
             patch("agents.sysml.nodes.get_llm", side_effect=fake_layer3_get_llm):

            middle_graph = build_middle_graph(checkpointer=checkpointer)
            config = build_middle_config(outer_thread_id)

            result_1 = await middle_graph.ainvoke(
                {"user_input": "give me a use case diagram", "session_id": session.id}, config
            )
            assert result_1.get("__interrupt__")
            payload = result_1["__interrupt__"][0].value
            assert payload["pattern"] == "select_requirements_for_diagram"
            print(f"RUN 1: paused at user_confirm_inputs with {len(payload['options'])} options")

            result_2 = await middle_graph.ainvoke(Command(resume={"action": "cancel"}), config)
            assert not result_2.get("__interrupt__"), "cancel must end the run, not pause again"
            assert result_2.get("confirm_decision") == "cancelled"
            assert result_2.get("result") == "cancelled"
            print(f"RUN 2: resumed with cancel. confirm_decision={result_2.get('confirm_decision')!r} "
                  f"result={result_2.get('result')!r}")

    async with async_session_factory() as db:
        diagrams_a = await DiagramRepo.get_by_requirement(db, requirement_id=req_a.id, session_id=session.id)
        diagrams_b = await DiagramRepo.get_by_requirement(db, requirement_id=req_b.id, session_id=session.id)
        assert diagrams_a == [] and diagrams_b == [], "nothing must be persisted on cancel"
        print("assert OK: no diagram persisted for either requirement after cancel")

    await cleanup_user(user)
    print("Scenario PASSED: cancel path")


async def main() -> None:
    await test_unambiguous_direct_route()
    await clear_checkpoints()
    await test_ambiguous_select_requirement()
    await clear_checkpoints()
    await test_cancel_path()
    await clear_checkpoints()
    print("\n=== T5B TEST SUITE PASSED ===")


if __name__ == "__main__":
    asyncio.run(main())
=======
"""Standalone tests for T5b: conditional user_confirm_inputs in the SysML middle layer,
under a REAL Postgres checkpointer (same as T5a).

LLM call sites are stubbed (same rationale as T4/T5a: local Ollama models don't reliably
support LangChain structured-output tool-calling in this environment). Everything else —
graph wiring, ambiguity routing (router-as-code, reads Postgres), interrupt/resume, and
persistence via the T2 repository — runs for real.

Run: python -m scripts.smoke_test_t5b
"""
import asyncio
import sys
import uuid
from unittest.mock import patch

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
# NOTE: this policy is required for AsyncPostgresSaver/psycopg on Windows, but is
# incompatible with asyncio subprocesses (needed by Layer 3's real SysML v2 tooling).
# This test's job is ambiguity routing/confirm patterns, not the tool integration
# (covered for real by scripts/smoke_test_layer3_rebuild.py), so verify_node's tool
# call is stubbed below rather than fighting that Windows-only conflict.

from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver  # noqa: E402
from langgraph.types import Command  # noqa: E402
from sqlalchemy import text  # noqa: E402

from agents.sysml.middle_graph import build_middle_config, build_middle_graph  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.schemas.sysml import Intent, IntentDecision, MiddleDecision  # noqa: E402
from data.db import async_session_factory  # noqa: E402
from data.models import DiagramType, VersionStatus  # noqa: E402
from data.repository import DiagramRepo, ProjectRepo, RequirementRepo, SessionRepo, UserRepo  # noqa: E402

VALID_DIAGRAM_MODEL = (
    "package BrakeStates {\n"
    "    part def BrakingController {\n"
    "        state def Idle;\n"
    "        state def Braking;\n"
    "    }\n"
    "    part controller : BrakingController;\n"
    "}\n"
)


class FakeMessage:
    def __init__(self, content):
        self.content = content


class FakeStructuredLLM:
    def __init__(self, decisions):
        self._decisions = decisions if isinstance(decisions, list) else [decisions]
        self.calls = 0

    async def ainvoke(self, prompt):
        decision = self._decisions[min(self.calls, len(self._decisions) - 1)]
        self.calls += 1
        return decision


class FakeStructuredWrapperLLM:
    def __init__(self, decisions):
        self._structured = FakeStructuredLLM(decisions)

    def with_structured_output(self, schema):
        return self._structured


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
        user = await UserRepo.create(db, email=f"t5b-{uuid.uuid4()}@test.dev", password_hash="hashed")
        project = await ProjectRepo.create(db, user_id=user.id, name="T5b Project")
        session = await SessionRepo.create(
            db, project_id=project.id, thread_id=str(uuid.uuid4()), title="T5b Session"
        )
        await db.commit()
        return user, session


async def cleanup_user(user):
    async with async_session_factory() as db:
        db_user = await UserRepo.get_by_id(db, user.id)
        await db.delete(db_user)
        await db.commit()


async def clear_checkpoints():
    async with async_session_factory() as db:
        await db.execute(text("DELETE FROM checkpoint_writes"))
        await db.execute(text("DELETE FROM checkpoint_blobs"))
        await db.execute(text("DELETE FROM checkpoints"))
        await db.commit()


# ---------------------------------------------------------------------------
# Scenario 1: unambiguous (exactly one active requirement) -> direct to processing,
# user_confirm_inputs SKIPPED.
# ---------------------------------------------------------------------------
async def test_unambiguous_direct_route():
    print("\n--- Scenario: unambiguous (1 active requirement) -> direct route, confirm SKIPPED ---")
    user, session = await setup_user_project_session()

    async with async_session_factory() as db:
        req = await RequirementRepo.create(db, session_id=session.id, content="The system shall stop within 50 meters.")
        req = await RequirementRepo.promote(db, id=req.id, session_id=session.id)
        await db.commit()
        requirement_id = req.id

    middle_llm = FakeStructuredWrapperLLM(
        MiddleDecision(has_request=True, resolved_intent=Intent.generate_diagram, diagram_type=DiagramType.state_machine)
    )
    layer3_supervisor_llm = FakeStructuredWrapperLLM(
        IntentDecision(intent=Intent.generate_diagram, diagram_type=DiagramType.state_machine)
    )
    plan_llm = FakeSequenceLLM(["State machine with Idle and Braking states."])
    diagram_llm = FakeSequenceLLM([VALID_DIAGRAM_MODEL])

    def fake_middle_get_llm(node_name=None):
        if node_name == "sysml_middle_supervisor":
            return middle_llm
        if node_name == "sysml_confirm_question":
            raise AssertionError("user_confirm_inputs must NOT run on the unambiguous path")
        raise AssertionError(f"unexpected node_name in middle_nodes: {node_name}")

    def fake_layer3_get_llm(node_name=None):
        if node_name == "sysml_supervisor":
            return layer3_supervisor_llm
        if node_name == "sysml_plan":
            return plan_llm
        if node_name == "sysml_generate":
            return diagram_llm
        raise AssertionError(f"unexpected node_name in layer-3 nodes: {node_name}")

    outer_thread_id = f"outer-{uuid.uuid4()}"
    settings = get_settings()

    async with AsyncPostgresSaver.from_conn_string(settings.checkpointer_database_url) as checkpointer:
        await checkpointer.setup()
        with patch("agents.sysml.middle_nodes.get_llm", side_effect=fake_middle_get_llm), \
             patch("agents.sysml.nodes.get_llm", side_effect=fake_layer3_get_llm), \
             patch("agents.sysml.nodes.validate", return_value=[]):

            middle_graph = build_middle_graph(checkpointer=checkpointer)
            config = build_middle_config(outer_thread_id)

            result_1 = await middle_graph.ainvoke(
                {"user_input": "give me a state machine diagram", "session_id": session.id}, config
            )
            assert result_1.get("__interrupt__"), "expected layer-3 to pause at requirement_review"
            payload = result_1["__interrupt__"][0].value
            assert payload["source_node"] == "diagram"
            print(f"RUN 1: paused DIRECTLY at layer-3 review (no confirm step). draft={payload['draft'][:30]}...")

            result_2 = await middle_graph.ainvoke(Command(resume={"action": "approve"}), config)
            light_ref = result_2.get("processing_result")
            print(f"RUN 2: completed. processing_result={light_ref}")

    async with async_session_factory() as db:
        diagrams = await DiagramRepo.get_by_requirement(db, requirement_id=requirement_id, session_id=session.id)
        assert len(diagrams) == 1
        assert diagrams[0].status == VersionStatus.active
        assert diagrams[0].requirement_id == requirement_id
        print(f"assert OK: diagram persisted and linked to the sole active requirement {requirement_id}")

    await cleanup_user(user)
    print("Scenario PASSED: unambiguous direct route (confirm skipped)")


# ---------------------------------------------------------------------------
# Scenario 2: ambiguous (2 active requirements, none named) -> pauses with the
# select_requirements_for_diagram MULTI-SELECT pattern (Step 4) -> resume selecting
# ONE of the two -> processing targets the chosen one.
# ---------------------------------------------------------------------------
async def test_ambiguous_select_requirement():
    print("\n--- Scenario: ambiguous (2 active requirements) -> select_requirements_for_diagram -> resume selection ---")
    user, session = await setup_user_project_session()

    async with async_session_factory() as db:
        req_a = await RequirementRepo.create(db, session_id=session.id, content="The system shall stop within 50 meters.")
        req_a = await RequirementRepo.promote(db, id=req_a.id, session_id=session.id)
        req_b = await RequirementRepo.create(db, session_id=session.id, content="The system shall log all sensor faults.")
        req_b = await RequirementRepo.promote(db, id=req_b.id, session_id=session.id)
        await db.commit()

    # named_requirement_id left unset -> genuinely ambiguous with 2 active requirements
    middle_llm = FakeStructuredWrapperLLM(
        MiddleDecision(has_request=True, resolved_intent=Intent.generate_diagram, diagram_type=DiagramType.state_machine)
    )
    confirm_question_llm = FakeSequenceLLM(["Which requirement should this diagram be for?"])
    layer3_supervisor_llm = FakeStructuredWrapperLLM(
        IntentDecision(intent=Intent.generate_diagram, diagram_type=DiagramType.state_machine)
    )
    plan_llm = FakeSequenceLLM(["State machine with Idle and Braking states."])
    diagram_llm = FakeSequenceLLM([VALID_DIAGRAM_MODEL])

    def fake_middle_get_llm(node_name=None):
        if node_name == "sysml_middle_supervisor":
            return middle_llm
        if node_name == "sysml_confirm_question":
            return confirm_question_llm
        raise AssertionError(f"unexpected node_name in middle_nodes: {node_name}")

    def fake_layer3_get_llm(node_name=None):
        if node_name == "sysml_supervisor":
            return layer3_supervisor_llm
        if node_name == "sysml_plan":
            return plan_llm
        if node_name == "sysml_generate":
            return diagram_llm
        raise AssertionError(f"unexpected node_name in layer-3 nodes: {node_name}")

    outer_thread_id = f"outer-{uuid.uuid4()}"
    settings = get_settings()

    async with AsyncPostgresSaver.from_conn_string(settings.checkpointer_database_url) as checkpointer:
        await checkpointer.setup()
        with patch("agents.sysml.middle_nodes.get_llm", side_effect=fake_middle_get_llm), \
             patch("agents.sysml.nodes.get_llm", side_effect=fake_layer3_get_llm), \
             patch("agents.sysml.nodes.validate", return_value=[]):

            middle_graph = build_middle_graph(checkpointer=checkpointer)
            config = build_middle_config(outer_thread_id)

            result_1 = await middle_graph.ainvoke(
                {"user_input": "give me a state machine diagram", "session_id": session.id}, config
            )
            assert result_1.get("__interrupt__"), "expected pause at user_confirm_inputs"
            payload = result_1["__interrupt__"][0].value
            assert payload["pattern"] == "select_requirements_for_diagram"
            assert payload["multi_select"] is True and payload["min_selected"] == 1 and payload["allow_all"] is True
            option_ids = {o["id"] for o in payload["options"]}
            assert option_ids == {str(req_a.id), str(req_b.id)}, "options must list BOTH active requirements"
            print(f"RUN 1: paused at user_confirm_inputs. pattern={payload['pattern']!r} "
                  f"question={payload['question']!r} options={payload['options']}")

            result_2 = await middle_graph.ainvoke(
                Command(resume={"action": "confirm", "selected_ids": [str(req_b.id)]}), config
            )
            assert result_2.get("__interrupt__"), "expected layer-3 to now pause at requirement_review"
            payload_2 = result_2["__interrupt__"][0].value
            assert payload_2["source_node"] == "diagram"
            print(f"RUN 2: resumed with selection={req_b.id}, layer-3 now paused at review. "
                  f"draft={payload_2['draft'][:30]}...")

            result_3 = await middle_graph.ainvoke(Command(resume={"action": "approve"}), config)
            light_ref = result_3.get("processing_result")
            print(f"RUN 3: completed. processing_result={light_ref}")
            assert light_ref["artifact_type"] == "diagram"

    async with async_session_factory() as db:
        diagrams_b = await DiagramRepo.get_by_requirement(db, requirement_id=req_b.id, session_id=session.id)
        diagrams_a = await DiagramRepo.get_by_requirement(db, requirement_id=req_a.id, session_id=session.id)
        assert len(diagrams_b) == 1 and diagrams_b[0].status == VersionStatus.active
        assert len(diagrams_a) == 0, "the diagram must be linked to the SELECTED requirement, not the other one"
        print(f"assert OK: diagram persisted against the CHOSEN requirement ({req_b.id}), "
              f"none created for the other ({req_a.id})")

    await cleanup_user(user)
    print("Scenario PASSED: ambiguous -> select_requirement -> resume selection")


# ---------------------------------------------------------------------------
# Scenario 3: cancel at user_confirm_inputs -> END, nothing processed.
# ---------------------------------------------------------------------------
async def test_cancel_path():
    print("\n--- Scenario: cancel at user_confirm_inputs -> END, nothing processed ---")
    user, session = await setup_user_project_session()

    async with async_session_factory() as db:
        req_a = await RequirementRepo.create(db, session_id=session.id, content="The system shall stop within 50 meters.")
        req_a = await RequirementRepo.promote(db, id=req_a.id, session_id=session.id)
        req_b = await RequirementRepo.create(db, session_id=session.id, content="The system shall log all sensor faults.")
        req_b = await RequirementRepo.promote(db, id=req_b.id, session_id=session.id)
        await db.commit()

    middle_llm = FakeStructuredWrapperLLM(
        MiddleDecision(has_request=True, resolved_intent=Intent.generate_diagram, diagram_type=DiagramType.use_case)
    )
    confirm_question_llm = FakeSequenceLLM(["Which requirement should this diagram be for?"])

    def fake_middle_get_llm(node_name=None):
        if node_name == "sysml_middle_supervisor":
            return middle_llm
        if node_name == "sysml_confirm_question":
            return confirm_question_llm
        raise AssertionError(f"unexpected node_name in middle_nodes: {node_name}")

    def fake_layer3_get_llm(node_name=None):
        raise AssertionError(f"layer-3 must NEVER run on the cancel path (node_name={node_name})")

    outer_thread_id = f"outer-{uuid.uuid4()}"
    settings = get_settings()

    async with AsyncPostgresSaver.from_conn_string(settings.checkpointer_database_url) as checkpointer:
        await checkpointer.setup()
        with patch("agents.sysml.middle_nodes.get_llm", side_effect=fake_middle_get_llm), \
             patch("agents.sysml.nodes.get_llm", side_effect=fake_layer3_get_llm):

            middle_graph = build_middle_graph(checkpointer=checkpointer)
            config = build_middle_config(outer_thread_id)

            result_1 = await middle_graph.ainvoke(
                {"user_input": "give me a use case diagram", "session_id": session.id}, config
            )
            assert result_1.get("__interrupt__")
            payload = result_1["__interrupt__"][0].value
            assert payload["pattern"] == "select_requirements_for_diagram"
            print(f"RUN 1: paused at user_confirm_inputs with {len(payload['options'])} options")

            result_2 = await middle_graph.ainvoke(Command(resume={"action": "cancel"}), config)
            assert not result_2.get("__interrupt__"), "cancel must end the run, not pause again"
            assert result_2.get("confirm_decision") == "cancelled"
            assert result_2.get("result") == "cancelled"
            print(f"RUN 2: resumed with cancel. confirm_decision={result_2.get('confirm_decision')!r} "
                  f"result={result_2.get('result')!r}")

    async with async_session_factory() as db:
        diagrams_a = await DiagramRepo.get_by_requirement(db, requirement_id=req_a.id, session_id=session.id)
        diagrams_b = await DiagramRepo.get_by_requirement(db, requirement_id=req_b.id, session_id=session.id)
        assert diagrams_a == [] and diagrams_b == [], "nothing must be persisted on cancel"
        print("assert OK: no diagram persisted for either requirement after cancel")

    await cleanup_user(user)
    print("Scenario PASSED: cancel path")


async def main() -> None:
    await test_unambiguous_direct_route()
    await clear_checkpoints()
    await test_ambiguous_select_requirement()
    await clear_checkpoints()
    await test_cancel_path()
    await clear_checkpoints()
    print("\n=== T5B TEST SUITE PASSED ===")


if __name__ == "__main__":
    asyncio.run(main())
>>>>>>> Stashed changes
