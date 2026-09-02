"""T6b Step 3b: execution trace channel (stream-only, additive to the Step-3a
event contract).

Runs against REAL running FastAPI apps + REAL Postgres + REAL model, over HTTP.
Needs TWO server processes (start both before running this script):

    python -m scripts.run_chat_test_server              # TRACE_ENABLED=true,  port 8125
    python -m scripts.run_chat_test_server_trace_off     # TRACE_ENABLED=false, port 8126

Both stub ONLY agents.sysml.nodes.validate/.to_mermaid (the real subprocess-based
SysML v2 LSP/MCP tools) -- the same pre-existing, documented Windows-only
AsyncPostgresSaver-vs-subprocess event-loop constraint navigated by every
integration test in this repo (see scripts/run_chat_test_server.py's docstring).
Everything else -- the LLM calls, the graph, the checkpointer -- is 100% real.

Then: python -m scripts.smoke_test_chat_trace [base_url] [base_url_trace_off]
(defaults: http://127.0.0.1:8125, http://127.0.0.1:8126)
"""
import asyncio
import json
import sys
import uuid

import httpx

from app.config import get_settings

BASE_URL = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8125"
BASE_URL_TRACE_OFF = sys.argv[2] if len(sys.argv) > 2 else "http://127.0.0.1:8126"

REQUIREMENT_MESSAGE = (
    "Generate an operational requirement stating that the vehicle shall stop safely "
    "within the available road distance when braking."
)


async def _register_and_login(client: httpx.AsyncClient, label: str) -> str:
    email = f"chattrace-{label}-{uuid.uuid4()}@test.dev"
    password = "correct-horse-battery-staple"
    r = await client.post("/auth/register", json={"email": email, "password": password})
    assert r.status_code == 201, f"register failed: {r.status_code} {r.text}"
    r = await client.post("/auth/login", json={"email": email, "password": password})
    assert r.status_code == 200, f"login failed: {r.status_code} {r.text}"
    return r.json()["access_token"]


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


async def _new_session(client: httpx.AsyncClient, token: str, label: str) -> str:
    r = await client.post("/projects", json={"name": f"Chat Trace {label}"}, headers=_auth(token))
    assert r.status_code == 201, r.text
    project_id = r.json()["id"]
    r = await client.post(f"/projects/{project_id}/sessions", json={}, headers=_auth(token))
    assert r.status_code == 201, r.text
    return r.json()["id"]


async def _stream_turn(client: httpx.AsyncClient, session_id: str, token: str, message: str, trace: bool):
    """Collects the whole SSE stream into a list of (event_name, payload_dict)."""
    events: list[tuple[str, dict]] = []
    url = f"/sessions/{session_id}/turn" + ("?trace=1" if trace else "")
    async with client.stream("POST", url, json={"message": message}, headers=_auth(token), timeout=90.0) as resp:
        status_code = resp.status_code
        if status_code != 200:
            await resp.aread()
            return status_code, events
        event_name = None
        async for line in resp.aiter_lines():
            if line.startswith("event: "):
                event_name = line[len("event: "):]
            elif line.startswith("data: "):
                events.append((event_name, json.loads(line[len("data: "):])))
    return status_code, events


async def main() -> None:
    async with httpx.AsyncClient(base_url=BASE_URL, timeout=90.0) as client:
        token = await _register_and_login(client, "a")

        # --- 1. off by default: no trace param -> zero trace events, shape unchanged ---
        print("\n--- 1. off by default (no ?trace param) -> zero trace events ---")
        session_default = await _new_session(client, token, "default")
        status_code, events = await _stream_turn(client, session_default, token, "hello", trace=False)
        assert status_code == 200, f"expected 200, got {status_code}"
        trace_events = [p for name, p in events if name == "trace"]
        assert trace_events == [], f"expected ZERO trace events, got {len(trace_events)}"
        event_names_in_order = [name for name, _ in events]
        assert event_names_in_order[0] == "turn_started"
        assert event_names_in_order[-1] == "done"
        assert "token" in event_names_in_order and "status" in event_names_in_order
        assert "interrupt" not in event_names_in_order and "error" not in event_names_in_order
        print(f"assert OK: 0 trace events; frozen event shape unchanged: {event_names_in_order}")

        # --- 2. on: ?trace=1 -> trace events, seq strictly increasing, valid layer/phase ---
        print("\n--- 2. ?trace=1 -> trace events present, seq strictly increasing, valid layer/phase ---")
        session_on = await _new_session(client, token, "on")
        status_code, events = await _stream_turn(client, session_on, token, "hello", trace=True)
        assert status_code == 200, f"expected 200, got {status_code}"
        trace_events = [p for name, p in events if name == "trace"]
        assert len(trace_events) > 0, "expected at least one trace event with ?trace=1"
        seqs = [t["seq"] for t in trace_events]
        assert seqs == sorted(seqs), f"seq not increasing: {seqs}"
        assert len(set(seqs)) == len(seqs), f"duplicate seq values: {seqs}"
        assert seqs == list(range(1, len(seqs) + 1)), f"expected NO gaps (1..N), got {seqs}"
        valid_phases = {"enter", "exit", "llm", "decision", "interrupt", "error"}
        for t in trace_events:
            assert t["layer"] in (1, 2, 3, None), f"invalid layer: {t}"
            assert t["phase"] in valid_phases, f"invalid phase: {t}"
        print(f"assert OK: {len(trace_events)} trace events, seq={seqs[0]}..{seqs[-1]} "
              f"(no gaps, no dupes), all layers/phases valid")

        # --- 3. three layers visible + ordered execution path ---
        print("\n--- 3. single-task requirement request -> layers 1, 2 AND 3 all present ---")
        session_path = await _new_session(client, token, "path")
        status_code, events = await _stream_turn(client, session_path, token, REQUIREMENT_MESSAGE, trace=True)
        assert status_code == 200, f"expected 200, got {status_code}"
        trace_events = [p for name, p in events if name == "trace"]
        layers_seen = {t["layer"] for t in trace_events if t["layer"] is not None}
        assert {1, 2, 3} <= layers_seen, f"expected layers 1, 2 AND 3, got {layers_seen}"
        path = [(t["layer"], t["node"], t["phase"]) for t in trace_events]
        print("assert OK: layers {1,2,3} all present. Ordered (layer, node, phase) execution path:")
        for entry in path:
            print(f"    {entry}")

        # --- 4. routing decisions visible in full ---
        print("\n--- 4. hub classification, plan/TODO, and level resolution appear IN FULL ---")
        # top_level_supervisor plays a dual role (Step 1's hub classification, and
        # later the execution-loop driver revisiting with plan_state) -- both are
        # (1, "top_level_supervisor") "decision" events, so find the SPECIFIC ones by
        # content rather than last-write-wins keying on (layer, node) alone.
        decisions_all = [t for t in trace_events if t["phase"] == "decision"]

        def _find(layer, node, key):
            return next(
                (t["data"] for t in decisions_all if t["layer"] == layer and t["node"] == node and key in t["data"]),
                None,
            )

        hub_data = _find(1, "top_level_supervisor", "classification")
        assert hub_data and hub_data.get("classification") == "needs_execution", f"hub decision missing/wrong: {hub_data}"
        plan_data = _find(1, "plan_node", "plan_state")
        assert plan_data and "plan_state" in plan_data and plan_data["plan_state"]["tasks"], (
            f"plan/TODO missing: {plan_data}"
        )
        level_data = _find(2, "resolve_level", "requested_level")
        assert level_data and "requested_level" in level_data, f"level resolution missing: {level_data}"
        print(f"assert OK: hub classification={hub_data['classification']!r}, "
              f"plan has {len(plan_data['plan_state']['tasks'])} task(s), "
              f"level resolution requested_level={level_data['requested_level']!r}")

        # --- 5. THE KEY ASSERTION: no artifact leakage anywhere in the trace ---
        print("\n--- 5. KEY ASSERTION: no artifact leakage across ALL trace payloads ---")
        interrupt_events = [p for name, p in events if name == "interrupt"]
        assert len(interrupt_events) == 1, f"expected exactly one interrupt event, got {len(interrupt_events)}"
        draft_text = interrupt_events[0]["payload"].get("draft", "")
        assert len(draft_text) >= 40, f"test precondition failed: draft too short to test with ({len(draft_text)} chars)"

        all_trace_json = json.dumps([t["data"] for t in trace_events])
        leaked_substrings = [
            draft_text[i:i + 40] for i in range(0, len(draft_text) - 40, 40) if draft_text[i:i + 40] in all_trace_json
        ]
        assert leaked_substrings == [], f"LEAK: found draft substrings in trace payloads: {leaked_substrings}"

        generate_decision = next(
            (t["data"] for t in decisions_all if t["layer"] == 3 and t["node"] == "generate_node"), None
        )
        assert generate_decision is not None, "expected generate_node to appear in the trace"
        assert "draft_sysml_length" in generate_decision and generate_decision["draft_sysml_length"] > 0
        assert not any("draft" in k and "length" not in k for k in generate_decision), (
            f"generate_node's trace data must be metadata-only: {generate_decision}"
        )
        generate_llm = next(
            (t for t in trace_events if t["layer"] == 3 and t["node"] == "generate_node" and t["phase"] == "llm"), None
        )
        assert generate_llm is not None and generate_llm.get("duration_ms") is not None and generate_llm["data"].get("model"), (
            f"expected generate_node's llm phase to carry duration+model: {generate_llm}"
        )
        print(f"assert OK: NO 40+ char substring of the {len(draft_text)}-char draft appears anywhere in "
              f"{len(trace_events)} trace payloads. generate_node present with metadata only: "
              f"draft_sysml_length={generate_decision['draft_sysml_length']}, "
              f"llm duration_ms={generate_llm['duration_ms']}, model={generate_llm['data']['model']}")

        # --- 6. redaction: API key and base URL never appear ---
        print("\n--- 6. redaction: API key / base URL absent from every trace payload ---")
        settings = get_settings()
        full_trace_blob = json.dumps(trace_events)
        if settings.llm_api_key:
            assert settings.llm_api_key not in full_trace_blob, "LEAK: API key found in trace payload!"
        if settings.llm_base_url:
            assert settings.llm_base_url not in full_trace_blob, "LEAK: base URL found in trace payload!"
        print(f"assert OK: neither the configured API key nor base URL appear in any of "
              f"{len(trace_events)} trace payloads")

        # --- 7. interrupt path unaffected by trace ---
        print("\n--- 7. interrupt path still works correctly with trace=1 ---")
        assert events[-1][0] == "done" and events[-1][1]["status"] == "interrupted"
        assert events[-2][0] == "interrupt"
        recognized_patterns = {
            "plan_review", "requirement_review", "select_requirements_for_diagram",
            "select_requirement", "confirm_diagram_type", "confirm_action", "clarify_request", "plan_clarify",
        }
        assert interrupt_events[0]["pattern"] in recognized_patterns
        print(f"assert OK: interrupt pattern={interrupt_events[0]['pattern']!r}, "
              f"stream ended at done(interrupted), trace did not alter control flow")

    # --- 8. TRACE_ENABLED=false server-side -> ?trace=1 yields zero trace events ---
    print("\n--- 8. TRACE_ENABLED=false -> ?trace=1 yields ZERO trace events ---")
    async with httpx.AsyncClient(base_url=BASE_URL_TRACE_OFF, timeout=90.0) as client_off:
        token_off = await _register_and_login(client_off, "off")
        session_off = await _new_session(client_off, token_off, "off")
        status_code, events = await _stream_turn(client_off, session_off, token_off, "hello", trace=True)
        assert status_code == 200, f"expected 200, got {status_code}"
        trace_events = [p for name, p in events if name == "trace"]
        assert trace_events == [], f"expected ZERO trace events (server-side off-switch), got {len(trace_events)}"
        print(f"assert OK: server TRACE_ENABLED=false wins over ?trace=1 -- 0 trace events")

    print("\n=== CHAT TRACE SMOKE TEST SUITE PASSED (all 8 DoD checks) ===")


if __name__ == "__main__":
    asyncio.run(main())
