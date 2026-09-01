"""Standalone tests for the Layer-2 redesign, Step 4: wiring user_confirm_inputs with
the Step-1/2/3 nodes (validate_inputs, resolve_level, build_structured_format) and the
new select_requirements_for_diagram multi-select pattern, on a REAL Postgres
checkpointer.

LLM call sites are stubbed (same rationale as prior Layer-2 steps). agents.sysml.nodes.
validate is ALSO stubbed here for the same Windows event-loop reason documented in
scripts/smoke_test_level_resolution.py.

Run: python -m scripts.smoke_test_confirm_wiring
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

from agents.sysml.middle_graph import build_middle_config, build_middle_graph  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.schemas.sysml import DiagramType, Intent, IntentDecision, MiddleDecision  # noqa: E402
from data.db import async_session_factory  # noqa: E402
from data.models import RequirementLevel  # noqa: E402
from data.repository import DiagramRepo, ProjectRepo, RequirementRepo, SessionRepo, UserRepo  # noqa: E402

VALID_OPERATIONAL = "package Ops { requirement def OpReq { doc /* op */ subject s : ScalarValues::Boolean; require constraint { true } } }"
VALID_DIAGRAM = "package UseCases { part def System { } }"


class FakeMessage:
    def __init__(self, content):
        self.content = content


class FakeStructuredLLM:
    def __init__(self, decision):
        self.decision = decision

    async def ainvoke(self, prompt):
        return self.decision

    async def astream(self, prompt):
        yield await self.ainvoke(prompt)

    def with_config(self, **kwargs):
        return self


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
        user = await UserRepo.create(db, email=f"cw-{label}-{uuid.uuid4()}@test.dev", password_hash="hashed")
        project = await ProjectRepo.create(db, user_id=user.id, name=f"ConfirmWiring {label}")
        session = await SessionRepo.create(
            db, project_id=project.id, thread_id=str(uuid.uuid4()), title=f"ConfirmWiring {label}"
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


def middle_llm_stubs(fake_middle_get_llm):
    return patch("agents.sysml.middle_nodes.get_llm", side_effect=fake_middle_get_llm)


# ---------------------------------------------------------------------------
# Scenario 1: clear request (operational, no ambiguity) -> straight through
# validate_inputs -> resolve_level -> build_structured_format -> sysml_processing,
# with NO confirm interrupt anywhere.
# ---------------------------------------------------------------------------
async def test_straight_through_no_confirm():
    print("\n--- Scenario 1: clear request -> straight through, NO confirm interrupt ---")
    user, session = await setup_session("straight")

    middle_llm = FakeStructuredWrapperLLM(
        MiddleDecision(has_request=True, resolved_intent=Intent.generate_requirement, level=RequirementLevel.operational)
    )
    layer3_supervisor_llm = FakeStructuredWrapperLLM(IntentDecision(intent=Intent.generate_requirement))
    plan_llm = FakeSequenceLLM(["plan"])
    generate_llm = FakeSequenceLLM([VALID_OPERATIONAL])

    def fake_middle_get_llm(node_name=None):
        if node_name == "sysml_middle_supervisor":
            return middle_llm
        if node_name == "sysml_confirm_question":
            raise AssertionError("user_confirm_inputs must NOT run on a clear request")
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

    async with AsyncPostgresSaver.from_conn_string(settings.checkpointer_database_url) as checkpointer:
        await checkpointer.setup()
        with middle_llm_stubs(fake_middle_get_llm), \
             patch("agents.sysml.nodes.get_llm", side_effect=fake_layer3_get_llm), \
             patch("agents.sysml.nodes.validate", return_value=[]):

            middle_graph = build_middle_graph(checkpointer=checkpointer)
            config = build_middle_config(outer_thread_id)

            result = await middle_graph.ainvoke(
                {"user_input": "Define a high-level operational need.", "session_id": session.id}, config
            )
            assert result.get("__interrupt__"), "expected layer-3's own review pause"
            payload = result["__interrupt__"][0].value
            assert payload["type"] == "requirement_review", "the ONLY interrupt must be layer-3's review, not a confirm"
            assert result.get("pending_pattern") is None
            assert result.get("processing_input") is not None
            print(f"assert OK: no confirm interrupt anywhere; reached layer-3 review directly "
                  f"(processing_input built: level={result['processing_input']['level']!r})")

            await middle_graph.ainvoke(Command(resume={"action": "approve"}), config)

    async with async_session_factory() as db:
        rows = await RequirementRepo.list_by_session(db, session_id=session.id)
        assert len(rows) == 1 and rows[0].level == RequirementLevel.operational
        print(f"assert OK: finalized operational requirement id={rows[0].id}")

    await cleanup_user(user)
    print("Scenario 1 PASSED")


# ---------------------------------------------------------------------------
# Scenario 2: diagram, no named target, MULTIPLE candidates -> select_requirements_
# for_diagram interrupt with ALL candidates as options; resume selecting two ->
# target_requirement_ids has exactly those two -> layer-3 produces the diagram.
# ---------------------------------------------------------------------------
async def test_multi_select_diagram_targets():
    print("\n--- Scenario 2: diagram, no named target, >1 candidates -> multi-select -> resume with two ---")
    user, session = await setup_session("multi")

    async with async_session_factory() as db:
        req_a = await RequirementRepo.finalize(db, session_id=session.id, content="req A content", level=RequirementLevel.operational)
        req_b = await RequirementRepo.finalize(db, session_id=session.id, content="req B content", level=RequirementLevel.operational)
        req_c = await RequirementRepo.finalize(db, session_id=session.id, content="req C content", level=RequirementLevel.operational)
        await db.commit()

    middle_llm = FakeStructuredWrapperLLM(
        MiddleDecision(has_request=True, resolved_intent=Intent.generate_diagram, diagram_type=DiagramType.use_case)
    )
    confirm_question_llm = FakeSequenceLLM(["Which requirements should this diagram represent?"])
    layer3_supervisor_llm = FakeStructuredWrapperLLM(
        IntentDecision(intent=Intent.generate_diagram, diagram_type=DiagramType.use_case)
    )
    plan_llm = FakeSequenceLLM(["plan"])
    generate_llm = FakeSequenceLLM([VALID_DIAGRAM])

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
            return generate_llm
        raise AssertionError(f"unexpected node_name in layer-3 nodes: {node_name}")

    outer_thread_id = f"outer-{uuid.uuid4()}"
    settings = get_settings()

    async with AsyncPostgresSaver.from_conn_string(settings.checkpointer_database_url) as checkpointer:
        await checkpointer.setup()
        with middle_llm_stubs(fake_middle_get_llm), \
             patch("agents.sysml.nodes.get_llm", side_effect=fake_layer3_get_llm), \
             patch("agents.sysml.nodes.validate", return_value=[]), \
             patch("agents.sysml.nodes.to_mermaid", return_value="graph TD; A-->B;"):

            middle_graph = build_middle_graph(checkpointer=checkpointer)
            config = build_middle_config(outer_thread_id)

            result_1 = await middle_graph.ainvoke(
                {"user_input": "Show a use case diagram.", "session_id": session.id}, config
            )
            assert result_1.get("__interrupt__"), "expected pause at user_confirm_inputs"
            payload = result_1["__interrupt__"][0].value
            assert payload["pattern"] == "select_requirements_for_diagram"
            assert payload["multi_select"] is True
            assert payload["min_selected"] == 1
            assert payload["allow_all"] is True
            option_ids = {o["id"] for o in payload["options"]}
            assert option_ids == {str(req_a.id), str(req_b.id), str(req_c.id)}, "ALL candidates must be offered"
            print(f"RUN 1: paused. pattern={payload['pattern']!r} options={len(payload['options'])} "
                  f"(== all 3 candidates: {option_ids == {str(req_a.id), str(req_b.id), str(req_c.id)}})")

            chosen = [str(req_a.id), str(req_c.id)]
            result_2 = await middle_graph.ainvoke(
                Command(resume={"action": "confirm", "selected_ids": chosen}), config
            )
            assert result_2.get("__interrupt__"), "expected layer-3 to now pause at review"
            pi = result_2.get("processing_input")
            assert sorted(pi["target_requirement_ids"]) == sorted(chosen), (
                f"expected target_requirement_ids == {chosen}, got {pi['target_requirement_ids']}"
            )
            print(f"RUN 2: resumed selecting {chosen}. processing_input.target_requirement_ids="
                  f"{pi['target_requirement_ids']} -> matches exactly the two selected")

            await middle_graph.ainvoke(Command(resume={"action": "approve"}), config)

    async with async_session_factory() as db:
        diagrams_a = await DiagramRepo.get_by_requirement(db, requirement_id=req_a.id, session_id=session.id)
        diagrams_b = await DiagramRepo.get_by_requirement(db, requirement_id=req_b.id, session_id=session.id)
        # the DB's Diagram.requirement_id FK is singular -> linked to the FIRST selected id.
        assert len(diagrams_a) == 1, "diagram must be linked to a SELECTED requirement"
        assert diagrams_b == [], "the un-selected requirement must have no diagram"
        print(f"assert OK: layer-3 produced the diagram id={diagrams_a[0].id} for the selected requirements "
              f"(linked via FK to req_a={req_a.id}; req_b={req_b.id} correctly untouched)")

    await cleanup_user(user)
    print("Scenario 2 PASSED")


# ---------------------------------------------------------------------------
# Scenario 3: diagram with a SINGLE candidate -> no multi-select, proceeds directly.
# ---------------------------------------------------------------------------
async def test_single_candidate_skips_confirm():
    print("\n--- Scenario 3: diagram, single candidate -> no multi-select, proceeds directly ---")
    user, session = await setup_session("single")

    async with async_session_factory() as db:
        req = await RequirementRepo.finalize(db, session_id=session.id, content="lone req content", level=RequirementLevel.operational)
        await db.commit()

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
        if node_name == "sysml_confirm_question":
            raise AssertionError("user_confirm_inputs must NOT run with a single candidate")
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

    async with AsyncPostgresSaver.from_conn_string(settings.checkpointer_database_url) as checkpointer:
        await checkpointer.setup()
        with middle_llm_stubs(fake_middle_get_llm), \
             patch("agents.sysml.nodes.get_llm", side_effect=fake_layer3_get_llm), \
             patch("agents.sysml.nodes.validate", return_value=[]), \
             patch("agents.sysml.nodes.to_mermaid", return_value="graph TD; A-->B;"):

            middle_graph = build_middle_graph(checkpointer=checkpointer)
            config = build_middle_config(outer_thread_id)

            result = await middle_graph.ainvoke(
                {"user_input": "Show a use case diagram.", "session_id": session.id}, config
            )
            assert result.get("__interrupt__"), "expected layer-3's own review pause, directly"
            payload = result["__interrupt__"][0].value
            assert payload["type"] == "requirement_review"
            pi = result.get("processing_input")
            assert pi["target_requirement_ids"] == [str(req.id)]
            print(f"assert OK: no multi-select interrupt; proceeded directly with target_requirement_ids="
                  f"{pi['target_requirement_ids']}")

            await middle_graph.ainvoke(Command(resume={"action": "approve"}), config)

    async with async_session_factory() as db:
        diagrams = await DiagramRepo.get_by_requirement(db, requirement_id=req.id, session_id=session.id)
        assert len(diagrams) == 1
        print(f"assert OK: diagram finalized id={diagrams[0].id}")

    await cleanup_user(user)
    print("Scenario 3 PASSED")


# ---------------------------------------------------------------------------
# Scenario 4: missing-source case (Step 1) -> confirm_action interrupt, unchanged.
# ---------------------------------------------------------------------------
async def test_missing_source_confirm_action_unchanged():
    print("\n--- Scenario 4: missing-source -> confirm_action interrupt (unchanged) ---")
    user, session = await setup_session("missing-src")

    middle_llm = FakeStructuredWrapperLLM(
        MiddleDecision(has_request=True, resolved_intent=Intent.generate_requirement, level=RequirementLevel.functional)
    )
    confirm_question_llm = FakeSequenceLLM(["No operational requirement exists yet — create one first?"])

    def fake_middle_get_llm(node_name=None):
        if node_name == "sysml_middle_supervisor":
            return middle_llm
        if node_name == "sysml_confirm_question":
            return confirm_question_llm
        raise AssertionError(f"unexpected node_name in middle_nodes: {node_name}")

    outer_thread_id = f"outer-{uuid.uuid4()}"
    settings = get_settings()

    async with AsyncPostgresSaver.from_conn_string(settings.checkpointer_database_url) as checkpointer:
        await checkpointer.setup()
        with middle_llm_stubs(fake_middle_get_llm):
            middle_graph = build_middle_graph(checkpointer=checkpointer)
            config = build_middle_config(outer_thread_id)

            result = await middle_graph.ainvoke(
                {"user_input": "Define a specific function.", "session_id": session.id}, config
            )
            assert result.get("__interrupt__")
            payload = result["__interrupt__"][0].value
            assert payload["pattern"] == "confirm_action"
            print(f"assert OK: missing-source still routes to confirm_action. question={payload['question']!r}")

    await cleanup_user(user)
    print("Scenario 4 PASSED")


# ---------------------------------------------------------------------------
# Scenario 5: invalid input (Step 2) -> clarify_request interrupt, unchanged.
# ---------------------------------------------------------------------------
async def test_invalid_input_clarify_unchanged():
    print("\n--- Scenario 5: invalid input -> clarify_request interrupt (unchanged) ---")
    user, session = await setup_session("invalid")

    middle_llm = FakeStructuredWrapperLLM(
        MiddleDecision(has_request=True, resolved_intent=Intent.apply_published_delta)
    )
    confirm_question_llm = FakeSequenceLLM(["I couldn't tell what you'd like me to do — could you rephrase?"])

    def fake_middle_get_llm(node_name=None):
        if node_name == "sysml_middle_supervisor":
            return middle_llm
        if node_name == "sysml_confirm_question":
            return confirm_question_llm
        raise AssertionError(f"unexpected node_name in middle_nodes: {node_name}")

    outer_thread_id = f"outer-{uuid.uuid4()}"
    settings = get_settings()

    async with AsyncPostgresSaver.from_conn_string(settings.checkpointer_database_url) as checkpointer:
        await checkpointer.setup()
        with middle_llm_stubs(fake_middle_get_llm):
            middle_graph = build_middle_graph(checkpointer=checkpointer)
            config = build_middle_config(outer_thread_id)

            result = await middle_graph.ainvoke(
                {"user_input": "apply the published delta thing", "session_id": session.id}, config
            )
            assert result.get("__interrupt__")
            payload = result["__interrupt__"][0].value
            assert payload["pattern"] == "clarify_request"
            print(f"assert OK: invalid input still routes to clarify_request. question={payload['question']!r}")

    await cleanup_user(user)
    print("Scenario 5 PASSED")


# ---------------------------------------------------------------------------
# Scenario 6: min_selected enforced — resuming the multi-select with ZERO selected is
# rejected and re-asked (a second interrupt with the same pattern + an error), then a
# valid resume proceeds normally.
# ---------------------------------------------------------------------------
async def test_min_selected_enforced():
    print("\n--- Scenario 6: min_selected=1 enforced -> zero-selection resume is rejected/re-asked ---")
    user, session = await setup_session("min-selected")

    async with async_session_factory() as db:
        req_a = await RequirementRepo.finalize(db, session_id=session.id, content="req A content", level=RequirementLevel.operational)
        req_b = await RequirementRepo.finalize(db, session_id=session.id, content="req B content", level=RequirementLevel.operational)
        await db.commit()

    middle_llm = FakeStructuredWrapperLLM(
        MiddleDecision(has_request=True, resolved_intent=Intent.generate_diagram, diagram_type=DiagramType.use_case)
    )
    confirm_question_llm = FakeSequenceLLM(["Which requirements should this diagram represent?"])
    layer3_supervisor_llm = FakeStructuredWrapperLLM(
        IntentDecision(intent=Intent.generate_diagram, diagram_type=DiagramType.use_case)
    )
    plan_llm = FakeSequenceLLM(["plan"])
    generate_llm = FakeSequenceLLM([VALID_DIAGRAM])

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
            return generate_llm
        raise AssertionError(f"unexpected node_name in layer-3 nodes: {node_name}")

    outer_thread_id = f"outer-{uuid.uuid4()}"
    settings = get_settings()

    async with AsyncPostgresSaver.from_conn_string(settings.checkpointer_database_url) as checkpointer:
        await checkpointer.setup()
        with middle_llm_stubs(fake_middle_get_llm), \
             patch("agents.sysml.nodes.get_llm", side_effect=fake_layer3_get_llm), \
             patch("agents.sysml.nodes.validate", return_value=[]), \
             patch("agents.sysml.nodes.to_mermaid", return_value="graph TD; A-->B;"):

            middle_graph = build_middle_graph(checkpointer=checkpointer)
            config = build_middle_config(outer_thread_id)

            result_1 = await middle_graph.ainvoke(
                {"user_input": "Show a use case diagram.", "session_id": session.id}, config
            )
            assert result_1.get("__interrupt__")
            payload_1 = result_1["__interrupt__"][0].value
            assert payload_1["pattern"] == "select_requirements_for_diagram"
            assert "error" not in payload_1
            print("RUN 1: paused, first ask (no error yet)")

            # resume with an EMPTY selection -> must be rejected and re-asked, not
            # silently accepted and not a crash.
            result_2 = await middle_graph.ainvoke(
                Command(resume={"action": "confirm", "selected_ids": []}), config
            )
            assert result_2.get("__interrupt__"), "expected a SECOND interrupt (re-ask), not proceeding"
            payload_2 = result_2["__interrupt__"][0].value
            assert payload_2["pattern"] == "select_requirements_for_diagram"
            assert "error" in payload_2 and "at least" in payload_2["error"].lower()
            print(f"RUN 2: resumed with selected_ids=[] -> REJECTED and re-asked. error={payload_2['error']!r}")

            async with async_session_factory() as db:
                rows = await RequirementRepo.list_by_session(db, session_id=session.id)
                # no new artifacts written; still just the two seed requirements.
                assert len(rows) == 2

            # now resume the RE-ASK with a valid, non-empty selection -> proceeds.
            result_3 = await middle_graph.ainvoke(
                Command(resume={"action": "confirm", "selected_ids": [str(req_a.id)]}), config
            )
            assert result_3.get("__interrupt__"), "expected layer-3 to now pause at review"
            pi = result_3.get("processing_input")
            assert pi["target_requirement_ids"] == [str(req_a.id)]
            print(f"RUN 3: resumed with a valid selection -> proceeded. target_requirement_ids="
                  f"{pi['target_requirement_ids']}")

            await middle_graph.ainvoke(Command(resume={"action": "approve"}), config)

    async with async_session_factory() as db:
        diagrams = await DiagramRepo.get_by_requirement(db, requirement_id=req_a.id, session_id=session.id)
        assert len(diagrams) == 1
        print(f"assert OK: diagram finalized id={diagrams[0].id} after the min_selected guard was satisfied")

    await cleanup_user(user)
    print("Scenario 6 PASSED")


async def main() -> None:
    await test_straight_through_no_confirm()
    await clear_checkpoints()
    await test_multi_select_diagram_targets()
    await clear_checkpoints()
    await test_single_candidate_skips_confirm()
    await clear_checkpoints()
    await test_missing_source_confirm_action_unchanged()
    await clear_checkpoints()
    await test_invalid_input_clarify_unchanged()
    await clear_checkpoints()
    await test_min_selected_enforced()
    await clear_checkpoints()
    print("\n=== CONFIRM WIRING (STEP 4) TEST SUITE PASSED ===")


if __name__ == "__main__":
    asyncio.run(main())
