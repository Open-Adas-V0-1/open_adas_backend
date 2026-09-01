"""Layer-1 rebuild, Step 2: conditional plan_node + TODO-list plan_state.

Proves planning is a CONDITIONAL path the hub chooses only for needs_execution:
  - single-task request -> plan_node builds a 1-item TODO, returns to the hub.
  - multi-part request -> an ORDERED multi-item TODO with dependencies correct
    (a diagram task after its requirement task).
  - simple message -> still NO plan_node (Step 1 path intact).
  - plan-level insufficiency -> clarify interrupt (fail-open, no fabricated plan).

Uses the SAME production checkpointer as T6a/Step 1 (encrypted, durability-configured).

As of the Layer-1 rebuild Step 3, a built plan starts EXECUTING automatically (the
hub immediately marks task-1 in_progress and delegates to sysml_middle_node) -- this
file's own focus stays on plan STRUCTURE, inspected via the resulting interrupt at
task-1's layer-3 review (proving the plan was built correctly) rather than on
execution semantics, which are scripts/smoke_test_supervisor_execution.py's job. The
downstream layer-2/3 pipeline is stubbed just enough to let that first task start
without crashing on unstubbed real LLM calls.

LLM call sites are stubbed (same rationale as every prior step in this project).
agents.sysml.nodes.validate is ALSO stubbed for the Windows event-loop reason
documented in scripts/smoke_test_level_resolution.py.

Run: python -m scripts.smoke_test_supervisor_plan
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
from data.repository import ProjectRepo, SessionRepo, UserRepo  # noqa: E402
from harness.checkpointer import build_production_checkpointer  # noqa: E402
from supervisor.graph import build_supervisor_config, build_supervisor_graph  # noqa: E402

VALID_OPERATIONAL = "package Ops { requirement def OpReq { doc /* op */ subject s : ScalarValues::Boolean; require constraint { true } } }"


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
        user = await UserRepo.create(db, email=f"plan-{label}-{uuid.uuid4()}@test.dev", password_hash="hashed")
        project = await ProjectRepo.create(db, user_id=user.id, name=f"Plan {label}")
        session = await SessionRepo.create(
            db, project_id=project.id, thread_id=str(uuid.uuid4()), title=f"Plan {label}"
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


def downstream_ctxs():
    """Standard stubs letting task-1's automatic execution reach layer-3's review
    without crashing on a real LLM call -- one operational requirement, once.
    """
    middle_llm = FakeStructuredWrapperLLM([
        MiddleDecision(has_request=True, resolved_intent=Intent.generate_requirement, level=RequirementLevel.operational),
        MiddleDecision(has_request=False, message="nothing further"),
    ])
    layer3_supervisor_llm = FakeStructuredWrapperLLM(IntentDecision(intent=Intent.generate_requirement))
    plan_step_llm = FakeSequenceLLM(["plan"])
    generate_llm = FakeSequenceLLM([VALID_OPERATIONAL])

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


def stub_llms(top_llm, plan_llm=None):
    def fake_get_llm(node_name=None):
        if node_name == "top_level_supervisor":
            return top_llm
        if node_name == "plan_node":
            if plan_llm is None:
                raise AssertionError("plan_node must NOT be reached on this path")
            return plan_llm
        raise AssertionError(f"unexpected node_name: {node_name}")
    return fake_get_llm


# ---------------------------------------------------------------------------
# Scenario 1: single-task request -> plan_node builds a 1-item TODO (correct
# intent/level), returns to hub, which starts executing it (Step 3) -- the plan's
# STRUCTURE is inspected via the resulting layer-3 pause.
# ---------------------------------------------------------------------------
async def test_single_task_plan():
    print("\n--- Scenario 1: single-task request -> 1-item TODO ---")
    user, session = await setup_user_project_session("single")

    top_llm = FakeStructuredWrapperLLM(
        HubDecision(classification=HubClassification.needs_execution)
    )
    plan_llm = FakeStructuredWrapperLLM(
        PlanDecision(
            sufficient=True,
            tasks=[
                PlannedTask(
                    description="Generate an operational requirement for braking.",
                    intent="generate_requirement",
                    level=RequirementLevel.operational,
                )
            ],
        )
    )

    outer_thread_id = f"outer-{uuid.uuid4()}"
    async with build_production_checkpointer() as checkpointer:
        with patch("supervisor.router.get_llm", side_effect=stub_llms(top_llm, plan_llm)), \
             patch("supervisor.plan.get_llm", side_effect=stub_llms(top_llm, plan_llm)), \
             _MultiPatch(downstream_ctxs()):
            supervisor_graph = build_supervisor_graph(checkpointer=checkpointer)
            config = build_supervisor_config(outer_thread_id)

            result = await supervisor_graph.ainvoke(
                {"user_input": "generate an operational requirement for braking", "session_id": session.id},
                config,
            )
            assert result.get("__interrupt__"), "expected task-1's automatic execution to reach layer-3 review"

            plan_state = result.get("plan_state")
            assert plan_state is not None
            assert len(plan_state["tasks"]) == 1
            task = plan_state["tasks"][0]
            assert task["intent"] == "generate_requirement"
            assert task["level"] == "operational"
            assert task["status"] == "in_progress", "task-1 has started executing (Step 3), not merely pending"
            assert task["depends_on"] is None
            assert plan_state["original_request"] == "generate an operational requirement for braking"
            print(f"assert OK: 1-item TODO built correctly: {[{k: v for k, v in task.items() if k != 'result_ref'}]}")

            await supervisor_graph.ainvoke(Command(resume={"action": "approve"}), config)

    await clear_checkpoints()
    await cleanup_user(user)
    print("Scenario 1 PASSED")


# ---------------------------------------------------------------------------
# Scenario 2: multi-part request -> an ORDERED multi-item TODO, dependencies correct
# (the diagram task comes after its requirement task). Only task-1 (independent of
# the others) starts executing this visit -- tasks 2 and 3 stay pending, which is
# itself part of what's being proven here (dependency + order correctness).
# ---------------------------------------------------------------------------
async def test_multi_part_ordered_plan():
    print("\n--- Scenario 2: multi-part request -> ordered TODO with dependency ---")
    user, session = await setup_user_project_session("multi")

    top_llm = FakeStructuredWrapperLLM(
        HubDecision(classification=HubClassification.needs_execution)
    )
    plan_llm = FakeStructuredWrapperLLM(
        PlanDecision(
            sufficient=True,
            tasks=[
                PlannedTask(
                    description="Generate a braking requirement.",
                    intent="generate_requirement",
                    level=RequirementLevel.operational,
                ),
                PlannedTask(
                    description="Generate a diagram for the braking requirement.",
                    intent="generate_diagram",
                    depends_on_task_number=1,
                ),
                PlannedTask(
                    description="Generate a speed requirement.",
                    intent="generate_requirement",
                    level=RequirementLevel.operational,
                ),
            ],
        )
    )

    outer_thread_id = f"outer-{uuid.uuid4()}"
    async with build_production_checkpointer() as checkpointer:
        with patch("supervisor.router.get_llm", side_effect=stub_llms(top_llm, plan_llm)), \
             patch("supervisor.plan.get_llm", side_effect=stub_llms(top_llm, plan_llm)), \
             _MultiPatch(downstream_ctxs()):
            supervisor_graph = build_supervisor_graph(checkpointer=checkpointer)
            config = build_supervisor_config(outer_thread_id)

            result_0 = await supervisor_graph.ainvoke(
                {
                    "user_input": "generate a braking requirement, then its diagram, then a speed requirement",
                    "session_id": session.id,
                },
                config,
            )
            # 3 tasks -> COMPLEX (Layer-1 rebuild Step 4) -> plan_review gates execution
            # first; the ordered TODO (with dependency) is already fully visible here,
            # before any task has started.
            assert result_0.get("__interrupt__"), "expected plan_review to pause BEFORE execution (complex plan)"
            payload_0 = result_0["__interrupt__"][0].value
            assert payload_0["pattern"] == "plan_review"

            plan_state = result_0.get("plan_state")
            tasks = plan_state["tasks"]
            assert len(tasks) == 3
            assert [t["id"] for t in tasks] == ["task-1", "task-2", "task-3"]
            assert tasks[0]["intent"] == "generate_requirement" and tasks[0]["depends_on"] is None
            assert tasks[0]["status"] == "pending", "nothing has started yet -- plan_review runs first"
            assert tasks[1]["intent"] == "generate_diagram" and tasks[1]["depends_on"] == "task-1", (
                "the diagram task must depend on ITS requirement task (task-1), not float free"
            )
            assert tasks[1]["status"] == "pending"
            assert tasks[2]["intent"] == "generate_requirement" and tasks[2]["depends_on"] is None
            assert tasks[2]["status"] == "pending"
            # order correctness: the diagram's dependency must appear EARLIER in the list.
            dep_index = {t["id"]: i for i, t in enumerate(tasks)}
            assert dep_index[tasks[1]["depends_on"]] < dep_index[tasks[1]["id"]], (
                "dependency task must come BEFORE the dependent task"
            )
            print("assert OK: ordered 3-item TODO, visible in plan_review BEFORE any execution:")
            for t in tasks:
                print(f"    {t['id']}: intent={t['intent']!r} level={t['level']!r} "
                      f"depends_on={t['depends_on']!r} status={t['status']!r}")
            print("assert OK: diagram task (task-2) depends on its requirement task (task-1), "
                  "which correctly precedes it; the unrelated speed requirement (task-3) has no dependency")

            result = await supervisor_graph.ainvoke(Command(resume={"action": "approve"}), config)
            assert result.get("__interrupt__"), "expected task-1's execution to now reach layer-3 review"
            tasks = result["plan_state"]["tasks"]
            assert tasks[0]["status"] == "in_progress", "task-1 (no dependency) starts first, after approval"
            assert tasks[1]["status"] == "pending", "the diagram task must wait for its dependency"
            assert tasks[2]["status"] == "pending"
            print("assert OK: after plan_review approval, task-1 (no dependency) started first, "
                  "task-2 (depends on task-1) correctly still pending")

            await supervisor_graph.ainvoke(Command(resume={"action": "approve"}), config)

    await clear_checkpoints()
    await cleanup_user(user)
    print("Scenario 2 PASSED")


# ---------------------------------------------------------------------------
# Scenario 3: simple message -> still NO plan_node (Step 1 path intact).
# ---------------------------------------------------------------------------
async def test_simple_message_skips_plan_node():
    print("\n--- Scenario 3: simple message -> NO plan_node (Step 1 path intact) ---")
    user, session = await setup_user_project_session("simple")

    top_llm = FakeStructuredWrapperLLM(
        HubDecision(classification=HubClassification.simple_response, response="Hi there!")
    )

    outer_thread_id = f"outer-{uuid.uuid4()}"
    async with build_production_checkpointer() as checkpointer:
        # plan_llm=None -> stub_llms raises AssertionError if plan_node's LLM is ever
        # requested, proving plan_node is never reached.
        with patch("supervisor.router.get_llm", side_effect=stub_llms(top_llm, None)), \
             patch("supervisor.plan.get_llm", side_effect=stub_llms(top_llm, None)):
            supervisor_graph = build_supervisor_graph(checkpointer=checkpointer)
            config = build_supervisor_config(outer_thread_id)

            result = await supervisor_graph.ainvoke({"user_input": "hello", "session_id": session.id}, config)

    assert not result.get("__interrupt__")
    assert result.get("classification") == "simple_response"
    assert result.get("response") == "Hi there!"
    assert result.get("plan_state") is None, "simple messages must never build a plan"
    assert result.get("done") is True
    print(f"assert OK: classification={result.get('classification')!r} response={result.get('response')!r} "
          f"plan_state={result.get('plan_state')!r} -- plan_node never invoked")

    await clear_checkpoints()
    await cleanup_user(user)
    print("Scenario 3 PASSED")


# ---------------------------------------------------------------------------
# Scenario 4: plan-level insufficiency -> clarify interrupt, fail-open, no crash, no
# fabricated plan. Also proves resuming with a clarified request proceeds normally.
# ---------------------------------------------------------------------------
async def test_plan_insufficiency_clarify():
    print("\n--- Scenario 4: plan-level insufficiency -> clarify interrupt ---")
    user, session = await setup_user_project_session("insufficient")

    top_llm = FakeStructuredWrapperLLM([
        HubDecision(classification=HubClassification.needs_execution),
        HubDecision(classification=HubClassification.needs_execution),
    ])
    plan_llm = FakeStructuredWrapperLLM([
        PlanDecision(sufficient=False, tasks=[], clarifying_message="What would you like me to generate?"),
        PlanDecision(
            sufficient=True,
            tasks=[PlannedTask(description="Generate an operational requirement for braking.",
                                intent="generate_requirement", level=RequirementLevel.operational)],
        ),
    ])

    outer_thread_id = f"outer-{uuid.uuid4()}"
    async with build_production_checkpointer() as checkpointer:
        with patch("supervisor.router.get_llm", side_effect=stub_llms(top_llm, plan_llm)), \
             patch("supervisor.plan.get_llm", side_effect=stub_llms(top_llm, plan_llm)), \
             _MultiPatch(downstream_ctxs()):
            supervisor_graph = build_supervisor_graph(checkpointer=checkpointer)
            config = build_supervisor_config(outer_thread_id)

            result_1 = await supervisor_graph.ainvoke(
                {"user_input": "do something with it", "session_id": session.id}, config
            )
            assert result_1.get("__interrupt__"), "expected plan_node to pause with a clarify interrupt"
            payload = result_1["__interrupt__"][0].value
            assert payload["type"] == "plan_clarify"
            assert payload["question"] == "What would you like me to generate?"
            assert result_1.get("plan_state") is None, "no plan must be fabricated when insufficient"
            print(f"assert OK: plan-level insufficiency -> interrupt type={payload['type']!r} "
                  f"question={payload['question']!r}, no fabricated plan (fail-open, no crash)")

            # The clarify interrupt is only re-reachable if the LLM's FRESH verdict on
            # the (still stale, pre-update) user_input is insufficient again -- so the
            # clarified text must be applied via Command(update=...) BEFORE the replay,
            # not read out of the resume value by the node itself. See plan_node's
            # resume-contract docstring.
            result_2 = await supervisor_graph.ainvoke(
                Command(
                    update={"user_input": "generate an operational requirement for braking"},
                    resume={"acknowledged": True},
                ),
                config,
            )
            assert result_2.get("__interrupt__"), "expected task-1's automatic execution to reach layer-3 review"

            plan_state = result_2.get("plan_state")
            assert plan_state is not None and len(plan_state["tasks"]) == 1
            assert plan_state["original_request"] == "generate an operational requirement for braking"
            print(f"assert OK: resuming with a clarified request -> re-classified -> planned normally: "
                  f"{[{k: v for k, v in plan_state['tasks'][0].items() if k != 'result_ref'}]}")

            await supervisor_graph.ainvoke(Command(resume={"action": "approve"}), config)

    await clear_checkpoints()
    await cleanup_user(user)
    print("Scenario 4 PASSED")


async def main() -> None:
    await test_single_task_plan()
    await test_multi_part_ordered_plan()
    await test_simple_message_skips_plan_node()
    await test_plan_insufficiency_clarify()
    print("\n=== SUPERVISOR PLAN (LAYER-1 STEP 2) TEST SUITE PASSED ===")


if __name__ == "__main__":
    asyncio.run(main())
