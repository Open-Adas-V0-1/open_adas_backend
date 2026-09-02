"""T6b Step 3a: chat turn endpoint (SSE, token streaming).

Runs against a REAL running FastAPI app + REAL Postgres + REAL model, over HTTP
(same shape as scripts/smoke_test_auth.py / smoke_test_projects.py). Start the
DEDICATED test server first (NOT the regular app.main:app dev server):

    python -m scripts.run_chat_test_server

This uses port 8125 and stubs ONLY agents.sysml.nodes.validate/.to_mermaid (the
Node.js LSP/MCP subprocess tools) -- a pre-existing, documented Windows-only
constraint: AsyncPostgresSaver (the real production checkpointer, genuinely used
here) needs SelectorEventLoop, the real subprocess tooling needs ProactorEventLoop,
and Windows cannot run both in one process. Every other integration test in this
repo already navigates this the same way. The LLM calls, the graph, the
checkpointer, and every HTTP round trip in this script are 100% real.

Then: python -m scripts.smoke_test_chat_turn [base_url]  (default: http://127.0.0.1:8125)
"""
import asyncio
import json
import sys
import uuid

import httpx

from data.db import async_session_factory
from data.repository import RequirementRepo

BASE_URL = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8125"


async def _register_and_login(client: httpx.AsyncClient, label: str) -> str:
    email = f"chatturn-{label}-{uuid.uuid4()}@test.dev"
    password = "correct-horse-battery-staple"
    r = await client.post("/auth/register", json={"email": email, "password": password})
    assert r.status_code == 201, f"register failed: {r.status_code} {r.text}"
    r = await client.post("/auth/login", json={"email": email, "password": password})
    assert r.status_code == 200, f"login failed: {r.status_code} {r.text}"
    return r.json()["access_token"]


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


async def _new_session(client: httpx.AsyncClient, token: str, label: str) -> str:
    r = await client.post("/projects", json={"name": f"Chat Turn {label}"}, headers=_auth(token))
    assert r.status_code == 201, r.text
    project_id = r.json()["id"]
    r = await client.post(f"/projects/{project_id}/sessions", json={}, headers=_auth(token))
    assert r.status_code == 201, r.text
    return r.json()["id"]


async def _stream_turn(client: httpx.AsyncClient, session_id: str, token: str, message: str):
    """Collects the whole SSE stream into a list of (event_name, payload_dict)."""
    events: list[tuple[str, dict]] = []
    async with client.stream(
        "POST", f"/sessions/{session_id}/turn", json={"message": message}, headers=_auth(token), timeout=90.0
    ) as resp:
        status_code = resp.status_code
        if status_code != 200:
            body = await resp.aread()
            return status_code, events, body
        event_name = None
        async for line in resp.aiter_lines():
            if line.startswith("event: "):
                event_name = line[len("event: "):]
            elif line.startswith("data: "):
                payload = json.loads(line[len("data: "):])
                events.append((event_name, payload))
    return status_code, events, None


async def main() -> None:
    async with httpx.AsyncClient(base_url=BASE_URL, timeout=90.0) as client:
        token_a = await _register_and_login(client, "a")
        token_b = await _register_and_login(client, "b")

        # --- 1. auth: no token -> 401; another user's session -> 404 ---
        print("\n--- 1. auth: no token -> 401; another user's session -> 404 ---")
        session_a = await _new_session(client, token_a, "auth")
        r = await client.post(f"/sessions/{session_a}/turn", json={"message": "hello"})
        assert r.status_code == 401, f"expected 401, got {r.status_code}: {r.text}"
        r = await client.post(f"/sessions/{session_a}/turn", json={"message": "hello"}, headers=_auth(token_b))
        assert r.status_code == 404, f"expected 404, got {r.status_code}: {r.text}"
        print(f"assert OK: no-token -> 401; foreign session -> 404 (not 403)")

        # --- 2. simple response: real token streaming ---
        print("\n--- 2. simple response -- real token-by-token streaming ---")
        session_simple = await _new_session(client, token_a, "simple")
        status_code, events, _ = await _stream_turn(client, session_simple, token_a, "hello")
        assert status_code == 200, f"expected 200, got {status_code}"
        token_events = [p["text"] for name, p in events if name == "token"]
        assert len(token_events) >= 2, f"expected >=2 token events, got {len(token_events)}: {token_events}"
        full_text = "".join(token_events)
        assert len(full_text) > 5, f"concatenated token text looks too short: {full_text!r}"
        done_events = [p for name, p in events if name == "done"]
        assert len(done_events) == 1 and done_events[0]["status"] == "completed", (
            f"expected exactly one done(status=completed), got {done_events}"
        )
        assert any(name == "turn_started" for name, _ in events)
        assert not any(name == "interrupt" for name, _ in events)
        assert not any(name == "error" for name, _ in events)
        print(f"assert OK: {len(token_events)} token events, concatenation={full_text!r}, "
              f"done.status={done_events[0]['status']!r}")

        # --- 3 & 4. no-leakage + interrupt: generate a requirement, approve nothing ---
        print("\n--- 3 & 4. requirement generation -- no token leakage, real interrupt, nothing persisted ---")
        session_gen = await _new_session(client, token_a, "gen")
        status_code, events, _ = await _stream_turn(
            client, session_gen, token_a,
            "Generate an operational requirement stating that the vehicle shall stop safely "
            "within the available road distance when braking.",
        )
        assert status_code == 200, f"expected 200, got {status_code}"

        # DoD #3 (the key assertion): ZERO token events anywhere in the whole stream --
        # top_level_supervisor's response field is unused (None) for needs_execution,
        # so the allow-listed tag never produces deltas here; every other node's LLM
        # call (planning, generation, verification) is untagged by construction.
        leak_token_events = [p for name, p in events if name == "token"]
        assert leak_token_events == [], f"LEAK: expected zero token events, got {leak_token_events}"

        # `status` events must never carry more than {node, layer} -- never content.
        for name, payload in events:
            if name == "status":
                assert set(payload.keys()) <= {"node", "layer"}, f"status payload leaked extra data: {payload}"

        interrupt_events = [p for name, p in events if name == "interrupt"]
        assert len(interrupt_events) == 1, f"expected exactly one interrupt event, got {len(interrupt_events)}"
        interrupt_payload = interrupt_events[0]
        recognized_patterns = {
            "plan_review", "requirement_review", "select_requirements_for_diagram",
            "select_requirement", "confirm_diagram_type", "confirm_action", "clarify_request", "plan_clarify",
        }
        assert interrupt_payload["pattern"] in recognized_patterns, (
            f"unrecognized pattern: {interrupt_payload['pattern']!r}"
        )
        # The interrupt's OWN payload is legitimately allowed to carry the draft (the
        # entire point of human review) -- distinct from the leakage check above,
        # which covers token/status events, never this one.
        assert "draft" in interrupt_payload["payload"] or "question" in interrupt_payload["payload"], (
            f"interrupt payload missing expected review content: {interrupt_payload['payload']}"
        )

        # After the interrupt, the stream must end: done(interrupted), nothing after.
        assert events[-1][0] == "done" and events[-1][1]["status"] == "interrupted", (
            f"expected the LAST event to be done(status=interrupted), got {events[-1]}"
        )
        assert events[-2][0] == "interrupt", "expected interrupt to be the event immediately before done"

        async with async_session_factory() as db:
            rows = await RequirementRepo.list_by_session(db, session_id=uuid.UUID(session_gen))
        assert rows == [], f"expected NO finalized requirement (nothing approved), found {rows}"

        print(f"assert OK: 0 token events across {len(events)} total events, interrupt pattern="
              f"{interrupt_payload['pattern']!r}, stream ended at done(interrupted), "
              f"0 requirements finalized in Postgres")

        # --- 5. concurrency: awaiting_input ---
        print("\n--- 5. POST another turn on the awaiting-input session -> expect 409 awaiting_input ---")
        r = await client.post(f"/sessions/{session_gen}/turn", json={"message": "anything"}, headers=_auth(token_a))
        assert r.status_code == 409, f"expected 409, got {r.status_code}: {r.text}"
        detail = r.json().get("detail", {})
        assert detail.get("status") == "awaiting_input", f"expected status=awaiting_input, got {detail}"
        print(f"assert OK: status={r.status_code} detail={detail}")

        # bonus: the "running" case of the SAME concurrency guard (not a separately
        # numbered DoD item, but it's the other half of the feature this step built).
        print("\n--- 5b (bonus). two concurrent turns on a FRESH session -> one 200, one 409 running ---")
        session_race = await _new_session(client, token_a, "race")

        async def _fire():
            return await client.post(
                f"/sessions/{session_race}/turn", json={"message": "hello"}, headers=_auth(token_a)
            )

        r1, r2 = await asyncio.gather(_fire(), _fire())
        statuses = sorted([r1.status_code, r2.status_code])
        assert statuses == [200, 409], f"expected one 200 and one 409, got {statuses}"
        running_resp = r1 if r1.status_code == 409 else r2
        assert running_resp.json().get("detail", {}).get("status") == "running", running_resp.text
        # drain the winning stream so its background task finishes cleanly before we move on.
        winning_resp = r1 if r1.status_code == 200 else r2
        async for _ in winning_resp.aiter_lines():
            pass
        print(f"assert OK: concurrent turns on the same session -> statuses={statuses}, "
              f"the rejected one carries status='running'")

        # --- 6. malformed body (empty message) -> 422, no graph invocation ---
        print("\n--- 6. malformed body (empty message) -> expect 422 ---")
        session_bad = await _new_session(client, token_a, "bad")
        r = await client.post(f"/sessions/{session_bad}/turn", json={"message": ""}, headers=_auth(token_a))
        assert r.status_code == 422, f"expected 422, got {r.status_code}: {r.text}"
        # FastAPI/Pydantic reject the body before the route function ever runs --
        # the route (and therefore the graph) is architecturally unreachable on a
        # validation failure, not just "didn't happen to be called this time".
        r = await client.post(f"/sessions/{session_bad}/turn", json={}, headers=_auth(token_a))
        assert r.status_code == 422, f"expected 422 for missing field, got {r.status_code}: {r.text}"
        print(f"assert OK: empty message -> 422; missing field -> 422; route body never reached")

    print("\n=== CHAT TURN SMOKE TEST SUITE PASSED (all 6 DoD checks + bonus) ===")


if __name__ == "__main__":
    asyncio.run(main())
