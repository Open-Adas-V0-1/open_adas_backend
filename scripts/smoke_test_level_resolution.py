<<<<<<< Updated upstream
"""Standalone tests for the Layer-2 redesign, Step 1: resolve_level (sequential
Op->Func->Phys ordering + source resolution) and thread TTL, on a REAL Postgres
checkpointer.

LLM call sites are stubbed (same rationale as T5a/b/T6a). agents.sysml.nodes.validate is
ALSO stubbed here: this test uses AsyncPostgresSaver (needed for the TTL scenario's real
adelete_thread), and psycopg's async mode requires SelectorEventLoop on Windows while
asyncio subprocesses (the real SysML v2 tooling) require ProactorEventLoop — the two
conflict in one process on Windows (not on the Linux/Docker target). The real tool
integration is already covered by scripts/smoke_test_layer3_rebuild.py.

Run: python -m scripts.smoke_test_level_resolution
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
from app.schemas.sysml import Intent, IntentDecision, MiddleDecision  # noqa: E402
from data.db import async_session_factory  # noqa: E402
from data.models import RequirementLevel, VersionStatus  # noqa: E402
from data.repository import (  # noqa: E402
    ProjectRepo,
    RequirementRepo,
    SessionRepo,
    ThreadActivityRepo,
    UserRepo,
)
from harness.thread_ttl import expire_if_stale, is_expired, touch_thread  # noqa: E402

VALID_OPERATIONAL = "package Ops { requirement def OpReq { doc /* op */ subject s : ScalarValues::Boolean; require constraint { true } } }"
VALID_FUNCTIONAL = "package Func { requirement def FuncReq { doc /* func */ subject s : ScalarValues::Boolean; require constraint { true } } }"


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
        user = await UserRepo.create(db, email=f"lvl-{label}-{uuid.uuid4()}@test.dev", password_hash="hashed")
        project = await ProjectRepo.create(db, user_id=user.id, name=f"Level {label}")
        session = await SessionRepo.create(
            db, project_id=project.id, thread_id=str(uuid.uuid4()), title=f"Level {label}"
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
# Scenario 1: operational request in a fresh thread -> allowed, no source needed.
# ---------------------------------------------------------------------------
async def test_operational_fresh_thread():
    print("\n--- Scenario 1: operational request, fresh thread -> allowed, no source ---")
    user, session = await setup_session("op-fresh")

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
            assert result.get("requested_level") == "operational"
            assert result.get("resolved_source_id") is None
            assert result.get("pending_pattern") is None, "operational must never need user_confirm_inputs"
            print(f"resolve_level: requested_level={result.get('requested_level')} "
                  f"resolved_source_id={result.get('resolved_source_id')} level_progress={result.get('level_progress')}")
            print("assert OK: operational allowed immediately, no source required, no confirm interrupt")

            await middle_graph.ainvoke(Command(resume={"action": "approve"}), config)

    async with async_session_factory() as db:
        rows = await RequirementRepo.list_by_session(db, session_id=session.id)
        assert len(rows) == 1 and rows[0].level == RequirementLevel.operational
        print(f"assert OK: finalized operational requirement id={rows[0].id}")

    await cleanup_user(user)
    print("Scenario 1 PASSED")


# ---------------------------------------------------------------------------
# Scenario 2: functional request in a thread that HAS an operational -> resolves it
# as source and proceeds directly (no confirm interrupt).
# ---------------------------------------------------------------------------
async def test_functional_with_operational_present():
    print("\n--- Scenario 2: functional request, thread HAS operational -> source resolved ---")
    user, session = await setup_session("func-with-op")

    async with async_session_factory() as db:
        op = await RequirementRepo.finalize(
            db, session_id=session.id, content=VALID_OPERATIONAL, level=RequirementLevel.operational
        )
        await db.commit()
        op_id = op.id

    middle_llm = FakeStructuredWrapperLLM(
        MiddleDecision(has_request=True, resolved_intent=Intent.generate_requirement, level=RequirementLevel.functional)
    )
    layer3_supervisor_llm = FakeStructuredWrapperLLM(IntentDecision(intent=Intent.generate_requirement))
    plan_llm = FakeSequenceLLM(["plan"])
    generate_llm = FakeSequenceLLM([VALID_FUNCTIONAL])

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
                {"user_input": "Define a specific function this system performs.", "session_id": session.id}, config
            )
            assert result.get("__interrupt__"), "expected layer-3 to pause at requirement_review"
            assert result.get("requested_level") == "functional"
            assert result.get("resolved_source_id") == str(op_id), (
                f"expected resolved_source_id={op_id}, got {result.get('resolved_source_id')}"
            )
            assert result.get("pending_pattern") is None, "single operational candidate must NOT trigger confirm"
            print(f"resolve_level: requested_level={result.get('requested_level')} "
                  f"resolved_source_id={result.get('resolved_source_id')} (== operational.id: "
                  f"{result.get('resolved_source_id') == str(op_id)}) level_progress={result.get('level_progress')}")
            print("assert OK: operational auto-resolved as source, proceeded straight to layer-3")

            await middle_graph.ainvoke(Command(resume={"action": "approve"}), config)

    async with async_session_factory() as db:
        rows = await RequirementRepo.list_by_session(db, session_id=session.id)
        levels = sorted(r.level.value for r in rows)
        assert levels == ["functional", "operational"]
        print(f"assert OK: thread now has {levels} — forward progression recorded")

    await cleanup_user(user)
    print("Scenario 2 PASSED")


# ---------------------------------------------------------------------------
# Scenario 3: functional request, NO operational in thread -> user_confirm_inputs
# (interrupt) asking to create operational first.
# ---------------------------------------------------------------------------
async def test_functional_without_operational():
    print("\n--- Scenario 3: functional request, NO operational -> user_confirm_inputs interrupt ---")
    user, session = await setup_session("func-no-op")

    middle_llm = FakeStructuredWrapperLLM(
        MiddleDecision(has_request=True, resolved_intent=Intent.generate_requirement, level=RequirementLevel.functional)
    )
    confirm_question_llm = FakeSequenceLLM(["No operational requirement exists yet — create one first?"])
    layer3_supervisor_llm = FakeStructuredWrapperLLM(IntentDecision(intent=Intent.generate_requirement))
    plan_llm = FakeSequenceLLM(["plan"])
    generate_llm = FakeSequenceLLM([VALID_OPERATIONAL])

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
             patch("agents.sysml.nodes.validate", return_value=[]):

            middle_graph = build_middle_graph(checkpointer=checkpointer)
            config = build_middle_config(outer_thread_id)

            result_1 = await middle_graph.ainvoke(
                {"user_input": "Define a specific function this system performs.", "session_id": session.id}, config
            )
            assert result_1.get("__interrupt__"), "expected user_confirm_inputs to pause (missing source)"
            payload = result_1["__interrupt__"][0].value
            assert payload["pattern"] == "confirm_action"
            print(f"RUN 1: paused at user_confirm_inputs. pattern={payload['pattern']!r} "
                  f"question={payload['question']!r}")
            print(f"assert OK: missing-source ask observed via interrupt (requested_level="
                  f"{result_1.get('requested_level')!r}, resolved_source_id={result_1.get('resolved_source_id')})")
            assert result_1.get("requested_level") == "functional"
            assert result_1.get("resolved_source_id") is None

            async with async_session_factory() as db:
                rows = await RequirementRepo.list_by_session(db, session_id=session.id)
                assert rows == [], "no DB write before any approval"

            # bonus: confirm the pivot — "yes, create operational first" should redirect
            # generation to the missing source level and proceed.
            result_2 = await middle_graph.ainvoke(Command(resume={"action": "confirm"}), config)
            assert result_2.get("__interrupt__"), "expected layer-3 to now pause, generating the PIVOTED operational"
            payload_2 = result_2["__interrupt__"][0].value
            print(f"RUN 2 (confirmed pivot): layer-3 paused generating the operational instead. "
                  f"draft={payload_2['draft'][:60]}...")
            assert result_2.get("requested_level") == "operational"

            await middle_graph.ainvoke(Command(resume={"action": "approve"}), config)

    async with async_session_factory() as db:
        rows = await RequirementRepo.list_by_session(db, session_id=session.id)
        assert len(rows) == 1 and rows[0].level == RequirementLevel.operational
        print(f"assert OK: pivot flow finalized the operational requirement id={rows[0].id}")

    await cleanup_user(user)
    print("Scenario 3 PASSED")


# ---------------------------------------------------------------------------
# Scenario 4 + 5: TTL lazy expiry (checkpointer state only, artifact preserved) and
# last_accessed updated on access.
# ---------------------------------------------------------------------------
async def test_ttl_and_last_accessed():
    print("\n--- Scenario 4+5: TTL lazy expiry (artifact preserved) + last_accessed updates ---")
    user, session = await setup_session("ttl")

    # An approved artifact that must survive TTL expiry regardless.
    async with async_session_factory() as db:
        req = await RequirementRepo.finalize(
            db, session_id=session.id, content=VALID_OPERATIONAL, level=RequirementLevel.operational
        )
        await db.commit()
        req_id = req.id

    thread_id = f"ttl-thread-{uuid.uuid4()}"
    settings = get_settings()

    async with AsyncPostgresSaver.from_conn_string(settings.checkpointer_database_url) as checkpointer:
        await checkpointer.setup()

        # --- Scenario 5: last_accessed updated on access ---
        async with async_session_factory() as db:
            await touch_thread(db, thread_id=thread_id, session_id=session.id)
            await db.commit()
            last_accessed_1 = await ThreadActivityRepo.get_last_accessed(db, thread_id)

        await asyncio.sleep(1.1)  # ensure a measurable timestamp difference

        async with async_session_factory() as db:
            await touch_thread(db, thread_id=thread_id, session_id=session.id)
            await db.commit()
            last_accessed_2 = await ThreadActivityRepo.get_last_accessed(db, thread_id)

        assert last_accessed_2 > last_accessed_1, (
            f"expected last_accessed to advance on touch: {last_accessed_1} -> {last_accessed_2}"
        )
        print(f"assert OK: last_accessed advanced on access ({last_accessed_1} -> {last_accessed_2})")

        # Put SOME real checkpointer state under this thread_id, so expiry has
        # something concrete to delete.
        with patch("agents.sysml.middle_nodes.get_llm") as fake_get_llm, \
             patch("agents.sysml.nodes.validate", return_value=[]):
            fake_get_llm.side_effect = lambda node_name=None: FakeStructuredWrapperLLM(
                MiddleDecision(has_request=False, message="nothing to do")
            )
            middle_graph = build_middle_graph(checkpointer=checkpointer)
            config = build_middle_config(thread_id)
            await middle_graph.ainvoke({"user_input": "hello", "session_id": session.id}, config)

        async with async_session_factory() as db:
            count_before = (await db.execute(
                text("SELECT count(*) FROM checkpoints WHERE thread_id = :tid"), {"tid": thread_id}
            )).scalar()
        assert count_before > 0, "expected real checkpointer rows for this thread before expiry"
        print(f"checkpointer rows present before expiry: {count_before}")

        # --- Scenario 4: force expiry by backdating last_accessed, then check + expire ---
        os.environ["SYSML_THREAD_TTL_DAYS"] = "1"
        get_settings.cache_clear()
        assert get_settings().sysml_thread_ttl_days == 1
        print(f"Settings.sysml_thread_ttl_days = {get_settings().sysml_thread_ttl_days} (env override; .env default is 30)")

        try:
            async with async_session_factory() as db:
                await db.execute(
                    text("UPDATE thread_activity SET last_accessed = NOW() - INTERVAL '999 days' WHERE thread_id = :tid"),
                    {"tid": thread_id},
                )
                await db.commit()

            async with async_session_factory() as db:
                expired_before_action = await is_expired(db, thread_id)
            assert expired_before_action is True
            print(f"assert OK: thread reported expired (last_accessed backdated 999 days, TTL=1 day)")

            async with async_session_factory() as db:
                did_expire = await expire_if_stale(db, checkpointer, thread_id)
                await db.commit()
            assert did_expire is True
            print("assert OK: expire_if_stale deleted the checkpointer state (returned True)")

            async with async_session_factory() as db:
                count_after = (await db.execute(
                    text("SELECT count(*) FROM checkpoints WHERE thread_id = :tid"), {"tid": thread_id}
                )).scalar()
            assert count_after == 0, "checkpointer state must be gone after expiry"
            print(f"assert OK: checkpointer rows for this thread after expiry = {count_after} (time-travel unavailable)")

            async with async_session_factory() as db:
                still_there = await RequirementRepo.get_by_id(db, id=req_id, session_id=session.id)
            assert still_there is not None and still_there.status == VersionStatus.active
            print(f"assert OK: the APPROVED requirement (id={req_id}) is STILL in Postgres, "
                  f"untouched by checkpointer expiry — status={still_there.status}")
        finally:
            os.environ.pop("SYSML_THREAD_TTL_DAYS", None)
            get_settings.cache_clear()

    await cleanup_user(user)
    print("Scenario 4+5 PASSED")


async def main() -> None:
    await test_operational_fresh_thread()
    await clear_checkpoints()
    await test_functional_with_operational_present()
    await clear_checkpoints()
    await test_functional_without_operational()
    await clear_checkpoints()
    await test_ttl_and_last_accessed()
    await clear_checkpoints()
    print("\n=== LEVEL RESOLUTION + TTL TEST SUITE PASSED ===")


if __name__ == "__main__":
    asyncio.run(main())
=======
"""Standalone tests for the Layer-2 redesign, Step 1: resolve_level (sequential
Op->Func->Phys ordering + source resolution) and thread TTL, on a REAL Postgres
checkpointer.

LLM call sites are stubbed (same rationale as T5a/b/T6a). agents.sysml.nodes.validate is
ALSO stubbed here: this test uses AsyncPostgresSaver (needed for the TTL scenario's real
adelete_thread), and psycopg's async mode requires SelectorEventLoop on Windows while
asyncio subprocesses (the real SysML v2 tooling) require ProactorEventLoop — the two
conflict in one process on Windows (not on the Linux/Docker target). The real tool
integration is already covered by scripts/smoke_test_layer3_rebuild.py.

Run: python -m scripts.smoke_test_level_resolution
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
from app.schemas.sysml import Intent, IntentDecision, MiddleDecision  # noqa: E402
from data.db import async_session_factory  # noqa: E402
from data.models import RequirementLevel, VersionStatus  # noqa: E402
from data.repository import (  # noqa: E402
    ProjectRepo,
    RequirementRepo,
    SessionRepo,
    ThreadActivityRepo,
    UserRepo,
)
from harness.thread_ttl import expire_if_stale, is_expired, touch_thread  # noqa: E402

VALID_OPERATIONAL = "package Ops { requirement def OpReq { doc /* op */ subject s : ScalarValues::Boolean; require constraint { true } } }"
VALID_FUNCTIONAL = "package Func { requirement def FuncReq { doc /* func */ subject s : ScalarValues::Boolean; require constraint { true } } }"


class FakeMessage:
    def __init__(self, content):
        self.content = content


class FakeStructuredLLM:
    def __init__(self, decision):
        self.decision = decision

    async def ainvoke(self, prompt):
        return self.decision


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
        user = await UserRepo.create(db, email=f"lvl-{label}-{uuid.uuid4()}@test.dev", password_hash="hashed")
        project = await ProjectRepo.create(db, user_id=user.id, name=f"Level {label}")
        session = await SessionRepo.create(
            db, project_id=project.id, thread_id=str(uuid.uuid4()), title=f"Level {label}"
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
# Scenario 1: operational request in a fresh thread -> allowed, no source needed.
# ---------------------------------------------------------------------------
async def test_operational_fresh_thread():
    print("\n--- Scenario 1: operational request, fresh thread -> allowed, no source ---")
    user, session = await setup_session("op-fresh")

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
            assert result.get("requested_level") == "operational"
            assert result.get("resolved_source_id") is None
            assert result.get("pending_pattern") is None, "operational must never need user_confirm_inputs"
            print(f"resolve_level: requested_level={result.get('requested_level')} "
                  f"resolved_source_id={result.get('resolved_source_id')} level_progress={result.get('level_progress')}")
            print("assert OK: operational allowed immediately, no source required, no confirm interrupt")

            await middle_graph.ainvoke(Command(resume={"action": "approve"}), config)

    async with async_session_factory() as db:
        rows = await RequirementRepo.list_by_session(db, session_id=session.id)
        assert len(rows) == 1 and rows[0].level == RequirementLevel.operational
        print(f"assert OK: finalized operational requirement id={rows[0].id}")

    await cleanup_user(user)
    print("Scenario 1 PASSED")


# ---------------------------------------------------------------------------
# Scenario 2: functional request in a thread that HAS an operational -> resolves it
# as source and proceeds directly (no confirm interrupt).
# ---------------------------------------------------------------------------
async def test_functional_with_operational_present():
    print("\n--- Scenario 2: functional request, thread HAS operational -> source resolved ---")
    user, session = await setup_session("func-with-op")

    async with async_session_factory() as db:
        op = await RequirementRepo.finalize(
            db, session_id=session.id, content=VALID_OPERATIONAL, level=RequirementLevel.operational
        )
        await db.commit()
        op_id = op.id

    middle_llm = FakeStructuredWrapperLLM(
        MiddleDecision(has_request=True, resolved_intent=Intent.generate_requirement, level=RequirementLevel.functional)
    )
    layer3_supervisor_llm = FakeStructuredWrapperLLM(IntentDecision(intent=Intent.generate_requirement))
    plan_llm = FakeSequenceLLM(["plan"])
    generate_llm = FakeSequenceLLM([VALID_FUNCTIONAL])

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
                {"user_input": "Define a specific function this system performs.", "session_id": session.id}, config
            )
            assert result.get("__interrupt__"), "expected layer-3 to pause at requirement_review"
            assert result.get("requested_level") == "functional"
            assert result.get("resolved_source_id") == str(op_id), (
                f"expected resolved_source_id={op_id}, got {result.get('resolved_source_id')}"
            )
            assert result.get("pending_pattern") is None, "single operational candidate must NOT trigger confirm"
            print(f"resolve_level: requested_level={result.get('requested_level')} "
                  f"resolved_source_id={result.get('resolved_source_id')} (== operational.id: "
                  f"{result.get('resolved_source_id') == str(op_id)}) level_progress={result.get('level_progress')}")
            print("assert OK: operational auto-resolved as source, proceeded straight to layer-3")

            await middle_graph.ainvoke(Command(resume={"action": "approve"}), config)

    async with async_session_factory() as db:
        rows = await RequirementRepo.list_by_session(db, session_id=session.id)
        levels = sorted(r.level.value for r in rows)
        assert levels == ["functional", "operational"]
        print(f"assert OK: thread now has {levels} — forward progression recorded")

    await cleanup_user(user)
    print("Scenario 2 PASSED")


# ---------------------------------------------------------------------------
# Scenario 3: functional request, NO operational in thread -> user_confirm_inputs
# (interrupt) asking to create operational first.
# ---------------------------------------------------------------------------
async def test_functional_without_operational():
    print("\n--- Scenario 3: functional request, NO operational -> user_confirm_inputs interrupt ---")
    user, session = await setup_session("func-no-op")

    middle_llm = FakeStructuredWrapperLLM(
        MiddleDecision(has_request=True, resolved_intent=Intent.generate_requirement, level=RequirementLevel.functional)
    )
    confirm_question_llm = FakeSequenceLLM(["No operational requirement exists yet — create one first?"])
    layer3_supervisor_llm = FakeStructuredWrapperLLM(IntentDecision(intent=Intent.generate_requirement))
    plan_llm = FakeSequenceLLM(["plan"])
    generate_llm = FakeSequenceLLM([VALID_OPERATIONAL])

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
             patch("agents.sysml.nodes.validate", return_value=[]):

            middle_graph = build_middle_graph(checkpointer=checkpointer)
            config = build_middle_config(outer_thread_id)

            result_1 = await middle_graph.ainvoke(
                {"user_input": "Define a specific function this system performs.", "session_id": session.id}, config
            )
            assert result_1.get("__interrupt__"), "expected user_confirm_inputs to pause (missing source)"
            payload = result_1["__interrupt__"][0].value
            assert payload["pattern"] == "confirm_action"
            print(f"RUN 1: paused at user_confirm_inputs. pattern={payload['pattern']!r} "
                  f"question={payload['question']!r}")
            print(f"assert OK: missing-source ask observed via interrupt (requested_level="
                  f"{result_1.get('requested_level')!r}, resolved_source_id={result_1.get('resolved_source_id')})")
            assert result_1.get("requested_level") == "functional"
            assert result_1.get("resolved_source_id") is None

            async with async_session_factory() as db:
                rows = await RequirementRepo.list_by_session(db, session_id=session.id)
                assert rows == [], "no DB write before any approval"

            # bonus: confirm the pivot — "yes, create operational first" should redirect
            # generation to the missing source level and proceed.
            result_2 = await middle_graph.ainvoke(Command(resume={"action": "confirm"}), config)
            assert result_2.get("__interrupt__"), "expected layer-3 to now pause, generating the PIVOTED operational"
            payload_2 = result_2["__interrupt__"][0].value
            print(f"RUN 2 (confirmed pivot): layer-3 paused generating the operational instead. "
                  f"draft={payload_2['draft'][:60]}...")
            assert result_2.get("requested_level") == "operational"

            await middle_graph.ainvoke(Command(resume={"action": "approve"}), config)

    async with async_session_factory() as db:
        rows = await RequirementRepo.list_by_session(db, session_id=session.id)
        assert len(rows) == 1 and rows[0].level == RequirementLevel.operational
        print(f"assert OK: pivot flow finalized the operational requirement id={rows[0].id}")

    await cleanup_user(user)
    print("Scenario 3 PASSED")


# ---------------------------------------------------------------------------
# Scenario 4 + 5: TTL lazy expiry (checkpointer state only, artifact preserved) and
# last_accessed updated on access.
# ---------------------------------------------------------------------------
async def test_ttl_and_last_accessed():
    print("\n--- Scenario 4+5: TTL lazy expiry (artifact preserved) + last_accessed updates ---")
    user, session = await setup_session("ttl")

    # An approved artifact that must survive TTL expiry regardless.
    async with async_session_factory() as db:
        req = await RequirementRepo.finalize(
            db, session_id=session.id, content=VALID_OPERATIONAL, level=RequirementLevel.operational
        )
        await db.commit()
        req_id = req.id

    thread_id = f"ttl-thread-{uuid.uuid4()}"
    settings = get_settings()

    async with AsyncPostgresSaver.from_conn_string(settings.checkpointer_database_url) as checkpointer:
        await checkpointer.setup()

        # --- Scenario 5: last_accessed updated on access ---
        async with async_session_factory() as db:
            await touch_thread(db, thread_id=thread_id, session_id=session.id)
            await db.commit()
            last_accessed_1 = await ThreadActivityRepo.get_last_accessed(db, thread_id)

        await asyncio.sleep(1.1)  # ensure a measurable timestamp difference

        async with async_session_factory() as db:
            await touch_thread(db, thread_id=thread_id, session_id=session.id)
            await db.commit()
            last_accessed_2 = await ThreadActivityRepo.get_last_accessed(db, thread_id)

        assert last_accessed_2 > last_accessed_1, (
            f"expected last_accessed to advance on touch: {last_accessed_1} -> {last_accessed_2}"
        )
        print(f"assert OK: last_accessed advanced on access ({last_accessed_1} -> {last_accessed_2})")

        # Put SOME real checkpointer state under this thread_id, so expiry has
        # something concrete to delete.
        with patch("agents.sysml.middle_nodes.get_llm") as fake_get_llm, \
             patch("agents.sysml.nodes.validate", return_value=[]):
            fake_get_llm.side_effect = lambda node_name=None: FakeStructuredWrapperLLM(
                MiddleDecision(has_request=False, message="nothing to do")
            )
            middle_graph = build_middle_graph(checkpointer=checkpointer)
            config = build_middle_config(thread_id)
            await middle_graph.ainvoke({"user_input": "hello", "session_id": session.id}, config)

        async with async_session_factory() as db:
            count_before = (await db.execute(
                text("SELECT count(*) FROM checkpoints WHERE thread_id = :tid"), {"tid": thread_id}
            )).scalar()
        assert count_before > 0, "expected real checkpointer rows for this thread before expiry"
        print(f"checkpointer rows present before expiry: {count_before}")

        # --- Scenario 4: force expiry by backdating last_accessed, then check + expire ---
        os.environ["SYSML_THREAD_TTL_DAYS"] = "1"
        get_settings.cache_clear()
        assert get_settings().sysml_thread_ttl_days == 1
        print(f"Settings.sysml_thread_ttl_days = {get_settings().sysml_thread_ttl_days} (env override; .env default is 30)")

        try:
            async with async_session_factory() as db:
                await db.execute(
                    text("UPDATE thread_activity SET last_accessed = NOW() - INTERVAL '999 days' WHERE thread_id = :tid"),
                    {"tid": thread_id},
                )
                await db.commit()

            async with async_session_factory() as db:
                expired_before_action = await is_expired(db, thread_id)
            assert expired_before_action is True
            print(f"assert OK: thread reported expired (last_accessed backdated 999 days, TTL=1 day)")

            async with async_session_factory() as db:
                did_expire = await expire_if_stale(db, checkpointer, thread_id)
                await db.commit()
            assert did_expire is True
            print("assert OK: expire_if_stale deleted the checkpointer state (returned True)")

            async with async_session_factory() as db:
                count_after = (await db.execute(
                    text("SELECT count(*) FROM checkpoints WHERE thread_id = :tid"), {"tid": thread_id}
                )).scalar()
            assert count_after == 0, "checkpointer state must be gone after expiry"
            print(f"assert OK: checkpointer rows for this thread after expiry = {count_after} (time-travel unavailable)")

            async with async_session_factory() as db:
                still_there = await RequirementRepo.get_by_id(db, id=req_id, session_id=session.id)
            assert still_there is not None and still_there.status == VersionStatus.active
            print(f"assert OK: the APPROVED requirement (id={req_id}) is STILL in Postgres, "
                  f"untouched by checkpointer expiry — status={still_there.status}")
        finally:
            os.environ.pop("SYSML_THREAD_TTL_DAYS", None)
            get_settings.cache_clear()

    await cleanup_user(user)
    print("Scenario 4+5 PASSED")


async def main() -> None:
    await test_operational_fresh_thread()
    await clear_checkpoints()
    await test_functional_with_operational_present()
    await clear_checkpoints()
    await test_functional_without_operational()
    await clear_checkpoints()
    await test_ttl_and_last_accessed()
    await clear_checkpoints()
    print("\n=== LEVEL RESOLUTION + TTL TEST SUITE PASSED ===")


if __name__ == "__main__":
    asyncio.run(main())
>>>>>>> Stashed changes
