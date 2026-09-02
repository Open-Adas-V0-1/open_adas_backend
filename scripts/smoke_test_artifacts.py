"""T6b Step 5: artifacts read API (requirements & diagrams).

Runs against a REAL running FastAPI app + REAL Postgres + REAL model, over HTTP.
Start the test server first (same one Steps 3a/3b/4 use):

    python -m scripts.run_chat_test_server

Then: python -m scripts.smoke_test_artifacts [base_url]  (default: http://127.0.0.1:8125)

ARCHITECTURAL GAP disclosed here (see chat report): the real graph's finalize() node
(agents/sysml/nodes.py) ALWAYS inserts a fresh row (version=1, root_id=self,
status=active) for both "generate" and "modify" intents -- it never calls
RequirementRepo/DiagramRepo.supersede_and_create_version()/.promote(). Those two
methods exist and are correct (already covered by scripts/smoke_test_t2.py), but
NOTHING in the current graph wiring invokes them, so a real v1->v2 supersede can't be
produced by driving the graph over HTTP today. Scenario 3 below therefore seeds the
version lineage directly via the repository (same precedent as smoke_test_chat_resume
.py's scenario 5, which seeds requirements directly since the graph can't be told to
pre-populate state either) to verify the READ side (this step's actual scope) reflects
active/superseded correctly. This is a pre-existing gap in the WRITE path, not
something Step 5 (read-only, by explicit constraint) can or should fix.
"""
import asyncio
import sys
import uuid

import httpx

from data.db import async_session_factory
from data.models import DiagramType, RequirementLevel
from data.repository import DiagramRepo, RequirementRepo

BASE_URL = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8125"


async def _register_and_login(client: httpx.AsyncClient, label: str) -> str:
    email = f"artifacts-{label}-{uuid.uuid4()}@test.dev"
    password = "correct-horse-battery-staple"
    r = await client.post("/auth/register", json={"email": email, "password": password})
    assert r.status_code == 201, f"register failed: {r.status_code} {r.text}"
    r = await client.post("/auth/login", json={"email": email, "password": password})
    assert r.status_code == 200, f"login failed: {r.status_code} {r.text}"
    return r.json()["access_token"]


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


async def _new_session(client: httpx.AsyncClient, token: str, label: str) -> str:
    r = await client.post("/projects", json={"name": f"Artifacts {label}"}, headers=_auth(token))
    assert r.status_code == 201, r.text
    project_id = r.json()["id"]
    r = await client.post(f"/projects/{project_id}/sessions", json={}, headers=_auth(token))
    assert r.status_code == 201, r.text
    return r.json()["id"]


import json as _json


async def _stream(client: httpx.AsyncClient, path: str, token: str, json_body: dict | None = None):
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
                events.append((event_name, _json.loads(line[len("data: "):])))
    return status_code, events, None


async def _turn(client, session_id, token, message):
    return await _stream(client, f"/sessions/{session_id}/turn", token, {"message": message})


async def _resume(client, session_id, token, action_body):
    return await _stream(client, f"/sessions/{session_id}/resume", token, action_body)


def _last_interrupt(events) -> dict:
    interrupts = [p for n, p in events if n == "interrupt"]
    assert len(interrupts) == 1, f"expected exactly one interrupt event, got {len(interrupts)}: {interrupts}"
    return interrupts[0]


def _resume_body_for(interrupt_payload: dict) -> dict:
    pattern = interrupt_payload["pattern"]
    if pattern == "plan_clarify":
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
    return {"action": "approve"}


async def _drive_to_completion(client, session_id, token, status_code, events, max_hops=20):
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

        # --- 1. full loop end-to-end via HTTP only: light_refs id matches /artifacts ---
        print("\n--- 1. full loop: turn -> resume approve -> /sessions/{id}/artifacts matches light_refs ---")
        session_1 = await _new_session(client, token_a, "loop")
        status_code, events, _ = await _turn(
            client, session_1, token_a,
            "Generate an operational requirement stating that the vehicle shall stop safely "
            "within the available road distance when braking.",
        )
        assert status_code == 200
        events, hops = await _drive_to_completion(client, session_1, token_a, status_code, events)
        done_payload = events[-1][1]
        assert done_payload["status"] == "completed", done_payload
        light_refs = done_payload["light_refs"]
        assert len(light_refs) >= 1, done_payload

        r = await client.get(f"/sessions/{session_1}/artifacts", headers=_auth(token_a))
        assert r.status_code == 200, r.text
        artifact_ids = {item["id"] for item in r.json()}
        light_ref_ids = {ref["artifact_id"] for ref in light_refs}
        assert light_ref_ids <= artifact_ids, (
            f"light_ref ids {light_ref_ids} not fully present in /artifacts ids {artifact_ids}"
        )
        print(f"assert OK: light_refs {light_ref_ids} all resolve via GET /sessions/{{id}}/artifacts")

        # --- 2. GET /requirements/{id} returns full text ---
        print("\n--- 2. GET /requirements/{id} returns non-empty full SysML v2 text ---")
        requirement_light_ref = next(ref for ref in light_refs if ref["artifact_type"] == "requirement")
        req_id = requirement_light_ref["artifact_id"]
        r = await client.get(f"/requirements/{req_id}", headers=_auth(token_a))
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["id"] == req_id
        assert isinstance(body["content"], str) and len(body["content"]) > 0
        assert body["is_active"] is True
        print(f"assert OK: requirement {req_id} content length={len(body['content'])}, is_active=True")

        # --- 3. versioning: seed a v2 (see module docstring re: the graph gap), verify reads ---
        print("\n--- 3. versioning: active_only shows v2 only; /versions shows both flagged; v1 superseded ---")
        session_3 = await _new_session(client, token_a, "version")
        async with async_session_factory() as db:
            v1 = await RequirementRepo.finalize(
                db, session_id=uuid.UUID(session_3), content="v1: vehicle shall stop safely.",
                level=RequirementLevel.operational,
            )
            await db.commit()
            v2 = await RequirementRepo.supersede_and_create_version(
                db, old_id=v1.id, new_content="v2: vehicle shall stop safely within available road distance.",
                session_id=uuid.UUID(session_3),
            )
            await db.commit()
            v2 = await RequirementRepo.promote(db, id=v2.id, session_id=uuid.UUID(session_3))
            await db.commit()

        r = await client.get(f"/sessions/{session_3}/requirements", headers=_auth(token_a))
        assert r.status_code == 200, r.text
        active_list = r.json()
        assert len(active_list) == 1, f"expected exactly 1 active entry for this lineage, got {active_list}"
        assert active_list[0]["id"] == str(v2.id)
        assert active_list[0]["root_id"] == str(v1.id)

        r = await client.get(f"/requirements/{v2.id}/versions", headers=_auth(token_a))
        assert r.status_code == 200, r.text
        versions = r.json()
        assert [v["version"] for v in versions] == [1, 2], versions
        assert versions[0]["is_active"] is False and versions[0]["status"] == "superseded"
        assert versions[1]["is_active"] is True and versions[1]["status"] == "active"

        r = await client.get(f"/requirements/{v1.id}", headers=_auth(token_a))
        assert r.status_code == 200, r.text
        assert r.json()["is_active"] is False
        print(f"assert OK: active_only=[v2 id={v2.id}]; /versions=[v1 superseded, v2 active] in order; "
              f"v1 directly retrievable and marked superseded")

        # --- 4. level filter: functional only, source/parent points at operational ---
        print("\n--- 4. level filter: ?level=functional returns only functional, derived-from points to operational ---")
        session_4 = await _new_session(client, token_a, "level")
        status_code, events, _ = await _turn(
            client, session_4, token_a,
            "I need two things, in this order. First, generate an operational requirement "
            "stating that the vehicle shall stop safely within the available road distance "
            "when braking. Second, generate the functional requirement derived from that "
            "operational requirement.",
        )
        assert status_code == 200
        events, hops = await _drive_to_completion(client, session_4, token_a, status_code, events)
        assert events[-1][1]["status"] == "completed", events[-1]

        r = await client.get(f"/sessions/{session_4}/requirements", params={"level": "functional"}, headers=_auth(token_a))
        assert r.status_code == 200, r.text
        functional_list = r.json()
        assert len(functional_list) == 1, f"expected exactly 1 functional requirement, got {functional_list}"
        assert functional_list[0]["level"] == "functional"

        r = await client.get(f"/sessions/{session_4}/requirements", params={"level": "operational"}, headers=_auth(token_a))
        operational_list = r.json()
        assert len(operational_list) == 1, operational_list
        derived_from = functional_list[0]["derived_from_requirement_id"]
        assert derived_from == operational_list[0]["id"], (
            f"expected functional's derived_from to point at the operational id "
            f"{operational_list[0]['id']}, got {derived_from}"
        )
        print(f"assert OK: level=functional -> exactly 1 result; derived_from_requirement_id="
              f"{derived_from} matches the session's operational requirement")

        # --- 5. diagram GET: model + non-empty Mermaid + linked requirement ids ---
        print("\n--- 5. GET /diagrams/{id}: model text, non-empty Mermaid, linked requirement id(s) ---")
        session_5 = await _new_session(client, token_a, "diagram")
        async with async_session_factory() as db:
            seed_req = await RequirementRepo.finalize(
                db, session_id=uuid.UUID(session_5), content="req: log sensor faults",
                level=RequirementLevel.operational,
            )
            await db.commit()

        status_code, events, _ = await _turn(client, session_5, token_a, "Give me a use case diagram.")
        assert status_code == 200
        events, hops = await _drive_to_completion(client, session_5, token_a, status_code, events)
        assert events[-1][1]["status"] == "completed", events[-1]
        diagram_light_ref = next(ref for ref in events[-1][1]["light_refs"] if ref["artifact_type"] == "diagram")
        diagram_id = diagram_light_ref["artifact_id"]

        r = await client.get(f"/diagrams/{diagram_id}", headers=_auth(token_a))
        assert r.status_code == 200, r.text
        diagram_body = r.json()
        assert isinstance(diagram_body["sysml_text"], str) and len(diagram_body["sysml_text"]) > 0
        assert isinstance(diagram_body["mermaid"], str) and len(diagram_body["mermaid"]) > 0
        assert isinstance(diagram_body["requirement_ids"], list) and len(diagram_body["requirement_ids"]) >= 1
        print(f"assert OK: diagram {diagram_id} sysml_text len={len(diagram_body['sysml_text'])}, "
              f"mermaid len={len(diagram_body['mermaid'])}, requirement_ids={diagram_body['requirement_ids']}")

        # --- 6. cross-user isolation: 404 everywhere, never 403 ---
        print("\n--- 6. cross-user isolation: user B -> 404 on every endpoint touching user A's ids ---")
        r = await client.get(f"/sessions/{session_1}/requirements", headers=_auth(token_b))
        assert r.status_code == 404, r.text
        r = await client.get(f"/requirements/{req_id}", headers=_auth(token_b))
        assert r.status_code == 404, r.text
        r = await client.get(f"/requirements/{req_id}/versions", headers=_auth(token_b))
        assert r.status_code == 404, r.text
        r = await client.get(f"/sessions/{session_5}/diagrams", headers=_auth(token_b))
        assert r.status_code == 404, r.text
        r = await client.get(f"/diagrams/{diagram_id}", headers=_auth(token_b))
        assert r.status_code == 404, r.text
        r = await client.get(f"/sessions/{session_1}/artifacts", headers=_auth(token_b))
        assert r.status_code == 404, r.text
        print("assert OK: 404 on requirements list, requirement, versions, diagrams list, diagram, artifacts")

        # --- 7. unknown UUID -> 404, malformed UUID -> 422 ---
        print("\n--- 7. unknown UUID -> 404; malformed UUID -> 422 ---")
        random_id = uuid.uuid4()
        r = await client.get(f"/requirements/{random_id}", headers=_auth(token_a))
        assert r.status_code == 404, r.text
        r = await client.get(f"/diagrams/{random_id}", headers=_auth(token_a))
        assert r.status_code == 404, r.text
        r = await client.get("/requirements/not-a-uuid", headers=_auth(token_a))
        assert r.status_code == 422, r.text
        r = await client.get("/diagrams/not-a-uuid", headers=_auth(token_a))
        assert r.status_code == 422, r.text
        print("assert OK: unknown UUID -> 404; malformed UUID -> 422")

        # --- 8. pagination: limit=1, offset advances correctly ---
        print("\n--- 8. pagination: limit=1 returns one item, offset advances ---")
        session_8 = await _new_session(client, token_a, "page")
        async with async_session_factory() as db:
            r1 = await RequirementRepo.finalize(
                db, session_id=uuid.UUID(session_8), content="req 1", level=RequirementLevel.operational,
            )
            r2 = await RequirementRepo.finalize(
                db, session_id=uuid.UUID(session_8), content="req 2", level=RequirementLevel.operational,
            )
            await db.commit()

        resp = await client.get(f"/sessions/{session_8}/requirements", params={"limit": 1, "offset": 0}, headers=_auth(token_a))
        assert resp.status_code == 200, resp.text
        page_0 = resp.json()
        assert len(page_0) == 1, page_0

        resp = await client.get(f"/sessions/{session_8}/requirements", params={"limit": 1, "offset": 1}, headers=_auth(token_a))
        assert resp.status_code == 200, resp.text
        page_1 = resp.json()
        assert len(page_1) == 1, page_1
        assert page_0[0]["id"] != page_1[0]["id"], "offset=1 must return a DIFFERENT item than offset=0"
        print(f"assert OK: limit=1 -> 1 item per page; offset=0 id={page_0[0]['id']} != offset=1 id={page_1[0]['id']}")

        # --- 9. no auth header -> 401 on every endpoint ---
        print("\n--- 9. no auth header -> 401 on every endpoint ---")
        r = await client.get(f"/sessions/{session_1}/requirements")
        assert r.status_code == 401, r.text
        r = await client.get(f"/requirements/{req_id}")
        assert r.status_code == 401, r.text
        r = await client.get(f"/requirements/{req_id}/versions")
        assert r.status_code == 401, r.text
        r = await client.get(f"/sessions/{session_5}/diagrams")
        assert r.status_code == 401, r.text
        r = await client.get(f"/diagrams/{diagram_id}")
        assert r.status_code == 401, r.text
        r = await client.get(f"/sessions/{session_1}/artifacts")
        assert r.status_code == 401, r.text
        print("assert OK: 401 on every endpoint with no Authorization header")

    print("\n=== ARTIFACTS SMOKE TEST SUITE PASSED (all 9 scenarios; #10 is the regression sweep, run separately) ===")


if __name__ == "__main__":
    asyncio.run(main())
