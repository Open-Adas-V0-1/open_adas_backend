"""Fix: one TODO task = one processing (Layer-2 no longer re-generates the same task).

Full end-to-end integration testing (scripts/smoke_test_e2e_full_integration.py)
revealed that Layer-2's middle_supervisor, driven by a REAL model, could re-judge an
already-satisfied single TODO task as "still actionable" and dispatch it again --
duplicating the requested artifact before its own loop guard (SYSML_MIDDLE_MAX_VISITS)
eventually stopped it.

The fix (agents/sysml/middle_nodes.py): a delegated task's target (intent/level/
diagram_type) is LOCKED the first time middle_supervisor makes a real, dispatch-worthy
judgement -- but ONLY when this middle graph invocation came from Layer-1's
sysml_middle_node (state["single_task_dispatch"] is True; see supervisor/graph.py).
Once locked, re-entering middle_supervisor (after a processing loops back) is a
DETERMINISTIC check -- did the processing that just finished satisfy the locked target
(target_fulfilled, set by sysml_processing from the already-resolved ProcessingInput,
no LLM call) -- never a fresh "is there still work?" LLM judgement on the same static
task text. If not yet fulfilled, the processing that ran was resolve_level's bounded,
purposeful MISSING-PREREQUISITE step (e.g. a functional task with no operational source
yet generates the operational first) -- the loop resumes deterministically toward the
ORIGINAL target, not by asking the LLM again. Layer-2's OWN standalone contract (driven
directly, without Layer-1, where ONE free-form message may legitimately describe
SEVERAL distinct asks) is untouched: without single_task_dispatch, middle_supervisor
keeps its original open-ended re-judgement -- see scripts/smoke_test_layer2_integration
.py's "sequential levels" scenario, still passing unchanged.

REAL (unstubbed) LLM calls throughout -- the bug only reproduced under real model
non-determinism, so the fix is proven the same way. The one exception (agents.sysml.
nodes.validate / .to_mermaid) stays stubbed for the same Windows/AsyncPostgresSaver
event-loop reason documented in every integration test in this repo.

Run: python -m scripts.smoke_test_task_completion_fix
"""
import asyncio
import sys
import uuid
from unittest.mock import patch

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

from langchain_core.exceptions import OutputParserException  # noqa: E402
from langgraph.types import Command  # noqa: E402
from sqlalchemy import text  # noqa: E402

from app.config import get_settings  # noqa: E402
from data.db import async_session_factory  # noqa: E402
from data.models import RequirementLevel  # noqa: E402
from data.repository import DiagramRepo, ProjectRepo, RequirementRepo, SessionRepo, UserRepo  # noqa: E402
from harness.checkpointer import build_production_checkpointer  # noqa: E402
from supervisor.graph import build_supervisor_config, build_supervisor_graph  # noqa: E402


async def fake_validate(text_):
    return []


async def fake_to_mermaid(text_):
    return "graph TD; A-->B;"


async def setup_user_project_session(label: str):
    async with async_session_factory() as db:
        user = await UserRepo.create(db, email=f"taskfix-{label}-{uuid.uuid4()}@test.dev", password_hash="hashed")
        project = await ProjectRepo.create(db, user_id=user.id, name=f"TaskFix {label}")
        session = await SessionRepo.create(
            db, project_id=project.id, thread_id=str(uuid.uuid4()), title=f"TaskFix {label}"
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


async def ainvoke_resilient(graph, arg, config, attempts=4):
    """Test-level resilience for the real gateway's occasional structured-output parse
    flakiness (see smoke_test_e2e_full_integration.py's docstring) -- NOT related to
    the fix under test.
    """
    last_exc = None
    for attempt in range(1, attempts + 1):
        try:
            return await graph.ainvoke(arg, config)
        except (OutputParserException, RuntimeError) as exc:
            # RuntimeError: the SAME gateway flakiness, a different failure shape --
            # top_level_supervisor's streamed structured-output call (T6b Step 3a)
            # occasionally yields an EMPTY stream (no chunks, no exception from the
            # provider) instead of a parse error. supervisor/router.py raises this
            # exact RuntimeError for that case so it's retryable here the same way
            # (see smoke_test_e2e_full_integration.py's identical ainvoke_resilient).
            last_exc = exc
            print(f"    [gateway flake, attempt {attempt}/{attempts}] {type(exc).__name__}, "
                  f"retrying: {str(exc)[:150]}")
            await asyncio.sleep(1.5 * attempt)
    raise last_exc


def _resume_for(payload: dict) -> dict:
    pattern = payload.get("pattern")
    if pattern == "plan_review":
        return {"action": "approve"}
    if pattern == "select_requirements_for_diagram":
        return {"action": "confirm", "select_all": True}
    if pattern == "select_requirement":
        options = payload.get("options") or []
        if options:
            return {"action": "confirm", "selected_id": options[0]["id"]}
        return {"action": "cancel"}
    if pattern:
        return {"action": "confirm"}
    if payload.get("type") == "requirement_review":
        return {"action": "approve"}
    if payload.get("type") == "plan_clarify":
        # plan_node's own insufficiency interrupt (Layer-1, Step 2) -- distinct resume
        # contract: {"user_input": <clarified text>}, not {"action": ...}.
        return {"user_input": "Generate a SysML v2 use case diagram representing the existing requirement in this session."}
    return {"action": "approve"}


async def drive_to_completion(graph, config, result, max_steps=15):
    hops = []
    steps = 0
    while result.get("__interrupt__") and steps < max_steps:
        payload = result["__interrupt__"][0].value
        label = payload.get("pattern") or payload.get("type") or "unknown"
        resume = _resume_for(payload)
        hops.append(label)
        print(f"    interrupt: {label!r} -> resuming {resume}")
        result = await ainvoke_resilient(graph, Command(resume=resume), config)
        steps += 1
    if result.get("__interrupt__"):
        print(f"    WARNING: still interrupted after {max_steps} hops -- payload="
              f"{result['__interrupt__'][0].value}")
    return result, hops


# ---------------------------------------------------------------------------
# Test 1: single requirement task -> EXACTLY ONE requirement finalized. Re-run 3x for
# robustness against real-model non-determinism -- assert exactly one EACH time, at the
# DEFAULT SYSML_MIDDLE_MAX_VISITS (10) -- the completion condition must stop the
# duplication itself, not a tightened guard (DoD #1 and #5).
# ---------------------------------------------------------------------------
async def test_single_requirement_task_exactly_once(checkpointer, run_number):
    print(f"\n--- Test 1 (run {run_number}): single requirement task -> exactly ONE requirement ---")
    user, session = await setup_user_project_session(f"single-req-{run_number}")

    with patch("agents.sysml.nodes.validate", side_effect=fake_validate), \
         patch("agents.sysml.nodes.to_mermaid", side_effect=fake_to_mermaid):
        supervisor_graph = build_supervisor_graph(checkpointer=checkpointer)
        outer_thread_id = f"outer-{uuid.uuid4()}"
        config = build_supervisor_config(outer_thread_id)

        result_0 = await ainvoke_resilient(
            supervisor_graph,
            {
                "user_input": (
                    "Define a high-level operational requirement stating that the vehicle "
                    "shall come to a complete stop within the available road distance ahead "
                    "when the driver applies the brake."
                ),
                "session_id": session.id,
            },
            config,
        )
        result, hops = await drive_to_completion(supervisor_graph, config, result_0)

    assert result.get("done") is True
    assert result.get("result") == "execution_complete", (
        f"expected the completion condition (not the guard) to end the task -- got {result.get('result')!r}"
    )
    task = result["plan_state"]["tasks"][0]
    assert task["status"] == "done" and task["result_ref"] is not None

    async with async_session_factory() as db:
        rows = await RequirementRepo.list_by_session(db, session_id=session.id)
    assert len(rows) == 1, f"expected EXACTLY 1 requirement, found {len(rows)}: {[str(r.id) for r in rows]}"
    assert rows[0].level == RequirementLevel.operational
    assert str(rows[0].id) == task["result_ref"]["artifact_id"]
    print(f"  assert OK: exactly 1 requirement finalized (id={rows[0].id}), hops={hops}, "
          f"result={result.get('result')!r} -- no duplication, default guard never needed to fire")

    await cleanup_user(user)
    print(f"Test 1 (run {run_number}) PASSED")


# ---------------------------------------------------------------------------
# Test 2: single diagram task -> exactly one diagram finalized (no duplicate).
# ---------------------------------------------------------------------------
async def test_single_diagram_task_exactly_once(checkpointer):
    print("\n--- Test 2: single diagram task -> exactly ONE diagram ---")
    user, session = await setup_user_project_session("single-diagram")

    async with async_session_factory() as db:
        req = await RequirementRepo.finalize(
            db, session_id=session.id,
            content="package Braking { requirement def BrakingReq { doc /* braking */ subject s : ScalarValues::Boolean; require constraint { true } } }",
            level=RequirementLevel.operational,
        )
        await db.commit()
    print(f"  pre-seeded 1 active requirement: {req.id} (unambiguous diagram target)")

    with patch("agents.sysml.nodes.validate", side_effect=fake_validate), \
         patch("agents.sysml.nodes.to_mermaid", side_effect=fake_to_mermaid):
        supervisor_graph = build_supervisor_graph(checkpointer=checkpointer)
        outer_thread_id = f"outer-{uuid.uuid4()}"
        config = build_supervisor_config(outer_thread_id)

        result_0 = await ainvoke_resilient(
            supervisor_graph,
            {
                "user_input": (
                    "Generate a SysML v2 use case diagram representing the existing braking "
                    "operational requirement already recorded in this session."
                ),
                "session_id": session.id,
            },
            config,
        )
        result, hops = await drive_to_completion(supervisor_graph, config, result_0)

    assert result.get("done") is True
    assert result.get("result") == "execution_complete"
    task = result["plan_state"]["tasks"][0]
    assert task["status"] == "done"

    async with async_session_factory() as db:
        diagrams = await DiagramRepo.get_by_requirement(db, requirement_id=req.id, session_id=session.id)
    assert len(diagrams) == 1, f"expected EXACTLY 1 diagram, found {len(diagrams)}"
    print(f"  assert OK: exactly 1 diagram finalized (id={diagrams[0].id}), hops={hops}")

    await cleanup_user(user)
    print("Test 2 PASSED")


# ---------------------------------------------------------------------------
# Test 3: missing-prerequisite case PRESERVED -- a functional task with NO operational
# in the thread -> exactly TWO artifacts (operational prerequisite, then functional),
# correctly ordered and derived. Not more (the bounded sub-sequence must not repeat).
# ---------------------------------------------------------------------------
async def test_missing_prerequisite_exactly_two(checkpointer):
    print("\n--- Test 3: missing prerequisite -> exactly TWO artifacts (operational, then functional) ---")
    user, session = await setup_user_project_session("prereq")

    with patch("agents.sysml.nodes.validate", side_effect=fake_validate), \
         patch("agents.sysml.nodes.to_mermaid", side_effect=fake_to_mermaid):
        supervisor_graph = build_supervisor_graph(checkpointer=checkpointer)
        outer_thread_id = f"outer-{uuid.uuid4()}"
        config = build_supervisor_config(outer_thread_id)

        result_0 = await ainvoke_resilient(
            supervisor_graph,
            {
                # Deliberately does NOT say "functional"/"operational" anywhere: that
                # word, once in the task text, flows UNCHANGED into every one of
                # Layer-2's internal processings (Layer-1's dispatch text is fixed for
                # the whole task) and into Layer-3's OWN independent level
                # classification (agents/sysml/nodes.py's sysml_supervisor derives
                # level from raw text too) -- which can re-assert it even for the
                # prerequisite (operational) processing, a real but PRE-EXISTING
                # Layer-3 characteristic outside this fix's scope. Letting the system's
                # own default (unset -> functional, see resolve_level) carry the
                # request keeps the prerequisite step's own classification clean.
                "user_input": (
                    "Generate a requirement describing how the vehicle's braking function "
                    "achieves its overall stopping-distance need, building on that existing need."
                ),
                "session_id": session.id,
            },
            config,
        )
        result, hops = await drive_to_completion(supervisor_graph, config, result_0, max_steps=20)

    assert result.get("done") is True
    assert result.get("result") == "execution_complete"
    print(f"  hops taken: {hops}")

    async with async_session_factory() as db:
        rows = await RequirementRepo.list_by_session(db, session_id=session.id)
    by_level = {r.level.value: r for r in rows}
    print(f"  levels finalized: {sorted(by_level.keys())}")
    assert len(rows) == 2, f"expected EXACTLY 2 requirements (prerequisite + target), found {len(rows)}: {rows}"
    assert "operational" in by_level and "functional" in by_level, (
        f"expected BOTH operational (prerequisite) and functional (target), got {sorted(by_level.keys())}"
    )
    assert by_level["operational"].created_at <= by_level["functional"].created_at, (
        "expected the prerequisite (operational) to be finalized BEFORE the target (functional)"
    )
    print(f"  assert OK: exactly 2 artifacts -- operational (prerequisite, id={by_level['operational'].id}) "
          f"finalized before functional (target, id={by_level['functional'].id}) -- bounded, not repeated")

    await cleanup_user(user)
    print("Test 3 PASSED")


# ---------------------------------------------------------------------------
# Test 4: multi-task turn (Layer-1's own TODO list) -- each DISTINCT task still runs
# exactly once, in order (the fix must not collapse legitimately distinct tasks).
# ---------------------------------------------------------------------------
async def test_multi_task_each_once(checkpointer):
    print("\n--- Test 4: multi-task turn -- each distinct task runs exactly once, in order ---")
    user, session = await setup_user_project_session("multi")

    with patch("agents.sysml.nodes.validate", side_effect=fake_validate), \
         patch("agents.sysml.nodes.to_mermaid", side_effect=fake_to_mermaid):
        supervisor_graph = build_supervisor_graph(checkpointer=checkpointer)
        outer_thread_id = f"outer-{uuid.uuid4()}"
        config = build_supervisor_config(outer_thread_id)

        result_0 = await ainvoke_resilient(
            supervisor_graph,
            {
                "user_input": (
                    "I need three things, in this order. First, generate an operational "
                    "requirement stating that the vehicle shall stop safely within the "
                    "available road distance when braking. Second, generate the functional "
                    "requirement derived from that operational requirement. Third, generate "
                    "a use_case diagram of that functional requirement."
                ),
                "session_id": session.id,
            },
            config,
        )
        plan_state_0 = result_0.get("plan_state")
        task_count = len(plan_state_0["tasks"]) if plan_state_0 else 0
        print(f"  plan_node decomposed into {task_count} task(s)")

        result, hops = await drive_to_completion(supervisor_graph, config, result_0, max_steps=25)
        print(f"  hops taken: {hops}")

    assert result.get("done") is True
    assert result.get("result") == "execution_complete"
    tasks = result["plan_state"]["tasks"]
    assert all(t["status"] == "done" for t in tasks)
    for t in tasks:
        assert t.get("result_ref") is not None

    async with async_session_factory() as db:
        rows = await RequirementRepo.list_by_session(db, session_id=session.id)
        by_level = {r.level.value: r for r in rows}
        diagrams = []
        for r in rows:
            diagrams.extend(await DiagramRepo.get_by_requirement(db, requirement_id=r.id, session_id=session.id))

    print(f"  requirements finalized: {sorted(by_level.keys())} (count={len(rows)})")
    print(f"  diagrams finalized: {len(diagrams)}")
    assert len(rows) == 2, f"expected EXACTLY 2 requirements (operational + functional), found {len(rows)}"
    assert sorted(by_level.keys()) == ["functional", "operational"]
    assert len(diagrams) == 1, f"expected EXACTLY 1 diagram, found {len(diagrams)}"
    print(f"  assert OK: {len(tasks)} distinct tasks -> exactly 2 requirements + 1 diagram -- "
          f"no duplication of any individual task")

    await cleanup_user(user)
    print("Test 4 PASSED")


async def main() -> None:
    print("Real LLM backend:", get_settings().llm_backend, get_settings().llm_model_id)
    async with build_production_checkpointer() as checkpointer:
        for run_number in (1, 2, 3):
            await test_single_requirement_task_exactly_once(checkpointer, run_number)
        await test_single_diagram_task_exactly_once(checkpointer)
        await test_missing_prerequisite_exactly_two(checkpointer)
        await test_multi_task_each_once(checkpointer)

    await clear_checkpoints()
    print("\n=== TASK-COMPLETION FIX TEST SUITE PASSED (exactly-once generation proven) ===")


if __name__ == "__main__":
    asyncio.run(main())
