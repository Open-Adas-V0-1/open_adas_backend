<<<<<<< Updated upstream
"""Standalone tests for the Layer-3 rebuild: plan -> generate -> verify loop on real
SysML v2 tooling (daltskin/sysml-v2-lsp), wrapping the T4 review/finalize core.

The supervisor/plan/generate LLM call sites are stubbed (same rationale as prior layers:
local Ollama models don't reliably support LangChain structured-output tool-calling in
this environment). verify_node, however, runs for REAL against the actual LSP and MCP
servers (tools/sysml_v2) — that's the whole point of this test: proving the iterative
verify loop against the real tool, not a mock of it.

Run: python -m scripts.smoke_test_layer3_rebuild
"""
import asyncio
import os
import uuid
from unittest.mock import patch

# NOTE: unlike the T5a/T6a scripts, this one does NOT force WindowsSelectorEventLoopPolicy.
# Those needed it for AsyncPostgresSaver/psycopg; this script uses MemorySaver (no psycopg)
# plus asyncio subprocesses for the SysML v2 tooling, which require the default
# ProactorEventLoop on Windows — the two requirements are mutually exclusive there.

from langgraph.checkpoint.memory import MemorySaver  # noqa: E402
from langgraph.types import Command  # noqa: E402

from agents.sysml.graph import build_sysml_graph  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.schemas.sysml import Intent, IntentDecision  # noqa: E402
from data.db import async_session_factory  # noqa: E402
from data.models import DiagramType, RequirementLevel, VersionStatus  # noqa: E402
from data.repository import (  # noqa: E402
    DiagramRepo,
    ProjectRepo,
    RequirementRepo,
    SessionRepo,
    UserRepo,
)
from tools.sysml_v2.lsp_client import shutdown_lsp_client  # noqa: E402
from tools.sysml_v2.mcp_client import shutdown_mcp_client  # noqa: E402

VALID_REQUIREMENT = """package BrakingSystem {
    part def Vehicle {
        attribute stoppingDistance : ISQ::LengthValue;
    }
    part vehicle : Vehicle {
        attribute :>> stoppingDistance = 45 [SI::m];
    }
    requirement def StoppingDistanceRequirement {
        doc /* The vehicle shall stop within 50 meters when braking. */
        subject veh : Vehicle;
        require constraint { veh.stoppingDistance <= 50 [SI::m] }
    }
}
"""
INVALID_REQUIREMENT = VALID_REQUIREMENT.replace("requirement def", "requirment def")

VALID_DIAGRAM_MODEL = """package BrakeStates {
    part def BrakingController {
        state def Idle;
        state def Braking;
    }
    part controller : BrakingController;
}
"""


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
        user = await UserRepo.create(db, email=f"l3-{uuid.uuid4()}@test.dev", password_hash="hashed")
        project = await ProjectRepo.create(db, user_id=user.id, name="Layer3 Project")
        session = await SessionRepo.create(
            db, project_id=project.id, thread_id=str(uuid.uuid4()), title="Layer3 Session"
        )
        await db.commit()
        return user, session


async def cleanup_user(user):
    async with async_session_factory() as db:
        db_user = await UserRepo.get_by_id(db, user.id)
        await db.delete(db_user)
        await db.commit()


# ---------------------------------------------------------------------------
# Scenario 1: happy path — clean on first generation.
# ---------------------------------------------------------------------------
async def test_happy_path():
    print("\n--- Scenario 1: happy path (generate clean SysML v2, verify CLEAN, approve) ---")
    user, session = await setup_user_project_session()

    supervisor_llm = FakeStructuredWrapperLLM(IntentDecision(intent=Intent.generate_requirement))
    plan_llm = FakeSequenceLLM(["Requirement def with subject Vehicle and a stopping-distance constraint."])
    generate_llm = FakeSequenceLLM([VALID_REQUIREMENT])

    def fake_get_llm(node_name=None):
        if node_name == "sysml_supervisor":
            return supervisor_llm
        if node_name == "sysml_plan":
            return plan_llm
        if node_name == "sysml_generate":
            return generate_llm
        raise AssertionError(f"unexpected node_name: {node_name}")

    with patch("agents.sysml.nodes.get_llm", side_effect=fake_get_llm):
        checkpointer = MemorySaver()
        graph = build_sysml_graph(checkpointer=checkpointer)
        config = {"configurable": {"thread_id": str(session.id)}}

        result_1 = await graph.ainvoke(
            {
                "user_input": "I need a requirement about braking distance",
                "session_id": session.id,
                "level": "functional",
            },
            config,
        )
        assert result_1.get("__interrupt__"), "expected pause at requirement_review"
        payload = result_1["__interrupt__"][0].value
        assert payload["verify_clean"] is True
        assert payload["verify_diagnostics"] == []
        print(f"RUN 1: paused at review. verify_clean={payload['verify_clean']} "
              f"generate_calls={generate_llm.calls}")
        assert generate_llm.calls == 1, "clean on the first attempt must not trigger a second generate"

        async with async_session_factory() as db:
            rows = await RequirementRepo.list_by_session(db, session_id=session.id)
            assert rows == [], "no DB write before approval"
        print("assert OK: no DB row before approval")

        result_2 = await graph.ainvoke(Command(resume={"action": "approve"}), config)
        assert result_2.get("result") == "finalized"
        print(f"RUN 2: completed. result={result_2['result']!r}")

        async with async_session_factory() as db:
            rows = await RequirementRepo.list_by_session(db, session_id=session.id)
            assert len(rows) == 1
            requirement = rows[0]
            assert requirement.content == VALID_REQUIREMENT
            assert requirement.status == VersionStatus.active
            assert requirement.session_id == session.id
            assert requirement.level.value == "functional"
            assert requirement.metadata_["verify_clean_at_approval"] is True
            assert requirement.metadata_["regeneration_rounds"] == 1
            print(f"assert OK: finalized, keyed by session(=thread) {session.id} + level=functional. "
                  f"metadata={requirement.metadata_}")

    await cleanup_user(user)
    print("Scenario 1 PASSED")


# ---------------------------------------------------------------------------
# Scenario 2: verify-loop — first draft has an error, regenerate fixes it.
# ---------------------------------------------------------------------------
async def test_verify_loop_recovers():
    print("\n--- Scenario 2: verify-loop (bad draft -> diagnostics -> regenerate -> clean) ---")
    user, session = await setup_user_project_session()

    supervisor_llm = FakeStructuredWrapperLLM(IntentDecision(intent=Intent.generate_requirement))
    plan_llm = FakeSequenceLLM(["Requirement def with subject Vehicle and a stopping-distance constraint."])
    generate_llm = FakeSequenceLLM([INVALID_REQUIREMENT, VALID_REQUIREMENT])

    def fake_get_llm(node_name=None):
        if node_name == "sysml_supervisor":
            return supervisor_llm
        if node_name == "sysml_plan":
            return plan_llm
        if node_name == "sysml_generate":
            return generate_llm
        raise AssertionError(f"unexpected node_name: {node_name}")

    with patch("agents.sysml.nodes.get_llm", side_effect=fake_get_llm):
        checkpointer = MemorySaver()
        graph = build_sysml_graph(checkpointer=checkpointer)
        config = {"configurable": {"thread_id": str(session.id)}}

        result = await graph.ainvoke(
            {"user_input": "I need a requirement about braking distance", "session_id": session.id}, config
        )
        assert result.get("__interrupt__"), "expected pause at requirement_review after the loop clears"
        payload = result["__interrupt__"][0].value

        print(f"generate_node ran {generate_llm.calls} time(s)")
        assert generate_llm.calls == 2, "must have regenerated once after the first draft's diagnostics"
        assert payload["verify_clean"] is True, "loop must exit on CLEAN, not just 'tried once'"
        assert payload["verify_diagnostics"] == []
        assert payload["draft"] == VALID_REQUIREMENT
        print(f"assert OK: loop exited on CLEAN after regeneration. verify_clean={payload['verify_clean']}")

        async with async_session_factory() as db:
            rows = await RequirementRepo.list_by_session(db, session_id=session.id)
            assert rows == [], "no DB write before approval, even across multiple verify rounds"
        print("assert OK: no DB row before approval (across the whole verify loop)")

        result_2 = await graph.ainvoke(Command(resume={"action": "approve"}), config)
        assert result_2.get("result") == "finalized"

        async with async_session_factory() as db:
            rows = await RequirementRepo.list_by_session(db, session_id=session.id)
            assert len(rows) == 1
            assert rows[0].metadata_["regeneration_rounds"] == 2
            print(f"assert OK: finalized. regeneration_rounds={rows[0].metadata_['regeneration_rounds']}")

    await cleanup_user(user)
    print("Scenario 2 PASSED")


# ---------------------------------------------------------------------------
# Scenario 3: fail-open — persistent errors hit the guard, hand to human with a warning.
# ---------------------------------------------------------------------------
async def test_fail_open_on_persistent_errors():
    print("\n--- Scenario 3: fail-open (SYSML_PROC_MAX_VISITS=2, persistent errors) ---")

    os.environ["SYSML_PROC_MAX_VISITS"] = "2"
    get_settings.cache_clear()
    settings = get_settings()
    print(f"Settings.sysml_proc_max_visits = {settings.sysml_proc_max_visits} (env override; .env default is 5)")
    assert settings.sysml_proc_max_visits == 2

    user, session = await setup_user_project_session()

    supervisor_llm = FakeStructuredWrapperLLM(IntentDecision(intent=Intent.generate_requirement))
    plan_llm = FakeSequenceLLM(["Requirement def with subject Vehicle and a stopping-distance constraint."])
    # ALWAYS invalid, regardless of feedback -> the loop can never reach CLEAN on its own.
    generate_llm = FakeSequenceLLM([INVALID_REQUIREMENT])

    def fake_get_llm(node_name=None):
        if node_name == "sysml_supervisor":
            return supervisor_llm
        if node_name == "sysml_plan":
            return plan_llm
        if node_name == "sysml_generate":
            return generate_llm
        raise AssertionError(f"unexpected node_name: {node_name}")

    try:
        with patch("agents.sysml.nodes.get_llm", side_effect=fake_get_llm):
            checkpointer = MemorySaver()
            graph = build_sysml_graph(checkpointer=checkpointer)
            config = {"configurable": {"thread_id": str(session.id)}}

            result = await graph.ainvoke(
                {"user_input": "I need a requirement about braking distance", "session_id": session.id}, config
            )
            assert result.get("__interrupt__"), "must hand off to human, not crash or hang"
            payload = result["__interrupt__"][0].value

            print(f"generate_node ran {generate_llm.calls} time(s) (capped by max_visits=2)")
            assert generate_llm.calls == 2, "must stop generating once max_visits is reached"
            assert payload["verify_clean"] is False
            assert len(payload["verify_diagnostics"]) > 0, "remaining diagnostics must be shown to the human"
            assert payload["verify_warning"] is not None, "a warning must accompany the fail-open handoff"
            print(f"assert OK: fail-open handoff. verify_clean={payload['verify_clean']} "
                  f"warning={payload['verify_warning']!r}")
            print(f"remaining diagnostics shown to human: {payload['verify_diagnostics']}")

            async with async_session_factory() as db:
                rows = await RequirementRepo.list_by_session(db, session_id=session.id)
                assert rows == [], "no DB write before approval, even on the fail-open path"
            print("assert OK: no DB row before approval")

        await cleanup_user(user)
    finally:
        os.environ.pop("SYSML_PROC_MAX_VISITS", None)
        get_settings.cache_clear()

    print("Scenario 3 PASSED")


# ---------------------------------------------------------------------------
# Scenario 4: diagram path — generate a model, verify, derive Mermaid, approve, finalize.
# ---------------------------------------------------------------------------
async def test_diagram_path():
    print("\n--- Scenario 4: diagram path (model -> verify -> Mermaid -> approve -> finalize) ---")
    user, session = await setup_user_project_session()

    async with async_session_factory() as db:
        base_requirement = await RequirementRepo.finalize(
            db, session_id=session.id, content=VALID_REQUIREMENT, level=RequirementLevel.functional,
        )
        await db.commit()
        requirement_id = base_requirement.id

    supervisor_llm = FakeStructuredWrapperLLM(
        IntentDecision(intent=Intent.generate_diagram, diagram_type=DiagramType.state_machine)
    )
    plan_llm = FakeSequenceLLM(["State machine with Idle and Braking states for the braking controller."])
    generate_llm = FakeSequenceLLM([VALID_DIAGRAM_MODEL])

    def fake_get_llm(node_name=None):
        if node_name == "sysml_supervisor":
            return supervisor_llm
        if node_name == "sysml_plan":
            return plan_llm
        if node_name == "sysml_generate":
            return generate_llm
        raise AssertionError(f"unexpected node_name: {node_name}")

    with patch("agents.sysml.nodes.get_llm", side_effect=fake_get_llm):
        checkpointer = MemorySaver()
        graph = build_sysml_graph(checkpointer=checkpointer)
        config = {"configurable": {"thread_id": str(session.id)}}

        result_1 = await graph.ainvoke(
            {
                "user_input": "give me a state machine diagram of the braking controller",
                "session_id": session.id,
                "target_requirement_id": requirement_id,
            },
            config,
        )
        assert result_1.get("__interrupt__")
        payload = result_1["__interrupt__"][0].value
        assert payload["source_node"] == "diagram"
        assert payload["verify_clean"] is True
        assert payload["mermaid"], "Mermaid must have been derived by verify_node before review"
        print(f"RUN 1: paused. verify_clean={payload['verify_clean']} mermaid derived "
              f"({len(payload['mermaid'])} chars): {payload['mermaid'][:80]}...")

        async with async_session_factory() as db:
            diagrams = await DiagramRepo.get_by_requirement(db, requirement_id=requirement_id, session_id=session.id)
            assert diagrams == [], "no DB write before approval"
        print("assert OK: no diagram row before approval")

        result_2 = await graph.ainvoke(Command(resume={"action": "approve"}), config)
        assert result_2.get("result") == "finalized"

        async with async_session_factory() as db:
            diagrams = await DiagramRepo.get_by_requirement(db, requirement_id=requirement_id, session_id=session.id)
            assert len(diagrams) == 1
            diagram = diagrams[0]
            assert diagram.sysml_text == VALID_DIAGRAM_MODEL
            assert diagram.mermaid == payload["mermaid"]
            assert diagram.status == VersionStatus.active
            assert diagram.type == DiagramType.state_machine
            print(f"assert OK: finalize stored BOTH the SysML v2 model and the derived Mermaid. "
                  f"sysml_text_len={len(diagram.sysml_text)} mermaid_len={len(diagram.mermaid)}")

    await cleanup_user(user)
    print("Scenario 4 PASSED")


async def main() -> None:
    try:
        await test_happy_path()
        await test_verify_loop_recovers()
        await test_fail_open_on_persistent_errors()
        await test_diagram_path()
    finally:
        await shutdown_lsp_client()
        await shutdown_mcp_client()

    print("\n=== LAYER-3 REBUILD TEST SUITE PASSED ===")


if __name__ == "__main__":
    asyncio.run(main())
=======
"""Standalone tests for the Layer-3 rebuild: plan -> generate -> verify loop on real
SysML v2 tooling (daltskin/sysml-v2-lsp), wrapping the T4 review/finalize core.

The supervisor/plan/generate LLM call sites are stubbed (same rationale as prior layers:
local Ollama models don't reliably support LangChain structured-output tool-calling in
this environment). verify_node, however, runs for REAL against the actual LSP and MCP
servers (tools/sysml_v2) — that's the whole point of this test: proving the iterative
verify loop against the real tool, not a mock of it.

Run: python -m scripts.smoke_test_layer3_rebuild
"""
import asyncio
import os
import uuid
from unittest.mock import patch

# NOTE: unlike the T5a/T6a scripts, this one does NOT force WindowsSelectorEventLoopPolicy.
# Those needed it for AsyncPostgresSaver/psycopg; this script uses MemorySaver (no psycopg)
# plus asyncio subprocesses for the SysML v2 tooling, which require the default
# ProactorEventLoop on Windows — the two requirements are mutually exclusive there.

from langgraph.checkpoint.memory import MemorySaver  # noqa: E402
from langgraph.types import Command  # noqa: E402

from agents.sysml.graph import build_sysml_graph  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.schemas.sysml import Intent, IntentDecision  # noqa: E402
from data.db import async_session_factory  # noqa: E402
from data.models import DiagramType, RequirementLevel, VersionStatus  # noqa: E402
from data.repository import (  # noqa: E402
    DiagramRepo,
    ProjectRepo,
    RequirementRepo,
    SessionRepo,
    UserRepo,
)
from tools.sysml_v2.lsp_client import shutdown_lsp_client  # noqa: E402
from tools.sysml_v2.mcp_client import shutdown_mcp_client  # noqa: E402

VALID_REQUIREMENT = """package BrakingSystem {
    part def Vehicle {
        attribute stoppingDistance : ISQ::LengthValue;
    }
    part vehicle : Vehicle {
        attribute :>> stoppingDistance = 45 [SI::m];
    }
    requirement def StoppingDistanceRequirement {
        doc /* The vehicle shall stop within 50 meters when braking. */
        subject veh : Vehicle;
        require constraint { veh.stoppingDistance <= 50 [SI::m] }
    }
}
"""
INVALID_REQUIREMENT = VALID_REQUIREMENT.replace("requirement def", "requirment def")

VALID_DIAGRAM_MODEL = """package BrakeStates {
    part def BrakingController {
        state def Idle;
        state def Braking;
    }
    part controller : BrakingController;
}
"""


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


async def setup_user_project_session():
    async with async_session_factory() as db:
        user = await UserRepo.create(db, email=f"l3-{uuid.uuid4()}@test.dev", password_hash="hashed")
        project = await ProjectRepo.create(db, user_id=user.id, name="Layer3 Project")
        session = await SessionRepo.create(
            db, project_id=project.id, thread_id=str(uuid.uuid4()), title="Layer3 Session"
        )
        await db.commit()
        return user, session


async def cleanup_user(user):
    async with async_session_factory() as db:
        db_user = await UserRepo.get_by_id(db, user.id)
        await db.delete(db_user)
        await db.commit()


# ---------------------------------------------------------------------------
# Scenario 1: happy path — clean on first generation.
# ---------------------------------------------------------------------------
async def test_happy_path():
    print("\n--- Scenario 1: happy path (generate clean SysML v2, verify CLEAN, approve) ---")
    user, session = await setup_user_project_session()

    supervisor_llm = FakeStructuredWrapperLLM(IntentDecision(intent=Intent.generate_requirement))
    plan_llm = FakeSequenceLLM(["Requirement def with subject Vehicle and a stopping-distance constraint."])
    generate_llm = FakeSequenceLLM([VALID_REQUIREMENT])

    def fake_get_llm(node_name=None):
        if node_name == "sysml_supervisor":
            return supervisor_llm
        if node_name == "sysml_plan":
            return plan_llm
        if node_name == "sysml_generate":
            return generate_llm
        raise AssertionError(f"unexpected node_name: {node_name}")

    with patch("agents.sysml.nodes.get_llm", side_effect=fake_get_llm):
        checkpointer = MemorySaver()
        graph = build_sysml_graph(checkpointer=checkpointer)
        config = {"configurable": {"thread_id": str(session.id)}}

        result_1 = await graph.ainvoke(
            {
                "user_input": "I need a requirement about braking distance",
                "session_id": session.id,
                "level": "functional",
            },
            config,
        )
        assert result_1.get("__interrupt__"), "expected pause at requirement_review"
        payload = result_1["__interrupt__"][0].value
        assert payload["verify_clean"] is True
        assert payload["verify_diagnostics"] == []
        print(f"RUN 1: paused at review. verify_clean={payload['verify_clean']} "
              f"generate_calls={generate_llm.calls}")
        assert generate_llm.calls == 1, "clean on the first attempt must not trigger a second generate"

        async with async_session_factory() as db:
            rows = await RequirementRepo.list_by_session(db, session_id=session.id)
            assert rows == [], "no DB write before approval"
        print("assert OK: no DB row before approval")

        result_2 = await graph.ainvoke(Command(resume={"action": "approve"}), config)
        assert result_2.get("result") == "finalized"
        print(f"RUN 2: completed. result={result_2['result']!r}")

        async with async_session_factory() as db:
            rows = await RequirementRepo.list_by_session(db, session_id=session.id)
            assert len(rows) == 1
            requirement = rows[0]
            assert requirement.content == VALID_REQUIREMENT
            assert requirement.status == VersionStatus.active
            assert requirement.session_id == session.id
            assert requirement.level.value == "functional"
            assert requirement.metadata_["verify_clean_at_approval"] is True
            assert requirement.metadata_["regeneration_rounds"] == 1
            print(f"assert OK: finalized, keyed by session(=thread) {session.id} + level=functional. "
                  f"metadata={requirement.metadata_}")

    await cleanup_user(user)
    print("Scenario 1 PASSED")


# ---------------------------------------------------------------------------
# Scenario 2: verify-loop — first draft has an error, regenerate fixes it.
# ---------------------------------------------------------------------------
async def test_verify_loop_recovers():
    print("\n--- Scenario 2: verify-loop (bad draft -> diagnostics -> regenerate -> clean) ---")
    user, session = await setup_user_project_session()

    supervisor_llm = FakeStructuredWrapperLLM(IntentDecision(intent=Intent.generate_requirement))
    plan_llm = FakeSequenceLLM(["Requirement def with subject Vehicle and a stopping-distance constraint."])
    generate_llm = FakeSequenceLLM([INVALID_REQUIREMENT, VALID_REQUIREMENT])

    def fake_get_llm(node_name=None):
        if node_name == "sysml_supervisor":
            return supervisor_llm
        if node_name == "sysml_plan":
            return plan_llm
        if node_name == "sysml_generate":
            return generate_llm
        raise AssertionError(f"unexpected node_name: {node_name}")

    with patch("agents.sysml.nodes.get_llm", side_effect=fake_get_llm):
        checkpointer = MemorySaver()
        graph = build_sysml_graph(checkpointer=checkpointer)
        config = {"configurable": {"thread_id": str(session.id)}}

        result = await graph.ainvoke(
            {"user_input": "I need a requirement about braking distance", "session_id": session.id}, config
        )
        assert result.get("__interrupt__"), "expected pause at requirement_review after the loop clears"
        payload = result["__interrupt__"][0].value

        print(f"generate_node ran {generate_llm.calls} time(s)")
        assert generate_llm.calls == 2, "must have regenerated once after the first draft's diagnostics"
        assert payload["verify_clean"] is True, "loop must exit on CLEAN, not just 'tried once'"
        assert payload["verify_diagnostics"] == []
        assert payload["draft"] == VALID_REQUIREMENT
        print(f"assert OK: loop exited on CLEAN after regeneration. verify_clean={payload['verify_clean']}")

        async with async_session_factory() as db:
            rows = await RequirementRepo.list_by_session(db, session_id=session.id)
            assert rows == [], "no DB write before approval, even across multiple verify rounds"
        print("assert OK: no DB row before approval (across the whole verify loop)")

        result_2 = await graph.ainvoke(Command(resume={"action": "approve"}), config)
        assert result_2.get("result") == "finalized"

        async with async_session_factory() as db:
            rows = await RequirementRepo.list_by_session(db, session_id=session.id)
            assert len(rows) == 1
            assert rows[0].metadata_["regeneration_rounds"] == 2
            print(f"assert OK: finalized. regeneration_rounds={rows[0].metadata_['regeneration_rounds']}")

    await cleanup_user(user)
    print("Scenario 2 PASSED")


# ---------------------------------------------------------------------------
# Scenario 3: fail-open — persistent errors hit the guard, hand to human with a warning.
# ---------------------------------------------------------------------------
async def test_fail_open_on_persistent_errors():
    print("\n--- Scenario 3: fail-open (SYSML_PROC_MAX_VISITS=2, persistent errors) ---")

    os.environ["SYSML_PROC_MAX_VISITS"] = "2"
    get_settings.cache_clear()
    settings = get_settings()
    print(f"Settings.sysml_proc_max_visits = {settings.sysml_proc_max_visits} (env override; .env default is 5)")
    assert settings.sysml_proc_max_visits == 2

    user, session = await setup_user_project_session()

    supervisor_llm = FakeStructuredWrapperLLM(IntentDecision(intent=Intent.generate_requirement))
    plan_llm = FakeSequenceLLM(["Requirement def with subject Vehicle and a stopping-distance constraint."])
    # ALWAYS invalid, regardless of feedback -> the loop can never reach CLEAN on its own.
    generate_llm = FakeSequenceLLM([INVALID_REQUIREMENT])

    def fake_get_llm(node_name=None):
        if node_name == "sysml_supervisor":
            return supervisor_llm
        if node_name == "sysml_plan":
            return plan_llm
        if node_name == "sysml_generate":
            return generate_llm
        raise AssertionError(f"unexpected node_name: {node_name}")

    try:
        with patch("agents.sysml.nodes.get_llm", side_effect=fake_get_llm):
            checkpointer = MemorySaver()
            graph = build_sysml_graph(checkpointer=checkpointer)
            config = {"configurable": {"thread_id": str(session.id)}}

            result = await graph.ainvoke(
                {"user_input": "I need a requirement about braking distance", "session_id": session.id}, config
            )
            assert result.get("__interrupt__"), "must hand off to human, not crash or hang"
            payload = result["__interrupt__"][0].value

            print(f"generate_node ran {generate_llm.calls} time(s) (capped by max_visits=2)")
            assert generate_llm.calls == 2, "must stop generating once max_visits is reached"
            assert payload["verify_clean"] is False
            assert len(payload["verify_diagnostics"]) > 0, "remaining diagnostics must be shown to the human"
            assert payload["verify_warning"] is not None, "a warning must accompany the fail-open handoff"
            print(f"assert OK: fail-open handoff. verify_clean={payload['verify_clean']} "
                  f"warning={payload['verify_warning']!r}")
            print(f"remaining diagnostics shown to human: {payload['verify_diagnostics']}")

            async with async_session_factory() as db:
                rows = await RequirementRepo.list_by_session(db, session_id=session.id)
                assert rows == [], "no DB write before approval, even on the fail-open path"
            print("assert OK: no DB row before approval")

        await cleanup_user(user)
    finally:
        os.environ.pop("SYSML_PROC_MAX_VISITS", None)
        get_settings.cache_clear()

    print("Scenario 3 PASSED")


# ---------------------------------------------------------------------------
# Scenario 4: diagram path — generate a model, verify, derive Mermaid, approve, finalize.
# ---------------------------------------------------------------------------
async def test_diagram_path():
    print("\n--- Scenario 4: diagram path (model -> verify -> Mermaid -> approve -> finalize) ---")
    user, session = await setup_user_project_session()

    async with async_session_factory() as db:
        base_requirement = await RequirementRepo.finalize(
            db, session_id=session.id, content=VALID_REQUIREMENT, level=RequirementLevel.functional,
        )
        await db.commit()
        requirement_id = base_requirement.id

    supervisor_llm = FakeStructuredWrapperLLM(
        IntentDecision(intent=Intent.generate_diagram, diagram_type=DiagramType.state_machine)
    )
    plan_llm = FakeSequenceLLM(["State machine with Idle and Braking states for the braking controller."])
    generate_llm = FakeSequenceLLM([VALID_DIAGRAM_MODEL])

    def fake_get_llm(node_name=None):
        if node_name == "sysml_supervisor":
            return supervisor_llm
        if node_name == "sysml_plan":
            return plan_llm
        if node_name == "sysml_generate":
            return generate_llm
        raise AssertionError(f"unexpected node_name: {node_name}")

    with patch("agents.sysml.nodes.get_llm", side_effect=fake_get_llm):
        checkpointer = MemorySaver()
        graph = build_sysml_graph(checkpointer=checkpointer)
        config = {"configurable": {"thread_id": str(session.id)}}

        result_1 = await graph.ainvoke(
            {
                "user_input": "give me a state machine diagram of the braking controller",
                "session_id": session.id,
                "target_requirement_id": requirement_id,
            },
            config,
        )
        assert result_1.get("__interrupt__")
        payload = result_1["__interrupt__"][0].value
        assert payload["source_node"] == "diagram"
        assert payload["verify_clean"] is True
        assert payload["mermaid"], "Mermaid must have been derived by verify_node before review"
        print(f"RUN 1: paused. verify_clean={payload['verify_clean']} mermaid derived "
              f"({len(payload['mermaid'])} chars): {payload['mermaid'][:80]}...")

        async with async_session_factory() as db:
            diagrams = await DiagramRepo.get_by_requirement(db, requirement_id=requirement_id, session_id=session.id)
            assert diagrams == [], "no DB write before approval"
        print("assert OK: no diagram row before approval")

        result_2 = await graph.ainvoke(Command(resume={"action": "approve"}), config)
        assert result_2.get("result") == "finalized"

        async with async_session_factory() as db:
            diagrams = await DiagramRepo.get_by_requirement(db, requirement_id=requirement_id, session_id=session.id)
            assert len(diagrams) == 1
            diagram = diagrams[0]
            assert diagram.sysml_text == VALID_DIAGRAM_MODEL
            assert diagram.mermaid == payload["mermaid"]
            assert diagram.status == VersionStatus.active
            assert diagram.type == DiagramType.state_machine
            print(f"assert OK: finalize stored BOTH the SysML v2 model and the derived Mermaid. "
                  f"sysml_text_len={len(diagram.sysml_text)} mermaid_len={len(diagram.mermaid)}")

    await cleanup_user(user)
    print("Scenario 4 PASSED")


async def main() -> None:
    try:
        await test_happy_path()
        await test_verify_loop_recovers()
        await test_fail_open_on_persistent_errors()
        await test_diagram_path()
    finally:
        await shutdown_lsp_client()
        await shutdown_mcp_client()

    print("\n=== LAYER-3 REBUILD TEST SUITE PASSED ===")


if __name__ == "__main__":
    asyncio.run(main())
>>>>>>> Stashed changes
