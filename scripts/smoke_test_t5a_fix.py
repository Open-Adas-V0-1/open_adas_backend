<<<<<<< Updated upstream
"""Tiny check for T5a-fix: SYSML_MIDDLE_MAX_VISITS is genuinely read from env/Settings,
not hardcoded — set it very low and confirm the loop guard actually triggers, and that
a breach ends the run safely (fail-open) rather than crashing or looping.

Must set the env var BEFORE importing anything that transitively imports app.config,
since python-dotenv's load_dotenv() (called at app.config import time) does not
override an already-set os.environ value.

Run: python -m scripts.smoke_test_t5a_fix
"""
import asyncio
import os
import sys
import uuid

os.environ["SYSML_MIDDLE_MAX_VISITS"] = "1"  # must happen before any project import

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

from unittest.mock import patch  # noqa: E402

from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver  # noqa: E402
from langgraph.types import Command  # noqa: E402
from sqlalchemy import text  # noqa: E402

from agents.sysml.middle_graph import build_middle_config, build_middle_graph  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.schemas.sysml import Intent, MiddleDecision  # noqa: E402
from data.db import async_session_factory  # noqa: E402
from data.models import DiagramType  # noqa: E402
from data.repository import ProjectRepo, RequirementRepo, SessionRepo, UserRepo  # noqa: E402


class FakeMessage:
    def __init__(self, content):
        self.content = content


class FakeStructuredLLM:
    def __init__(self, decisions):
        self._decisions = decisions
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


async def main() -> None:
    settings = get_settings()
    print(f"Settings.sysml_middle_max_visits = {settings.sysml_middle_max_visits} "
          f"(env override applied; .env default is 10)")
    assert settings.sysml_middle_max_visits == 1, "env override did not take effect"

    async with async_session_factory() as db:
        user = await UserRepo.create(db, email=f"t5afix-{uuid.uuid4()}@test.dev", password_hash="hashed")
        project = await ProjectRepo.create(db, user_id=user.id, name="T5a-fix Project")
        session = await SessionRepo.create(
            db, project_id=project.id, thread_id=str(uuid.uuid4()), title="T5a-fix Session"
        )
        req_a = await RequirementRepo.create(db, session_id=session.id, content="Req A")
        req_a = await RequirementRepo.promote(db, id=req_a.id, session_id=session.id)
        req_b = await RequirementRepo.create(db, session_id=session.id, content="Req B")
        req_b = await RequirementRepo.promote(db, id=req_b.id, session_id=session.id)
        await db.commit()

    # 2 active requirements, none named -> ambiguous on visit 1 -> pauses at
    # user_confirm_inputs. Resuming with "modify" sends it back to middle_supervisor
    # for visit 2, which is where max_visits=1 must kick in.
    middle_llm = FakeStructuredWrapperLLM(
        [MiddleDecision(has_request=True, resolved_intent=Intent.generate_diagram, diagram_type=DiagramType.use_case)]
    )
    confirm_question_llm = FakeSequenceLLM(["Which requirement do you mean?"])

    def fake_middle_get_llm(node_name=None):
        if node_name == "sysml_middle_supervisor":
            return middle_llm
        if node_name == "sysml_confirm_question":
            return confirm_question_llm
        raise AssertionError(f"unexpected node_name: {node_name}")

    def fake_layer3_get_llm(node_name=None):
        raise AssertionError(f"layer-3 must never run: the guard should stop things first (node_name={node_name})")

    outer_thread_id = f"outer-{uuid.uuid4()}"

    async with AsyncPostgresSaver.from_conn_string(settings.checkpointer_database_url) as checkpointer:
        await checkpointer.setup()
        with patch("agents.sysml.middle_nodes.get_llm", side_effect=fake_middle_get_llm), \
             patch("agents.sysml.nodes.get_llm", side_effect=fake_layer3_get_llm):

            middle_graph = build_middle_graph(checkpointer=checkpointer)
            config = build_middle_config(outer_thread_id)
            print(f"invoke config includes recursion_limit={config['recursion_limit']} "
                  f"(from Settings.sysml_middle_recursion_limit, unchanged default here)")

            result_1 = await middle_graph.ainvoke(
                {"user_input": "give me a use case diagram", "session_id": session.id}, config
            )
            assert result_1.get("__interrupt__"), "expected pause at user_confirm_inputs (visit 1, ambiguous)"
            print("visit 1: paused at user_confirm_inputs (ambiguous), as expected")

            result_2 = await middle_graph.ainvoke(Command(resume={"action": "modify"}), config)

    print(f"\nfinal state after visit 2: result={result_2.get('result')!r} "
          f"supervisor_visits={result_2.get('supervisor_visits')}")

    assert not result_2.get("__interrupt__"), "guard breach must end the run, not pause again"
    assert result_2.get("result") == "stopped: max supervisor visits reached", (
        "guard did not trigger with SYSML_MIDDLE_MAX_VISITS=1 on the 2nd visit"
    )
    assert result_2.get("supervisor_visits") == 2
    print("assert OK: guard triggered on visit 2 (> max_visits=1), ended safely via fail-open to END — no crash")

    async with async_session_factory() as db:
        await db.execute(text("DELETE FROM checkpoint_writes"))
        await db.execute(text("DELETE FROM checkpoint_blobs"))
        await db.execute(text("DELETE FROM checkpoints"))
        db_user = await UserRepo.get_by_id(db, user.id)
        await db.delete(db_user)
        await db.commit()

    print("\n=== T5A-FIX ENV-GUARD CHECK PASSED ===")


if __name__ == "__main__":
    asyncio.run(main())
=======
"""Tiny check for T5a-fix: SYSML_MIDDLE_MAX_VISITS is genuinely read from env/Settings,
not hardcoded — set it very low and confirm the loop guard actually triggers, and that
a breach ends the run safely (fail-open) rather than crashing or looping.

Must set the env var BEFORE importing anything that transitively imports app.config,
since python-dotenv's load_dotenv() (called at app.config import time) does not
override an already-set os.environ value.

Run: python -m scripts.smoke_test_t5a_fix
"""
import asyncio
import os
import sys
import uuid

os.environ["SYSML_MIDDLE_MAX_VISITS"] = "1"  # must happen before any project import

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

from unittest.mock import patch  # noqa: E402

from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver  # noqa: E402
from langgraph.types import Command  # noqa: E402
from sqlalchemy import text  # noqa: E402

from agents.sysml.middle_graph import build_middle_config, build_middle_graph  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.schemas.sysml import Intent, MiddleDecision  # noqa: E402
from data.db import async_session_factory  # noqa: E402
from data.models import DiagramType  # noqa: E402
from data.repository import ProjectRepo, RequirementRepo, SessionRepo, UserRepo  # noqa: E402


class FakeMessage:
    def __init__(self, content):
        self.content = content


class FakeStructuredLLM:
    def __init__(self, decisions):
        self._decisions = decisions
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


async def main() -> None:
    settings = get_settings()
    print(f"Settings.sysml_middle_max_visits = {settings.sysml_middle_max_visits} "
          f"(env override applied; .env default is 10)")
    assert settings.sysml_middle_max_visits == 1, "env override did not take effect"

    async with async_session_factory() as db:
        user = await UserRepo.create(db, email=f"t5afix-{uuid.uuid4()}@test.dev", password_hash="hashed")
        project = await ProjectRepo.create(db, user_id=user.id, name="T5a-fix Project")
        session = await SessionRepo.create(
            db, project_id=project.id, thread_id=str(uuid.uuid4()), title="T5a-fix Session"
        )
        req_a = await RequirementRepo.create(db, session_id=session.id, content="Req A")
        req_a = await RequirementRepo.promote(db, id=req_a.id, session_id=session.id)
        req_b = await RequirementRepo.create(db, session_id=session.id, content="Req B")
        req_b = await RequirementRepo.promote(db, id=req_b.id, session_id=session.id)
        await db.commit()

    # 2 active requirements, none named -> ambiguous on visit 1 -> pauses at
    # user_confirm_inputs. Resuming with "modify" sends it back to middle_supervisor
    # for visit 2, which is where max_visits=1 must kick in.
    middle_llm = FakeStructuredWrapperLLM(
        [MiddleDecision(has_request=True, resolved_intent=Intent.generate_diagram, diagram_type=DiagramType.use_case)]
    )
    confirm_question_llm = FakeSequenceLLM(["Which requirement do you mean?"])

    def fake_middle_get_llm(node_name=None):
        if node_name == "sysml_middle_supervisor":
            return middle_llm
        if node_name == "sysml_confirm_question":
            return confirm_question_llm
        raise AssertionError(f"unexpected node_name: {node_name}")

    def fake_layer3_get_llm(node_name=None):
        raise AssertionError(f"layer-3 must never run: the guard should stop things first (node_name={node_name})")

    outer_thread_id = f"outer-{uuid.uuid4()}"

    async with AsyncPostgresSaver.from_conn_string(settings.checkpointer_database_url) as checkpointer:
        await checkpointer.setup()
        with patch("agents.sysml.middle_nodes.get_llm", side_effect=fake_middle_get_llm), \
             patch("agents.sysml.nodes.get_llm", side_effect=fake_layer3_get_llm):

            middle_graph = build_middle_graph(checkpointer=checkpointer)
            config = build_middle_config(outer_thread_id)
            print(f"invoke config includes recursion_limit={config['recursion_limit']} "
                  f"(from Settings.sysml_middle_recursion_limit, unchanged default here)")

            result_1 = await middle_graph.ainvoke(
                {"user_input": "give me a use case diagram", "session_id": session.id}, config
            )
            assert result_1.get("__interrupt__"), "expected pause at user_confirm_inputs (visit 1, ambiguous)"
            print("visit 1: paused at user_confirm_inputs (ambiguous), as expected")

            result_2 = await middle_graph.ainvoke(Command(resume={"action": "modify"}), config)

    print(f"\nfinal state after visit 2: result={result_2.get('result')!r} "
          f"supervisor_visits={result_2.get('supervisor_visits')}")

    assert not result_2.get("__interrupt__"), "guard breach must end the run, not pause again"
    assert result_2.get("result") == "stopped: max supervisor visits reached", (
        "guard did not trigger with SYSML_MIDDLE_MAX_VISITS=1 on the 2nd visit"
    )
    assert result_2.get("supervisor_visits") == 2
    print("assert OK: guard triggered on visit 2 (> max_visits=1), ended safely via fail-open to END — no crash")

    async with async_session_factory() as db:
        await db.execute(text("DELETE FROM checkpoint_writes"))
        await db.execute(text("DELETE FROM checkpoint_blobs"))
        await db.execute(text("DELETE FROM checkpoints"))
        db_user = await UserRepo.get_by_id(db, user.id)
        await db.delete(db_user)
        await db.commit()

    print("\n=== T5A-FIX ENV-GUARD CHECK PASSED ===")


if __name__ == "__main__":
    asyncio.run(main())
>>>>>>> Stashed changes
