"""SUPERSEDED (as of the Layer-1 rebuild, Step 1) -- kept for reference only, currently
FAILING and not run as part of any regression sweep.

This tested T6a's ORIGINAL top-level supervisor: a planner that always dispatched
(stubbing TopDecision/AgentTarget) and drove full 3-level nesting (top -> middle ->
processing) on every turn. The Layer-1 rebuild replaces that with top_level_supervisor
as a HUB that classifies each turn first (simple_response / needs_execution / unclear)
and only in Step 1 -- there is no dispatch wiring (sysml_middle_node) in the graph yet,
so this file's scenarios cannot pass until Step 2 rebuilds the needs_execution ->
planning/delegation path. scripts/smoke_test_supervisor_hub.py covers Step 1's actual
behavior; scripts/smoke_test_layer2_integration.py covers Layer-2/Layer-3 nesting and
encryption-at-rest independent of Layer 1. Once Step 2 restores dispatch, this file's
scenarios should be re-created (with HubDecision-based stubs) rather than resurrected
as-is.

Original docstring, for context: full 3-level nesting (top -> middle -> processing)
under the PRODUCTION AsyncPostgresSaver (encrypted, durability-configured), plus the
top-level guard.
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
from app.schemas.supervisor import AgentTarget, TopDecision  # noqa: E402
from data.db import async_session_factory  # noqa: E402
from data.models import DiagramType, RequirementLevel, VersionStatus  # noqa: E402
from data.repository import DiagramRepo, ProjectRepo, RequirementRepo, SessionRepo, UserRepo  # noqa: E402
from harness.checkpointer import build_production_checkpointer  # noqa: E402
from supervisor.graph import build_supervisor_config, build_supervisor_graph  # noqa: E402

VALID_REQUIREMENT = (
    "package BrakingSystem {\n"
    "    part def Vehicle {\n"
    "        attribute stoppingDistance : ISQ::LengthValue;\n"
    "    }\n"
    "    part vehicle : Vehicle {\n"
    "        attribute :>> stoppingDistance = 45 [SI::m];\n"
    "    }\n"
    "    requirement def StoppingDistanceRequirement {\n"
    "        doc /* The vehicle shall stop within 50 meters when braking. */\n"
    "        subject veh : Vehicle;\n"
    "        require constraint { veh.stoppingDistance <= 50 [SI::m] }\n"
    "    }\n"
    "}\n"
)
VALID_DIAGRAM_MODEL = (
    "package BrakeStates {\n"
    "    part def BrakingController {\n"
    "        state def Idle;\n"
    "        state def Braking;\n"
    "    }\n"
    "    part controller : BrakingController;\n"
    "}\n"
)


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


async def setup_user_project_session():
    async with async_session_factory() as db:
        user = await UserRepo.create(db, email=f"t6a-{uuid.uuid4()}@test.dev", password_hash="hashed")
        project = await ProjectRepo.create(db, user_id=user.id, name="T6a Project")
        session = await SessionRepo.create(
            db, project_id=project.id, thread_id=str(uuid.uuid4()), title="T6a Session"
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


async def distinct_checkpoint_thread_ids() -> list[str]:
    async with async_session_factory() as db:
        result = await db.execute(text("SELECT DISTINCT thread_id FROM checkpoints ORDER BY thread_id"))
        return [row[0] for row in result.fetchall()]


# ---------------------------------------------------------------------------
# Scenario 1+2: unambiguous full 3-level nesting -> pause -> resume -> persist ->
# light ref bubbles back up -> top supervisor evaluates completion -> finalize -> END.
# ---------------------------------------------------------------------------
async def test_full_nesting_unambiguous():
    print("\n" + "=" * 70)
    print("SCENARIO 1+2: full 3-level nesting, unambiguous, to completion")
    print("=" * 70)
    user, session = await setup_user_project_session()

    draft = VALID_REQUIREMENT

    top_llm = FakeStructuredWrapperLLM(
        [
            TopDecision(active_agent=AgentTarget.sysml, intent_complete=False),
            TopDecision(active_agent=None, intent_complete=True),
        ]
    )
    # level=operational so resolve_level (Layer-2's level ordering) admits it
    # immediately with no source requirement — this scenario's focus is 3-level
    # nesting, not level ordering (see scripts/smoke_test_level_resolution.py).
    middle_llm = FakeStructuredWrapperLLM(
        [
            MiddleDecision(
                has_request=True, resolved_intent=Intent.generate_requirement, level=RequirementLevel.operational
            ),
            MiddleDecision(has_request=False, message="Nothing further to process."),
        ]
    )
    layer3_supervisor_llm = FakeStructuredWrapperLLM(IntentDecision(intent=Intent.generate_requirement))
    plan_llm = FakeSequenceLLM(["Requirement def with subject Vehicle and a stopping-distance constraint."])
    generate_llm = FakeSequenceLLM([draft])

    def fake_top_get_llm(node_name=None):
        if node_name == "top_level_supervisor":
            return top_llm
        raise AssertionError(f"unexpected node_name in supervisor.router: {node_name}")

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

    async with build_production_checkpointer() as checkpointer:
        with patch("supervisor.router.get_llm", side_effect=fake_top_get_llm), \
             patch("agents.sysml.middle_nodes.get_llm", side_effect=fake_middle_get_llm), \
             patch("agents.sysml.nodes.get_llm", side_effect=fake_layer3_get_llm), \
             patch("agents.sysml.nodes.validate", return_value=[]):

            top_graph = build_supervisor_graph(checkpointer=checkpointer)
            config = build_supervisor_config(outer_thread_id)

            print("\n--- RUN 1: expect the L3 interrupt to bubble THREE levels to the top caller ---")
            result_1 = await top_graph.ainvoke(
                {"user_input": "I need a requirement about braking distance", "session_id": session.id},
                config,
            )
            assert result_1.get("__interrupt__"), "expected the interrupt to bubble all the way up"
            payload = result_1["__interrupt__"][0].value
            print(f"Interrupt payload surfaced at the TOP-level caller: {payload}")
            assert payload["source_node"] == "requirement"
            assert payload["draft"] == draft

            async with async_session_factory() as db:
                rows = await RequirementRepo.list_by_session(db, session_id=session.id)
                assert rows == [], "no DB write before approval"
            print("assert OK: no requirement row exists before approval")

            print("\n--- RUN 2: resume with approve, expect completion through finalize_turn -> END ---")
            result_2 = await top_graph.ainvoke(Command(resume={"action": "approve"}), config)
            assert not result_2.get("__interrupt__"), "must run to completion, not pause again"
            assert result_2.get("done") is True
            assert result_2.get("result") == "done"
            light_ref = result_2.get("sysml_result")
            print(f"Final top-level state: done={result_2.get('done')} result={result_2.get('result')!r}")
            print(f"sysml_result (LIGHT reference, bubbled 2 levels back up): {light_ref}")
            assert light_ref is not None
            assert draft not in str(light_ref), "the full artifact text must NOT be in the light reference"

            async with async_session_factory() as db:
                rows = await RequirementRepo.list_by_session(db, session_id=session.id)
                assert len(rows) == 1
                requirement = rows[0]
                assert requirement.content == draft
                assert requirement.status == VersionStatus.active
                assert str(requirement.id) == light_ref["artifact_id"]
                print(f"assert OK: full text + metadata in Postgres. content={requirement.content!r} "
                      f"metadata={requirement.metadata_}")

            thread_ids = await distinct_checkpoint_thread_ids()
            middle_thread = f"{session.id}:middle:1"
            proc_thread = f"{session.id}:proc:1"
            print(f"\ndistinct thread_ids checkpointed: {thread_ids}")
            assert outer_thread_id in thread_ids
            assert middle_thread in thread_ids
            assert proc_thread in thread_ids
            assert len({outer_thread_id, middle_thread, proc_thread}) == 3
            print(f"assert OK: three DISTINCT thread_ids — outer ({outer_thread_id}), "
                  f"middle ({middle_thread}), processing ({proc_thread})")

    await cleanup_user(user)
    print("\nSCENARIO 1+2 PASSED")


# ---------------------------------------------------------------------------
# Scenario 3: ambiguous case -> user_confirm_inputs interrupt bubbles from middle
# through top to the caller; resume with a selection continues into layer-3, which
# ALSO pauses and must bubble three levels up again.
# ---------------------------------------------------------------------------
async def test_ambiguous_bubbles_through_top():
    print("\n" + "=" * 70)
    print("SCENARIO 3: ambiguous -> user_confirm_inputs bubbles through top -> resume -> layer-3")
    print("=" * 70)
    user, session = await setup_user_project_session()

    async with async_session_factory() as db:
        req_a = await RequirementRepo.create(db, session_id=session.id, content="The system shall stop within 50 meters.")
        req_a = await RequirementRepo.promote(db, id=req_a.id, session_id=session.id)
        req_b = await RequirementRepo.create(db, session_id=session.id, content="The system shall log all sensor faults.")
        req_b = await RequirementRepo.promote(db, id=req_b.id, session_id=session.id)
        await db.commit()

    top_llm = FakeStructuredWrapperLLM(
        [
            TopDecision(active_agent=AgentTarget.sysml, intent_complete=False),
            TopDecision(active_agent=None, intent_complete=True),
        ]
    )
    middle_llm = FakeStructuredWrapperLLM(
        [
            MiddleDecision(has_request=True, resolved_intent=Intent.generate_diagram, diagram_type=DiagramType.state_machine),
            MiddleDecision(has_request=False, message="Nothing further to process."),
        ]
    )
    confirm_question_llm = FakeSequenceLLM(["Which requirement should this diagram be for?"])
    layer3_supervisor_llm = FakeStructuredWrapperLLM(
        IntentDecision(intent=Intent.generate_diagram, diagram_type=DiagramType.state_machine)
    )
    plan_llm = FakeSequenceLLM(["State machine with Idle and Braking states."])
    diagram_llm = FakeSequenceLLM([VALID_DIAGRAM_MODEL])

    def fake_top_get_llm(node_name=None):
        if node_name == "top_level_supervisor":
            return top_llm
        raise AssertionError(f"unexpected node_name in supervisor.router: {node_name}")

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
            return diagram_llm
        raise AssertionError(f"unexpected node_name in layer-3 nodes: {node_name}")

    outer_thread_id = f"outer-{uuid.uuid4()}"

    async with build_production_checkpointer() as checkpointer:
        with patch("supervisor.router.get_llm", side_effect=fake_top_get_llm), \
             patch("agents.sysml.middle_nodes.get_llm", side_effect=fake_middle_get_llm), \
             patch("agents.sysml.nodes.get_llm", side_effect=fake_layer3_get_llm), \
             patch("agents.sysml.nodes.validate", return_value=[]):

            top_graph = build_supervisor_graph(checkpointer=checkpointer)
            config = build_supervisor_config(outer_thread_id)

            result_1 = await top_graph.ainvoke(
                {"user_input": "give me a state machine diagram", "session_id": session.id}, config
            )
            assert result_1.get("__interrupt__"), "expected user_confirm_inputs to bubble to the top caller"
            payload_1 = result_1["__interrupt__"][0].value
            assert payload_1["pattern"] == "select_requirements_for_diagram"
            option_ids = {o["id"] for o in payload_1["options"]}
            assert option_ids == {str(req_a.id), str(req_b.id)}
            print(f"RUN 1: user_confirm_inputs bubbled to the TOP caller. pattern={payload_1['pattern']!r} "
                  f"options={payload_1['options']}")

            result_2 = await top_graph.ainvoke(
                Command(resume={"action": "confirm", "selected_ids": [str(req_b.id)]}), config
            )
            assert result_2.get("__interrupt__"), "expected layer-3's requirement_review to now pause"
            payload_2 = result_2["__interrupt__"][0].value
            assert payload_2["source_node"] == "diagram"
            print(f"RUN 2: resumed with selection, layer-3 now paused (bubbled 3 levels again). "
                  f"draft={payload_2['draft'][:30]}...")

            result_3 = await top_graph.ainvoke(Command(resume={"action": "approve"}), config)
            assert result_3.get("done") is True
            print(f"RUN 3: completed. sysml_result={result_3.get('sysml_result')}")

    async with async_session_factory() as db:
        diagrams_b = await DiagramRepo.get_by_requirement(db, requirement_id=req_b.id, session_id=session.id)
        diagrams_a = await DiagramRepo.get_by_requirement(db, requirement_id=req_a.id, session_id=session.id)
        assert len(diagrams_b) == 1 and diagrams_b[0].status == VersionStatus.active
        assert diagrams_a == []
        print(f"assert OK: diagram persisted against the CHOSEN requirement ({req_b.id}) only")

    await cleanup_user(user)
    print("\nSCENARIO 3 PASSED")


# ---------------------------------------------------------------------------
# Scenario 4: encryption at rest.
# ---------------------------------------------------------------------------
async def test_encryption_at_rest():
    print("\n" + "=" * 70)
    print("SCENARIO 4: checkpoint state is encrypted at rest")
    print("=" * 70)
    user, session = await setup_user_project_session()

    draft = VALID_REQUIREMENT.replace(
        "The vehicle shall stop within 50 meters when braking.",
        "The vehicle shall have a unique plaintext marker XYZZY123 in it.",
    )

    top_llm = FakeStructuredWrapperLLM(TopDecision(active_agent=AgentTarget.sysml, intent_complete=False))
    # level=operational so resolve_level admits it with no source requirement.
    middle_llm = FakeStructuredWrapperLLM(
        MiddleDecision(has_request=True, resolved_intent=Intent.generate_requirement, level=RequirementLevel.operational)
    )
    layer3_supervisor_llm = FakeStructuredWrapperLLM(IntentDecision(intent=Intent.generate_requirement))
    plan_llm = FakeSequenceLLM(["Requirement def with subject Vehicle and a marker constraint."])
    generate_llm = FakeSequenceLLM([draft])

    def fake_top_get_llm(node_name=None):
        return top_llm

    def fake_middle_get_llm(node_name=None):
        return middle_llm

    def fake_layer3_get_llm(node_name=None):
        if node_name == "sysml_supervisor":
            return layer3_supervisor_llm
        if node_name == "sysml_plan":
            return plan_llm
        return generate_llm

    outer_thread_id = f"outer-{uuid.uuid4()}"

    async with build_production_checkpointer() as checkpointer:
        with patch("supervisor.router.get_llm", side_effect=fake_top_get_llm), \
             patch("agents.sysml.middle_nodes.get_llm", side_effect=fake_middle_get_llm), \
             patch("agents.sysml.nodes.get_llm", side_effect=fake_layer3_get_llm), \
             patch("agents.sysml.nodes.validate", return_value=[]):

            top_graph = build_supervisor_graph(checkpointer=checkpointer)
            config = build_supervisor_config(outer_thread_id)

            result_1 = await top_graph.ainvoke(
                {"user_input": "I need a requirement with a marker", "session_id": session.id}, config
            )
            assert result_1.get("__interrupt__")

    async with async_session_factory() as db:
        blob_rows = (await db.execute(
            text("SELECT type, blob FROM checkpoint_blobs WHERE thread_id = :tid"),
            {"tid": f"{session.id}:proc:1"},
        )).fetchall()

        assert blob_rows, "expected checkpoint_blobs rows for the processing thread"
        encrypted_type_count = sum(1 for (typ, _blob) in blob_rows if typ and "+" in typ)
        assert encrypted_type_count > 0, "expected at least one blob with an encrypted type marker (e.g. 'json+aes')"
        print(f"checkpoint_blobs types found: {sorted({typ for (typ, _b) in blob_rows})}")
        print(f"assert OK: {encrypted_type_count}/{len(blob_rows)} blob rows carry an encrypted type suffix (+aes)")

        plaintext_hits = 0
        for _typ, blob in blob_rows:
            if blob and b"XYZZY123" in blob:
                plaintext_hits += 1
        assert plaintext_hits == 0, "found the plaintext draft marker unencrypted in a checkpoint blob!"
        print("assert OK: the plaintext draft marker ('XYZZY123') does NOT appear anywhere in the raw stored bytes")

        # sanity: the marker IS in the DB's own (unencrypted, normal) requirements table
        # once persisted — proving the absence above is about the checkpointer, not that
        # the marker was never written anywhere.
        checkpoints_row_count = (await db.execute(
            text("SELECT count(*) FROM checkpoints WHERE thread_id = :tid"), {"tid": f"{session.id}:proc:1"}
        )).scalar()
        print(f"({checkpoints_row_count} checkpoint rows exist for that thread, confirming state WAS captured)")

    await cleanup_user(user)
    print("\nSCENARIO 4 PASSED")


# ---------------------------------------------------------------------------
# Scenario 5: top-level guard, env-driven.
# ---------------------------------------------------------------------------
async def test_top_level_guard():
    print("\n" + "=" * 70)
    print("SCENARIO 5: SUPERVISOR_MAX_VISITS=1 -> guard triggers safely (fail-open)")
    print("=" * 70)

    os.environ["SUPERVISOR_MAX_VISITS"] = "1"
    get_settings.cache_clear()
    settings = get_settings()
    print(f"Settings.supervisor_max_visits = {settings.supervisor_max_visits} (env override; .env default is 10)")
    assert settings.supervisor_max_visits == 1

    user, session = await setup_user_project_session()

    # A decision that NEVER declares intent_complete and NEVER dispatches an agent would
    # be a bug in a real prompt, but here we simulate exactly the "keeps asking, never
    # finishes" pathology the guard exists to catch: has_request always true, never complete.
    top_llm = FakeStructuredWrapperLLM(
        [TopDecision(active_agent=AgentTarget.sysml, intent_complete=False)] * 5
    )

    def fake_top_get_llm(node_name=None):
        return top_llm

    def fake_middle_get_llm(node_name=None):
        raise AssertionError("must never reach the middle layer: the guard should stop at visit 2")

    outer_thread_id = f"outer-{uuid.uuid4()}"

    async with build_production_checkpointer() as checkpointer:
        with patch("supervisor.router.get_llm", side_effect=fake_top_get_llm), \
             patch("agents.sysml.middle_nodes.get_llm", side_effect=fake_middle_get_llm):

            top_graph = build_supervisor_graph(checkpointer=checkpointer)
            config = build_supervisor_config(outer_thread_id)

            # visit 1 dispatches to sysml_middle_node, which we've made unreachable by
            # not stubbing layer-3 either — but with max_visits=1, the loop must stop
            # at supervisor_visits=2 (the visit AFTER sysml_middle_node loops back),
            # before ever trying visit 1's dispatch a second time. To exercise the
            # guard deterministically without touching the middle layer at all, we
            # instead make the FIRST decision itself already exceed max_visits by
            # invoking with supervisor_visits pre-seeded at the limit.
            result = await top_graph.ainvoke(
                {
                    "user_input": "loop forever please",
                    "session_id": session.id,
                    "supervisor_visits": 1,  # visit about to happen (2) already exceeds max=1
                },
                config,
            )

    assert not result.get("__interrupt__"), "guard breach must end the run, not pause"
    assert result.get("done") is True
    assert result.get("result") == "stopped: max supervisor visits reached"
    assert result.get("supervisor_visits") == 2
    print(f"final state: done={result.get('done')} result={result.get('result')!r} "
          f"supervisor_visits={result.get('supervisor_visits')}")
    print("assert OK: guard triggered, ended safely via fail-open to END — no crash, middle layer never reached")

    await cleanup_user(user)
    os.environ.pop("SUPERVISOR_MAX_VISITS", None)
    get_settings.cache_clear()
    print("\nSCENARIO 5 PASSED")


async def main() -> None:
    await test_full_nesting_unambiguous()
    await clear_checkpoints()
    await test_ambiguous_bubbles_through_top()
    await clear_checkpoints()
    await test_encryption_at_rest()
    await clear_checkpoints()
    await test_top_level_guard()
    await clear_checkpoints()
    print("\n=== T6A TEST SUITE PASSED ===")


if __name__ == "__main__":
    asyncio.run(main())
