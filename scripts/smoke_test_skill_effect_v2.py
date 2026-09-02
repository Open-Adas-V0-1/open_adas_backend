<<<<<<< Updated upstream
"""Honest skill-effect measurement, v2: harder/varied SysML v2 generation cases,
averaged over multiple real-model runs per condition.

The v1 script (scripts/smoke_test_skill_effect.py) used a single case that mostly
converged in 1 round either way — a ceiling effect that couldn't show whether the skill
helps. This version uses 6 cases chosen to actually stress what the skill's ERRORS.md/
PATTERNS.md/SYNTAX.md cover (and one case — state machines — chosen specifically because
the skill does NOT cover it, as a built-in negative control), runs each case N=3 times
per condition (skill ENABLED vs DISABLED), and reports averages + spread.

Real LLM calls against whatever backend is configured in .env (Capgemini/gpt-4o at the
time of writing) — sysml_plan/sysml_generate are NOT stubbed. sysml_supervisor IS stubbed
(fixed intent/level per case) so the experiment isolates generation quality, not intent
classification. Does NOT modify Layer-3 or skills/loader.py — measurement only.

Run: python -m scripts.smoke_test_skill_effect_v2
"""
import asyncio
import statistics
import uuid
from unittest.mock import patch

# Small pause between trials to avoid bursting the real backend's rate limit
# (observed 429s from Capgemini when firing requests back-to-back).
INTER_TRIAL_DELAY_SECONDS = 3

from langgraph.checkpoint.memory import MemorySaver
from langgraph.types import Command

from agents.sysml.graph import build_sysml_graph
from app.schemas.sysml import DiagramType, Intent, IntentDecision
from data.db import async_session_factory
from data.models import RequirementLevel
from data.repository import ProjectRepo, RequirementRepo, SessionRepo, UserRepo
from tools.sysml_v2.lsp_client import shutdown_lsp_client
from tools.sysml_v2.mcp_client import shutdown_mcp_client

N_TRIALS = 3

CASES = [
    {
        "name": "operational_vague",
        "level": "operational",
        "intent": Intent.generate_requirement,
        "diagram_type": None,
        "user_input": (
            "The vehicle shall be able to safely bring itself to a complete stop within "
            "the available road distance under normal operating conditions, without "
            "dictating a specific braking mechanism."
        ),
        "needs_base_requirement": False,
    },
    {
        "name": "functional_reserved_keyword_bait",
        "level": "functional",
        "intent": Intent.generate_requirement,
        "diagram_type": None,
        "user_input": (
            "Define a requirement for a braking control module: it must track the "
            "required brake torque (refer to this quantity as 'require') and the "
            "vehicle's current speed, and state that the required torque stays below "
            "4000 newton-meters."
        ),
        "needs_base_requirement": False,
    },
    {
        "name": "physical_multi_constraint",
        "level": "physical",
        "intent": Intent.generate_requirement,
        "diagram_type": None,
        "user_input": (
            "Define a physical-level requirement for a brake caliper assembly made of "
            "a caliper body and two mounting bolts: the assembly's combined mass shall "
            "not exceed 2.5 kilograms, and it shall interface with a hydraulic port "
            "rated for at least 10 megapascals of pressure."
        ),
        "needs_base_requirement": False,
    },
    {
        "name": "functional_enum_bait",
        "level": "functional",
        "intent": Intent.generate_requirement,
        "diagram_type": None,
        "user_input": (
            "Define a requirement for a brake pad selection system that must support "
            "three pad material options: ceramic, semi-metallic, and organic. The "
            "system shall correctly report the currently selected material."
        ),
        "needs_base_requirement": False,
    },
    {
        "name": "cross_reference_parts",
        "level": "functional",
        "intent": Intent.generate_diagram,
        "diagram_type": DiagramType.sequence,
        "user_input": (
            "Model how the ECU references the Wheel part's speed sensor: the ECU "
            "reads the wheel's current speed to compute the target deceleration."
        ),
        "needs_base_requirement": True,
        "base_requirement_text": (
            "The system shall compute target deceleration based on current wheel speed."
        ),
    },
    {
        "name": "state_machine_behavioral",
        "level": "functional",
        "intent": Intent.generate_diagram,
        "diagram_type": DiagramType.state_machine,
        "user_input": (
            "Model the states of a garage door opener: Closed, Opening, Open, Closing, "
            "with transitions triggered by a button press and by obstacle detection."
        ),
        "needs_base_requirement": True,
        "base_requirement_text": (
            "The system shall control the garage door through its Closed, Opening, "
            "Open, and Closing states."
        ),
    },
]


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


async def setup_session(label: str):
    async with async_session_factory() as db:
        user = await UserRepo.create(db, email=f"skilleffect2-{label}-{uuid.uuid4()}@test.dev", password_hash="hashed")
        project = await ProjectRepo.create(db, user_id=user.id, name=f"SkillEffect2 {label}")
        session = await SessionRepo.create(
            db, project_id=project.id, thread_id=str(uuid.uuid4()), title=f"SkillEffect2 {label}"
        )
        await db.commit()
        return user, session


async def maybe_create_base_requirement(session_id, case: dict) -> str | None:
    if not case.get("needs_base_requirement"):
        return None
    async with async_session_factory() as db:
        req = await RequirementRepo.finalize(
            db, session_id=session_id, content=case["base_requirement_text"], level=RequirementLevel.functional,
        )
        await db.commit()
        return str(req.id)


async def cleanup_user(user):
    async with async_session_factory() as db:
        db_user = await UserRepo.get_by_id(db, user.id)
        await db.delete(db_user)
        await db.commit()


async def run_single_trial(case: dict, disable_skill: bool) -> dict:
    user, session = await setup_session(case["name"])
    target_requirement_id = await maybe_create_base_requirement(session.id, case)

    supervisor_llm = FakeStructuredWrapperLLM(
        IntentDecision(intent=case["intent"], diagram_type=case.get("diagram_type"), level=None)
    )

    def fake_get_llm(node_name=None):
        if node_name == "sysml_supervisor":
            return supervisor_llm
        from llm.factory import get_llm as real_get_llm
        return real_get_llm(node_name)

    patches = [patch("agents.sysml.nodes.get_llm", side_effect=fake_get_llm)]
    if disable_skill:
        patches.append(patch("agents.sysml.nodes._skill_guidance", return_value=""))
        patches.append(patch("agents.sysml.nodes.get_error_help", return_value=""))

    for p in patches:
        p.start()
    try:
        checkpointer = MemorySaver()
        graph = build_sysml_graph(checkpointer=checkpointer)
        config = {"configurable": {"thread_id": str(session.id)}}

        initial_state = {
            "user_input": case["user_input"],
            "session_id": session.id,
            "level": case["level"],
        }
        if target_requirement_id:
            initial_state["target_requirement_id"] = target_requirement_id

        result = await graph.ainvoke(initial_state, config)

        rounds = result.get("verify_visits")
        clean = bool(result.get("verify_clean"))
        interrupt = result.get("__interrupt__", [None])[0]
        diag_count = len((interrupt.value or {}).get("verify_diagnostics") or []) if interrupt else 0

        # close the run out (we don't care about finalize/persistence for this measurement)
        await graph.ainvoke(Command(resume={"action": "approve"}), config)
    finally:
        for p in reversed(patches):
            p.stop()
        await cleanup_user(user)

    return {"rounds": rounds, "clean": clean, "diagnostics_remaining": diag_count}


async def run_case(case: dict) -> dict:
    print(f"\n=== Case: {case['name']} (level={case['level']}, "
          f"intent={case['intent'].value}{', diagram=' + case['diagram_type'].value if case.get('diagram_type') else ''}) ===")

    results = {"A_enabled": [], "B_disabled": []}
    for label, key, disable in [("A (skill ENABLED)", "A_enabled", False), ("B (skill DISABLED)", "B_disabled", True)]:
        for trial in range(1, N_TRIALS + 1):
            await asyncio.sleep(INTER_TRIAL_DELAY_SECONDS)
            try:
                r = await run_single_trial(case, disable_skill=disable)
            except Exception as exc:  # noqa: BLE001 - real backend flakiness (e.g. 429s); keep the batch going
                print(f"  {label} trial {trial}/{N_TRIALS}: FAILED ({type(exc).__name__}: {exc}) — skipped, not counted")
                continue
            results[key].append(r)
            print(f"  {label} trial {trial}/{N_TRIALS}: rounds={r['rounds']} clean={r['clean']} "
                  f"remaining_diagnostics={r['diagnostics_remaining']}")

    return results


def _summarize(runs: list[dict]) -> dict:
    if not runs:
        return {"avg_rounds": None, "stdev_rounds": None, "min_rounds": None, "max_rounds": None,
                "clean_rate": None, "n": 0}
    rounds = [r["rounds"] for r in runs]
    clean_count = sum(1 for r in runs if r["clean"])
    return {
        "avg_rounds": statistics.mean(rounds),
        "stdev_rounds": statistics.stdev(rounds) if len(rounds) > 1 else 0.0,
        "min_rounds": min(rounds),
        "max_rounds": max(rounds),
        "clean_rate": clean_count / len(runs),
        "n": len(runs),
    }


async def main() -> None:
    all_results: dict[str, dict] = {}
    for case in CASES:
        all_results[case["name"]] = await run_case(case)

    await shutdown_lsp_client()
    await shutdown_mcp_client()

    print("\n" + "=" * 100)
    print("SUMMARY — per case, average rounds and clean-rate (A = skill enabled, B = skill disabled)")
    print("=" * 100)
    header = (f"{'case':32s} {'A avg':>7s} {'A sd':>6s} {'A n':>4s} {'A clean%':>9s} "
              f"{'B avg':>7s} {'B sd':>6s} {'B n':>4s} {'B clean%':>9s} {'delta(A-B)':>11s}")
    print(header)
    print("-" * len(header))

    all_a_rounds: list[float] = []
    all_b_rounds: list[float] = []
    all_a_clean: list[float] = []
    all_b_clean: list[float] = []

    for case in CASES:
        name = case["name"]
        a = _summarize(all_results[name]["A_enabled"])
        b = _summarize(all_results[name]["B_disabled"])
        if a["n"] == 0 or b["n"] == 0:
            print(f"{name:32s}  (insufficient data: A n={a['n']}, B n={b['n']} — some trials failed)")
            continue
        delta = a["avg_rounds"] - b["avg_rounds"]
        print(f"{name:32s} {a['avg_rounds']:7.2f} {a['stdev_rounds']:6.2f} {a['n']:4d} {a['clean_rate']*100:8.0f}% "
              f"{b['avg_rounds']:7.2f} {b['stdev_rounds']:6.2f} {b['n']:4d} {b['clean_rate']*100:8.0f}% {delta:+11.2f}")
        all_a_rounds.extend(r["rounds"] for r in all_results[name]["A_enabled"])
        all_b_rounds.extend(r["rounds"] for r in all_results[name]["B_disabled"])
        all_a_clean.append(a["clean_rate"])
        all_b_clean.append(b["clean_rate"])

    print("-" * len(header))
    overall_a_avg = statistics.mean(all_a_rounds)
    overall_b_avg = statistics.mean(all_b_rounds)
    overall_a_clean = statistics.mean(all_a_clean)
    overall_b_clean = statistics.mean(all_b_clean)
    print(f"{'OVERALL':32s} {overall_a_avg:7.2f} {'':6s} {overall_a_clean*100:8.0f}% "
          f"{overall_b_avg:7.2f} {'':6s} {overall_b_clean*100:8.0f}% {overall_a_avg - overall_b_avg:+11.2f}")

    print("\n" + "=" * 100)
    print("CONCLUSION")
    print("=" * 100)
    round_delta = overall_a_avg - overall_b_avg
    clean_delta = overall_a_clean - overall_b_clean

    print(f"Overall average rounds: WITH skill = {overall_a_avg:.2f}, WITHOUT skill = {overall_b_avg:.2f} "
          f"(delta {round_delta:+.2f})")
    print(f"Overall clean-rate:     WITH skill = {overall_a_clean*100:.0f}%, WITHOUT skill = {overall_b_clean*100:.0f}% "
          f"(delta {clean_delta*100:+.0f} pts)")

    if round_delta < -0.15:
        print(f"\n=> The skill REDUCED average verify/regenerate rounds by {-round_delta:.2f} rounds overall.")
    elif round_delta > 0.15:
        print(f"\n=> The skill INCREASED average verify/regenerate rounds by {round_delta:.2f} rounds overall "
              "— it did not help on this measurement.")
    else:
        print("\n=> No meaningful difference in average rounds between WITH and WITHOUT the skill.")

    if clean_delta > 0.05:
        print(f"=> The skill IMPROVED the clean-rate by {clean_delta*100:.0f} points (fewer fail-open handoffs).")
    elif clean_delta < -0.05:
        print(f"=> The skill WORSENED the clean-rate by {-clean_delta*100:.0f} points.")
    else:
        print("=> No meaningful difference in clean-rate.")


if __name__ == "__main__":
    asyncio.run(main())
=======
"""Honest skill-effect measurement, v2: harder/varied SysML v2 generation cases,
averaged over multiple real-model runs per condition.

The v1 script (scripts/smoke_test_skill_effect.py) used a single case that mostly
converged in 1 round either way — a ceiling effect that couldn't show whether the skill
helps. This version uses 6 cases chosen to actually stress what the skill's ERRORS.md/
PATTERNS.md/SYNTAX.md cover (and one case — state machines — chosen specifically because
the skill does NOT cover it, as a built-in negative control), runs each case N=3 times
per condition (skill ENABLED vs DISABLED), and reports averages + spread.

Real LLM calls against whatever backend is configured in .env (Capgemini/gpt-4o at the
time of writing) — sysml_plan/sysml_generate are NOT stubbed. sysml_supervisor IS stubbed
(fixed intent/level per case) so the experiment isolates generation quality, not intent
classification. Does NOT modify Layer-3 or skills/loader.py — measurement only.

Run: python -m scripts.smoke_test_skill_effect_v2
"""
import asyncio
import statistics
import uuid
from unittest.mock import patch

# Small pause between trials to avoid bursting the real backend's rate limit
# (observed 429s from Capgemini when firing requests back-to-back).
INTER_TRIAL_DELAY_SECONDS = 3

from langgraph.checkpoint.memory import MemorySaver
from langgraph.types import Command

from agents.sysml.graph import build_sysml_graph
from app.schemas.sysml import DiagramType, Intent, IntentDecision
from data.db import async_session_factory
from data.models import RequirementLevel
from data.repository import ProjectRepo, RequirementRepo, SessionRepo, UserRepo
from tools.sysml_v2.lsp_client import shutdown_lsp_client
from tools.sysml_v2.mcp_client import shutdown_mcp_client

N_TRIALS = 3

CASES = [
    {
        "name": "operational_vague",
        "level": "operational",
        "intent": Intent.generate_requirement,
        "diagram_type": None,
        "user_input": (
            "The vehicle shall be able to safely bring itself to a complete stop within "
            "the available road distance under normal operating conditions, without "
            "dictating a specific braking mechanism."
        ),
        "needs_base_requirement": False,
    },
    {
        "name": "functional_reserved_keyword_bait",
        "level": "functional",
        "intent": Intent.generate_requirement,
        "diagram_type": None,
        "user_input": (
            "Define a requirement for a braking control module: it must track the "
            "required brake torque (refer to this quantity as 'require') and the "
            "vehicle's current speed, and state that the required torque stays below "
            "4000 newton-meters."
        ),
        "needs_base_requirement": False,
    },
    {
        "name": "physical_multi_constraint",
        "level": "physical",
        "intent": Intent.generate_requirement,
        "diagram_type": None,
        "user_input": (
            "Define a physical-level requirement for a brake caliper assembly made of "
            "a caliper body and two mounting bolts: the assembly's combined mass shall "
            "not exceed 2.5 kilograms, and it shall interface with a hydraulic port "
            "rated for at least 10 megapascals of pressure."
        ),
        "needs_base_requirement": False,
    },
    {
        "name": "functional_enum_bait",
        "level": "functional",
        "intent": Intent.generate_requirement,
        "diagram_type": None,
        "user_input": (
            "Define a requirement for a brake pad selection system that must support "
            "three pad material options: ceramic, semi-metallic, and organic. The "
            "system shall correctly report the currently selected material."
        ),
        "needs_base_requirement": False,
    },
    {
        "name": "cross_reference_parts",
        "level": "functional",
        "intent": Intent.generate_diagram,
        "diagram_type": DiagramType.sequence,
        "user_input": (
            "Model how the ECU references the Wheel part's speed sensor: the ECU "
            "reads the wheel's current speed to compute the target deceleration."
        ),
        "needs_base_requirement": True,
        "base_requirement_text": (
            "The system shall compute target deceleration based on current wheel speed."
        ),
    },
    {
        "name": "state_machine_behavioral",
        "level": "functional",
        "intent": Intent.generate_diagram,
        "diagram_type": DiagramType.state_machine,
        "user_input": (
            "Model the states of a garage door opener: Closed, Opening, Open, Closing, "
            "with transitions triggered by a button press and by obstacle detection."
        ),
        "needs_base_requirement": True,
        "base_requirement_text": (
            "The system shall control the garage door through its Closed, Opening, "
            "Open, and Closing states."
        ),
    },
]


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


async def setup_session(label: str):
    async with async_session_factory() as db:
        user = await UserRepo.create(db, email=f"skilleffect2-{label}-{uuid.uuid4()}@test.dev", password_hash="hashed")
        project = await ProjectRepo.create(db, user_id=user.id, name=f"SkillEffect2 {label}")
        session = await SessionRepo.create(
            db, project_id=project.id, thread_id=str(uuid.uuid4()), title=f"SkillEffect2 {label}"
        )
        await db.commit()
        return user, session


async def maybe_create_base_requirement(session_id, case: dict) -> str | None:
    if not case.get("needs_base_requirement"):
        return None
    async with async_session_factory() as db:
        req = await RequirementRepo.finalize(
            db, session_id=session_id, content=case["base_requirement_text"], level=RequirementLevel.functional,
        )
        await db.commit()
        return str(req.id)


async def cleanup_user(user):
    async with async_session_factory() as db:
        db_user = await UserRepo.get_by_id(db, user.id)
        await db.delete(db_user)
        await db.commit()


async def run_single_trial(case: dict, disable_skill: bool) -> dict:
    user, session = await setup_session(case["name"])
    target_requirement_id = await maybe_create_base_requirement(session.id, case)

    supervisor_llm = FakeStructuredWrapperLLM(
        IntentDecision(intent=case["intent"], diagram_type=case.get("diagram_type"), level=None)
    )

    def fake_get_llm(node_name=None):
        if node_name == "sysml_supervisor":
            return supervisor_llm
        from llm.factory import get_llm as real_get_llm
        return real_get_llm(node_name)

    patches = [patch("agents.sysml.nodes.get_llm", side_effect=fake_get_llm)]
    if disable_skill:
        patches.append(patch("agents.sysml.nodes._skill_guidance", return_value=""))
        patches.append(patch("agents.sysml.nodes.get_error_help", return_value=""))

    for p in patches:
        p.start()
    try:
        checkpointer = MemorySaver()
        graph = build_sysml_graph(checkpointer=checkpointer)
        config = {"configurable": {"thread_id": str(session.id)}}

        initial_state = {
            "user_input": case["user_input"],
            "session_id": session.id,
            "level": case["level"],
        }
        if target_requirement_id:
            initial_state["target_requirement_id"] = target_requirement_id

        result = await graph.ainvoke(initial_state, config)

        rounds = result.get("verify_visits")
        clean = bool(result.get("verify_clean"))
        interrupt = result.get("__interrupt__", [None])[0]
        diag_count = len((interrupt.value or {}).get("verify_diagnostics") or []) if interrupt else 0

        # close the run out (we don't care about finalize/persistence for this measurement)
        await graph.ainvoke(Command(resume={"action": "approve"}), config)
    finally:
        for p in reversed(patches):
            p.stop()
        await cleanup_user(user)

    return {"rounds": rounds, "clean": clean, "diagnostics_remaining": diag_count}


async def run_case(case: dict) -> dict:
    print(f"\n=== Case: {case['name']} (level={case['level']}, "
          f"intent={case['intent'].value}{', diagram=' + case['diagram_type'].value if case.get('diagram_type') else ''}) ===")

    results = {"A_enabled": [], "B_disabled": []}
    for label, key, disable in [("A (skill ENABLED)", "A_enabled", False), ("B (skill DISABLED)", "B_disabled", True)]:
        for trial in range(1, N_TRIALS + 1):
            await asyncio.sleep(INTER_TRIAL_DELAY_SECONDS)
            try:
                r = await run_single_trial(case, disable_skill=disable)
            except Exception as exc:  # noqa: BLE001 - real backend flakiness (e.g. 429s); keep the batch going
                print(f"  {label} trial {trial}/{N_TRIALS}: FAILED ({type(exc).__name__}: {exc}) — skipped, not counted")
                continue
            results[key].append(r)
            print(f"  {label} trial {trial}/{N_TRIALS}: rounds={r['rounds']} clean={r['clean']} "
                  f"remaining_diagnostics={r['diagnostics_remaining']}")

    return results


def _summarize(runs: list[dict]) -> dict:
    if not runs:
        return {"avg_rounds": None, "stdev_rounds": None, "min_rounds": None, "max_rounds": None,
                "clean_rate": None, "n": 0}
    rounds = [r["rounds"] for r in runs]
    clean_count = sum(1 for r in runs if r["clean"])
    return {
        "avg_rounds": statistics.mean(rounds),
        "stdev_rounds": statistics.stdev(rounds) if len(rounds) > 1 else 0.0,
        "min_rounds": min(rounds),
        "max_rounds": max(rounds),
        "clean_rate": clean_count / len(runs),
        "n": len(runs),
    }


async def main() -> None:
    all_results: dict[str, dict] = {}
    for case in CASES:
        all_results[case["name"]] = await run_case(case)

    await shutdown_lsp_client()
    await shutdown_mcp_client()

    print("\n" + "=" * 100)
    print("SUMMARY — per case, average rounds and clean-rate (A = skill enabled, B = skill disabled)")
    print("=" * 100)
    header = (f"{'case':32s} {'A avg':>7s} {'A sd':>6s} {'A n':>4s} {'A clean%':>9s} "
              f"{'B avg':>7s} {'B sd':>6s} {'B n':>4s} {'B clean%':>9s} {'delta(A-B)':>11s}")
    print(header)
    print("-" * len(header))

    all_a_rounds: list[float] = []
    all_b_rounds: list[float] = []
    all_a_clean: list[float] = []
    all_b_clean: list[float] = []

    for case in CASES:
        name = case["name"]
        a = _summarize(all_results[name]["A_enabled"])
        b = _summarize(all_results[name]["B_disabled"])
        if a["n"] == 0 or b["n"] == 0:
            print(f"{name:32s}  (insufficient data: A n={a['n']}, B n={b['n']} — some trials failed)")
            continue
        delta = a["avg_rounds"] - b["avg_rounds"]
        print(f"{name:32s} {a['avg_rounds']:7.2f} {a['stdev_rounds']:6.2f} {a['n']:4d} {a['clean_rate']*100:8.0f}% "
              f"{b['avg_rounds']:7.2f} {b['stdev_rounds']:6.2f} {b['n']:4d} {b['clean_rate']*100:8.0f}% {delta:+11.2f}")
        all_a_rounds.extend(r["rounds"] for r in all_results[name]["A_enabled"])
        all_b_rounds.extend(r["rounds"] for r in all_results[name]["B_disabled"])
        all_a_clean.append(a["clean_rate"])
        all_b_clean.append(b["clean_rate"])

    print("-" * len(header))
    overall_a_avg = statistics.mean(all_a_rounds)
    overall_b_avg = statistics.mean(all_b_rounds)
    overall_a_clean = statistics.mean(all_a_clean)
    overall_b_clean = statistics.mean(all_b_clean)
    print(f"{'OVERALL':32s} {overall_a_avg:7.2f} {'':6s} {overall_a_clean*100:8.0f}% "
          f"{overall_b_avg:7.2f} {'':6s} {overall_b_clean*100:8.0f}% {overall_a_avg - overall_b_avg:+11.2f}")

    print("\n" + "=" * 100)
    print("CONCLUSION")
    print("=" * 100)
    round_delta = overall_a_avg - overall_b_avg
    clean_delta = overall_a_clean - overall_b_clean

    print(f"Overall average rounds: WITH skill = {overall_a_avg:.2f}, WITHOUT skill = {overall_b_avg:.2f} "
          f"(delta {round_delta:+.2f})")
    print(f"Overall clean-rate:     WITH skill = {overall_a_clean*100:.0f}%, WITHOUT skill = {overall_b_clean*100:.0f}% "
          f"(delta {clean_delta*100:+.0f} pts)")

    if round_delta < -0.15:
        print(f"\n=> The skill REDUCED average verify/regenerate rounds by {-round_delta:.2f} rounds overall.")
    elif round_delta > 0.15:
        print(f"\n=> The skill INCREASED average verify/regenerate rounds by {round_delta:.2f} rounds overall "
              "— it did not help on this measurement.")
    else:
        print("\n=> No meaningful difference in average rounds between WITH and WITHOUT the skill.")

    if clean_delta > 0.05:
        print(f"=> The skill IMPROVED the clean-rate by {clean_delta*100:.0f} points (fewer fail-open handoffs).")
    elif clean_delta < -0.05:
        print(f"=> The skill WORSENED the clean-rate by {-clean_delta*100:.0f} points.")
    else:
        print("=> No meaningful difference in clean-rate.")


if __name__ == "__main__":
    asyncio.run(main())
>>>>>>> Stashed changes
