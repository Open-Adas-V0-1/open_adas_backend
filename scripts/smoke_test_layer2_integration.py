<<<<<<< Updated upstream
"""Layer-2 redesign, Step 5: FULL middle-layer integration test.

Steps 1-4 tested each middle-layer node/feature in isolation (level resolution,
validate_inputs, build_structured_format, conditional user_confirm_inputs). This
script proves the WHOLE middle layer (Layer 2) works as ONE integrated unit, driving
the rebuilt Layer 3, end-to-end on a REAL Postgres checkpointer -- isolated from
Layer 1 (which doesn't exist yet). The test itself owns the ONE Postgres checkpointer,
exactly the role Layer 1 will play later; both subgraphs are compiled WITHOUT their
own checkpointer and inherit this one.

LLM call sites are stubbed (same rationale as every prior Layer-2 step).
agents.sysml.nodes.validate is ALSO stubbed for the Windows event-loop reason
documented in scripts/smoke_test_level_resolution.py.

Run: python -m scripts.smoke_test_layer2_integration
"""
import asyncio
import os
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
from data.models import RequirementLevel, VersionStatus  # noqa: E402
from data.repository import (  # noqa: E402
    DiagramRepo,
    ProjectRepo,
    RequirementRepo,
    SessionRepo,
    ThreadActivityRepo,
    UserRepo,
)
from harness.thread_ttl import touch_thread  # noqa: E402

VALID_OPERATIONAL = "package Ops { requirement def OpReq { doc /* op */ subject s : ScalarValues::Boolean; require constraint { true } } }"
VALID_FUNCTIONAL = "package Func { requirement def FuncReq { doc /* func */ subject s : ScalarValues::Boolean; require constraint { true } } }"
VALID_DIAGRAM = "package UseCases { part def System { } }"


class FakeMessage:
    def __init__(self, content):
        self.content = content


class FakeStructuredLLM:
    """Serves decisions IN SEQUENCE across successive calls -- supports both a
    single decision (repeated) and a list (one per call, clamped at the end).
    """
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


async def setup_session(label: str):
    async with async_session_factory() as db:
        user = await UserRepo.create(db, email=f"l2int-{label}-{uuid.uuid4()}@test.dev", password_hash="hashed")
        project = await ProjectRepo.create(db, user_id=user.id, name=f"L2Integration {label}")
        session = await SessionRepo.create(
            db, project_id=project.id, thread_id=str(uuid.uuid4()), title=f"L2Integration {label}"
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
# Scenario 1: full happy path, two-level nesting. A clear operational request flows
# middle_supervisor -> validate_inputs -> resolve_level -> build_structured_format ->
# sysml_processing -> layer-3 (plan -> generate -> verify -> PAUSE at
# requirement_review). The layer-3 interrupt bubbles from INSIDE layer-3's own graph,
# through sysml_processing's node body (level 1), through the middle graph's runner
# (level 2), to this test's ainvoke call -- exactly the "inside a node" two-level
# bubbling validated by the original spike, now exercising the FULL Step 1-3 pipeline.
# ---------------------------------------------------------------------------
async def test_full_happy_path_two_level_nesting():
    print("\n--- Scenario 1: full happy path, two-level nested interrupt bubbling ---")
    user, session = await setup_session("happy")

    middle_llm = FakeStructuredWrapperLLM([
        MiddleDecision(has_request=True, resolved_intent=Intent.generate_requirement, level=RequirementLevel.operational),
        MiddleDecision(has_request=False, message="nothing further"),
    ])
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

            result_1 = await middle_graph.ainvoke(
                {"user_input": "Define a high-level operational need.", "session_id": session.id}, config
            )
            # The interrupt payload is layer-3's OWN requirement_review shape, surfacing
            # DIRECTLY to this test caller -- proof the bubble crossed both levels intact.
            assert result_1.get("__interrupt__"), "expected the nested layer-3 interrupt to bubble to the caller"
            payload = result_1["__interrupt__"][0].value
            assert payload["type"] == "requirement_review"
            assert payload["level"] == "operational"
            print(f"RUN 1: layer-3's requirement_review interrupt bubbled TWO levels (layer-3 -> "
                  f"sysml_processing node -> middle graph -> test caller). draft={payload['draft'][:40]}...")

            async with async_session_factory() as db:
                rows = await RequirementRepo.list_by_session(db, session_id=session.id)
                assert rows == [], "no DB write before approval"
            print("assert OK: no DB write before approval")

            result_2 = await middle_graph.ainvoke(Command(resume={"action": "approve"}), config)
            light_ref = result_2.get("processing_result")
            assert light_ref["artifact_type"] == "requirement"
            assert set(light_ref.keys()) == {"processing_id", "thread_id", "artifact_type", "artifact_id", "summary"}, (
                "MiddleState must carry only the LIGHT reference, not full content"
            )
            assert not result_2.get("__interrupt__"), "expected the loop to reach END, not pause again"
            print(f"RUN 2: resumed approve -> finalized -> looped middle_supervisor -> END. "
                  f"light_ref={light_ref}")

    async with async_session_factory() as db:
        rows = await RequirementRepo.list_by_session(db, session_id=session.id)
        assert len(rows) == 1 and rows[0].level == RequirementLevel.operational
        assert rows[0].session_id == session.id
        print(f"assert OK: finalized, keyed by thread(session)={session.id} + level=operational, "
              f"id={rows[0].id}")

    await cleanup_user(user)
    print("Scenario 1 PASSED")


# ---------------------------------------------------------------------------
# Scenario 2: confirm-then-process, nested interrupts STACKED. An ambiguous diagram
# request pauses at user_confirm_inputs (a Layer-2 interrupt: payload has "pattern").
# Resume with a selection -> continues into layer-3 which pauses AGAIN at
# requirement_review (a Layer-3 interrupt: payload has "type"). Both interrupts must
# surface correctly, IN SEQUENCE, to this test caller; resuming each must advance
# correctly.
# ---------------------------------------------------------------------------
async def test_stacked_layer2_then_layer3_interrupts():
    print("\n--- Scenario 2: stacked interrupts -- Layer-2 confirm THEN Layer-3 review ---")
    user, session = await setup_session("stacked")

    async with async_session_factory() as db:
        req_a = await RequirementRepo.finalize(db, session_id=session.id, content="req A", level=RequirementLevel.operational)
        req_b = await RequirementRepo.finalize(db, session_id=session.id, content="req B", level=RequirementLevel.operational)
        await db.commit()

    middle_llm = FakeStructuredWrapperLLM([
        MiddleDecision(has_request=True, resolved_intent=Intent.generate_diagram, diagram_type=DiagramType.use_case),
        MiddleDecision(has_request=False, message="nothing further"),
    ])
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
        with patch("agents.sysml.middle_nodes.get_llm", side_effect=fake_middle_get_llm), \
             patch("agents.sysml.nodes.get_llm", side_effect=fake_layer3_get_llm), \
             patch("agents.sysml.nodes.validate", return_value=[]), \
             patch("agents.sysml.nodes.to_mermaid", return_value="graph TD; A-->B;"):

            middle_graph = build_middle_graph(checkpointer=checkpointer)
            config = build_middle_config(outer_thread_id)

            # --- Interrupt #1: Layer-2's user_confirm_inputs ---
            result_1 = await middle_graph.ainvoke(
                {"user_input": "Show a use case diagram.", "session_id": session.id}, config
            )
            assert result_1.get("__interrupt__")
            payload_1 = result_1["__interrupt__"][0].value
            assert "pattern" in payload_1 and payload_1["pattern"] == "select_requirements_for_diagram"
            assert "type" not in payload_1, "this must be Layer-2's confirm interrupt, not Layer-3's"
            print(f"INTERRUPT #1 (Layer-2, user_confirm_inputs): pattern={payload_1['pattern']!r} "
                  f"options={[o['id'] for o in payload_1['options']]}")

            # --- resume #1: select both -> continues INTO layer-3 ---
            result_2 = await middle_graph.ainvoke(
                Command(resume={"action": "confirm", "selected_ids": [str(req_a.id), str(req_b.id)]}), config
            )
            assert result_2.get("__interrupt__"), "expected layer-3 to now pause at requirement_review"
            payload_2 = result_2["__interrupt__"][0].value
            assert payload_2["type"] == "requirement_review"
            assert "pattern" not in payload_2, "this must be Layer-3's review interrupt, not Layer-2's confirm"
            print(f"INTERRUPT #2 (Layer-3, requirement_review): type={payload_2['type']!r} "
                  f"source_node={payload_2['source_node']!r}")

            # --- resume #2: approve -> finalizes ---
            result_3 = await middle_graph.ainvoke(Command(resume={"action": "approve"}), config)
            assert not result_3.get("__interrupt__"), "expected completion, no further pause"
            light_ref = result_3.get("processing_result")
            assert light_ref["artifact_type"] == "diagram"
            print(f"RESUMED both interrupts correctly, in sequence -> finalized. light_ref={light_ref}")

    async with async_session_factory() as db:
        diagrams_a = await DiagramRepo.get_by_requirement(db, requirement_id=req_a.id, session_id=session.id)
        assert len(diagrams_a) == 1 and diagrams_a[0].mermaid
        print(f"assert OK: diagram id={diagrams_a[0].id} finalized with model + mermaid, "
              f"after BOTH stacked interrupts resolved correctly")

    await cleanup_user(user)
    print("Scenario 2 PASSED")


# ---------------------------------------------------------------------------
# Scenario 3: sequential levels across processings in ONE thread. Process an
# operational (finalize), then -- automatically, in the SAME invocation chain, via
# middle_supervisor's own loop -- a functional request: resolve_level auto-resolves
# the operational as source, layer-3 derives + finalizes. Also demonstrates the
# middle_supervisor <-> sysml_processing loop handling MORE THAN ONE processing in a
# single turn (DoD #4's ">1 processing" requirement).
# ---------------------------------------------------------------------------
async def test_sequential_levels_one_thread():
    print("\n--- Scenario 3: sequential levels (operational -> functional) in ONE thread ---")
    user, session = await setup_session("sequential")

    middle_llm = FakeStructuredWrapperLLM([
        MiddleDecision(has_request=True, resolved_intent=Intent.generate_requirement, level=RequirementLevel.operational),
        MiddleDecision(has_request=True, resolved_intent=Intent.generate_requirement, level=RequirementLevel.functional),
        MiddleDecision(has_request=False, message="nothing further"),
    ])
    layer3_supervisor_llm = FakeStructuredWrapperLLM(IntentDecision(intent=Intent.generate_requirement))
    plan_llm = FakeSequenceLLM(["plan op", "plan func"])
    generate_llm = FakeSequenceLLM([VALID_OPERATIONAL, VALID_FUNCTIONAL])

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

            result_1 = await middle_graph.ainvoke(
                {"user_input": "Define the operational need, then the function.", "session_id": session.id}, config
            )
            assert result_1.get("__interrupt__")
            payload_1 = result_1["__interrupt__"][0].value
            assert payload_1["level"] == "operational"
            print(f"PROCESSING 1: paused at layer-3 review, level={payload_1['level']!r}")

            result_2 = await middle_graph.ainvoke(Command(resume={"action": "approve"}), config)
            # middle_supervisor loops automatically (visit 2, still inside this SAME
            # resume call) -> decides functional -> resolve_level auto-resolves the
            # just-finalized operational as source -> layer-3 pauses a SECOND time.
            assert result_2.get("__interrupt__"), "expected the SECOND (functional) processing to pause too"
            payload_2 = result_2["__interrupt__"][0].value
            assert payload_2["level"] == "functional"
            assert result_2.get("requested_level") == "functional"
            assert result_2.get("resolved_source_id") is not None
            print(f"PROCESSING 2 (auto-looped, same turn): paused at layer-3 review, level={payload_2['level']!r}, "
                  f"resolved_source_id={result_2.get('resolved_source_id')!r}, "
                  f"supervisor_visits={result_2.get('supervisor_visits')}")
            assert result_2.get("supervisor_visits") == 2, ">1 processing handled within a single turn"

            result_3 = await middle_graph.ainvoke(Command(resume={"action": "approve"}), config)
            assert not result_3.get("__interrupt__"), "expected the loop to end after the third (no-op) visit"
            print(f"PROCESSING 2 approved -> finalized -> looped to a third (no-op) visit -> END. "
                  f"result={result_3.get('result')!r}")

    async with async_session_factory() as db:
        rows = await RequirementRepo.list_by_session(db, session_id=session.id)
        by_level = {r.level.value: r for r in rows}
        assert sorted(by_level.keys()) == ["functional", "operational"]
        print(f"assert OK: forward progression recorded -- levels present: {sorted(by_level.keys())}")

        level_progress = await RequirementRepo.level_progress(db, session_id=session.id)
        assert level_progress == ["operational", "functional"] or sorted(level_progress) == ["functional", "operational"]
        print(f"assert OK: level_progress reflects both levels in this thread: {level_progress}")

    await cleanup_user(user)
    print("Scenario 3 PASSED")


# ---------------------------------------------------------------------------
# Scenario 4: orchestration loop guard. SYSML_MIDDLE_MAX_VISITS set LOW in the test
# env forces the guard to trip on an ambiguous case needing a second supervisor visit
# -- proving the env-driven guard fires safely (fail-open to END, no crash).
# ---------------------------------------------------------------------------
async def test_guard_fires_safely():
    print("\n--- Scenario 4: SYSML_MIDDLE_MAX_VISITS=1 -> guard fires safely (fail-open) ---")
    user, session = await setup_session("guard")

    async with async_session_factory() as db:
        req_a = await RequirementRepo.finalize(db, session_id=session.id, content="req A", level=RequirementLevel.operational)
        req_b = await RequirementRepo.finalize(db, session_id=session.id, content="req B", level=RequirementLevel.operational)
        await db.commit()

    # generate_diagram (not modify_requirement): its ambiguous-target pause resumes via
    # "modify" below, which routes straight back to middle_supervisor (visit 2) WITHOUT
    # ever touching layer-3 -- exactly where the guard must fire. (A "confirm" resume
    # would instead proceed to build_structured_format -> sysml_processing -> layer-3,
    # which isn't stubbed here and isn't the guard path being tested.)
    middle_llm = FakeStructuredWrapperLLM(
        [MiddleDecision(has_request=True, resolved_intent=Intent.generate_diagram, diagram_type=DiagramType.use_case)]
    )
    confirm_question_llm = FakeSequenceLLM(["Which requirement do you mean?"])

    def fake_middle_get_llm(node_name=None):
        if node_name == "sysml_middle_supervisor":
            return middle_llm
        if node_name == "sysml_confirm_question":
            return confirm_question_llm
        raise AssertionError(f"unexpected node_name in middle_nodes: {node_name}")

    def fake_layer3_get_llm(node_name=None):
        raise AssertionError(f"layer-3 must never run: the guard should stop things first (node_name={node_name})")

    outer_thread_id = f"outer-{uuid.uuid4()}"

    os.environ["SYSML_MIDDLE_MAX_VISITS"] = "1"
    get_settings.cache_clear()
    try:
        assert get_settings().sysml_middle_max_visits == 1
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
                assert result_1.get("__interrupt__"), "visit 1 (<= max_visits=1) should reach the ambiguous pause"
                print(f"visit 1: paused at user_confirm_inputs (ambiguous, allowed under max_visits=1)")

                result_2 = await middle_graph.ainvoke(Command(resume={"action": "modify"}), config)
                assert not result_2.get("__interrupt__"), "guard must fail-open to END, not pause or crash"
                assert result_2.get("result") == "stopped: max supervisor visits reached"
                assert result_2.get("supervisor_visits") == 2
                print(f"visit 2: guard triggered SAFELY (visits=2 > max_visits=1) -> fail-open to END, "
                      f"result={result_2.get('result')!r}, no crash")
    finally:
        os.environ.pop("SYSML_MIDDLE_MAX_VISITS", None)
        get_settings.cache_clear()

    await cleanup_user(user)
    print("Scenario 4 PASSED")


# ---------------------------------------------------------------------------
# Scenario 5: independent per-processing thread + TTL still intact. Each processing
# gets its own DETERMINISTIC thread id, distinct from the outer thread, verified
# directly in the Postgres checkpointer; last_accessed updates on access.
# ---------------------------------------------------------------------------
async def test_independent_thread_and_ttl():
    print("\n--- Scenario 5: independent per-processing thread id + TTL touch, verified in Postgres ---")
    user, session = await setup_session("thread-ttl")

    middle_llm = FakeStructuredWrapperLLM([
        MiddleDecision(has_request=True, resolved_intent=Intent.generate_requirement, level=RequirementLevel.operational),
        MiddleDecision(has_request=False, message="nothing further"),
    ])
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

    # last_accessed BEFORE any invocation (a manual touch, mirroring how the outer
    # thread is first registered) -> then let the real integration touch it again.
    async with async_session_factory() as db:
        await touch_thread(db, thread_id=outer_thread_id, session_id=session.id)
        await db.commit()
        last_accessed_1 = await ThreadActivityRepo.get_last_accessed(db, outer_thread_id)

    await asyncio.sleep(1.1)

    async with AsyncPostgresSaver.from_conn_string(settings.checkpointer_database_url) as checkpointer:
        await checkpointer.setup()
        with patch("agents.sysml.middle_nodes.get_llm", side_effect=fake_middle_get_llm), \
             patch("agents.sysml.nodes.get_llm", side_effect=fake_layer3_get_llm), \
             patch("agents.sysml.nodes.validate", return_value=[]):

            middle_graph = build_middle_graph(checkpointer=checkpointer)
            config = build_middle_config(outer_thread_id)

            result_1 = await middle_graph.ainvoke(
                {"user_input": "Define a high-level operational need.", "session_id": session.id}, config
            )
            assert result_1.get("__interrupt__")
            result_2 = await middle_graph.ainvoke(Command(resume={"action": "approve"}), config)
            proc_thread_id = result_2["processing_result"]["thread_id"]
            assert proc_thread_id != outer_thread_id
            print(f"assert OK: proc_thread_id={proc_thread_id!r} != outer_thread_id={outer_thread_id!r}")

        async with async_session_factory() as db:
            outer_rows = (await db.execute(
                text("SELECT count(*) FROM checkpoints WHERE thread_id = :tid"), {"tid": outer_thread_id}
            )).scalar()
            proc_rows = (await db.execute(
                text("SELECT count(*) FROM checkpoints WHERE thread_id = :tid"), {"tid": proc_thread_id}
            )).scalar()
        assert outer_rows > 0 and proc_rows > 0
        print(f"assert OK: BOTH thread ids have distinct checkpoint rows in Postgres "
              f"(outer={outer_rows}, proc={proc_rows})")

    async with async_session_factory() as db:
        last_accessed_2 = await ThreadActivityRepo.get_last_accessed(db, outer_thread_id)
    assert last_accessed_2 > last_accessed_1, (
        f"expected last_accessed to advance on real access: {last_accessed_1} -> {last_accessed_2}"
    )
    print(f"assert OK: last_accessed advanced on real access ({last_accessed_1} -> {last_accessed_2})")

    await cleanup_user(user)
    print("Scenario 5 PASSED")


# ---------------------------------------------------------------------------
# Scenario 6: diagram path end-to-end (single, unambiguous candidate -- no confirm
# needed). layer-3 produces the SysML v2 model AND the derived Mermaid; finalize
# stores both.
# ---------------------------------------------------------------------------
async def test_diagram_path_end_to_end():
    print("\n--- Scenario 6: diagram path end-to-end -- model + derived Mermaid, both persisted ---")
    user, session = await setup_session("diagram")

    async with async_session_factory() as db:
        req = await RequirementRepo.finalize(db, session_id=session.id, content=VALID_OPERATIONAL, level=RequirementLevel.operational)
        await db.commit()

    middle_llm = FakeStructuredWrapperLLM([
        MiddleDecision(has_request=True, resolved_intent=Intent.generate_diagram, diagram_type=DiagramType.state_machine),
        MiddleDecision(has_request=False, message="nothing further"),
    ])
    layer3_supervisor_llm = FakeStructuredWrapperLLM(
        IntentDecision(intent=Intent.generate_diagram, diagram_type=DiagramType.state_machine)
    )
    plan_llm = FakeSequenceLLM(["plan"])
    generate_llm = FakeSequenceLLM([VALID_DIAGRAM])

    def fake_middle_get_llm(node_name=None):
        if node_name == "sysml_middle_supervisor":
            return middle_llm
        if node_name == "sysml_confirm_question":
            raise AssertionError("single candidate must NOT require a confirm step")
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
             patch("agents.sysml.nodes.validate", return_value=[]), \
             patch("agents.sysml.nodes.to_mermaid", return_value="stateDiagram-v2\n[*] --> Idle"):

            middle_graph = build_middle_graph(checkpointer=checkpointer)
            config = build_middle_config(outer_thread_id)

            result_1 = await middle_graph.ainvoke(
                {"user_input": "Show a state machine diagram for this requirement.", "session_id": session.id}, config
            )
            assert result_1.get("__interrupt__")
            payload = result_1["__interrupt__"][0].value
            assert payload["source_node"] == "diagram"
            assert payload["mermaid"] is not None
            print(f"assert OK: layer-3 derived Mermaid BEFORE human review too (verify_node's job). "
                  f"mermaid_preview={payload['mermaid'][:30]!r}")

            result_2 = await middle_graph.ainvoke(Command(resume={"action": "approve"}), config)
            assert not result_2.get("__interrupt__")

    async with async_session_factory() as db:
        diagrams = await DiagramRepo.get_by_requirement(db, requirement_id=req.id, session_id=session.id)
        assert len(diagrams) == 1
        diagram = diagrams[0]
        assert diagram.status == VersionStatus.active
        assert diagram.sysml_text and diagram.mermaid
        print(f"assert OK: finalize stored BOTH the SysML v2 model (len={len(diagram.sysml_text)}) "
              f"AND the derived Mermaid (len={len(diagram.mermaid)}) for diagram id={diagram.id}")

    await cleanup_user(user)
    print("Scenario 6 PASSED")


async def main() -> None:
    await test_full_happy_path_two_level_nesting()
    await clear_checkpoints()
    await test_stacked_layer2_then_layer3_interrupts()
    await clear_checkpoints()
    await test_sequential_levels_one_thread()
    await clear_checkpoints()
    await test_guard_fires_safely()
    await clear_checkpoints()
    await test_independent_thread_and_ttl()
    await clear_checkpoints()
    await test_diagram_path_end_to_end()
    await clear_checkpoints()
    print("\n=== LAYER-2 FULL INTEGRATION TEST SUITE PASSED (all 6 scenarios) ===")


if __name__ == "__main__":
    asyncio.run(main())
=======
"""Layer-2 redesign, Step 5: FULL middle-layer integration test.

Steps 1-4 tested each middle-layer node/feature in isolation (level resolution,
validate_inputs, build_structured_format, conditional user_confirm_inputs). This
script proves the WHOLE middle layer (Layer 2) works as ONE integrated unit, driving
the rebuilt Layer 3, end-to-end on a REAL Postgres checkpointer -- isolated from
Layer 1 (which doesn't exist yet). The test itself owns the ONE Postgres checkpointer,
exactly the role Layer 1 will play later; both subgraphs are compiled WITHOUT their
own checkpointer and inherit this one.

LLM call sites are stubbed (same rationale as every prior Layer-2 step).
agents.sysml.nodes.validate is ALSO stubbed for the Windows event-loop reason
documented in scripts/smoke_test_level_resolution.py.

Run: python -m scripts.smoke_test_layer2_integration
"""
import asyncio
import os
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
from data.models import RequirementLevel, VersionStatus  # noqa: E402
from data.repository import (  # noqa: E402
    DiagramRepo,
    ProjectRepo,
    RequirementRepo,
    SessionRepo,
    ThreadActivityRepo,
    UserRepo,
)
from harness.thread_ttl import touch_thread  # noqa: E402

VALID_OPERATIONAL = "package Ops { requirement def OpReq { doc /* op */ subject s : ScalarValues::Boolean; require constraint { true } } }"
VALID_FUNCTIONAL = "package Func { requirement def FuncReq { doc /* func */ subject s : ScalarValues::Boolean; require constraint { true } } }"
VALID_DIAGRAM = "package UseCases { part def System { } }"


class FakeMessage:
    def __init__(self, content):
        self.content = content


class FakeStructuredLLM:
    """Serves decisions IN SEQUENCE across successive calls -- supports both a
    single decision (repeated) and a list (one per call, clamped at the end).
    """
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


async def setup_session(label: str):
    async with async_session_factory() as db:
        user = await UserRepo.create(db, email=f"l2int-{label}-{uuid.uuid4()}@test.dev", password_hash="hashed")
        project = await ProjectRepo.create(db, user_id=user.id, name=f"L2Integration {label}")
        session = await SessionRepo.create(
            db, project_id=project.id, thread_id=str(uuid.uuid4()), title=f"L2Integration {label}"
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
# Scenario 1: full happy path, two-level nesting. A clear operational request flows
# middle_supervisor -> validate_inputs -> resolve_level -> build_structured_format ->
# sysml_processing -> layer-3 (plan -> generate -> verify -> PAUSE at
# requirement_review). The layer-3 interrupt bubbles from INSIDE layer-3's own graph,
# through sysml_processing's node body (level 1), through the middle graph's runner
# (level 2), to this test's ainvoke call -- exactly the "inside a node" two-level
# bubbling validated by the original spike, now exercising the FULL Step 1-3 pipeline.
# ---------------------------------------------------------------------------
async def test_full_happy_path_two_level_nesting():
    print("\n--- Scenario 1: full happy path, two-level nested interrupt bubbling ---")
    user, session = await setup_session("happy")

    middle_llm = FakeStructuredWrapperLLM([
        MiddleDecision(has_request=True, resolved_intent=Intent.generate_requirement, level=RequirementLevel.operational),
        MiddleDecision(has_request=False, message="nothing further"),
    ])
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

            result_1 = await middle_graph.ainvoke(
                {"user_input": "Define a high-level operational need.", "session_id": session.id}, config
            )
            # The interrupt payload is layer-3's OWN requirement_review shape, surfacing
            # DIRECTLY to this test caller -- proof the bubble crossed both levels intact.
            assert result_1.get("__interrupt__"), "expected the nested layer-3 interrupt to bubble to the caller"
            payload = result_1["__interrupt__"][0].value
            assert payload["type"] == "requirement_review"
            assert payload["level"] == "operational"
            print(f"RUN 1: layer-3's requirement_review interrupt bubbled TWO levels (layer-3 -> "
                  f"sysml_processing node -> middle graph -> test caller). draft={payload['draft'][:40]}...")

            async with async_session_factory() as db:
                rows = await RequirementRepo.list_by_session(db, session_id=session.id)
                assert rows == [], "no DB write before approval"
            print("assert OK: no DB write before approval")

            result_2 = await middle_graph.ainvoke(Command(resume={"action": "approve"}), config)
            light_ref = result_2.get("processing_result")
            assert light_ref["artifact_type"] == "requirement"
            assert set(light_ref.keys()) == {"processing_id", "thread_id", "artifact_type", "artifact_id", "summary"}, (
                "MiddleState must carry only the LIGHT reference, not full content"
            )
            assert not result_2.get("__interrupt__"), "expected the loop to reach END, not pause again"
            print(f"RUN 2: resumed approve -> finalized -> looped middle_supervisor -> END. "
                  f"light_ref={light_ref}")

    async with async_session_factory() as db:
        rows = await RequirementRepo.list_by_session(db, session_id=session.id)
        assert len(rows) == 1 and rows[0].level == RequirementLevel.operational
        assert rows[0].session_id == session.id
        print(f"assert OK: finalized, keyed by thread(session)={session.id} + level=operational, "
              f"id={rows[0].id}")

    await cleanup_user(user)
    print("Scenario 1 PASSED")


# ---------------------------------------------------------------------------
# Scenario 2: confirm-then-process, nested interrupts STACKED. An ambiguous diagram
# request pauses at user_confirm_inputs (a Layer-2 interrupt: payload has "pattern").
# Resume with a selection -> continues into layer-3 which pauses AGAIN at
# requirement_review (a Layer-3 interrupt: payload has "type"). Both interrupts must
# surface correctly, IN SEQUENCE, to this test caller; resuming each must advance
# correctly.
# ---------------------------------------------------------------------------
async def test_stacked_layer2_then_layer3_interrupts():
    print("\n--- Scenario 2: stacked interrupts -- Layer-2 confirm THEN Layer-3 review ---")
    user, session = await setup_session("stacked")

    async with async_session_factory() as db:
        req_a = await RequirementRepo.finalize(db, session_id=session.id, content="req A", level=RequirementLevel.operational)
        req_b = await RequirementRepo.finalize(db, session_id=session.id, content="req B", level=RequirementLevel.operational)
        await db.commit()

    middle_llm = FakeStructuredWrapperLLM([
        MiddleDecision(has_request=True, resolved_intent=Intent.generate_diagram, diagram_type=DiagramType.use_case),
        MiddleDecision(has_request=False, message="nothing further"),
    ])
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
        with patch("agents.sysml.middle_nodes.get_llm", side_effect=fake_middle_get_llm), \
             patch("agents.sysml.nodes.get_llm", side_effect=fake_layer3_get_llm), \
             patch("agents.sysml.nodes.validate", return_value=[]), \
             patch("agents.sysml.nodes.to_mermaid", return_value="graph TD; A-->B;"):

            middle_graph = build_middle_graph(checkpointer=checkpointer)
            config = build_middle_config(outer_thread_id)

            # --- Interrupt #1: Layer-2's user_confirm_inputs ---
            result_1 = await middle_graph.ainvoke(
                {"user_input": "Show a use case diagram.", "session_id": session.id}, config
            )
            assert result_1.get("__interrupt__")
            payload_1 = result_1["__interrupt__"][0].value
            assert "pattern" in payload_1 and payload_1["pattern"] == "select_requirements_for_diagram"
            assert "type" not in payload_1, "this must be Layer-2's confirm interrupt, not Layer-3's"
            print(f"INTERRUPT #1 (Layer-2, user_confirm_inputs): pattern={payload_1['pattern']!r} "
                  f"options={[o['id'] for o in payload_1['options']]}")

            # --- resume #1: select both -> continues INTO layer-3 ---
            result_2 = await middle_graph.ainvoke(
                Command(resume={"action": "confirm", "selected_ids": [str(req_a.id), str(req_b.id)]}), config
            )
            assert result_2.get("__interrupt__"), "expected layer-3 to now pause at requirement_review"
            payload_2 = result_2["__interrupt__"][0].value
            assert payload_2["type"] == "requirement_review"
            assert "pattern" not in payload_2, "this must be Layer-3's review interrupt, not Layer-2's confirm"
            print(f"INTERRUPT #2 (Layer-3, requirement_review): type={payload_2['type']!r} "
                  f"source_node={payload_2['source_node']!r}")

            # --- resume #2: approve -> finalizes ---
            result_3 = await middle_graph.ainvoke(Command(resume={"action": "approve"}), config)
            assert not result_3.get("__interrupt__"), "expected completion, no further pause"
            light_ref = result_3.get("processing_result")
            assert light_ref["artifact_type"] == "diagram"
            print(f"RESUMED both interrupts correctly, in sequence -> finalized. light_ref={light_ref}")

    async with async_session_factory() as db:
        diagrams_a = await DiagramRepo.get_by_requirement(db, requirement_id=req_a.id, session_id=session.id)
        assert len(diagrams_a) == 1 and diagrams_a[0].mermaid
        print(f"assert OK: diagram id={diagrams_a[0].id} finalized with model + mermaid, "
              f"after BOTH stacked interrupts resolved correctly")

    await cleanup_user(user)
    print("Scenario 2 PASSED")


# ---------------------------------------------------------------------------
# Scenario 3: sequential levels across processings in ONE thread. Process an
# operational (finalize), then -- automatically, in the SAME invocation chain, via
# middle_supervisor's own loop -- a functional request: resolve_level auto-resolves
# the operational as source, layer-3 derives + finalizes. Also demonstrates the
# middle_supervisor <-> sysml_processing loop handling MORE THAN ONE processing in a
# single turn (DoD #4's ">1 processing" requirement).
# ---------------------------------------------------------------------------
async def test_sequential_levels_one_thread():
    print("\n--- Scenario 3: sequential levels (operational -> functional) in ONE thread ---")
    user, session = await setup_session("sequential")

    middle_llm = FakeStructuredWrapperLLM([
        MiddleDecision(has_request=True, resolved_intent=Intent.generate_requirement, level=RequirementLevel.operational),
        MiddleDecision(has_request=True, resolved_intent=Intent.generate_requirement, level=RequirementLevel.functional),
        MiddleDecision(has_request=False, message="nothing further"),
    ])
    layer3_supervisor_llm = FakeStructuredWrapperLLM(IntentDecision(intent=Intent.generate_requirement))
    plan_llm = FakeSequenceLLM(["plan op", "plan func"])
    generate_llm = FakeSequenceLLM([VALID_OPERATIONAL, VALID_FUNCTIONAL])

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

            result_1 = await middle_graph.ainvoke(
                {"user_input": "Define the operational need, then the function.", "session_id": session.id}, config
            )
            assert result_1.get("__interrupt__")
            payload_1 = result_1["__interrupt__"][0].value
            assert payload_1["level"] == "operational"
            print(f"PROCESSING 1: paused at layer-3 review, level={payload_1['level']!r}")

            result_2 = await middle_graph.ainvoke(Command(resume={"action": "approve"}), config)
            # middle_supervisor loops automatically (visit 2, still inside this SAME
            # resume call) -> decides functional -> resolve_level auto-resolves the
            # just-finalized operational as source -> layer-3 pauses a SECOND time.
            assert result_2.get("__interrupt__"), "expected the SECOND (functional) processing to pause too"
            payload_2 = result_2["__interrupt__"][0].value
            assert payload_2["level"] == "functional"
            assert result_2.get("requested_level") == "functional"
            assert result_2.get("resolved_source_id") is not None
            print(f"PROCESSING 2 (auto-looped, same turn): paused at layer-3 review, level={payload_2['level']!r}, "
                  f"resolved_source_id={result_2.get('resolved_source_id')!r}, "
                  f"supervisor_visits={result_2.get('supervisor_visits')}")
            assert result_2.get("supervisor_visits") == 2, ">1 processing handled within a single turn"

            result_3 = await middle_graph.ainvoke(Command(resume={"action": "approve"}), config)
            assert not result_3.get("__interrupt__"), "expected the loop to end after the third (no-op) visit"
            print(f"PROCESSING 2 approved -> finalized -> looped to a third (no-op) visit -> END. "
                  f"result={result_3.get('result')!r}")

    async with async_session_factory() as db:
        rows = await RequirementRepo.list_by_session(db, session_id=session.id)
        by_level = {r.level.value: r for r in rows}
        assert sorted(by_level.keys()) == ["functional", "operational"]
        print(f"assert OK: forward progression recorded -- levels present: {sorted(by_level.keys())}")

        level_progress = await RequirementRepo.level_progress(db, session_id=session.id)
        assert level_progress == ["operational", "functional"] or sorted(level_progress) == ["functional", "operational"]
        print(f"assert OK: level_progress reflects both levels in this thread: {level_progress}")

    await cleanup_user(user)
    print("Scenario 3 PASSED")


# ---------------------------------------------------------------------------
# Scenario 4: orchestration loop guard. SYSML_MIDDLE_MAX_VISITS set LOW in the test
# env forces the guard to trip on an ambiguous case needing a second supervisor visit
# -- proving the env-driven guard fires safely (fail-open to END, no crash).
# ---------------------------------------------------------------------------
async def test_guard_fires_safely():
    print("\n--- Scenario 4: SYSML_MIDDLE_MAX_VISITS=1 -> guard fires safely (fail-open) ---")
    user, session = await setup_session("guard")

    async with async_session_factory() as db:
        req_a = await RequirementRepo.finalize(db, session_id=session.id, content="req A", level=RequirementLevel.operational)
        req_b = await RequirementRepo.finalize(db, session_id=session.id, content="req B", level=RequirementLevel.operational)
        await db.commit()

    # generate_diagram (not modify_requirement): its ambiguous-target pause resumes via
    # "modify" below, which routes straight back to middle_supervisor (visit 2) WITHOUT
    # ever touching layer-3 -- exactly where the guard must fire. (A "confirm" resume
    # would instead proceed to build_structured_format -> sysml_processing -> layer-3,
    # which isn't stubbed here and isn't the guard path being tested.)
    middle_llm = FakeStructuredWrapperLLM(
        [MiddleDecision(has_request=True, resolved_intent=Intent.generate_diagram, diagram_type=DiagramType.use_case)]
    )
    confirm_question_llm = FakeSequenceLLM(["Which requirement do you mean?"])

    def fake_middle_get_llm(node_name=None):
        if node_name == "sysml_middle_supervisor":
            return middle_llm
        if node_name == "sysml_confirm_question":
            return confirm_question_llm
        raise AssertionError(f"unexpected node_name in middle_nodes: {node_name}")

    def fake_layer3_get_llm(node_name=None):
        raise AssertionError(f"layer-3 must never run: the guard should stop things first (node_name={node_name})")

    outer_thread_id = f"outer-{uuid.uuid4()}"

    os.environ["SYSML_MIDDLE_MAX_VISITS"] = "1"
    get_settings.cache_clear()
    try:
        assert get_settings().sysml_middle_max_visits == 1
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
                assert result_1.get("__interrupt__"), "visit 1 (<= max_visits=1) should reach the ambiguous pause"
                print(f"visit 1: paused at user_confirm_inputs (ambiguous, allowed under max_visits=1)")

                result_2 = await middle_graph.ainvoke(Command(resume={"action": "modify"}), config)
                assert not result_2.get("__interrupt__"), "guard must fail-open to END, not pause or crash"
                assert result_2.get("result") == "stopped: max supervisor visits reached"
                assert result_2.get("supervisor_visits") == 2
                print(f"visit 2: guard triggered SAFELY (visits=2 > max_visits=1) -> fail-open to END, "
                      f"result={result_2.get('result')!r}, no crash")
    finally:
        os.environ.pop("SYSML_MIDDLE_MAX_VISITS", None)
        get_settings.cache_clear()

    await cleanup_user(user)
    print("Scenario 4 PASSED")


# ---------------------------------------------------------------------------
# Scenario 5: independent per-processing thread + TTL still intact. Each processing
# gets its own DETERMINISTIC thread id, distinct from the outer thread, verified
# directly in the Postgres checkpointer; last_accessed updates on access.
# ---------------------------------------------------------------------------
async def test_independent_thread_and_ttl():
    print("\n--- Scenario 5: independent per-processing thread id + TTL touch, verified in Postgres ---")
    user, session = await setup_session("thread-ttl")

    middle_llm = FakeStructuredWrapperLLM([
        MiddleDecision(has_request=True, resolved_intent=Intent.generate_requirement, level=RequirementLevel.operational),
        MiddleDecision(has_request=False, message="nothing further"),
    ])
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

    # last_accessed BEFORE any invocation (a manual touch, mirroring how the outer
    # thread is first registered) -> then let the real integration touch it again.
    async with async_session_factory() as db:
        await touch_thread(db, thread_id=outer_thread_id, session_id=session.id)
        await db.commit()
        last_accessed_1 = await ThreadActivityRepo.get_last_accessed(db, outer_thread_id)

    await asyncio.sleep(1.1)

    async with AsyncPostgresSaver.from_conn_string(settings.checkpointer_database_url) as checkpointer:
        await checkpointer.setup()
        with patch("agents.sysml.middle_nodes.get_llm", side_effect=fake_middle_get_llm), \
             patch("agents.sysml.nodes.get_llm", side_effect=fake_layer3_get_llm), \
             patch("agents.sysml.nodes.validate", return_value=[]):

            middle_graph = build_middle_graph(checkpointer=checkpointer)
            config = build_middle_config(outer_thread_id)

            result_1 = await middle_graph.ainvoke(
                {"user_input": "Define a high-level operational need.", "session_id": session.id}, config
            )
            assert result_1.get("__interrupt__")
            result_2 = await middle_graph.ainvoke(Command(resume={"action": "approve"}), config)
            proc_thread_id = result_2["processing_result"]["thread_id"]
            assert proc_thread_id != outer_thread_id
            print(f"assert OK: proc_thread_id={proc_thread_id!r} != outer_thread_id={outer_thread_id!r}")

        async with async_session_factory() as db:
            outer_rows = (await db.execute(
                text("SELECT count(*) FROM checkpoints WHERE thread_id = :tid"), {"tid": outer_thread_id}
            )).scalar()
            proc_rows = (await db.execute(
                text("SELECT count(*) FROM checkpoints WHERE thread_id = :tid"), {"tid": proc_thread_id}
            )).scalar()
        assert outer_rows > 0 and proc_rows > 0
        print(f"assert OK: BOTH thread ids have distinct checkpoint rows in Postgres "
              f"(outer={outer_rows}, proc={proc_rows})")

    async with async_session_factory() as db:
        last_accessed_2 = await ThreadActivityRepo.get_last_accessed(db, outer_thread_id)
    assert last_accessed_2 > last_accessed_1, (
        f"expected last_accessed to advance on real access: {last_accessed_1} -> {last_accessed_2}"
    )
    print(f"assert OK: last_accessed advanced on real access ({last_accessed_1} -> {last_accessed_2})")

    await cleanup_user(user)
    print("Scenario 5 PASSED")


# ---------------------------------------------------------------------------
# Scenario 6: diagram path end-to-end (single, unambiguous candidate -- no confirm
# needed). layer-3 produces the SysML v2 model AND the derived Mermaid; finalize
# stores both.
# ---------------------------------------------------------------------------
async def test_diagram_path_end_to_end():
    print("\n--- Scenario 6: diagram path end-to-end -- model + derived Mermaid, both persisted ---")
    user, session = await setup_session("diagram")

    async with async_session_factory() as db:
        req = await RequirementRepo.finalize(db, session_id=session.id, content=VALID_OPERATIONAL, level=RequirementLevel.operational)
        await db.commit()

    middle_llm = FakeStructuredWrapperLLM([
        MiddleDecision(has_request=True, resolved_intent=Intent.generate_diagram, diagram_type=DiagramType.state_machine),
        MiddleDecision(has_request=False, message="nothing further"),
    ])
    layer3_supervisor_llm = FakeStructuredWrapperLLM(
        IntentDecision(intent=Intent.generate_diagram, diagram_type=DiagramType.state_machine)
    )
    plan_llm = FakeSequenceLLM(["plan"])
    generate_llm = FakeSequenceLLM([VALID_DIAGRAM])

    def fake_middle_get_llm(node_name=None):
        if node_name == "sysml_middle_supervisor":
            return middle_llm
        if node_name == "sysml_confirm_question":
            raise AssertionError("single candidate must NOT require a confirm step")
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
             patch("agents.sysml.nodes.validate", return_value=[]), \
             patch("agents.sysml.nodes.to_mermaid", return_value="stateDiagram-v2\n[*] --> Idle"):

            middle_graph = build_middle_graph(checkpointer=checkpointer)
            config = build_middle_config(outer_thread_id)

            result_1 = await middle_graph.ainvoke(
                {"user_input": "Show a state machine diagram for this requirement.", "session_id": session.id}, config
            )
            assert result_1.get("__interrupt__")
            payload = result_1["__interrupt__"][0].value
            assert payload["source_node"] == "diagram"
            assert payload["mermaid"] is not None
            print(f"assert OK: layer-3 derived Mermaid BEFORE human review too (verify_node's job). "
                  f"mermaid_preview={payload['mermaid'][:30]!r}")

            result_2 = await middle_graph.ainvoke(Command(resume={"action": "approve"}), config)
            assert not result_2.get("__interrupt__")

    async with async_session_factory() as db:
        diagrams = await DiagramRepo.get_by_requirement(db, requirement_id=req.id, session_id=session.id)
        assert len(diagrams) == 1
        diagram = diagrams[0]
        assert diagram.status == VersionStatus.active
        assert diagram.sysml_text and diagram.mermaid
        print(f"assert OK: finalize stored BOTH the SysML v2 model (len={len(diagram.sysml_text)}) "
              f"AND the derived Mermaid (len={len(diagram.mermaid)}) for diagram id={diagram.id}")

    await cleanup_user(user)
    print("Scenario 6 PASSED")


async def main() -> None:
    await test_full_happy_path_two_level_nesting()
    await clear_checkpoints()
    await test_stacked_layer2_then_layer3_interrupts()
    await clear_checkpoints()
    await test_sequential_levels_one_thread()
    await clear_checkpoints()
    await test_guard_fires_safely()
    await clear_checkpoints()
    await test_independent_thread_and_ttl()
    await clear_checkpoints()
    await test_diagram_path_end_to_end()
    await clear_checkpoints()
    print("\n=== LAYER-2 FULL INTEGRATION TEST SUITE PASSED (all 6 scenarios) ===")


if __name__ == "__main__":
    asyncio.run(main())
>>>>>>> Stashed changes
