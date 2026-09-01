"""Layer-1 rebuild, Step 3: multi-task execution loop + Layer-2 integration.

Proves top_level_supervisor drives the TODO list to completion by delegating each task
to Layer-2 via sysml_middle_node, without stopping between tasks, with full three-level
interrupt bubbling (Layer-2's user_confirm_inputs AND Layer-3's requirement_review both
surface to the Layer-1 caller).

Uses the SAME production checkpointer as T6a/Steps 1-2 (encrypted, durability-configured).

LLM call sites are stubbed (same rationale as every prior step in this project).
agents.sysml.nodes.validate is ALSO stubbed for the Windows event-loop reason documented
in scripts/smoke_test_level_resolution.py.

Run: python -m scripts.smoke_test_supervisor_execution
"""
import asyncio
import os
import sys
import uuid
from unittest.mock import patch

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

from langgraph.types import Command  # noqa: E402
from sqlalchemy import text  # noqa: E402

from app.config import get_settings  # noqa: E402
from app.schemas.sysml import DiagramType, Intent, IntentDecision, MiddleDecision  # noqa: E402
from app.schemas.supervisor import HubClassification, HubDecision, PlanDecision, PlannedTask  # noqa: E402
from data.db import async_session_factory  # noqa: E402
from data.models import RequirementLevel, VersionStatus  # noqa: E402
from data.repository import DiagramRepo, ProjectRepo, RequirementRepo, SessionRepo, UserRepo  # noqa: E402
from harness.checkpointer import build_production_checkpointer  # noqa: E402
from supervisor.graph import build_supervisor_config, build_supervisor_graph  # noqa: E402

VALID_BRAKING = "package Braking { requirement def BrakingReq { doc /* braking */ subject s : ScalarValues::Boolean; require constraint { true } } }"
VALID_SPEED = "package Speed { requirement def SpeedReq { doc /* speed */ subject s : ScalarValues::Boolean; require constraint { true } } }"
VALID_DIAGRAM = "package UseCases { part def System { } }"


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
        user = await UserRepo.create(db, email=f"exec-{label}-{uuid.uuid4()}@test.dev", password_hash="hashed")
        project = await ProjectRepo.create(db, user_id=user.id, name=f"Exec {label}")
        session = await SessionRepo.create(
            db, project_id=project.id, thread_id=str(uuid.uuid4()), title=f"Exec {label}"
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


def patches(top_llm=None, plan_llm=None, middle_llm=None, layer3_supervisor_llm=None,
            plan_step_llm=None, generate_llm=None, confirm_question_llm=None,
            validate_stub=True, mermaid_stub=None):
    def fake_top_get_llm(node_name=None):
        if node_name == "top_level_supervisor" and top_llm is not None:
            return top_llm
        raise AssertionError(f"unexpected node_name in supervisor.router: {node_name}")

    def fake_plan_get_llm(node_name=None):
        if node_name == "plan_node" and plan_llm is not None:
            return plan_llm
        raise AssertionError(f"unexpected node_name in supervisor.plan: {node_name}")

    def fake_middle_get_llm(node_name=None):
        if node_name == "sysml_middle_supervisor" and middle_llm is not None:
            return middle_llm
        if node_name == "sysml_confirm_question" and confirm_question_llm is not None:
            return confirm_question_llm
        raise AssertionError(f"unexpected node_name in agents.sysml.middle_nodes: {node_name}")

    def fake_layer3_get_llm(node_name=None):
        if node_name == "sysml_supervisor" and layer3_supervisor_llm is not None:
            return layer3_supervisor_llm
        if node_name == "sysml_plan" and plan_step_llm is not None:
            return plan_step_llm
        if node_name == "sysml_generate" and generate_llm is not None:
            return generate_llm
        raise AssertionError(f"unexpected node_name in agents.sysml.nodes: {node_name}")

    ctxs = [
        patch("supervisor.router.get_llm", side_effect=fake_top_get_llm),
        patch("supervisor.plan.get_llm", side_effect=fake_plan_get_llm),
        patch("agents.sysml.middle_nodes.get_llm", side_effect=fake_middle_get_llm),
        patch("agents.sysml.nodes.get_llm", side_effect=fake_layer3_get_llm),
    ]
    if validate_stub:
        ctxs.append(patch("agents.sysml.nodes.validate", return_value=[]))
    if mermaid_stub is not None:
        ctxs.append(patch("agents.sysml.nodes.to_mermaid", return_value=mermaid_stub))
    return ctxs


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


# ---------------------------------------------------------------------------
# Scenario 1: single-task turn -> plan (1 item) -> delegate -> layer-3 pauses at
# requirement_review, bubbling THREE levels to the Layer-1 caller -> resume approve ->
# task marked done with its light ref -> all done -> END. No DB write before approval.
# ---------------------------------------------------------------------------
async def test_single_task_three_level_bubble():
    print("\n--- Scenario 1: single-task turn, THREE-level interrupt bubble ---")
    user, session = await setup_user_project_session("single")

    top_llm = FakeStructuredWrapperLLM(HubDecision(classification=HubClassification.needs_execution))
    plan_llm = FakeStructuredWrapperLLM(PlanDecision(
        sufficient=True,
        tasks=[PlannedTask(description="Generate an operational requirement for braking.",
                            intent="generate_requirement", level=RequirementLevel.operational)],
    ))
    middle_llm = FakeStructuredWrapperLLM([
        MiddleDecision(has_request=True, resolved_intent=Intent.generate_requirement, level=RequirementLevel.operational),
        MiddleDecision(has_request=False, message="nothing further"),
    ])
    layer3_supervisor_llm = FakeStructuredWrapperLLM(IntentDecision(intent=Intent.generate_requirement))
    plan_step_llm = FakeSequenceLLM(["plan"])
    generate_llm = FakeSequenceLLM([VALID_BRAKING])

    outer_thread_id = f"outer-{uuid.uuid4()}"
    async with build_production_checkpointer() as checkpointer:
        with _MultiPatch(patches(top_llm=top_llm, plan_llm=plan_llm, middle_llm=middle_llm,
                                  layer3_supervisor_llm=layer3_supervisor_llm,
                                  plan_step_llm=plan_step_llm, generate_llm=generate_llm)):
            supervisor_graph = build_supervisor_graph(checkpointer=checkpointer)
            config = build_supervisor_config(outer_thread_id)

            result_1 = await supervisor_graph.ainvoke(
                {"user_input": "generate an operational requirement for braking", "session_id": session.id}, config
            )
            assert result_1.get("__interrupt__"), "expected the layer-3 interrupt to bubble to the Layer-1 caller"
            payload = result_1["__interrupt__"][0].value
            assert payload["type"] == "requirement_review"
            assert payload["level"] == "operational"
            print(f"assert OK: layer-3's requirement_review interrupt bubbled THREE levels "
                  f"(layer-3 -> layer-2's sysml_processing -> layer-2 graph -> sysml_middle_node -> "
                  f"layer-1 graph -> test caller). draft={payload['draft'][:40]}...")

            async with async_session_factory() as db:
                rows = await RequirementRepo.list_by_session(db, session_id=session.id)
                assert rows == [], "no DB write before approval"
            print("assert OK: no DB write before approval")

            result_2 = await supervisor_graph.ainvoke(Command(resume={"action": "approve"}), config)

    assert not result_2.get("__interrupt__")
    assert result_2.get("done") is True
    assert result_2.get("result") == "execution_complete"
    plan_state = result_2.get("plan_state")
    task = plan_state["tasks"][0]
    assert task["status"] == "done"
    assert task["result_ref"] is not None
    assert task["result_ref"]["artifact_type"] == "requirement"
    print(f"assert OK: task marked done, result_ref={task['result_ref']}")

    async with async_session_factory() as db:
        rows = await RequirementRepo.list_by_session(db, session_id=session.id)
        assert len(rows) == 1 and rows[0].level == RequirementLevel.operational
        assert str(rows[0].id) == task["result_ref"]["artifact_id"]
        print(f"assert OK: finalized requirement id={rows[0].id} matches task's result_ref")

    await clear_checkpoints()
    await cleanup_user(user)
    print("Scenario 1 PASSED")


# ---------------------------------------------------------------------------
# Scenario 2: multi-task turn -> 2 independent tasks -> execute task 1 (pause/resume/
# approve), then AUTOMATICALLY proceed to task 2 (pause/resume/approve) with NO stop
# in between -> both marked done with light refs -> END.
# ---------------------------------------------------------------------------
async def test_multi_task_no_stop_between():
    print("\n--- Scenario 2: multi-task turn -- automatic continuation, no stop between tasks ---")
    user, session = await setup_user_project_session("multi")

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
    middle_llm = FakeStructuredWrapperLLM([
        MiddleDecision(has_request=True, resolved_intent=Intent.generate_requirement, level=RequirementLevel.operational),
        MiddleDecision(has_request=False, message="nothing further"),
        MiddleDecision(has_request=True, resolved_intent=Intent.generate_requirement, level=RequirementLevel.operational),
        MiddleDecision(has_request=False, message="nothing further"),
    ])
    layer3_supervisor_llm = FakeStructuredWrapperLLM([
        IntentDecision(intent=Intent.generate_requirement),
        IntentDecision(intent=Intent.generate_requirement),
    ])
    plan_step_llm = FakeSequenceLLM(["plan braking", "plan speed"])
    generate_llm = FakeSequenceLLM([VALID_BRAKING, VALID_SPEED])

    outer_thread_id = f"outer-{uuid.uuid4()}"
    async with build_production_checkpointer() as checkpointer:
        with _MultiPatch(patches(top_llm=top_llm, plan_llm=plan_llm, middle_llm=middle_llm,
                                  layer3_supervisor_llm=layer3_supervisor_llm,
                                  plan_step_llm=plan_step_llm, generate_llm=generate_llm)):
            supervisor_graph = build_supervisor_graph(checkpointer=checkpointer)
            config = build_supervisor_config(outer_thread_id)

            result_1 = await supervisor_graph.ainvoke(
                {"user_input": "a braking operational requirement, then a speed operational requirement",
                 "session_id": session.id},
                config,
            )
            assert result_1.get("__interrupt__")
            assert result_1["plan_state"]["tasks"][0]["status"] == "in_progress"
            print("TASK 1: paused at layer-3 review")

            result_2 = await supervisor_graph.ainvoke(Command(resume={"action": "approve"}), config)
            # task 1 finalizes AND task 2 starts AND pauses again, all within this ONE call.
            assert result_2.get("__interrupt__"), "expected task 2 to ALSO pause, automatically, with no stop"
            t1, t2 = result_2["plan_state"]["tasks"]
            assert t1["status"] == "done" and t1["result_ref"] is not None
            assert t2["status"] == "in_progress"
            print(f"TASK 1 done (result_ref={t1['result_ref']}) -> TASK 2 AUTOMATICALLY started, paused at layer-3 review")

            result_3 = await supervisor_graph.ainvoke(Command(resume={"action": "approve"}), config)

    assert not result_3.get("__interrupt__")
    assert result_3.get("done") is True
    assert result_3.get("result") == "execution_complete"
    t1, t2 = result_3["plan_state"]["tasks"]
    assert t1["status"] == "done" and t2["status"] == "done"
    assert t1["result_ref"] is not None and t2["result_ref"] is not None
    assert t1["result_ref"]["artifact_id"] != t2["result_ref"]["artifact_id"]
    print(f"assert OK: BOTH tasks done with DISTINCT result_refs: {t1['result_ref']} / {t2['result_ref']}")

    async with async_session_factory() as db:
        rows = await RequirementRepo.list_by_session(db, session_id=session.id)
        assert len(rows) == 2
        finalized_ids = {str(r.id) for r in rows}
        assert finalized_ids == {t1["result_ref"]["artifact_id"], t2["result_ref"]["artifact_id"]}
        print(f"assert OK: BOTH artifacts finalized in Postgres: {finalized_ids}")

    await clear_checkpoints()
    await cleanup_user(user)
    print("Scenario 2 PASSED")


# ---------------------------------------------------------------------------
# Scenario 3: dependency order -- "a braking requirement, then its diagram" -> the
# diagram task runs only AFTER the requirement task is done.
# ---------------------------------------------------------------------------
async def test_dependency_order_respected():
    print("\n--- Scenario 3: dependency order -- diagram task runs only after its requirement ---")
    user, session = await setup_user_project_session("dependency")

    top_llm = FakeStructuredWrapperLLM(HubDecision(classification=HubClassification.needs_execution))
    plan_llm = FakeStructuredWrapperLLM(PlanDecision(
        sufficient=True,
        tasks=[
            PlannedTask(description="Generate a braking requirement.",
                        intent="generate_requirement", level=RequirementLevel.operational),
            PlannedTask(description="Generate a diagram for the braking requirement.",
                        intent="generate_diagram", depends_on_task_number=1),
        ],
    ))
    middle_llm = FakeStructuredWrapperLLM([
        MiddleDecision(has_request=True, resolved_intent=Intent.generate_requirement, level=RequirementLevel.operational),
        MiddleDecision(has_request=False, message="nothing further"),
        MiddleDecision(has_request=True, resolved_intent=Intent.generate_diagram, diagram_type=DiagramType.use_case),
        MiddleDecision(has_request=False, message="nothing further"),
    ])
    layer3_supervisor_llm = FakeStructuredWrapperLLM([
        IntentDecision(intent=Intent.generate_requirement),
        IntentDecision(intent=Intent.generate_diagram, diagram_type=DiagramType.use_case),
    ])
    plan_step_llm = FakeSequenceLLM(["plan req", "plan diagram"])
    generate_llm = FakeSequenceLLM([VALID_BRAKING, VALID_DIAGRAM])

    outer_thread_id = f"outer-{uuid.uuid4()}"
    async with build_production_checkpointer() as checkpointer:
        with _MultiPatch(patches(top_llm=top_llm, plan_llm=plan_llm, middle_llm=middle_llm,
                                  layer3_supervisor_llm=layer3_supervisor_llm,
                                  plan_step_llm=plan_step_llm, generate_llm=generate_llm,
                                  mermaid_stub="graph TD; A-->B;")):
            supervisor_graph = build_supervisor_graph(checkpointer=checkpointer)
            config = build_supervisor_config(outer_thread_id)

            result_1 = await supervisor_graph.ainvoke(
                {"user_input": "generate a braking requirement, then its diagram", "session_id": session.id}, config
            )
            assert result_1.get("__interrupt__")
            task_1_active = result_1["plan_state"]["tasks"][0]
            task_2 = result_1["plan_state"]["tasks"][1]
            assert task_1_active["status"] == "in_progress", "the REQUIREMENT task must run FIRST"
            assert task_2["status"] == "pending", "the DIAGRAM task must NOT start before its dependency is done"
            print("assert OK: requirement task (task-1) runs first; diagram task (task-2) still pending")

            result_2 = await supervisor_graph.ainvoke(Command(resume={"action": "approve"}), config)
            assert result_2.get("__interrupt__"), "expected the diagram task to start automatically next"
            t1, t2 = result_2["plan_state"]["tasks"]
            assert t1["status"] == "done"
            assert t2["status"] == "in_progress", "the diagram task must start ONLY after its dependency is done"
            print(f"assert OK: requirement task done (result_ref={t1['result_ref']}) -> "
                  f"diagram task started ONLY NOW (dependency satisfied)")

            result_3 = await supervisor_graph.ainvoke(Command(resume={"action": "approve"}), config)

    assert not result_3.get("__interrupt__")
    t1, t2 = result_3["plan_state"]["tasks"]
    assert t1["status"] == "done" and t2["status"] == "done"
    print(f"assert OK: both done. requirement={t1['result_ref']} diagram={t2['result_ref']}")

    async with async_session_factory() as db:
        req_id = t1["result_ref"]["artifact_id"]
        import uuid as _uuid
        diagrams = await DiagramRepo.get_by_requirement(db, requirement_id=_uuid.UUID(req_id), session_id=session.id)
        assert len(diagrams) == 1
        print(f"assert OK: diagram id={diagrams[0].id} persisted, linked to the dependency requirement {req_id}")

    await clear_checkpoints()
    await cleanup_user(user)
    print("Scenario 3 PASSED")


# ---------------------------------------------------------------------------
# Scenario 4: ambiguous mid-task -- a task triggers Layer-2's user_confirm_inputs ->
# that interrupt surfaces at Layer-1, resume selection continues into Layer-3 -> then
# requirement_review interrupt surfaces -> resume approve -> done. Stacked interrupts
# across ALL layers.
# ---------------------------------------------------------------------------
async def test_stacked_interrupts_across_layers():
    print("\n--- Scenario 4: stacked interrupts across ALL layers (Layer-2 confirm THEN Layer-3 review) ---")
    user, session = await setup_user_project_session("stacked")

    async with async_session_factory() as db:
        req_a = await RequirementRepo.finalize(db, session_id=session.id, content="req A", level=RequirementLevel.operational)
        req_b = await RequirementRepo.finalize(db, session_id=session.id, content="req B", level=RequirementLevel.operational)
        await db.commit()

    top_llm = FakeStructuredWrapperLLM(HubDecision(classification=HubClassification.needs_execution))
    plan_llm = FakeStructuredWrapperLLM(PlanDecision(
        sufficient=True,
        tasks=[PlannedTask(description="Generate a use case diagram.", intent="generate_diagram")],
    ))
    middle_llm = FakeStructuredWrapperLLM([
        MiddleDecision(has_request=True, resolved_intent=Intent.generate_diagram, diagram_type=DiagramType.use_case),
        MiddleDecision(has_request=False, message="nothing further"),
    ])
    confirm_question_llm = FakeSequenceLLM(["Which requirements should this diagram represent?"])
    layer3_supervisor_llm = FakeStructuredWrapperLLM(
        IntentDecision(intent=Intent.generate_diagram, diagram_type=DiagramType.use_case)
    )
    plan_step_llm = FakeSequenceLLM(["plan"])
    generate_llm = FakeSequenceLLM([VALID_DIAGRAM])

    outer_thread_id = f"outer-{uuid.uuid4()}"
    async with build_production_checkpointer() as checkpointer:
        with _MultiPatch(patches(top_llm=top_llm, plan_llm=plan_llm, middle_llm=middle_llm,
                                  layer3_supervisor_llm=layer3_supervisor_llm,
                                  plan_step_llm=plan_step_llm, generate_llm=generate_llm,
                                  confirm_question_llm=confirm_question_llm,
                                  mermaid_stub="graph TD; A-->B;")):
            supervisor_graph = build_supervisor_graph(checkpointer=checkpointer)
            config = build_supervisor_config(outer_thread_id)

            # --- Interrupt #1: Layer-2's user_confirm_inputs, surfacing at Layer-1 ---
            result_1 = await supervisor_graph.ainvoke(
                {"user_input": "show a use case diagram", "session_id": session.id}, config
            )
            assert result_1.get("__interrupt__")
            payload_1 = result_1["__interrupt__"][0].value
            assert payload_1["pattern"] == "select_requirements_for_diagram"
            assert "type" not in payload_1
            print(f"INTERRUPT #1 (Layer-2, surfaced at Layer-1): pattern={payload_1['pattern']!r} "
                  f"options={[o['id'] for o in payload_1['options']]}")

            # --- resume #1: select both -> continues INTO layer-3 ---
            result_2 = await supervisor_graph.ainvoke(
                Command(resume={"action": "confirm", "selected_ids": [str(req_a.id), str(req_b.id)]}), config
            )
            assert result_2.get("__interrupt__"), "expected layer-3 to now pause"
            payload_2 = result_2["__interrupt__"][0].value
            assert payload_2["type"] == "requirement_review"
            assert "pattern" not in payload_2
            print(f"INTERRUPT #2 (Layer-3, surfaced at Layer-1): type={payload_2['type']!r} "
                  f"source_node={payload_2['source_node']!r}")

            # --- resume #2: approve -> finalizes ---
            result_3 = await supervisor_graph.ainvoke(Command(resume={"action": "approve"}), config)

    assert not result_3.get("__interrupt__")
    assert result_3.get("done") is True
    task = result_3["plan_state"]["tasks"][0]
    assert task["status"] == "done"
    assert task["result_ref"]["artifact_type"] == "diagram"
    print(f"assert OK: BOTH stacked interrupts (Layer-2 then Layer-3) resolved correctly, task done: "
          f"{task['result_ref']}")

    async with async_session_factory() as db:
        diagrams_a = await DiagramRepo.get_by_requirement(db, requirement_id=req_a.id, session_id=session.id)
        assert len(diagrams_a) == 1 and diagrams_a[0].mermaid
        print(f"assert OK: diagram id={diagrams_a[0].id} finalized with model + mermaid")

    await clear_checkpoints()
    await cleanup_user(user)
    print("Scenario 4 PASSED")


# ---------------------------------------------------------------------------
# Scenario 5: guard -- low SUPERVISOR_MAX_VISITS forces the loop to stop safely
# (fail-open) mid multi-task execution, no crash.
# ---------------------------------------------------------------------------
async def test_guard_stops_mid_execution():
    print("\n--- Scenario 5: SUPERVISOR_MAX_VISITS low -> loop stops safely mid multi-task execution ---")
    user, session = await setup_user_project_session("guard")

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
    middle_llm = FakeStructuredWrapperLLM([
        MiddleDecision(has_request=True, resolved_intent=Intent.generate_requirement, level=RequirementLevel.operational),
        MiddleDecision(has_request=False, message="nothing further"),
    ])
    layer3_supervisor_llm = FakeStructuredWrapperLLM(IntentDecision(intent=Intent.generate_requirement))
    plan_step_llm = FakeSequenceLLM(["plan"])
    generate_llm = FakeSequenceLLM([VALID_BRAKING])

    outer_thread_id = f"outer-{uuid.uuid4()}"

    # visits: 1) classify (needs_execution) 2) after plan_node, pick task-1 -> that's
    # visit 2 already at max_visits=2 boundary; visit 3 (after task-1 finishes, picking
    # task-2) breaches it -> guard fires there, safely, before task-2 ever starts.
    os.environ["SUPERVISOR_MAX_VISITS"] = "2"
    get_settings.cache_clear()
    try:
        assert get_settings().supervisor_max_visits == 2
        settings = get_settings()

        async with build_production_checkpointer() as checkpointer:
            with _MultiPatch(patches(top_llm=top_llm, plan_llm=plan_llm, middle_llm=middle_llm,
                                      layer3_supervisor_llm=layer3_supervisor_llm,
                                      plan_step_llm=plan_step_llm, generate_llm=generate_llm)):
                supervisor_graph = build_supervisor_graph(checkpointer=checkpointer)
                config = build_supervisor_config(outer_thread_id)

                result_1 = await supervisor_graph.ainvoke(
                    {"user_input": "a braking requirement, then a speed requirement", "session_id": session.id}, config
                )
                assert result_1.get("__interrupt__"), "task 1 (within max_visits) should still reach layer-3 review"
                print("visit 1-2 (<= max_visits=2): task 1 reached layer-3 review, as expected")

                result_2 = await supervisor_graph.ainvoke(Command(resume={"action": "approve"}), config)
    finally:
        os.environ.pop("SUPERVISOR_MAX_VISITS", None)
        get_settings.cache_clear()

    assert not result_2.get("__interrupt__"), "guard must fail-open to END, not pause or crash"
    assert result_2.get("result") == "stopped: max supervisor visits reached"
    t1, t2 = result_2["plan_state"]["tasks"]
    assert t1["status"] == "done", "task 1 (already completed before the breach) must keep its result"
    assert t2["status"] == "pending", "task 2 must NEVER have started"
    print(f"assert OK: guard fired safely on visit {result_2.get('supervisor_visits')} "
          f"(> max_visits=2) -- task 1 done (result_ref={t1['result_ref']}), task 2 never started, no crash")

    async with async_session_factory() as db:
        rows = await RequirementRepo.list_by_session(db, session_id=session.id)
        assert len(rows) == 1, "only task 1's artifact should exist -- task 2 never ran"
        print(f"assert OK: only 1 requirement persisted (task 2's work never happened)")

    await clear_checkpoints()
    await cleanup_user(user)
    print("Scenario 5 PASSED")


# ---------------------------------------------------------------------------
# Scenario 6: distinct per-processing thread ids in Postgres for each task.
# ---------------------------------------------------------------------------
async def test_distinct_thread_ids_per_task():
    print("\n--- Scenario 6: distinct per-processing (Layer-1 -> Layer-2) thread id per task, in Postgres ---")
    user, session = await setup_user_project_session("threads")

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
    middle_llm = FakeStructuredWrapperLLM([
        MiddleDecision(has_request=True, resolved_intent=Intent.generate_requirement, level=RequirementLevel.operational),
        MiddleDecision(has_request=False, message="nothing further"),
        MiddleDecision(has_request=True, resolved_intent=Intent.generate_requirement, level=RequirementLevel.operational),
        MiddleDecision(has_request=False, message="nothing further"),
    ])
    layer3_supervisor_llm = FakeStructuredWrapperLLM([
        IntentDecision(intent=Intent.generate_requirement),
        IntentDecision(intent=Intent.generate_requirement),
    ])
    plan_step_llm = FakeSequenceLLM(["plan braking", "plan speed"])
    generate_llm = FakeSequenceLLM([VALID_BRAKING, VALID_SPEED])

    outer_thread_id = f"outer-{uuid.uuid4()}"
    expected_middle_thread_ids = {f"{session.id}:middle:task-1", f"{session.id}:middle:task-2"}

    async with build_production_checkpointer() as checkpointer:
        with _MultiPatch(patches(top_llm=top_llm, plan_llm=plan_llm, middle_llm=middle_llm,
                                  layer3_supervisor_llm=layer3_supervisor_llm,
                                  plan_step_llm=plan_step_llm, generate_llm=generate_llm)):
            supervisor_graph = build_supervisor_graph(checkpointer=checkpointer)
            config = build_supervisor_config(outer_thread_id)

            result_1 = await supervisor_graph.ainvoke(
                {"user_input": "a braking operational requirement, then a speed operational requirement",
                 "session_id": session.id},
                config,
            )
            result_2 = await supervisor_graph.ainvoke(Command(resume={"action": "approve"}), config)
            result_3 = await supervisor_graph.ainvoke(Command(resume={"action": "approve"}), config)

        assert not result_3.get("__interrupt__")

        async with async_session_factory() as db:
            rows = (await db.execute(
                text("SELECT DISTINCT thread_id FROM checkpoints WHERE thread_id LIKE :pat"),
                {"pat": f"{session.id}:middle:%"},
            )).fetchall()
        found = {r[0] for r in rows}
        assert expected_middle_thread_ids.issubset(found), (
            f"expected {expected_middle_thread_ids} in Postgres checkpoints, found {found}"
        )
        assert outer_thread_id not in found
        print(f"assert OK: each task's Layer-1 -> Layer-2 dispatch has its OWN distinct thread id, "
              f"present as DISTINCT rows in Postgres checkpoints: {sorted(found)}")

    await clear_checkpoints()
    await cleanup_user(user)
    print("Scenario 6 PASSED")


async def main() -> None:
    await test_single_task_three_level_bubble()
    await test_multi_task_no_stop_between()
    await test_dependency_order_respected()
    await test_stacked_interrupts_across_layers()
    await test_guard_stops_mid_execution()
    await test_distinct_thread_ids_per_task()
    print("\n=== SUPERVISOR EXECUTION LOOP (LAYER-1 STEP 3) TEST SUITE PASSED ===")


if __name__ == "__main__":
    asyncio.run(main())
