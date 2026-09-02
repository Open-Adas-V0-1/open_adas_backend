"""T6b Step 4: resume endpoint (answering interrupts).

Runs against a REAL running FastAPI app + REAL Postgres + REAL model, over HTTP.
Start the test server first (same one Steps 3a/3b use):

    python -m scripts.run_chat_test_server

Then: python -m scripts.smoke_test_chat_resume [base_url]  (default: http://127.0.0.1:8125)
"""
import asyncio
import json
import sys
import uuid

import httpx

from data.db import async_session_factory
from data.models import RequirementLevel
from data.repository import DiagramRepo, RequirementRepo

BASE_URL = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8125"


async def _register_and_login(client: httpx.AsyncClient, label: str) -> str:
    email = f"resume-{label}-{uuid.uuid4()}@test.dev"
    password = "correct-horse-battery-staple"
    r = await client.post("/auth/register", json={"email": email, "password": password})
    assert r.status_code == 201, f"register failed: {r.status_code} {r.text}"
    r = await client.post("/auth/login", json={"email": email, "password": password})
    assert r.status_code == 200, f"login failed: {r.status_code} {r.text}"
    return r.json()["access_token"]


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


async def _new_session(client: httpx.AsyncClient, token: str, label: str) -> str:
    r = await client.post("/projects", json={"name": f"Resume {label}"}, headers=_auth(token))
    assert r.status_code == 201, r.text
    project_id = r.json()["id"]
    r = await client.post(f"/projects/{project_id}/sessions", json={}, headers=_auth(token))
    assert r.status_code == 201, r.text
    return r.json()["id"]


async def _stream(client: httpx.AsyncClient, path: str, token: str, json_body: dict | None = None):
    """POST `path` and collect the whole SSE stream into [(event_name, payload), ...]."""
    events: list[tuple[str, dict]] = []
    async with client.stream("POST", path, json=json_body or {}, headers=_auth(token), timeout=90.0) as resp:
        status_code = resp.status_code
        if status_code != 200:
            body = await resp.aread()
            return status_code, events, body
        event_name = None
        async for line in resp.aiter_lines():
            if line.startswith("event: "):
                event_name = line[len("event: "):]
            elif line.startswith("data: "):
                events.append((event_name, json.loads(line[len("data: "):])))
    return status_code, events, None


async def _turn(client, session_id, token, message):
    return await _stream(client, f"/sessions/{session_id}/turn", token, {"message": message})


async def _resume(client, session_id, token, action_body):
    return await _stream(client, f"/sessions/{session_id}/resume", token, action_body)


async def _pending(client: httpx.AsyncClient, session_id: str, token: str) -> dict:
    r = await client.get(f"/sessions/{session_id}/pending", headers=_auth(token))
    assert r.status_code == 200, r.text
    return r.json()


def _last_interrupt(events) -> dict:
    interrupts = [p for n, p in events if n == "interrupt"]
    assert len(interrupts) == 1, f"expected exactly one interrupt event, got {len(interrupts)}: {interrupts}"
    return interrupts[0]


def _resume_body_for(interrupt_payload: dict) -> dict:
    """The 'happy path' resume body for whatever pattern is actually pending -- real
    LLM decisions aren't scripted, so (as in scripts/smoke_test_e2e_full_integration
    .py) the exact interrupt sequence a turn produces can vary; this always resumes
    toward completion rather than hardcoding a brittle sequence.
    """
    pattern = interrupt_payload["pattern"]
    if pattern == "plan_clarify":
        # plan_node's OWN insufficiency check (Layer-1, Step 2) -- distinct resume
        # shape: {"user_input": ...}, no "action" field at all (see PlanClarifyResume).
        return {
            "user_input": "Generate a SysML v2 use case diagram representing an existing "
                           "requirement already recorded in this session."
        }
    if pattern in ("select_requirements_for_diagram",):
        return {"action": "confirm", "select_all": True}
    if pattern == "select_requirement":
        options = interrupt_payload["payload"]["options"]
        return {"action": "confirm", "selected_id": options[0]["id"]}
    if pattern == "plan_review":
        return {"action": "approve"}
    return {"action": "approve"}  # requirement_review, confirm_action, ...


async def _drive_to_completion(client, session_id, token, status_code, events, max_hops=20):
    """Keeps resuming with the 'happy path' action for whatever pattern is actually
    pending until the turn completes. Returns (final_events, hops_taken).
    """
    hops = []
    while status_code == 200 and events[-1][1].get("status") == "interrupted":
        interrupt_payload = _last_interrupt(events)
        hops.append(interrupt_payload["pattern"])
        assert len(hops) <= max_hops, f"too many hops, something isn't converging: {hops}"
        status_code, events, _ = await _resume(
            client, session_id, token, _resume_body_for(interrupt_payload)
        )
        assert status_code == 200, events
    return events, hops


async def main() -> None:
    async with httpx.AsyncClient(base_url=BASE_URL, timeout=90.0) as client:
        token_a = await _register_and_login(client, "a")
        token_b = await _register_and_login(client, "b")

        # --- 1. auth/ownership ---
        print("\n--- 1. auth/ownership: resume without token -> 401; foreign session -> 404 ---")
        session_auth = await _new_session(client, token_a, "auth")
        r = await client.post(f"/sessions/{session_auth}/resume", json={"action": "approve"})
        assert r.status_code == 401, f"expected 401, got {r.status_code}: {r.text}"
        r = await client.post(f"/sessions/{session_auth}/resume", json={"action": "approve"}, headers=_auth(token_b))
        assert r.status_code == 404, f"expected 404, got {r.status_code}: {r.text}"
        print(f"assert OK: no-token -> 401; foreign session -> 404 (not 403)")

        # --- 2. resume with no pending interrupt -> 409 no_pending_interrupt ---
        print("\n--- 2. resume with NO pending interrupt -> expect 409 no_pending_interrupt ---")
        status_code, _, body = await _resume(client, session_auth, token_a, {"action": "approve"})
        assert status_code == 409, f"expected 409, got {status_code}: {body}"
        r = await client.post(f"/sessions/{session_auth}/resume", json={"action": "approve"}, headers=_auth(token_a))
        assert r.json()["detail"]["status"] == "no_pending_interrupt", r.json()
        print(f"assert OK: status=409 detail={r.json()['detail']}")

        # --- 3. GET /pending before/after an interrupting turn ---
        print("\n--- 3. GET /pending: false before any turn; matches the stream's interrupt after ---")
        session_pending = await _new_session(client, token_a, "pending")
        before = await _pending(client, session_pending, token_a)
        assert before == {"pending": False}, before
        status_code, events, _ = await _turn(
            client, session_pending, token_a,
            "Generate an operational requirement stating that the vehicle shall stop safely "
            "within the available road distance when braking.",
        )
        assert status_code == 200
        stream_interrupt = _last_interrupt(events)
        after = await _pending(client, session_pending, token_a)
        assert after["pending"] is True
        assert after["pattern"] == stream_interrupt["pattern"]
        assert after["payload"] == stream_interrupt["payload"]
        print(f"assert OK: pending=False before turn; after turn, GET /pending pattern="
              f"{after['pattern']!r} matches the stream's interrupt payload exactly")

        # --- 4. single-task happy path ---
        print("\n--- 4. single-task happy path: interrupt -> resume approve -> done completed ---")
        session_happy = await _new_session(client, token_a, "happy")
        status_code, events, _ = await _turn(
            client, session_happy, token_a,
            "Generate an operational requirement stating that the vehicle shall stop safely "
            "within the available road distance when braking.",
        )
        assert status_code == 200
        interrupt_payload = _last_interrupt(events)
        assert interrupt_payload["pattern"] == "requirement_review"

        async with async_session_factory() as db:
            rows_before = await RequirementRepo.list_by_session(db, session_id=uuid.UUID(session_happy))
        assert rows_before == [], f"expected NOTHING persisted before resume, found {rows_before}"

        status_code, events, _ = await _resume(client, session_happy, token_a, {"action": "approve"})
        assert status_code == 200
        assert events[-1] == ("done", {"status": "completed", "light_refs": events[-1][1]["light_refs"]})
        assert events[-1][1]["status"] == "completed"

        async with async_session_factory() as db:
            rows_after = await RequirementRepo.list_by_session(db, session_id=uuid.UUID(session_happy))
        assert len(rows_after) == 1, f"expected EXACTLY 1 requirement, found {len(rows_after)}"
        print(f"assert OK: 0 requirements before resume, exactly 1 after -- id={rows_after[0].id}")

        # --- 5. multi-interrupt chain (ambiguous diagram target) ---
        print("\n--- 5. multi-interrupt chain: select_requirements_for_diagram THEN requirement_review ---")
        session_chain = await _new_session(client, token_a, "chain")
        async with async_session_factory() as db:
            req_a = await RequirementRepo.finalize(
                db, session_id=uuid.UUID(session_chain), content="req A: stop safely",
                level=RequirementLevel.operational,
            )
            req_b = await RequirementRepo.finalize(
                db, session_id=uuid.UUID(session_chain), content="req B: log sensor faults",
                level=RequirementLevel.operational,
            )
            await db.commit()

        status_code, events, _ = await _turn(client, session_chain, token_a, "Give me a use case diagram.")
        assert status_code == 200
        events, hops = await _drive_to_completion(client, session_chain, token_a, status_code, events)
        assert "select_requirements_for_diagram" in hops, (
            f"expected the ambiguous-target interrupt to occur, hops={hops}"
        )
        assert "requirement_review" in hops, f"expected the artifact review interrupt too, hops={hops}"
        assert hops.index("select_requirements_for_diagram") < hops.index("requirement_review")
        assert events[-1][1]["status"] == "completed"
        print(f"  hops: {hops}")

        async with async_session_factory() as db:
            diagrams_a = await DiagramRepo.get_by_requirement(db, requirement_id=req_a.id, session_id=uuid.UUID(session_chain))
            diagrams_b = await DiagramRepo.get_by_requirement(db, requirement_id=req_b.id, session_id=uuid.UUID(session_chain))
        all_diagram_ids = {d.id for d in diagrams_a} | {d.id for d in diagrams_b}
        assert len(all_diagram_ids) == 1, f"expected EXACTLY 1 diagram, found {len(all_diagram_ids)}"
        print(f"assert OK: select_requirements_for_diagram -> requirement_review -> completed; "
              f"exactly 1 diagram persisted (id={next(iter(all_diagram_ids))})")

        # --- 6. multi-task plan: plan_review + one review per task ---
        print("\n--- 6. multi-task plan: plan_review approve, then resume through each task's review ---")
        session_multi = await _new_session(client, token_a, "multi")
        status_code, events, _ = await _turn(
            client, session_multi, token_a,
            "I need three things, in this order. First, generate an operational requirement "
            "stating that the vehicle shall stop safely within the available road distance "
            "when braking. Second, generate the functional requirement derived from that "
            "operational requirement. Third, generate a use_case diagram of that functional "
            "requirement.",
        )
        assert status_code == 200
        plan_interrupt = _last_interrupt(events)
        assert plan_interrupt["pattern"] == "plan_review", plan_interrupt
        task_count = len(plan_interrupt["payload"].get("tasks") or [])
        assert task_count >= 1

        events, hops = await _drive_to_completion(client, session_multi, token_a, status_code, events)
        assert events[-1][1]["status"] == "completed", events[-1]

        async with async_session_factory() as db:
            req_rows = await RequirementRepo.list_by_session(db, session_id=uuid.UUID(session_multi))
            diagrams = []
            for r in req_rows:
                diagrams.extend(await DiagramRepo.get_by_requirement(db, requirement_id=r.id, session_id=uuid.UUID(session_multi)))
        print(f"  hops: {hops}")
        assert len(req_rows) == 2, f"expected exactly 2 requirements, found {len(req_rows)}"
        assert len(diagrams) == 1, f"expected exactly 1 diagram, found {len(diagrams)}"
        print(f"assert OK: multi-task plan completed via {len(hops)} resume(s); artifact counts match "
              f"the 3 tasks exactly -- {len(req_rows)} requirements + {len(diagrams)} diagram, no duplication")

        # --- 7. validation: wrong action for the pending pattern -> 422, interrupt untouched ---
        print("\n--- 7. validation: action from another pattern while requirement_review is pending -> 422 ---")
        session_val = await _new_session(client, token_a, "val")
        status_code, events, _ = await _turn(
            client, session_val, token_a,
            "Generate an operational requirement stating that the vehicle shall stop safely "
            "within the available road distance when braking.",
        )
        pending_before = await _pending(client, session_val, token_a)
        assert pending_before["pattern"] == "requirement_review"

        r = await client.post(
            f"/sessions/{session_val}/resume", json={"action": "select_all"}, headers=_auth(token_a)
        )
        assert r.status_code == 422, f"expected 422, got {r.status_code}: {r.text}"
        detail = r.json().get("detail", {})
        assert detail.get("pattern") == "requirement_review", detail

        pending_after = await _pending(client, session_val, token_a)
        assert pending_after == pending_before, "interrupt must be UNTOUCHED after a rejected resume"
        async with async_session_factory() as db:
            rows = await RequirementRepo.list_by_session(db, session_id=uuid.UUID(session_val))
        assert rows == [], "graph must NOT have been invoked on a validation failure"
        print(f"assert OK: status=422 detail={detail}; GET /pending unchanged; nothing persisted")

        # --- 8. cancel ---
        print("\n--- 8. cancel: the graph ends as designed, nothing persisted for that task ---")
        session_cancel = await _new_session(client, token_a, "cancel")
        status_code, events, _ = await _turn(
            client, session_cancel, token_a,
            "Generate an operational requirement stating that the vehicle shall stop safely "
            "within the available road distance when braking.",
        )
        assert _last_interrupt(events)["pattern"] == "requirement_review"
        status_code, events, _ = await _resume(client, session_cancel, token_a, {"action": "cancel"})
        assert status_code == 200, events
        assert not any(n == "interrupt" for n, _ in events), "cancel must not produce another interrupt"
        assert events[-1][0] == "done"
        async with async_session_factory() as db:
            rows = await RequirementRepo.list_by_session(db, session_id=uuid.UUID(session_cancel))
        assert rows == [], f"expected NOTHING persisted after cancel, found {rows}"
        print(f"assert OK: cancel -> done(status={events[-1][1]['status']!r}), 0 requirements persisted")

        # --- 9. no double-write on re-run ---
        print("\n--- 9. no double-write: artifact count stays exactly 1 after the successful approve ---")
        async with async_session_factory() as db:
            rows = await RequirementRepo.list_by_session(db, session_id=uuid.UUID(session_happy))
        assert len(rows) == 1, f"expected exactly 1 (re-verifying scenario 4's session), found {len(rows)}"
        print(f"assert OK: session from scenario 4 still has EXACTLY 1 requirement -- "
              f"resuming an interrupted node did not duplicate its DB write")

        # --- 10. concurrency: two identical resumes at once ---
        print("\n--- 10. concurrency: two simultaneous resumes -> one 200, one 409, exactly ONE artifact ---")
        session_race = await _new_session(client, token_a, "race")
        status_code, events, _ = await _turn(
            client, session_race, token_a,
            "Generate an operational requirement stating that the vehicle shall stop safely "
            "within the available road distance when braking.",
        )
        assert _last_interrupt(events)["pattern"] == "requirement_review"

        async def _fire():
            return await client.post(
                f"/sessions/{session_race}/resume", json={"action": "approve"}, headers=_auth(token_a)
            )

        r1, r2 = await asyncio.gather(_fire(), _fire())
        statuses = sorted([r1.status_code, r2.status_code])
        assert statuses == [200, 409], f"expected one 200 and one 409, got {statuses}"
        winning = r1 if r1.status_code == 200 else r2
        async for _ in winning.aiter_lines():
            pass
        async with async_session_factory() as db:
            rows = await RequirementRepo.list_by_session(db, session_id=uuid.UUID(session_race))
        assert len(rows) == 1, f"expected EXACTLY 1 artifact from the race, found {len(rows)}"
        print(f"assert OK: concurrent resumes -> statuses={statuses}, exactly 1 requirement persisted (not 2)")

    print("\n=== CHAT RESUME SMOKE TEST SUITE PASSED (all 10 DoD checks) ===")


if __name__ == "__main__":
    asyncio.run(main())
