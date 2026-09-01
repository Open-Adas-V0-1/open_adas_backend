"""Layer-1 rebuild, Step 1: top_level_supervisor as the HUB + simple direct responses.

Proves the rebuilt top-level supervisor classifies every turn via structured output +
router-as-code, and routes correctly WITHOUT any planning/delegation:
  - simple_response: answered directly, turn ends.
  - needs_execution: detected + marked (placeholder response), NOT answered as small talk.
  - unclear: clarification asked, fail-open, no crash.

Uses the SAME production checkpointer as T6a (encrypted, durability-configured) -- Layer 1
owns it; there is no Layer-2/Layer-3 dispatch in this step, so nothing beneath the hub is
exercised (that's Steps 2-3).

LLM call sites are stubbed (same rationale as every prior step in this project).

Run: python -m scripts.smoke_test_supervisor_hub
"""
import asyncio
import sys
import uuid
from unittest.mock import patch

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

from app.schemas.supervisor import HubClassification, HubDecision  # noqa: E402
from data.db import async_session_factory  # noqa: E402
from data.repository import ProjectRepo, SessionRepo, UserRepo  # noqa: E402
from harness.checkpointer import build_production_checkpointer  # noqa: E402
from sqlalchemy import text  # noqa: E402
from supervisor.graph import build_supervisor_config, build_supervisor_graph  # noqa: E402


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


async def setup_user_project_session(label: str):
    async with async_session_factory() as db:
        user = await UserRepo.create(db, email=f"hub-{label}-{uuid.uuid4()}@test.dev", password_hash="hashed")
        project = await ProjectRepo.create(db, user_id=user.id, name=f"Hub {label}")
        session = await SessionRepo.create(
            db, project_id=project.id, thread_id=str(uuid.uuid4()), title=f"Hub {label}"
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
# Scenario 1: simple_response -- greeting, thanks, and a capability question -- all
# answered DIRECTLY, turn ends, NO planning/delegation invoked (route_from_top_supervisor
# only ever returns END in this step -- there is nothing else to invoke).
# ---------------------------------------------------------------------------
async def test_simple_response():
    print("\n--- Scenario 1: simple_response -- answered directly, no planning/delegation ---")
    user, session = await setup_user_project_session("simple")

    for user_input, response_text in [
        ("hello", "Hi there! How can I help?"),
        ("thanks a lot!", "You're welcome!"),
        ("what can you do?", "I can help generate and modify SysML v2 requirements and diagrams."),
    ]:
        top_llm = FakeStructuredWrapperLLM(
            HubDecision(classification=HubClassification.simple_response, response=response_text)
        )

        def fake_top_get_llm(node_name=None):
            if node_name == "top_level_supervisor":
                return top_llm
            raise AssertionError(f"unexpected node_name: {node_name}")

        outer_thread_id = f"outer-{uuid.uuid4()}"

        async with build_production_checkpointer() as checkpointer:
            with patch("supervisor.router.get_llm", side_effect=fake_top_get_llm):
                supervisor_graph = build_supervisor_graph(checkpointer=checkpointer)
                config = build_supervisor_config(outer_thread_id)

                result = await supervisor_graph.ainvoke(
                    {"user_input": user_input, "session_id": session.id}, config
                )

        assert not result.get("__interrupt__"), "simple_response must never pause"
        assert result.get("classification") == "simple_response"
        assert result.get("response") == response_text
        assert result.get("done") is True
        assert result.get("result") == "simple_response"
        assert top_llm._structured.calls == 1, "exactly one classification call, no re-planning loop"
        print(f"input={user_input!r} -> classification={result.get('classification')!r} "
              f"response={result.get('response')!r} (LLM calls={top_llm._structured.calls})")

    await clear_checkpoints()
    await cleanup_user(user)
    print("Scenario 1 PASSED")


# ---------------------------------------------------------------------------
# Scenario 2: needs_execution -- a clear work request is classified as needing real
# work, NOT answered as small talk.
# ---------------------------------------------------------------------------
async def test_needs_execution_routes_onward():
    print("\n--- Scenario 2: needs_execution -- classified as work, NOT answered as small talk ---")
    # As of the Layer-1 rebuild Step 3, needs_execution routes ALL THE WAY through
    # plan_node -> sysml_middle_node -> layer-2/3 -- plan_node's own decomposition
    # coverage lives in scripts/smoke_test_supervisor_plan.py, and the execution loop's
    # own coverage lives in scripts/smoke_test_supervisor_execution.py. This scenario
    # stays focused on Step 1's actual contract (the hub classification), but must now
    # stub the full downstream pipeline for the turn to complete without hitting a real
    # LLM call.
    user, session = await setup_user_project_session("exec")

    from app.schemas.sysml import Intent, IntentDecision, MiddleDecision
    from app.schemas.supervisor import PlanDecision, PlannedTask
    from data.models import RequirementLevel
    from langgraph.types import Command

    top_llm = FakeStructuredWrapperLLM(
        HubDecision(classification=HubClassification.needs_execution, response=None)
    )
    plan_llm = FakeStructuredWrapperLLM(
        PlanDecision(
            sufficient=True,
            tasks=[PlannedTask(description="Generate an operational requirement for braking.",
                                intent="generate_requirement", level=RequirementLevel.operational)],
        )
    )
    middle_llm = FakeStructuredWrapperLLM([
        MiddleDecision(has_request=True, resolved_intent=Intent.generate_requirement, level=RequirementLevel.operational),
        MiddleDecision(has_request=False, message="nothing further"),
    ])
    layer3_supervisor_llm = FakeStructuredWrapperLLM(IntentDecision(intent=Intent.generate_requirement))

    class FakeMessage:
        def __init__(self, content):
            self.content = content

    class FakeSequenceLLM:
        def __init__(self, drafts):
            self._drafts = drafts
            self.calls = 0

        async def ainvoke(self, prompt):
            draft = self._drafts[min(self.calls, len(self._drafts) - 1)]
            self.calls += 1
            return FakeMessage(draft)

    plan_step_llm = FakeSequenceLLM(["plan"])
    generate_llm = FakeSequenceLLM([
        "package Ops { requirement def OpReq { doc /* op */ subject s : ScalarValues::Boolean; require constraint { true } } }"
    ])

    def fake_top_get_llm(node_name=None):
        if node_name == "top_level_supervisor":
            return top_llm
        raise AssertionError(f"unexpected node_name in supervisor.router: {node_name}")

    def fake_plan_get_llm(node_name=None):
        if node_name == "plan_node":
            return plan_llm
        raise AssertionError(f"unexpected node_name in supervisor.plan: {node_name}")

    def fake_middle_get_llm(node_name=None):
        if node_name == "sysml_middle_supervisor":
            return middle_llm
        raise AssertionError(f"unexpected node_name in agents.sysml.middle_nodes: {node_name}")

    def fake_layer3_get_llm(node_name=None):
        if node_name == "sysml_supervisor":
            return layer3_supervisor_llm
        if node_name == "sysml_plan":
            return plan_step_llm
        if node_name == "sysml_generate":
            return generate_llm
        raise AssertionError(f"unexpected node_name in agents.sysml.nodes: {node_name}")

    outer_thread_id = f"outer-{uuid.uuid4()}"

    async with build_production_checkpointer() as checkpointer:
        with patch("supervisor.router.get_llm", side_effect=fake_top_get_llm), \
             patch("supervisor.plan.get_llm", side_effect=fake_plan_get_llm), \
             patch("agents.sysml.middle_nodes.get_llm", side_effect=fake_middle_get_llm), \
             patch("agents.sysml.nodes.get_llm", side_effect=fake_layer3_get_llm), \
             patch("agents.sysml.nodes.validate", return_value=[]):
            supervisor_graph = build_supervisor_graph(checkpointer=checkpointer)
            config = build_supervisor_config(outer_thread_id)

            result_1 = await supervisor_graph.ainvoke(
                {"user_input": "generate an operational requirement for braking", "session_id": session.id},
                config,
            )
            assert result_1.get("__interrupt__"), "expected downstream layer-3 review, proving real work engaged"
            assert result_1.get("classification") == "needs_execution"
            assert result_1.get("response") is None, "must NOT be answered as small talk"
            print(f"input='generate an operational requirement for braking' -> classification="
                  f"{result_1.get('classification')!r} response={result_1.get('response')!r}")
            print("assert OK: NOT answered as small talk -- correctly routed onward, all the way to layer-3 review")

            result_2 = await supervisor_graph.ainvoke(Command(resume={"action": "approve"}), config)

    assert not result_2.get("__interrupt__")
    assert result_2.get("done") is True
    assert result_2["plan_state"]["tasks"][0]["status"] == "done"

    await clear_checkpoints()
    await cleanup_user(user)
    print("Scenario 2 PASSED")


# ---------------------------------------------------------------------------
# Scenario 3: unclear -- ambiguous message -> clarification asked, fail-open, no crash.
# ---------------------------------------------------------------------------
async def test_unclear_clarify():
    print("\n--- Scenario 3: unclear -- clarification asked, fail-open, no crash ---")
    user, session = await setup_user_project_session("unclear")

    top_llm = FakeStructuredWrapperLLM(
        HubDecision(classification=HubClassification.unclear, response="Sorry, I didn't quite catch that -- could you clarify?")
    )

    def fake_top_get_llm(node_name=None):
        if node_name == "top_level_supervisor":
            return top_llm
        raise AssertionError(f"unexpected node_name: {node_name}")

    outer_thread_id = f"outer-{uuid.uuid4()}"

    async with build_production_checkpointer() as checkpointer:
        with patch("supervisor.router.get_llm", side_effect=fake_top_get_llm):
            supervisor_graph = build_supervisor_graph(checkpointer=checkpointer)
            config = build_supervisor_config(outer_thread_id)

            result = await supervisor_graph.ainvoke(
                {"user_input": "asdkjh zzz ??? um maybe the thing", "session_id": session.id}, config
            )

    assert not result.get("__interrupt__"), "unclear must fail-open, never crash or hang"
    assert result.get("classification") == "unclear"
    assert result.get("response") == "Sorry, I didn't quite catch that -- could you clarify?"
    assert result.get("done") is True
    assert result.get("result") == "unclear"
    print(f"input='asdkjh zzz ??? um maybe the thing' -> classification={result.get('classification')!r} "
          f"response={result.get('response')!r}")
    print("assert OK: fail-open clarification, no crash")

    await clear_checkpoints()
    await cleanup_user(user)
    print("Scenario 3 PASSED")


# ---------------------------------------------------------------------------
# Scenario 4: state forward-compatibility -- plan_state and results are present in the
# TypedDict schema (even though unused/None this step), ready for Steps 2-5 to fill in
# without reshaping the state.
# ---------------------------------------------------------------------------
async def test_state_forward_compatible_placeholders():
    print("\n--- Scenario 4: state carries plan_state/results placeholders, ready for later steps ---")
    from supervisor.state import SupervisorState

    annotations = SupervisorState.__annotations__
    for field in ("plan_state", "results", "classification", "response", "done", "result",
                  "processing_index", "supervisor_visits", "target_requirement_id"):
        assert field in annotations, f"SupervisorState is missing forward-compatible field: {field}"
    print(f"assert OK: SupervisorState declares {sorted(annotations.keys())}")

    user, session = await setup_user_project_session("state")
    top_llm = FakeStructuredWrapperLLM(
        HubDecision(classification=HubClassification.simple_response, response="hi")
    )

    def fake_top_get_llm(node_name=None):
        if node_name == "top_level_supervisor":
            return top_llm
        raise AssertionError(f"unexpected node_name: {node_name}")

    outer_thread_id = f"outer-{uuid.uuid4()}"

    async with build_production_checkpointer() as checkpointer:
        with patch("supervisor.router.get_llm", side_effect=fake_top_get_llm):
            supervisor_graph = build_supervisor_graph(checkpointer=checkpointer)
            config = build_supervisor_config(outer_thread_id)

            result = await supervisor_graph.ainvoke(
                {"user_input": "hi", "session_id": session.id, "plan_state": None, "results": []}, config
            )

    # unset/None this step -- just proves the graph tolerates and carries these keys
    # without erroring, ready for Step 2 (plan_state) and Step 3 (results) to populate.
    assert result.get("plan_state") is None
    assert result.get("results") == []
    print(f"assert OK: plan_state={result.get('plan_state')!r} results={result.get('results')!r} "
          f"carried through untouched by the hub (Step 1 doesn't populate them)")

    await clear_checkpoints()
    await cleanup_user(user)
    print("Scenario 4 PASSED")


async def main() -> None:
    await test_simple_response()
    await test_needs_execution_routes_onward()
    await test_unclear_clarify()
    await test_state_forward_compatible_placeholders()
    print("\n=== SUPERVISOR HUB (LAYER-1 STEP 1) TEST SUITE PASSED ===")


if __name__ == "__main__":
    asyncio.run(main())
