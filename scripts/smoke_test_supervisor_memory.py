"""Layer-1 rebuild, Step 5: conditional memory_optimization + finalize_turn.

Proves every normal-completion turn now exits through finalize_turn instead of
route_from_top_supervisor returning END directly, and that memory_optimization is a
CONDITIONAL detour off that same routing decision -- only entered when the estimated
short-term context usage is near its configured budget (MEMORY_OPT_THRESHOLD_RATIO of
MEMORY_SHORT_TERM_BUDGET_TOKENS), both env-driven. Summarization itself stays DEFERRED
(memory_optimization is a pass-through node); only the routing is exercised here.

Uses the SAME production checkpointer as Steps 1-4 (encrypted, durability-configured).

Run: python -m scripts.smoke_test_supervisor_memory
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
from app.schemas.sysml import Intent, IntentDecision, MiddleDecision  # noqa: E402
from app.schemas.supervisor import HubClassification, HubDecision, PlanDecision, PlannedTask  # noqa: E402
from data.db import async_session_factory  # noqa: E402
from data.models import RequirementLevel  # noqa: E402
from data.repository import ProjectRepo, SessionRepo, UserRepo  # noqa: E402
from harness.checkpointer import build_production_checkpointer  # noqa: E402
from supervisor.graph import build_supervisor_config, build_supervisor_graph  # noqa: E402

VALID_BRAKING = "package Braking { requirement def BrakingReq { doc /* braking */ subject s : ScalarValues::Boolean; require constraint { true } } }"


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
        user = await UserRepo.create(db, email=f"mem-{label}-{uuid.uuid4()}@test.dev", password_hash="hashed")
        project = await ProjectRepo.create(db, user_id=user.id, name=f"Mem {label}")
        session = await SessionRepo.create(
            db, project_id=project.id, thread_id=str(uuid.uuid4()), title=f"Mem {label}"
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


def patches(top_llm=None, plan_llm=None, middle_llm=None, layer3_supervisor_llm=None,
            plan_step_llm=None, generate_llm=None):
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
        patch("agents.sysml.nodes.validate", return_value=[]),
    ]
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
# Scenario 1: normal turn (context well below threshold) -- simple_response ->
# top_level_supervisor -> finalize_turn -> END, WITHOUT entering memory_optimization.
# ---------------------------------------------------------------------------
async def test_direct_to_finalize_below_threshold():
    print("\n--- Scenario 1: context below threshold -- straight to finalize_turn, no memory_optimization ---")
    user, session = await setup_user_project_session("direct")

    top_llm = FakeStructuredWrapperLLM(
        HubDecision(classification=HubClassification.simple_response, response="Hi there!")
    )

    outer_thread_id = f"outer-{uuid.uuid4()}"

    entered_memory_optimization = {"value": False}
    import supervisor.graph as graph_module
    real_memory_optimization = graph_module.memory_optimization

    async def tracing_memory_optimization(state):
        entered_memory_optimization["value"] = True
        return await real_memory_optimization(state)

    async with build_production_checkpointer() as checkpointer:
        with _MultiPatch(patches(top_llm=top_llm)), \
             patch("supervisor.graph.memory_optimization", side_effect=tracing_memory_optimization):
            supervisor_graph = build_supervisor_graph(checkpointer=checkpointer)
            config = build_supervisor_config(outer_thread_id)

            result = await supervisor_graph.ainvoke(
                {"user_input": "hello", "session_id": session.id}, config
            )

    assert not result.get("__interrupt__")
    assert result.get("done") is True
    assert result.get("result") == "simple_response", (
        "finalize_turn must NOT clobber the result already set upstream"
    )
    assert entered_memory_optimization["value"] is False, (
        "memory_optimization must NOT be entered when the context is below threshold"
    )
    print(f"assert OK: done={result.get('done')} result={result.get('result')!r}, "
          f"memory_optimization entered={entered_memory_optimization['value']} -- "
          f"turn went top_level_supervisor -> finalize_turn -> END directly")

    await clear_checkpoints()
    await cleanup_user(user)
    print("Scenario 1 PASSED")


# ---------------------------------------------------------------------------
# Scenario 2: near-full context (budget forced tiny via env) -- routes through
# memory_optimization (pass-through) -> finalize_turn -> END.
# ---------------------------------------------------------------------------
async def test_near_full_routes_through_memory_optimization():
    print("\n--- Scenario 2: near-full context (env-forced) -- routes through memory_optimization ---")
    user, session = await setup_user_project_session("nearfull")

    top_llm = FakeStructuredWrapperLLM(
        HubDecision(classification=HubClassification.simple_response, response="Sure, here you go.")
    )

    outer_thread_id = f"outer-{uuid.uuid4()}"

    # Force the near-full estimate: a tiny budget means even a short response/user_input
    # crosses MEMORY_OPT_THRESHOLD_RATIO. Same env-override + cache_clear pattern as the
    # existing SUPERVISOR_MAX_VISITS guard test.
    os.environ["MEMORY_SHORT_TERM_BUDGET_TOKENS"] = "1"
    os.environ["MEMORY_OPT_THRESHOLD_RATIO"] = "0.8"
    get_settings.cache_clear()
    assert get_settings().memory_short_term_budget_tokens == 1
    assert get_settings().memory_opt_threshold_ratio == 0.8

    # Trace that memory_optimization actually ran, by wrapping the real node (patched
    # in at the graph module's import site, where build_supervisor_graph looks it up).
    entered_memory_optimization = {"value": False}

    import supervisor.graph as graph_module
    real_memory_optimization = graph_module.memory_optimization

    async def tracing_memory_optimization(state):
        entered_memory_optimization["value"] = True
        return await real_memory_optimization(state)

    try:
        async with build_production_checkpointer() as checkpointer:
            with _MultiPatch(patches(top_llm=top_llm)), \
                 patch("supervisor.graph.memory_optimization", side_effect=tracing_memory_optimization):
                supervisor_graph = build_supervisor_graph(checkpointer=checkpointer)
                config = build_supervisor_config(outer_thread_id)

                result = await supervisor_graph.ainvoke(
                    {"user_input": "hello there, a longer message to be safe", "session_id": session.id}, config
                )
    finally:
        os.environ.pop("MEMORY_SHORT_TERM_BUDGET_TOKENS", None)
        os.environ.pop("MEMORY_OPT_THRESHOLD_RATIO", None)
        get_settings.cache_clear()

    assert not result.get("__interrupt__")
    assert result.get("done") is True
    assert result.get("result") == "simple_response"
    assert entered_memory_optimization["value"] is True, (
        "expected memory_optimization to be entered when the near-full check trips"
    )
    print(f"assert OK: memory_optimization entered={entered_memory_optimization['value']}, "
          f"then finalize_turn -> END, done={result.get('done')} result={result.get('result')!r}")

    await clear_checkpoints()
    await cleanup_user(user)
    print("Scenario 2 PASSED")


# ---------------------------------------------------------------------------
# Scenario 3: threshold is read from env -- confirms the router's decision flips purely
# by env var, same input state, same code path.
# ---------------------------------------------------------------------------
async def test_threshold_read_from_env():
    print("\n--- Scenario 3: threshold + budget are env-driven (router flips with the SAME state) ---")
    from supervisor.memory_ops import is_context_near_full

    state = {"user_input": "a moderately long message to estimate", "response": "a moderately long reply back"}

    os.environ["MEMORY_SHORT_TERM_BUDGET_TOKENS"] = "8000"
    os.environ["MEMORY_OPT_THRESHOLD_RATIO"] = "0.8"
    get_settings.cache_clear()
    try:
        assert is_context_near_full(state) is False, "large budget -> nowhere near full"
        print("assert OK: large budget (8000) -> is_context_near_full=False")
    finally:
        os.environ.pop("MEMORY_SHORT_TERM_BUDGET_TOKENS", None)
        os.environ.pop("MEMORY_OPT_THRESHOLD_RATIO", None)
        get_settings.cache_clear()

    os.environ["MEMORY_SHORT_TERM_BUDGET_TOKENS"] = "1"
    os.environ["MEMORY_OPT_THRESHOLD_RATIO"] = "0.8"
    get_settings.cache_clear()
    try:
        assert is_context_near_full(state) is True, "tiny budget -> near full"
        print("assert OK: tiny budget (1) -> is_context_near_full=True, SAME state, purely env-driven")
    finally:
        os.environ.pop("MEMORY_SHORT_TERM_BUDGET_TOKENS", None)
        os.environ.pop("MEMORY_OPT_THRESHOLD_RATIO", None)
        get_settings.cache_clear()

    print("Scenario 3 PASSED")


# ---------------------------------------------------------------------------
# Scenario 4: Steps 1-4 flows still complete correctly through finalize_turn --
# single-task execution, multi-task (plan_review), and unclear.
# ---------------------------------------------------------------------------
async def test_steps_1_to_4_still_complete_through_finalize():
    print("\n--- Scenario 4: Steps 1-4 flows still end correctly through finalize_turn ---")

    # --- unclear (Step 1) ---
    user_u, session_u = await setup_user_project_session("unclear")
    top_llm_u = FakeStructuredWrapperLLM(
        HubDecision(classification=HubClassification.unclear, response="Could you clarify?")
    )
    outer_u = f"outer-{uuid.uuid4()}"
    async with build_production_checkpointer() as checkpointer:
        with _MultiPatch(patches(top_llm=top_llm_u)):
            supervisor_graph = build_supervisor_graph(checkpointer=checkpointer)
            config = build_supervisor_config(outer_u)
            result_u = await supervisor_graph.ainvoke(
                {"user_input": "asdkjh zzz ???", "session_id": session_u.id}, config
            )
    assert result_u.get("done") is True and result_u.get("result") == "unclear"
    print(f"assert OK: unclear (Step 1) -> done={result_u.get('done')} result={result_u.get('result')!r}")
    await clear_checkpoints()
    await cleanup_user(user_u)

    # --- single-task execution (Steps 2-3) ---
    user_s, session_s = await setup_user_project_session("single")
    top_llm_s = FakeStructuredWrapperLLM(HubDecision(classification=HubClassification.needs_execution))
    plan_llm_s = FakeStructuredWrapperLLM(PlanDecision(
        sufficient=True,
        tasks=[PlannedTask(description="Generate an operational requirement for braking.",
                            intent="generate_requirement", level=RequirementLevel.operational)],
    ))
    middle_llm_s = FakeStructuredWrapperLLM([
        MiddleDecision(has_request=True, resolved_intent=Intent.generate_requirement, level=RequirementLevel.operational),
    ])
    layer3_supervisor_llm_s = FakeStructuredWrapperLLM(IntentDecision(intent=Intent.generate_requirement))
    plan_step_llm_s = FakeSequenceLLM(["plan"])
    generate_llm_s = FakeSequenceLLM([VALID_BRAKING])

    outer_s = f"outer-{uuid.uuid4()}"
    async with build_production_checkpointer() as checkpointer:
        with _MultiPatch(patches(top_llm=top_llm_s, plan_llm=plan_llm_s, middle_llm=middle_llm_s,
                                  layer3_supervisor_llm=layer3_supervisor_llm_s,
                                  plan_step_llm=plan_step_llm_s, generate_llm=generate_llm_s)):
            supervisor_graph = build_supervisor_graph(checkpointer=checkpointer)
            config = build_supervisor_config(outer_s)
            result_s0 = await supervisor_graph.ainvoke(
                {"user_input": "generate an operational requirement for braking", "session_id": session_s.id}, config
            )
            assert result_s0.get("__interrupt__")
            result_s1 = await supervisor_graph.ainvoke(Command(resume={"action": "approve"}), config)
    assert result_s1.get("done") is True and result_s1.get("result") == "execution_complete"
    assert result_s1["plan_state"]["tasks"][0]["status"] == "done"
    print(f"assert OK: single-task execution (Steps 2-3) -> done={result_s1.get('done')} "
          f"result={result_s1.get('result')!r}")
    await clear_checkpoints()
    await cleanup_user(user_s)

    # --- multi-task plan_review (Step 4) ---
    user_m, session_m = await setup_user_project_session("multi")
    top_llm_m = FakeStructuredWrapperLLM(HubDecision(classification=HubClassification.needs_execution))
    plan_llm_m = FakeStructuredWrapperLLM(PlanDecision(
        sufficient=True,
        tasks=[
            PlannedTask(description="Generate a braking operational requirement.",
                        intent="generate_requirement", level=RequirementLevel.operational),
            PlannedTask(description="Generate a speed operational requirement.",
                        intent="generate_requirement", level=RequirementLevel.operational),
        ],
    ))
    middle_llm_m = FakeStructuredWrapperLLM([
        MiddleDecision(has_request=True, resolved_intent=Intent.generate_requirement, level=RequirementLevel.operational),
        MiddleDecision(has_request=True, resolved_intent=Intent.generate_requirement, level=RequirementLevel.operational),
    ])
    layer3_supervisor_llm_m = FakeStructuredWrapperLLM([
        IntentDecision(intent=Intent.generate_requirement),
        IntentDecision(intent=Intent.generate_requirement),
    ])
    plan_step_llm_m = FakeSequenceLLM(["plan braking", "plan speed"])
    generate_llm_m = FakeSequenceLLM([VALID_BRAKING, "package Speed { requirement def SpeedReq { doc /* speed */ subject s : ScalarValues::Boolean; require constraint { true } } }"])

    outer_m = f"outer-{uuid.uuid4()}"
    async with build_production_checkpointer() as checkpointer:
        with _MultiPatch(patches(top_llm=top_llm_m, plan_llm=plan_llm_m, middle_llm=middle_llm_m,
                                  layer3_supervisor_llm=layer3_supervisor_llm_m,
                                  plan_step_llm=plan_step_llm_m, generate_llm=generate_llm_m)):
            supervisor_graph = build_supervisor_graph(checkpointer=checkpointer)
            config = build_supervisor_config(outer_m)
            result_m0 = await supervisor_graph.ainvoke(
                {"user_input": "a braking operational requirement, then a speed operational requirement",
                 "session_id": session_m.id}, config
            )
            assert result_m0["__interrupt__"][0].value["pattern"] == "plan_review"
            result_m1 = await supervisor_graph.ainvoke(Command(resume={"action": "approve"}), config)
            result_m2 = await supervisor_graph.ainvoke(Command(resume={"action": "approve"}), config)
            result_m3 = await supervisor_graph.ainvoke(Command(resume={"action": "approve"}), config)
    assert result_m3.get("done") is True and result_m3.get("result") == "execution_complete"
    t1, t2 = result_m3["plan_state"]["tasks"]
    assert t1["status"] == "done" and t2["status"] == "done"
    print(f"assert OK: multi-task plan_review (Step 4) -> done={result_m3.get('done')} "
          f"result={result_m3.get('result')!r}, both tasks done")
    await clear_checkpoints()
    await cleanup_user(user_m)

    print("Scenario 4 PASSED")


async def main() -> None:
    await test_direct_to_finalize_below_threshold()
    await test_near_full_routes_through_memory_optimization()
    await test_threshold_read_from_env()
    await test_steps_1_to_4_still_complete_through_finalize()
    print("\n=== SUPERVISOR MEMORY OPTIMIZATION (LAYER-1 STEP 5) TEST SUITE PASSED ===")


if __name__ == "__main__":
    asyncio.run(main())
