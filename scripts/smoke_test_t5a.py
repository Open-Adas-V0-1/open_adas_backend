"""Standalone test for T5a: SysML middle layer on a REAL Postgres checkpointer.

LLM call sites are stubbed (same rationale as T4a/b/c: local Ollama models don't reliably
support LangChain structured-output tool-calling in this environment). Everything else —
graph wiring, TWO-level nested interrupt/resume bubbling, deterministic per-processing
thread_id, and persistence/promotion/metadata via the T2 repository — runs for real,
against the real Postgres instance from T1, using AsyncPostgresSaver (not MemorySaver).

Run: python -m scripts.smoke_test_t5a
"""
import asyncio
import sys
import uuid
from unittest.mock import patch

if sys.platform == "win32":
    # psycopg's async mode is incompatible with the default Windows ProactorEventLoop.
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.types import Command
from sqlalchemy import text

from agents.sysml.middle_graph import build_middle_graph
from app.config import get_settings
from app.schemas.sysml import Intent, IntentDecision, MiddleDecision
from data.db import async_session_factory
from data.repository import ProjectRepo, RequirementRepo, SessionRepo, UserRepo


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
    """Mimics get_llm(...).with_structured_output(schema) -> object with .ainvoke()."""

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
        user = await UserRepo.create(db, email=f"t5a-{uuid.uuid4()}@test.dev", password_hash="hashed")
        project = await ProjectRepo.create(db, user_id=user.id, name="T5a Project")
        session = await SessionRepo.create(
            db, project_id=project.id, thread_id=str(uuid.uuid4()), title="T5a Session"
        )
        await db.commit()
        return user, session


async def cleanup_user(user):
    async with async_session_factory() as db:
        db_user = await UserRepo.get_by_id(db, user.id)
        await db.delete(db_user)
        await db.commit()


async def distinct_checkpoint_thread_ids() -> list[str]:
    async with async_session_factory() as db:
        result = await db.execute(text("SELECT DISTINCT thread_id FROM checkpoints ORDER BY thread_id"))
        return [row[0] for row in result.fetchall()]


async def main() -> None:
    settings = get_settings()
    user, session = await setup_user_project_session()

    draft = "The system shall stop within 50 meters."

    # middle_supervisor is consulted twice: once to dispatch, once after the processing
    # loops back — the second time it must say "nothing more to do" or the loop guard
    # would eventually stop it anyway.
    middle_llm = FakeStructuredWrapperLLM(
        [
            MiddleDecision(has_request=True, resolved_intent=Intent.generate_requirement),
            MiddleDecision(has_request=False, message="Nothing further to process."),
        ]
    )
    layer3_supervisor_llm = FakeStructuredWrapperLLM(IntentDecision(intent=Intent.generate_requirement))
    generate_llm = FakeSequenceLLM([draft])

    def fake_middle_get_llm(node_name=None):
        if node_name == "sysml_middle_supervisor":
            return middle_llm
        raise AssertionError(f"unexpected node_name in middle_nodes: {node_name}")

    def fake_layer3_get_llm(node_name=None):
        if node_name == "sysml_supervisor":
            return layer3_supervisor_llm
        if node_name == "sysml_generate_requirement":
            return generate_llm
        raise AssertionError(f"unexpected node_name in layer-3 nodes: {node_name}")

    outer_thread_id = f"outer-{uuid.uuid4()}"
    proc_thread_id = f"{session.id}:proc:1"

    async with AsyncPostgresSaver.from_conn_string(settings.checkpointer_database_url) as checkpointer:
        await checkpointer.setup()

        with patch("agents.sysml.middle_nodes.get_llm", side_effect=fake_middle_get_llm), \
             patch("agents.sysml.nodes.get_llm", side_effect=fake_layer3_get_llm):

            middle_graph = build_middle_graph(checkpointer=checkpointer)
            config = {"configurable": {"thread_id": outer_thread_id}}

            print("=== RUN 1 (expect two-level nested pause, bubbling L3 -> middle -> caller) ===")
            result_1 = await middle_graph.ainvoke(
                {"user_input": "I need a requirement about braking distance", "session_id": session.id},
                config,
            )
            assert result_1.get("__interrupt__"), "expected the L3 interrupt to bubble up to the caller"
            payload = result_1["__interrupt__"][0].value
            print(f"Interrupt payload surfaced at the middle-graph caller: {payload}")
            assert payload["source_node"] == "requirement"
            assert payload["draft"] == draft

            async with async_session_factory() as db:
                rows = await RequirementRepo.list_by_session(db, session_id=session.id)
                assert rows == [], "no DB write before approval"
            print("assert OK: no requirement row exists before approval")

            print("\n=== RUN 2 (resume with approve) ===")
            result_2 = await middle_graph.ainvoke(Command(resume={"action": "approve"}), config)
            print(f"Final middle-graph state: resolved_intent={result_2.get('resolved_intent')!r} "
                  f"result={result_2.get('result')!r}")
            print(f"processing_result (LIGHT reference only): {result_2.get('processing_result')}")

            # --- Assertion 1: light reference only, no full artifact content in MiddleState ---
            light_ref = result_2.get("processing_result")
            assert light_ref is not None
            assert set(light_ref.keys()) == {"processing_id", "thread_id", "artifact_type", "artifact_id", "summary"}
            assert "content" not in light_ref and "draft" not in light_ref
            assert draft not in str(light_ref), "full artifact text must NOT be present in the light reference"
            print("assert OK: MiddleState's processing_result carries only ids + summary, not the full text")
            assert light_ref["thread_id"] == proc_thread_id

            # --- Assertion 2: full content + metadata are in Postgres ---
            async with async_session_factory() as db:
                rows = await RequirementRepo.list_by_session(db, session_id=session.id)
                assert len(rows) == 1, f"expected exactly 1 persisted requirement, got {len(rows)}"
                requirement = rows[0]
                assert requirement.content == draft
                assert str(requirement.id) == light_ref["artifact_id"]
                assert requirement.metadata_ is not None
                print(
                    f"assert OK: full requirement text + metadata are in Postgres. "
                    f"content={requirement.content!r} metadata={requirement.metadata_}"
                )

            # --- Assertion 3: distinct thread_ids in the checkpointer, mirroring the spike ---
            thread_ids = await distinct_checkpoint_thread_ids()
            print(f"\ndistinct thread_ids checkpointed in Postgres: {thread_ids}")
            assert outer_thread_id in thread_ids, "the outer (middle-graph) thread must be checkpointed"
            assert proc_thread_id in thread_ids, "layer-3's processing must be checkpointed under its OWN thread_id"
            assert outer_thread_id != proc_thread_id
            print(
                f"assert OK: layer-3 processing checkpointed under its own independent thread_id "
                f"({proc_thread_id!r}), distinct from the outer thread ({outer_thread_id!r}) — "
                f"mirrors the spike's Variant A result, now on Postgres"
            )

    await cleanup_user(user)
    print("\n=== T5A TEST PASSED ===")


if __name__ == "__main__":
    asyncio.run(main())
