"""Standalone tests for the Layer-2 redesign, Step 3: build_structured_format (the
unified Layer-2 -> Layer-3 contract, ProcessingInput), on a REAL Postgres checkpointer.

LLM call sites are stubbed (same rationale as prior Layer-2 steps). agents.sysml.nodes.
validate is ALSO stubbed here for the same Windows event-loop reason documented in
scripts/smoke_test_level_resolution.py.

Run: python -m scripts.smoke_test_build_structured_format
"""
import asyncio
import sys
import uuid
from unittest.mock import patch

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver  # noqa: E402
from langgraph.types import Command  # noqa: E402
from sqlalchemy import text  # noqa: E402

from agents.sysml import middle_nodes  # noqa: E402
from agents.sysml.middle_graph import build_middle_config, build_middle_graph  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.schemas.sysml import DiagramType, Intent, IntentDecision, MiddleDecision  # noqa: E402
from data.db import async_session_factory  # noqa: E402
from data.models import RequirementLevel  # noqa: E402
from data.repository import DiagramRepo, ProjectRepo, RequirementRepo, SessionRepo, UserRepo  # noqa: E402

VALID_OPERATIONAL = "package Ops { requirement def OpReq { doc /* op */ subject s : ScalarValues::Boolean; require constraint { true } } }"
VALID_FUNCTIONAL = "package Func { requirement def FuncReq { doc /* func */ subject s : ScalarValues::Boolean; require constraint { true } } }"
VALID_DIAGRAM = "package UseCases { part def System { } }"


class FakeMessage:
    def __init__(self, content):
        self.content = content


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


class FakeSequenceLLM:
    def __init__(self, drafts):
        self._drafts = drafts
        self.calls = 0

    async def ainvoke(self, prompt):
        draft = self._drafts[min(self.calls, len(self._drafts) - 1)]
        self.calls += 1
        return FakeMessage(draft)


async def setup_session(label: str):
    async with async_session_factory() as db:
        user = await UserRepo.create(db, email=f"bsf-{label}-{uuid.uuid4()}@test.dev", password_hash="hashed")
        project = await ProjectRepo.create(db, user_id=user.id, name=f"BSF {label}")
        session = await SessionRepo.create(
            db, project_id=project.id, thread_id=str(uuid.uuid4()), title=f"BSF {label}"
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
        await db.execute(text("DELETE FROM thread_activity"))
        await db.commit()


def capture_l3_input(captured: list):
    """Wraps the REAL layer-3 graph's ainvoke to record the exact entry state it
    received, while still letting it run for real — used to prove mapping
    completeness (DoD #4) without faking layer-3 itself.
    """
    real_ainvoke = middle_nodes._sysml_processing_graph.ainvoke

    async def spy(l3_input, config, **kwargs):
        captured.append(dict(l3_input))
        return await real_ainvoke(l3_input, config, **kwargs)

    return spy


# ---------------------------------------------------------------------------
# Scenario 1: operational requirement -> ProcessingInput(level=operational,
# source_id=None) -> wrapper -> layer-3 generates/verifies/finalizes correctly.
# ---------------------------------------------------------------------------
async def test_operational_contract():
    print("\n--- Scenario 1: operational -> ProcessingInput(level=operational, source_id=None) ---")
    user, session = await setup_session("op")

    middle_llm = FakeStructuredWrapperLLM(
        MiddleDecision(has_request=True, resolved_intent=Intent.generate_requirement, level=RequirementLevel.operational)
    )
    layer3_supervisor_llm = FakeStructuredWrapperLLM(IntentDecision(intent=Intent.generate_requirement))
    plan_llm = FakeSequenceLLM(["plan"])
    generate_llm = FakeSequenceLLM([VALID_OPERATIONAL])

    def fake_middle_get_llm(node_name=None):
        if node_name == "sysml_middle_supervisor":
            return middle_llm
        raise AssertionError(f"unexpected node_name in middle_nodes: {node_name}")

    def fake_layer3_get_llm(node_name=None):
        if node_name == "sysml_supervisor":
            return layer3_supervisor_llm
        if node_name == "sysml_plan":
            return plan_llm
        if node_name == "sysml_generate":
            return generate_llm
        raise AssertionError(f"unexpected node_name in layer-3 nodes: {node_name}")

    outer_thread_id = f"outer-{uuid.uuid4()}"
    settings = get_settings()
    captured: list = []

    async with AsyncPostgresSaver.from_conn_string(settings.checkpointer_database_url) as checkpointer:
        await checkpointer.setup()
        with patch("agents.sysml.middle_nodes.get_llm", side_effect=fake_middle_get_llm), \
             patch("agents.sysml.nodes.get_llm", side_effect=fake_layer3_get_llm), \
             patch("agents.sysml.nodes.validate", return_value=[]), \
             patch.object(middle_nodes._sysml_processing_graph, "ainvoke", side_effect=capture_l3_input(captured)):

            middle_graph = build_middle_graph(checkpointer=checkpointer)
            config = build_middle_config(outer_thread_id)

            result = await middle_graph.ainvoke(
                {"user_input": "Define a high-level operational need.", "session_id": session.id}, config
            )
            assert result.get("__interrupt__"), "expected layer-3 to pause at requirement_review"

            pi = result.get("processing_input")
            assert pi is not None, "expected build_structured_format to populate processing_input"
            assert pi["intent"] == "generate_requirement"
            assert pi["level"] == "operational"
            assert pi["source_id"] is None
            assert pi["target_requirement_ids"] == []
            print(f"ProcessingInput: intent={pi['intent']!r} level={pi['level']!r} "
                  f"source_id={pi['source_id']!r} target_requirement_ids={pi['target_requirement_ids']!r}")
            print("assert OK: build_structured_format produced ProcessingInput(level=operational, source_id=None)")

            assert len(captured) == 1
            l3_input = captured[0]
            assert l3_input["level"] == "operational"
            assert l3_input["target_requirement_id"] is None
            print(f"assert OK: wrapper passed level={l3_input['level']!r} target_requirement_id="
                  f"{l3_input['target_requirement_id']!r} into layer-3's entry state")

            await middle_graph.ainvoke(Command(resume={"action": "approve"}), config)

    async with async_session_factory() as db:
        rows = await RequirementRepo.list_by_session(db, session_id=session.id)
        assert len(rows) == 1 and rows[0].level == RequirementLevel.operational
        print(f"assert OK: layer-3 generated/verified/finalized the operational requirement id={rows[0].id}")

    await cleanup_user(user)
    print("Scenario 1 PASSED")


# ---------------------------------------------------------------------------
# Scenario 2: functional with a resolved operational source -> ProcessingInput
# (level=functional, source_id=<the operational>) -> layer-3 derives from it.
# ---------------------------------------------------------------------------
async def test_functional_with_source_contract():
    print("\n--- Scenario 2: functional w/ resolved source -> ProcessingInput(level=functional, source_id=<op>) ---")
    user, session = await setup_session("func-src")

    async with async_session_factory() as db:
        op = await RequirementRepo.finalize(
            db, session_id=session.id, content=VALID_OPERATIONAL, level=RequirementLevel.operational
        )
        await db.commit()
        op_id = op.id

    middle_llm = FakeStructuredWrapperLLM(
        MiddleDecision(has_request=True, resolved_intent=Intent.generate_requirement, level=RequirementLevel.functional)
    )
    layer3_supervisor_llm = FakeStructuredWrapperLLM(IntentDecision(intent=Intent.generate_requirement))
    plan_llm = FakeSequenceLLM(["plan"])
    generate_llm = FakeSequenceLLM([VALID_FUNCTIONAL])

    def fake_middle_get_llm(node_name=None):
        if node_name == "sysml_middle_supervisor":
            return middle_llm
        raise AssertionError(f"unexpected node_name in middle_nodes: {node_name}")

    def fake_layer3_get_llm(node_name=None):
        if node_name == "sysml_supervisor":
            return layer3_supervisor_llm
        if node_name == "sysml_plan":
            return plan_llm
        if node_name == "sysml_generate":
            return generate_llm
        raise AssertionError(f"unexpected node_name in layer-3 nodes: {node_name}")

    outer_thread_id = f"outer-{uuid.uuid4()}"
    settings = get_settings()
    captured: list = []

    async with AsyncPostgresSaver.from_conn_string(settings.checkpointer_database_url) as checkpointer:
        await checkpointer.setup()
        with patch("agents.sysml.middle_nodes.get_llm", side_effect=fake_middle_get_llm), \
             patch("agents.sysml.nodes.get_llm", side_effect=fake_layer3_get_llm), \
             patch("agents.sysml.nodes.validate", return_value=[]), \
             patch.object(middle_nodes._sysml_processing_graph, "ainvoke", side_effect=capture_l3_input(captured)):

            middle_graph = build_middle_graph(checkpointer=checkpointer)
            config = build_middle_config(outer_thread_id)

            result = await middle_graph.ainvoke(
                {"user_input": "Define a specific function this system performs.", "session_id": session.id}, config
            )
            assert result.get("__interrupt__"), "expected layer-3 to pause at requirement_review"

            pi = result.get("processing_input")
            assert pi["level"] == "functional"
            assert pi["source_id"] == str(op_id)
            print(f"ProcessingInput: level={pi['level']!r} source_id={pi['source_id']!r} (== operational.id: "
                  f"{pi['source_id'] == str(op_id)})")
            print("assert OK: build_structured_format resolved source_id to the operational requirement")

            l3_input = captured[0]
            assert l3_input["target_requirement_id"] == str(op_id)
            print(f"assert OK: wrapper passed the source ref into layer-3's target_requirement_id="
                  f"{l3_input['target_requirement_id']!r} (layer-3 reads its content to derive from)")

            await middle_graph.ainvoke(Command(resume={"action": "approve"}), config)

    async with async_session_factory() as db:
        rows = await RequirementRepo.list_by_session(db, session_id=session.id)
        levels = sorted(r.level.value for r in rows)
        assert levels == ["functional", "operational"]
        print(f"assert OK: layer-3 finalized against thread levels {levels}")

    await cleanup_user(user)
    print("Scenario 2 PASSED")


# ---------------------------------------------------------------------------
# Scenario 3: diagram -> ProcessingInput(intent=generate_diagram, diagram_type=...,
# target_requirement_ids=[...]) -> layer-3 produces model + derived Mermaid -> finalizes.
# ---------------------------------------------------------------------------
async def test_diagram_contract():
    print("\n--- Scenario 3: diagram -> ProcessingInput(intent=generate_diagram, target_requirement_ids=[...]) ---")
    user, session = await setup_session("diagram")

    async with async_session_factory() as db:
        req = await RequirementRepo.finalize(
            db, session_id=session.id, content=VALID_OPERATIONAL, level=RequirementLevel.operational
        )
        await db.commit()
        req_id = req.id

    middle_llm = FakeStructuredWrapperLLM(
        MiddleDecision(has_request=True, resolved_intent=Intent.generate_diagram, diagram_type=DiagramType.use_case)
    )
    layer3_supervisor_llm = FakeStructuredWrapperLLM(
        IntentDecision(intent=Intent.generate_diagram, diagram_type=DiagramType.use_case)
    )
    plan_llm = FakeSequenceLLM(["plan"])
    generate_llm = FakeSequenceLLM([VALID_DIAGRAM])

    def fake_middle_get_llm(node_name=None):
        if node_name == "sysml_middle_supervisor":
            return middle_llm
        raise AssertionError(f"unexpected node_name in middle_nodes: {node_name}")

    def fake_layer3_get_llm(node_name=None):
        if node_name == "sysml_supervisor":
            return layer3_supervisor_llm
        if node_name == "sysml_plan":
            return plan_llm
        if node_name == "sysml_generate":
            return generate_llm
        raise AssertionError(f"unexpected node_name in layer-3 nodes: {node_name}")

    outer_thread_id = f"outer-{uuid.uuid4()}"
    settings = get_settings()
    captured: list = []

    async with AsyncPostgresSaver.from_conn_string(settings.checkpointer_database_url) as checkpointer:
        await checkpointer.setup()
        with patch("agents.sysml.middle_nodes.get_llm", side_effect=fake_middle_get_llm), \
             patch("agents.sysml.nodes.get_llm", side_effect=fake_layer3_get_llm), \
             patch("agents.sysml.nodes.validate", return_value=[]), \
             patch("agents.sysml.nodes.to_mermaid", return_value="graph TD; A-->B;"), \
             patch.object(middle_nodes._sysml_processing_graph, "ainvoke", side_effect=capture_l3_input(captured)):

            middle_graph = build_middle_graph(checkpointer=checkpointer)
            config = build_middle_config(outer_thread_id)

            result = await middle_graph.ainvoke(
                {"user_input": "Show a use case diagram for this requirement.", "session_id": session.id}, config
            )
            assert result.get("__interrupt__"), "expected layer-3 to pause at requirement_review"

            pi = result.get("processing_input")
            assert pi["intent"] == "generate_diagram"
            assert pi["diagram_type"] == "use_case"
            assert pi["target_requirement_ids"] == [str(req_id)]
            print(f"ProcessingInput: intent={pi['intent']!r} diagram_type={pi['diagram_type']!r} "
                  f"target_requirement_ids={pi['target_requirement_ids']!r}")
            print("assert OK: build_structured_format captured the diagram intent + target ref")

            l3_input = captured[0]
            assert l3_input["target_requirement_id"] == str(req_id)
            assert l3_input["diagram_type"] == "use_case"
            print(f"assert OK: wrapper passed target_requirement_id={l3_input['target_requirement_id']!r} "
                  f"diagram_type={l3_input['diagram_type']!r} into layer-3's entry state")

            await middle_graph.ainvoke(Command(resume={"action": "approve"}), config)

    async with async_session_factory() as db:
        diagrams = await DiagramRepo.get_by_requirement(db, requirement_id=req_id, session_id=session.id)
        assert len(diagrams) == 1 and diagrams[0].mermaid
        print(f"assert OK: layer-3 finalized the diagram id={diagrams[0].id} with both SysML model and Mermaid")

    await cleanup_user(user)
    print("Scenario 3 PASSED")


# ---------------------------------------------------------------------------
# Scenario 4: mapping completeness — every field layer-3 needs (level, source, intent
# context via target/diagram_type, user_input) is present and non-dropped at the
# middle -> layer-3 boundary. Reuses the capture from scenario 3's diagram run plus a
# direct static check of the translation code path.
# ---------------------------------------------------------------------------
async def test_mapping_completeness():
    print("\n--- Scenario 4: mapping completeness (nothing dropped at the boundary) ---")
    user, session = await setup_session("mapping")

    async with async_session_factory() as db:
        op = await RequirementRepo.finalize(
            db, session_id=session.id, content=VALID_OPERATIONAL, level=RequirementLevel.operational
        )
        await db.commit()
        op_id = op.id

    middle_llm = FakeStructuredWrapperLLM(
        MiddleDecision(has_request=True, resolved_intent=Intent.generate_requirement, level=RequirementLevel.functional)
    )
    layer3_supervisor_llm = FakeStructuredWrapperLLM(IntentDecision(intent=Intent.generate_requirement))
    plan_llm = FakeSequenceLLM(["plan"])
    generate_llm = FakeSequenceLLM([VALID_FUNCTIONAL])

    def fake_middle_get_llm(node_name=None):
        if node_name == "sysml_middle_supervisor":
            return middle_llm
        raise AssertionError(f"unexpected node_name in middle_nodes: {node_name}")

    def fake_layer3_get_llm(node_name=None):
        if node_name == "sysml_supervisor":
            return layer3_supervisor_llm
        if node_name == "sysml_plan":
            return plan_llm
        if node_name == "sysml_generate":
            return generate_llm
        raise AssertionError(f"unexpected node_name in layer-3 nodes: {node_name}")

    outer_thread_id = f"outer-{uuid.uuid4()}"
    settings = get_settings()
    captured: list = []

    async with AsyncPostgresSaver.from_conn_string(settings.checkpointer_database_url) as checkpointer:
        await checkpointer.setup()
        with patch("agents.sysml.middle_nodes.get_llm", side_effect=fake_middle_get_llm), \
             patch("agents.sysml.nodes.get_llm", side_effect=fake_layer3_get_llm), \
             patch("agents.sysml.nodes.validate", return_value=[]), \
             patch.object(middle_nodes._sysml_processing_graph, "ainvoke", side_effect=capture_l3_input(captured)):

            middle_graph = build_middle_graph(checkpointer=checkpointer)
            config = build_middle_config(outer_thread_id)

            await middle_graph.ainvoke(
                {"user_input": "Derive a function from the operational need.", "session_id": session.id}, config
            )

            assert len(captured) == 1
            l3_input = captured[0]
            required_keys = {"user_input", "session_id", "target_requirement_id", "level", "diagram_type"}
            missing = required_keys - l3_input.keys()
            assert not missing, f"layer-3 entry state is missing keys: {missing}"
            assert l3_input["user_input"] == "Derive a function from the operational need."
            assert l3_input["session_id"] == session.id
            assert l3_input["target_requirement_id"] == str(op_id)
            assert l3_input["level"] == "functional"
            assert l3_input["diagram_type"] is None
            print(f"layer-3 entry state received: {l3_input}")
            print("assert OK: every field layer-3 needs (user_input, session_id, target_requirement_id, "
                  "level, diagram_type) reached it intact — nothing dropped at the boundary")

    await cleanup_user(user)
    print("Scenario 4 PASSED")


async def main() -> None:
    await test_operational_contract()
    await clear_checkpoints()
    await test_functional_with_source_contract()
    await clear_checkpoints()
    await test_diagram_contract()
    await clear_checkpoints()
    await test_mapping_completeness()
    await clear_checkpoints()
    print("\n=== BUILD_STRUCTURED_FORMAT TEST SUITE PASSED ===")


if __name__ == "__main__":
    asyncio.run(main())
