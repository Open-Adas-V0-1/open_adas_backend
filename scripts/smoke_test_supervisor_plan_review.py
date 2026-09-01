"""Layer-1 rebuild, Step 4: conditional plan_review (HITL plan approval).

Proves plan_review is a CONDITIONAL plan-level HITL step:
  - complex (multi-task) plan -> plan_review interrupt shows the ordered TODO ->
    resume APPROVE -> execution proceeds, all tasks complete.
  - simple (single-task) plan -> NO plan_review, straight to execution (frictionless).
  - modified plan -> user edits the TODO (drops a task) -> plan_state reflects the
    edit -> execution runs the edited plan only.
  - cancelled plan -> turn ends, nothing executed, no artifacts created.
  - after approval, nested interrupts (Layer-2/Layer-3) still bubble correctly.

Uses the SAME production checkpointer as Steps 1-3 (encrypted, durability-configured).

LLM call sites are stubbed (same rationale as every prior step in this project).
agents.sysml.nodes.validate is ALSO stubbed for the Windows event-loop reason
documented in scripts/smoke_test_level_resolution.py.

Run: python -m scripts.smoke_test_supervisor_plan_review
"""
import asyncio
import sys
import uuid
from unittest.mock import patch

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

from langgraph.types import Command  # noqa: E402
from sqlalchemy import text  # noqa: E402

from app.schemas.sysml import Intent, IntentDecision, MiddleDecision  # noqa: E402
from app.schemas.supervisor import HubClassification, HubDecision, PlanDecision, PlannedTask  # noqa: E402
from data.db import async_session_factory  # noqa: E402
from data.models import RequirementLevel  # noqa: E402
from data.repository import ProjectRepo, RequirementRepo, SessionRepo, UserRepo  # noqa: E402
from harness.checkpointer import build_production_checkpointer  # noqa: E402
from supervisor.graph import build_supervisor_config, build_supervisor_graph  # noqa: E402

VALID_BRAKING = "package Braking { requirement def BrakingReq { doc /* braking */ subject s : ScalarValues::Boolean; require constraint { true } } }"
VALID_SPEED = "package Speed { requirement def SpeedReq { doc /* speed */ subject s : ScalarValues::Boolean; require constraint { true } } }"


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


async def setup_user_project_session(label: str):
    async with async_session_factory() as db:
        user = await UserRepo.create(db, email=f"prev-{label}-{uuid.uuid4()}@test.dev", password_hash="hashed")
        project = await ProjectRepo.create(db, user_id=user.id, name=f"PlanReview {label}")
        session = await SessionRepo.create(
            db, project_id=project.id, thread_id=str(uuid.uuid4()), title=f"PlanReview {label}"
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


def downstream_ctxs(middle_decisions, layer3_decisions, plan_texts, generate_texts):
    layer3_supervisor_llm = FakeStructuredWrapperLLM(layer3_decisions)
    plan_step_llm = FakeSequenceLLM(plan_texts)
    generate_llm = FakeSequenceLLM(generate_texts)
    middle_llm = FakeStructuredWrapperLLM(middle_decisions)

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

    return [
        patch("agents.sysml.middle_nodes.get_llm", side_effect=fake_middle_get_llm),
        patch("agents.sysml.nodes.get_llm", side_effect=fake_layer3_get_llm),
        patch("agents.sysml.nodes.validate", return_value=[]),
    ]


class _MultiPatch:
    def __init__(self, ctxs):
        self._ctxs = ctxs

    def __enter__(self):
        for c in self._ctxs:
            c.__enter__()
        return self

    def __exit__(self, *a):
        for c in reversed(self._ctxs):
            c.__exit__(*a)


def stub_top_and_plan(top_llm, plan_llm):
    def fake_top_get_llm(node_name=None):
        if node_name == "top_level_supervisor":
            return top_llm
        raise AssertionError(f"unexpected node_name in supervisor.router: {node_name}")

    def fake_plan_get_llm(node_name=None):
        if node_name == "plan_node":
            return plan_llm
        raise AssertionError(f"unexpected node_name in supervisor.plan: {node_name}")

    return [
        patch("supervisor.router.get_llm", side_effect=fake_top_get_llm),
        patch("supervisor.plan.get_llm", side_effect=fake_plan_get_llm),
    ]


# ---------------------------------------------------------------------------
# Scenario 1: complex (multi-task) plan -> plan_review interrupt shows the ordered
# TODO -> resume APPROVE -> execution proceeds and all tasks complete.
# ---------------------------------------------------------------------------
async def test_complex_plan_approved():
    print("\n--- Scenario 1: complex plan -> plan_review interrupt -> approve -> executes fully ---")
    user, session = await setup_user_project_session("approve")

    top_llm = FakeStructuredWrapperLLM(HubDecision(classification=HubClassification.needs_execution))
    plan_llm = FakeStructuredWrapperLLM(PlanDecision(
        sufficient=True,
        tasks=[
            PlannedTask(description="Generate a braking operational requirement.",
                        intent="generate_requirement", level=RequirementLevel.operational),
            PlannedTask(description="Generate a speed operational requirement.",
                        intent="generate_requirement", level=RequirementLevel.operational),
        ],
    ))
    middle_decisions = [
        MiddleDecision(has_request=True, resolved_intent=Intent.generate_requirement, level=RequirementLevel.operational),
        MiddleDecision(has_request=True, resolved_intent=Intent.generate_requirement, level=RequirementLevel.operational),
    ]
    layer3_decisions = [IntentDecision(intent=Intent.generate_requirement)] * 2

    outer_thread_id = f"outer-{uuid.uuid4()}"
    async with build_production_checkpointer() as checkpointer:
        with _MultiPatch(stub_top_and_plan(top_llm, plan_llm)), \
             _MultiPatch(downstream_ctxs(middle_decisions, layer3_decisions,
                                          ["plan braking", "plan speed"], [VALID_BRAKING, VALID_SPEED])):
            supervisor_graph = build_supervisor_graph(checkpointer=checkpointer)
            config = build_supervisor_config(outer_thread_id)

            result_1 = await supervisor_graph.ainvoke(
                {"user_input": "a braking operational requirement, then a speed operational requirement",
                 "session_id": session.id},
                config,
            )
            assert result_1.get("__interrupt__"), "expected plan_review to pause BEFORE any execution"
            payload = result_1["__interrupt__"][0].value
            assert payload["pattern"] == "plan_review"
            assert len(payload["tasks"]) == 2
            assert [t["description"] for t in payload["tasks"]] == [
                "Generate a braking operational requirement.", "Generate a speed operational requirement.",
            ]
            print(f"assert OK: plan_review shows the ordered TODO BEFORE execution: "
                  f"{[t['description'] for t in payload['tasks']]}")

            async with async_session_factory() as db:
                rows = await RequirementRepo.list_by_session(db, session_id=session.id)
                assert rows == [], "nothing must be executed before plan approval"
            print("assert OK: no execution before approval")

            result_2 = await supervisor_graph.ainvoke(Command(resume={"action": "approve"}), config)
            assert result_2.get("__interrupt__"), "expected task-1 to now reach layer-3 review"
            result_3 = await supervisor_graph.ainvoke(Command(resume={"action": "approve"}), config)
            assert result_3.get("__interrupt__"), "expected task-2 to AUTOMATICALLY reach layer-3 review next"
            result_4 = await supervisor_graph.ainvoke(Command(resume={"action": "approve"}), config)

    assert not result_4.get("__interrupt__")
    assert result_4.get("done") is True
    assert result_4.get("result") == "execution_complete"
    t1, t2 = result_4["plan_state"]["tasks"]
    assert t1["status"] == "done" and t2["status"] == "done"
    print(f"assert OK: BOTH tasks completed after approval: {t1['result_ref']} / {t2['result_ref']}")

    async with async_session_factory() as db:
        rows = await RequirementRepo.list_by_session(db, session_id=session.id)
        assert len(rows) == 2
        print(f"assert OK: both artifacts finalized in Postgres")

    await clear_checkpoints()
    await cleanup_user(user)
    print("Scenario 1 PASSED")


# ---------------------------------------------------------------------------
# Scenario 2: simple (single-task) plan -> NO plan_review, straight to execution.
# ---------------------------------------------------------------------------
async def test_simple_plan_skips_review():
    print("\n--- Scenario 2: simple (single-task) plan -> NO plan_review, straight to execution ---")
    user, session = await setup_user_project_session("simple")

    top_llm = FakeStructuredWrapperLLM(HubDecision(classification=HubClassification.needs_execution))
    plan_llm = FakeStructuredWrapperLLM(PlanDecision(
        sufficient=True,
        tasks=[PlannedTask(description="Generate a braking operational requirement.",
                            intent="generate_requirement", level=RequirementLevel.operational)],
    ))
    middle_decisions = [
        MiddleDecision(has_request=True, resolved_intent=Intent.generate_requirement, level=RequirementLevel.operational),
    ]
    layer3_decisions = IntentDecision(intent=Intent.generate_requirement)

    outer_thread_id = f"outer-{uuid.uuid4()}"
    async with build_production_checkpointer() as checkpointer:
        with _MultiPatch(stub_top_and_plan(top_llm, plan_llm)), \
             _MultiPatch(downstream_ctxs(middle_decisions, layer3_decisions, ["plan"], [VALID_BRAKING])):
            supervisor_graph = build_supervisor_graph(checkpointer=checkpointer)
            config = build_supervisor_config(outer_thread_id)

            result_1 = await supervisor_graph.ainvoke(
                {"user_input": "generate a braking operational requirement", "session_id": session.id}, config
            )
            assert result_1.get("__interrupt__"), "expected task-1 to reach layer-3 review directly"
            payload = result_1["__interrupt__"][0].value
            assert payload["type"] == "requirement_review", (
                "single-task plan must go STRAIGHT to layer-3 review, not a plan_review interrupt"
            )
            assert "pattern" not in payload
            print(f"assert OK: single-task plan skipped plan_review entirely -- interrupt is "
                  f"layer-3's own type={payload['type']!r}, no plan-review friction")

            result_2 = await supervisor_graph.ainvoke(Command(resume={"action": "approve"}), config)

    assert not result_2.get("__interrupt__")
    assert result_2.get("done") is True
    assert result_2["plan_state"]["tasks"][0]["status"] == "done"

    await clear_checkpoints()
    await cleanup_user(user)
    print("Scenario 2 PASSED")


# ---------------------------------------------------------------------------
# Scenario 3: modified plan -- user edits the TODO (drops a task) -> plan_state
# reflects the edit -> execution runs the edited plan only.
# ---------------------------------------------------------------------------
async def test_modified_plan_drops_task():
    print("\n--- Scenario 3: modified plan -- user drops a task -> only the edited plan executes ---")
    user, session = await setup_user_project_session("modify")

    top_llm = FakeStructuredWrapperLLM(HubDecision(classification=HubClassification.needs_execution))
    plan_llm = FakeStructuredWrapperLLM(PlanDecision(
        sufficient=True,
        tasks=[
            PlannedTask(description="Generate a braking operational requirement.",
                        intent="generate_requirement", level=RequirementLevel.operational),
            PlannedTask(description="Generate a speed operational requirement.",
                        intent="generate_requirement", level=RequirementLevel.operational),
        ],
    ))
    # only ONE task will actually execute (the surviving, edited one).
    middle_decisions = [
        MiddleDecision(has_request=True, resolved_intent=Intent.generate_requirement, level=RequirementLevel.operational),
    ]
    layer3_decisions = IntentDecision(intent=Intent.generate_requirement)

    outer_thread_id = f"outer-{uuid.uuid4()}"
    async with build_production_checkpointer() as checkpointer:
        with _MultiPatch(stub_top_and_plan(top_llm, plan_llm)), \
             _MultiPatch(downstream_ctxs(middle_decisions, layer3_decisions, ["plan"], [VALID_BRAKING])):
            supervisor_graph = build_supervisor_graph(checkpointer=checkpointer)
            config = build_supervisor_config(outer_thread_id)

            result_1 = await supervisor_graph.ainvoke(
                {"user_input": "a braking operational requirement, then a speed operational requirement",
                 "session_id": session.id},
                config,
            )
            assert result_1.get("__interrupt__")
            payload = result_1["__interrupt__"][0].value
            assert payload["pattern"] == "plan_review"
            assert len(payload["tasks"]) == 2
            print(f"RUN 1: plan_review shows 2 tasks: {[t['description'] for t in payload['tasks']]}")

            # user drops the speed requirement, keeping only the braking one.
            result_2 = await supervisor_graph.ainvoke(
                Command(resume={
                    "action": "modify",
                    "tasks": [
                        {"description": "Generate a braking operational requirement.",
                         "intent": "generate_requirement", "level": "operational"},
                    ],
                }),
                config,
            )
            assert result_2.get("__interrupt__"), "expected the (single, edited) task to reach layer-3 review"
            plan_state = result_2.get("plan_state")
            assert len(plan_state["tasks"]) == 1, "the dropped task must NOT be present in the edited plan"
            assert plan_state["tasks"][0]["id"] == "task-1", "ids re-derived fresh from the edited order"
            assert plan_state["tasks"][0]["description"] == "Generate a braking operational requirement."
            print(f"RUN 2 (modified, dropped speed task): plan_state now has "
                  f"{len(plan_state['tasks'])} task(s): {[t['description'] for t in plan_state['tasks']]}")

            result_3 = await supervisor_graph.ainvoke(Command(resume={"action": "approve"}), config)

    assert not result_3.get("__interrupt__")
    assert result_3.get("done") is True
    assert len(result_3["plan_state"]["tasks"]) == 1
    assert result_3["plan_state"]["tasks"][0]["status"] == "done"

    async with async_session_factory() as db:
        rows = await RequirementRepo.list_by_session(db, session_id=session.id)
        assert len(rows) == 1, "ONLY the edited (surviving) task's artifact should exist"
        print(f"assert OK: only the SURVIVING task executed -- 1 artifact in Postgres, the dropped task never ran")

    await clear_checkpoints()
    await cleanup_user(user)
    print("Scenario 3 PASSED")


# ---------------------------------------------------------------------------
# Scenario 4: cancelled plan -> turn ends, nothing executed, no artifacts created.
# ---------------------------------------------------------------------------
async def test_cancelled_plan():
    print("\n--- Scenario 4: cancelled plan -> turn ends, nothing executed ---")
    user, session = await setup_user_project_session("cancel")

    top_llm = FakeStructuredWrapperLLM(HubDecision(classification=HubClassification.needs_execution))
    plan_llm = FakeStructuredWrapperLLM(PlanDecision(
        sufficient=True,
        tasks=[
            PlannedTask(description="Generate a braking operational requirement.",
                        intent="generate_requirement", level=RequirementLevel.operational),
            PlannedTask(description="Generate a speed operational requirement.",
                        intent="generate_requirement", level=RequirementLevel.operational),
        ],
    ))

    def fake_middle_get_llm(node_name=None):
        raise AssertionError(f"layer-2 must NEVER run on the cancel path (node_name={node_name})")

    def fake_layer3_get_llm(node_name=None):
        raise AssertionError(f"layer-3 must NEVER run on the cancel path (node_name={node_name})")

    outer_thread_id = f"outer-{uuid.uuid4()}"
    async with build_production_checkpointer() as checkpointer:
        with _MultiPatch(stub_top_and_plan(top_llm, plan_llm)), \
             patch("agents.sysml.middle_nodes.get_llm", side_effect=fake_middle_get_llm), \
             patch("agents.sysml.nodes.get_llm", side_effect=fake_layer3_get_llm):
            supervisor_graph = build_supervisor_graph(checkpointer=checkpointer)
            config = build_supervisor_config(outer_thread_id)

            result_1 = await supervisor_graph.ainvoke(
                {"user_input": "a braking operational requirement, then a speed operational requirement",
                 "session_id": session.id},
                config,
            )
            assert result_1.get("__interrupt__")
            payload = result_1["__interrupt__"][0].value
            assert payload["pattern"] == "plan_review"
            print(f"RUN 1: plan_review shows {len(payload['tasks'])} tasks")

            result_2 = await supervisor_graph.ainvoke(Command(resume={"action": "cancel"}), config)

    assert not result_2.get("__interrupt__"), "cancel must end the run, not pause again"
    assert result_2.get("plan_review_decision") == "cancelled"
    assert result_2.get("plan_state") is None, "cancelled plan must be cleared, nothing pending to resume"
    print(f"assert OK: cancelled -- plan_review_decision={result_2.get('plan_review_decision')!r} "
          f"plan_state={result_2.get('plan_state')!r}")

    async with async_session_factory() as db:
        rows = await RequirementRepo.list_by_session(db, session_id=session.id)
        assert rows == [], "NOTHING must be executed/created on cancel"
        print("assert OK: no artifacts created -- nothing was executed")

    await clear_checkpoints()
    await cleanup_user(user)
    print("Scenario 4 PASSED")


# ---------------------------------------------------------------------------
# Scenario 5: after plan approval, nested interrupts (Layer-2's user_confirm_inputs AND
# Layer-3's requirement_review) still bubble correctly during execution. A complex
# (2-task) plan where task 1 is an AMBIGUOUS diagram (triggers Layer-2's confirm, then
# Layer-3's review -- stacked) and task 2 is a plain requirement (Layer-3's review
# only) -- proving the full chain (plan_review -> Layer-2 -> Layer-3, repeated per
# task) surfaces correctly after approval, for BOTH tasks.
# ---------------------------------------------------------------------------
async def test_nested_interrupts_after_approval():
    print("\n--- Scenario 5: after approval, nested Layer-2/Layer-3 interrupts still bubble correctly ---")
    user, session = await setup_user_project_session("nested")

    async with async_session_factory() as db:
        req_a = await RequirementRepo.finalize(db, session_id=session.id, content="req A", level=RequirementLevel.operational)
        req_b = await RequirementRepo.finalize(db, session_id=session.id, content="req B", level=RequirementLevel.operational)
        await db.commit()

    from app.schemas.sysml import DiagramType

    top_llm = FakeStructuredWrapperLLM(HubDecision(classification=HubClassification.needs_execution))
    plan_llm = FakeStructuredWrapperLLM(PlanDecision(
        sufficient=True,
        tasks=[
            PlannedTask(description="Generate a use case diagram.", intent="generate_diagram"),
            PlannedTask(description="Generate a speed operational requirement.",
                        intent="generate_requirement", level=RequirementLevel.operational),
        ],
    ))
    middle_decisions = [
        MiddleDecision(has_request=True, resolved_intent=Intent.generate_diagram, diagram_type=DiagramType.use_case),
        MiddleDecision(has_request=True, resolved_intent=Intent.generate_requirement, level=RequirementLevel.operational),
    ]
    confirm_question_llm = FakeSequenceLLM(["Which requirements should this diagram represent?"])
    layer3_decisions = [
        IntentDecision(intent=Intent.generate_diagram, diagram_type=DiagramType.use_case),
        IntentDecision(intent=Intent.generate_requirement),
    ]

    middle_llm_wrapper = FakeStructuredWrapperLLM(middle_decisions)

    def fake_middle_get_llm(node_name=None):
        if node_name == "sysml_middle_supervisor":
            return middle_llm_wrapper
        if node_name == "sysml_confirm_question":
            return confirm_question_llm
        raise AssertionError(f"unexpected node_name in agents.sysml.middle_nodes: {node_name}")

    layer3_supervisor_llm = FakeStructuredWrapperLLM(layer3_decisions)
    plan_step_llm = FakeSequenceLLM(["plan diagram", "plan speed"])
    generate_llm = FakeSequenceLLM(["package UseCases { part def System { } }", VALID_SPEED])

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
        with _MultiPatch(stub_top_and_plan(top_llm, plan_llm)), \
             patch("agents.sysml.middle_nodes.get_llm", side_effect=fake_middle_get_llm), \
             patch("agents.sysml.nodes.get_llm", side_effect=fake_layer3_get_llm), \
             patch("agents.sysml.nodes.validate", return_value=[]), \
             patch("agents.sysml.nodes.to_mermaid", return_value="graph TD; A-->B;"):
            supervisor_graph = build_supervisor_graph(checkpointer=checkpointer)
            config = build_supervisor_config(outer_thread_id)

            # --- plan_review (complex: 2 tasks) ---
            result_1 = await supervisor_graph.ainvoke(
                {"user_input": "show a use case diagram, then a speed operational requirement",
                 "session_id": session.id},
                config,
            )
            assert result_1.get("__interrupt__")
            assert result_1["__interrupt__"][0].value["pattern"] == "plan_review"
            print("RUN 1: plan_review interrupt (2 tasks)")

            result_2 = await supervisor_graph.ainvoke(Command(resume={"action": "approve"}), config)
            # --- task 1: Layer-2's user_confirm_inputs (ambiguous diagram target) ---
            assert result_2.get("__interrupt__")
            payload_2 = result_2["__interrupt__"][0].value
            assert payload_2["pattern"] == "select_requirements_for_diagram"
            print(f"RUN 2: task 1's Layer-2 interrupt bubbled after plan approval -- pattern={payload_2['pattern']!r}")

            result_3 = await supervisor_graph.ainvoke(
                Command(resume={"action": "confirm", "selected_ids": [str(req_a.id), str(req_b.id)]}), config
            )
            # --- task 1: Layer-3's requirement_review ---
            assert result_3.get("__interrupt__")
            payload_3 = result_3["__interrupt__"][0].value
            assert payload_3["type"] == "requirement_review"
            print(f"RUN 3: task 1's Layer-3 interrupt bubbled next (stacked after the Layer-2 one) -- "
                  f"type={payload_3['type']!r}")

            result_4 = await supervisor_graph.ainvoke(Command(resume={"action": "approve"}), config)
            # --- task 2 (unambiguous, independent) starts AUTOMATICALLY -> Layer-3's review only ---
            assert result_4.get("__interrupt__")
            payload_4 = result_4["__interrupt__"][0].value
            assert payload_4["type"] == "requirement_review"
            assert "pattern" not in payload_4
            print(f"RUN 4: task 2 started automatically, its own Layer-3 interrupt bubbled correctly -- "
                  f"type={payload_4['type']!r}")

            result_5 = await supervisor_graph.ainvoke(Command(resume={"action": "approve"}), config)

    assert not result_5.get("__interrupt__")
    assert result_5.get("done") is True
    t1, t2 = result_5["plan_state"]["tasks"]
    assert t1["status"] == "done" and t2["status"] == "done"
    print(f"assert OK: BOTH tasks completed via the full plan_review -> Layer-2 -> Layer-3 chain, "
          f"repeated per task, all interrupts surfacing correctly after plan approval")

    async with async_session_factory() as db:
        from data.repository import DiagramRepo
        diagrams_a = await DiagramRepo.get_by_requirement(db, requirement_id=req_a.id, session_id=session.id)
        assert len(diagrams_a) == 1
        rows = await RequirementRepo.list_by_session(db, session_id=session.id)
        # req_a, req_b (seeded) + the newly finalized speed requirement.
        assert len(rows) == 3
        print(f"assert OK: task 1's diagram id={diagrams_a[0].id} persisted; task 2's requirement finalized too")

    await clear_checkpoints()
    await cleanup_user(user)
    print("Scenario 5 PASSED")


async def main() -> None:
    await test_complex_plan_approved()
    await test_simple_plan_skips_review()
    await test_modified_plan_drops_task()
    await test_cancelled_plan()
    await test_nested_interrupts_after_approval()
    print("\n=== SUPERVISOR PLAN REVIEW (LAYER-1 STEP 4) TEST SUITE PASSED ===")


if __name__ == "__main__":
    asyncio.run(main())
