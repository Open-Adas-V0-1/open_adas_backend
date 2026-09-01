"""Standalone tests for the Layer-2 redesign, Step 2: validate_inputs (input validity
gate run BEFORE resolve_level), on a REAL Postgres checkpointer.

LLM call sites are stubbed (same rationale as T5a/b/T6a/level-resolution).
agents.sysml.nodes.validate is ALSO stubbed here for the same Windows event-loop
reason documented in scripts/smoke_test_level_resolution.py.

Run: python -m scripts.smoke_test_validate_inputs
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
from agents.sysml import middle_nodes  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.schemas.sysml import Intent, IntentDecision, MiddleDecision  # noqa: E402
from data.db import async_session_factory  # noqa: E402
from data.models import RequirementLevel  # noqa: E402
from data.repository import ProjectRepo, RequirementRepo, SessionRepo, UserRepo  # noqa: E402

VALID_OPERATIONAL = "package Ops { requirement def OpReq { doc /* op */ subject s : ScalarValues::Boolean; require constraint { true } } }"


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
        user = await UserRepo.create(db, email=f"vi-{label}-{uuid.uuid4()}@test.dev", password_hash="hashed")
        project = await ProjectRepo.create(db, user_id=user.id, name=f"ValidateInputs {label}")
        session = await SessionRepo.create(
            db, project_id=project.id, thread_id=str(uuid.uuid4()), title=f"ValidateInputs {label}"
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


# ---------------------------------------------------------------------------
# Scenario 1: valid request (actionable intent, real session) -> validate_inputs
# passes through unchanged, proceeds to resolve_level exactly as Step 1 did.
# ---------------------------------------------------------------------------
async def test_valid_passes_through():
    print("\n--- Scenario 1: valid intent + valid context -> passes through unchanged ---")
    user, session = await setup_session("valid")

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

    async with AsyncPostgresSaver.from_conn_string(settings.checkpointer_database_url) as checkpointer:
        await checkpointer.setup()
        with patch("agents.sysml.middle_nodes.get_llm", side_effect=fake_middle_get_llm), \
             patch("agents.sysml.nodes.get_llm", side_effect=fake_layer3_get_llm), \
             patch("agents.sysml.nodes.validate", return_value=[]):

            middle_graph = build_middle_graph(checkpointer=checkpointer)
            config = build_middle_config(outer_thread_id)

            result = await middle_graph.ainvoke(
                {"user_input": "Define a high-level operational need.", "session_id": session.id}, config
            )
            assert result.get("__interrupt__"), "expected layer-3 to pause at requirement_review"
            assert result.get("input_valid") is True
            assert result.get("invalid_reason") is None
            assert result.get("requested_level") == "operational"
            assert result.get("pending_pattern") is None
            print(f"validate_inputs: input_valid={result.get('input_valid')} invalid_reason={result.get('invalid_reason')!r}")
            print("assert OK: valid intent + valid session passed validate_inputs unchanged, reached resolve_level/layer-3")

            await middle_graph.ainvoke(Command(resume={"action": "approve"}), config)

    async with async_session_factory() as db:
        rows = await RequirementRepo.list_by_session(db, session_id=session.id)
        assert len(rows) == 1 and rows[0].level == RequirementLevel.operational
        print(f"assert OK: finalized operational requirement id={rows[0].id}")

    await cleanup_user(user)
    print("Scenario 1 PASSED")


# ---------------------------------------------------------------------------
# Scenario 2: unrecognized/unsupported intent -> user_confirm_inputs (interrupt) with
# a clarify_request pattern; resuming with a "modify" (clarified) request re-enters
# middle_supervisor and proceeds normally.
# ---------------------------------------------------------------------------
async def test_invalid_intent_then_clarify():
    print("\n--- Scenario 2: invalid (unsupported) intent -> clarify_request interrupt -> resume clarified ---")
    user, session = await setup_session("invalid-intent")

    bad_decision = MiddleDecision(has_request=True, resolved_intent=Intent.apply_published_delta)
    good_decision = MiddleDecision(has_request=True, resolved_intent=Intent.generate_requirement, level=RequirementLevel.operational)
    middle_llm_calls = {"n": 0}

    class SequencedMiddleLLM:
        async def ainvoke(self, prompt):
            middle_llm_calls["n"] += 1
            return bad_decision if middle_llm_calls["n"] == 1 else good_decision

    class SequencedMiddleWrapper:
        def with_structured_output(self, schema):
            return SequencedMiddleLLM()

    confirm_question_llm = FakeSequenceLLM(["I couldn't tell what you'd like me to do — could you rephrase?"])
    layer3_supervisor_llm = FakeStructuredWrapperLLM(IntentDecision(intent=Intent.generate_requirement))
    plan_llm = FakeSequenceLLM(["plan"])
    generate_llm = FakeSequenceLLM([VALID_OPERATIONAL])

    def fake_middle_get_llm(node_name=None):
        if node_name == "sysml_middle_supervisor":
            return SequencedMiddleWrapper()
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
        with patch("agents.sysml.middle_nodes.get_llm", side_effect=fake_middle_get_llm), \
             patch("agents.sysml.nodes.get_llm", side_effect=fake_layer3_get_llm), \
             patch("agents.sysml.nodes.validate", return_value=[]):

            middle_graph = build_middle_graph(checkpointer=checkpointer)
            config = build_middle_config(outer_thread_id)

            result_1 = await middle_graph.ainvoke(
                {"user_input": "apply the published delta thing", "session_id": session.id}, config
            )
            assert result_1.get("__interrupt__"), "expected validate_inputs to route to user_confirm_inputs (invalid intent)"
            payload = result_1["__interrupt__"][0].value
            assert payload["pattern"] == "clarify_request"
            assert result_1.get("input_valid") is False
            assert result_1.get("invalid_reason")
            print(f"RUN 1: paused at user_confirm_inputs. pattern={payload['pattern']!r} question={payload['question']!r}")
            print(f"assert OK: invalid intent observed via interrupt (input_valid={result_1.get('input_valid')}, "
                  f"invalid_reason={result_1.get('invalid_reason')!r})")

            async with async_session_factory() as db:
                rows = await RequirementRepo.list_by_session(db, session_id=session.id)
                assert rows == [], "no DB write on the invalid-intent path"

            # resume with a clarified (rephrased) request via the "modify" action ->
            # loops back to middle_supervisor for re-classification, this time valid.
            result_2 = await middle_graph.ainvoke(
                Command(resume={"action": "modify", "user_input": "Define a high-level operational need."}), config
            )
            assert result_2.get("__interrupt__"), "expected layer-3 to now pause at requirement_review"
            assert result_2.get("input_valid") is True
            assert result_2.get("pending_pattern") is None
            print(f"RUN 2 (clarified/resumed): input_valid={result_2.get('input_valid')} "
                  f"requested_level={result_2.get('requested_level')}")
            print("assert OK: resuming with a clarified request proceeded past validate_inputs normally")

            await middle_graph.ainvoke(Command(resume={"action": "approve"}), config)

    async with async_session_factory() as db:
        rows = await RequirementRepo.list_by_session(db, session_id=session.id)
        assert len(rows) == 1 and rows[0].level == RequirementLevel.operational
        print(f"assert OK: clarified flow finalized the operational requirement id={rows[0].id}")

    await cleanup_user(user)
    print("Scenario 2 PASSED")


# ---------------------------------------------------------------------------
# Scenario 3: file-validity extension point exists but is inert on the text-only path.
# Static assertion that the TODO placeholder is present in validate_inputs, plus a
# behavioral check that passing no file-related state doesn't affect the outcome.
# ---------------------------------------------------------------------------
async def test_file_validity_extension_point_inert():
    print("\n--- Scenario 3: file-validity extension point present but inert ---")
    import inspect

    source = inspect.getsource(middle_nodes.validate_inputs)
    assert "TODO(file-validity)" in source, "expected an explicit file-validity extension point TODO in validate_inputs"
    print("assert OK: validate_inputs contains an explicit TODO(file-validity) extension point")

    user, session = await setup_session("file-inert")
    result = await middle_nodes.validate_inputs(
        {"resolved_intent": Intent.generate_requirement.value, "session_id": session.id}
    )
    assert result["input_valid"] is True
    print("assert OK: validate_inputs behaves identically with no file-related state present (inert placeholder)")

    await cleanup_user(user)
    print("Scenario 3 PASSED")


async def main() -> None:
    await test_valid_passes_through()
    await clear_checkpoints()
    await test_invalid_intent_then_clarify()
    await clear_checkpoints()
    await test_file_validity_extension_point_inert()
    await clear_checkpoints()
    print("\n=== VALIDATE_INPUTS TEST SUITE PASSED ===")


if __name__ == "__main__":
    asyncio.run(main())
