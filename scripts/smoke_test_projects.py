"""T6b Step 2: Projects & Sessions API.

Runs against the REAL FastAPI app + REAL Postgres over HTTP (same shape as
scripts/smoke_test_auth.py). Start the app first:

    uvicorn app.main:app --host 127.0.0.1 --port 8123

Then: python -m scripts.smoke_test_projects [base_url]  (default: http://127.0.0.1:8123)

Includes the "checkpoint purge" scenario: fake checkpoint rows are inserted directly
into the LangGraph checkpointer tables (checkpoints/checkpoint_blobs/checkpoint_writes)
for a real session id, its derived Layer-2/Layer-3 sub-threads, and an unrelated
control thread -- then DELETE /sessions/{id} must purge exactly the matching rows and
leave the control row untouched.
"""
import asyncio
import sys
import uuid

import httpx
from sqlalchemy import text

from data.db import async_session_factory

BASE_URL = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8123"

_CHECKPOINT_TABLES = ("checkpoints", "checkpoint_blobs", "checkpoint_writes")


async def _register_and_login(client: httpx.AsyncClient, label: str) -> tuple[str, str]:
    """Returns (user_id, access_token)."""
    email = f"projsmoke-{label}-{uuid.uuid4()}@test.dev"
    password = "correct-horse-battery-staple"
    r = await client.post("/auth/register", json={"email": email, "password": password})
    assert r.status_code == 201, f"register failed: {r.status_code} {r.text}"
    user_id = r.json()["id"]
    r = await client.post("/auth/login", json={"email": email, "password": password})
    assert r.status_code == 200, f"login failed: {r.status_code} {r.text}"
    return user_id, r.json()["access_token"]


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


async def _insert_fake_checkpoint_row(db, table: str, thread_id: str) -> None:
    """Minimal row satisfying each table's NOT NULL columns -- content is irrelevant,
    only thread_id (the purge key) and presence/absence matter for this test.
    """
    if table == "checkpoints":
        await db.execute(
            text(
                "INSERT INTO checkpoints "
                "(thread_id, checkpoint_ns, checkpoint_id, type, checkpoint, metadata) "
                "VALUES (:tid, '', :cid, 'json', '{}'::jsonb, '{}'::jsonb)"
            ),
            {"tid": thread_id, "cid": str(uuid.uuid4())},
        )
    elif table == "checkpoint_blobs":
        await db.execute(
            text(
                "INSERT INTO checkpoint_blobs "
                "(thread_id, checkpoint_ns, channel, version, type, blob) "
                "VALUES (:tid, '', 'test-channel', '1', 'json', '{}'::bytea)"
            ),
            {"tid": thread_id},
        )
    else:
        await db.execute(
            text(
                "INSERT INTO checkpoint_writes "
                "(thread_id, checkpoint_ns, checkpoint_id, task_id, idx, channel, type, blob, task_path) "
                "VALUES (:tid, '', :cid, :task_id, 0, 'test-channel', 'json', '{}'::bytea, '')"
            ),
            {"tid": thread_id, "cid": str(uuid.uuid4()), "task_id": str(uuid.uuid4())},
        )


async def _count_rows(db, table: str, thread_id: str) -> int:
    result = await db.execute(
        text(f"SELECT count(*) FROM {table} WHERE thread_id = :tid"), {"tid": thread_id}
    )
    return result.scalar_one()


async def main() -> None:
    async with httpx.AsyncClient(base_url=BASE_URL, timeout=10.0) as client:
        # --- 1. register + login user A and user B ---
        print("\n--- 1. register + login user A and user B ---")
        user_a_id, token_a = await _register_and_login(client, "a")
        user_b_id, token_b = await _register_and_login(client, "b")
        assert user_a_id != user_b_id
        print(f"assert OK: user A={user_a_id} user B={user_b_id}")

        # --- 2. A creates a project ---
        print("\n--- 2. A creates a project ---")
        r = await client.post(
            "/projects", json={"name": "A's Project", "description": "test"}, headers=_auth(token_a)
        )
        assert r.status_code == 201, f"expected 201, got {r.status_code}: {r.text}"
        project_a = r.json()
        assert project_a["user_id"] == user_a_id
        project_a_id = project_a["id"]
        print(f"assert OK: status={r.status_code} project_id={project_a_id} owner={project_a['user_id']}")

        # --- 3. A creates 2 sessions in it ---
        print("\n--- 3. A creates 2 sessions in the project ---")
        r1 = await client.post(f"/projects/{project_a_id}/sessions", json={"title": "Session 1"}, headers=_auth(token_a))
        r2 = await client.post(f"/projects/{project_a_id}/sessions", json={"title": "Session 2"}, headers=_auth(token_a))
        assert r1.status_code == 201 and r2.status_code == 201, (r1.text, r2.text)
        session_1, session_2 = r1.json(), r2.json()
        assert session_1["id"] != session_2["id"]
        assert uuid.UUID(session_1["id"]) and uuid.UUID(session_2["id"])
        print(f"assert OK: session_1={session_1['id']} session_2={session_2['id']} (distinct UUIDs)")

        # --- 4. GET /projects as A vs as B ---
        print("\n--- 4. GET /projects as A lists A's project; as B lists none of A's ---")
        r = await client.get("/projects", headers=_auth(token_a))
        assert r.status_code == 200
        a_project_ids = {p["id"] for p in r.json()}
        assert project_a_id in a_project_ids
        r = await client.get("/projects", headers=_auth(token_b))
        assert r.status_code == 200
        b_project_ids = {p["id"] for p in r.json()}
        assert project_a_id not in b_project_ids
        print(f"assert OK: A sees {len(a_project_ids)} project(s) incl. own; "
              f"B sees {len(b_project_ids)} project(s), none of A's")

        # --- 5. B GET /projects/{A's project} -> 404 ---
        print("\n--- 5. B does GET /projects/{A's project} -> expect 404 ---")
        r = await client.get(f"/projects/{project_a_id}", headers=_auth(token_b))
        assert r.status_code == 404, f"expected 404, got {r.status_code}: {r.text}"
        print(f"assert OK: status={r.status_code} detail={r.json().get('detail')!r}")

        # --- 6. B GET /sessions/{A's session} -> 404 ---
        print("\n--- 6. B does GET /sessions/{A's session} -> expect 404 ---")
        r = await client.get(f"/sessions/{session_1['id']}", headers=_auth(token_b))
        assert r.status_code == 404, f"expected 404, got {r.status_code}: {r.text}"
        print(f"assert OK: status={r.status_code} detail={r.json().get('detail')!r}")

        # --- 7. B DELETE /sessions/{A's session} -> 404, session still exists for A ---
        print("\n--- 7. B does DELETE /sessions/{A's session} -> expect 404, session survives ---")
        r = await client.delete(f"/sessions/{session_1['id']}", headers=_auth(token_b))
        assert r.status_code == 404, f"expected 404, got {r.status_code}: {r.text}"
        r = await client.get(f"/sessions/{session_1['id']}", headers=_auth(token_a))
        assert r.status_code == 200, "session must still exist for A after B's failed delete attempt"
        print(f"assert OK: B's delete attempt -> 404; A can still GET the session (status={r.status_code})")

        # --- 8. A renames a session via PATCH ---
        print("\n--- 8. A renames session_2 via PATCH ---")
        new_title = "Renamed Session 2"
        r = await client.patch(f"/sessions/{session_2['id']}", json={"title": new_title}, headers=_auth(token_a))
        assert r.status_code == 200, f"expected 200, got {r.status_code}: {r.text}"
        assert r.json()["title"] == new_title
        r = await client.get(f"/sessions/{session_2['id']}", headers=_auth(token_a))
        assert r.status_code == 200 and r.json()["title"] == new_title, "rename must persist on re-GET"
        print(f"assert OK: PATCH returned title={new_title!r}, persisted on re-GET")

        # --- 9. checkpoint purge test ---
        print("\n--- 9. checkpoint purge test (the key one) ---")
        # Fresh session dedicated to this test, so counts are unambiguous.
        r = await client.post(f"/projects/{project_a_id}/sessions", json={"title": "Purge Target"}, headers=_auth(token_a))
        assert r.status_code == 201
        purge_session_id = r.json()["id"]

        matching_thread_ids = [
            purge_session_id,
            f"{purge_session_id}:proc:1",
            f"{purge_session_id}:middle",
        ]
        control_thread_id = f"unrelated-{uuid.uuid4()}"

        async with async_session_factory() as db:
            for table in _CHECKPOINT_TABLES:
                for tid in matching_thread_ids:
                    await _insert_fake_checkpoint_row(db, table, tid)
                await _insert_fake_checkpoint_row(db, table, control_thread_id)
            await db.commit()

            # sanity: rows actually landed before we test the purge
            for table in _CHECKPOINT_TABLES:
                for tid in matching_thread_ids:
                    assert await _count_rows(db, table, tid) == 1, f"seed row missing in {table} for {tid}"
                assert await _count_rows(db, table, control_thread_id) == 1, f"control row missing in {table}"
        print(f"  seeded fake checkpoint rows across {_CHECKPOINT_TABLES} for "
              f"{matching_thread_ids} + control={control_thread_id}")

        r = await client.delete(f"/sessions/{purge_session_id}", headers=_auth(token_a))
        assert r.status_code == 204, f"expected 204, got {r.status_code}: {r.text}"

        async with async_session_factory() as db:
            for table in _CHECKPOINT_TABLES:
                for tid in matching_thread_ids:
                    count = await _count_rows(db, table, tid)
                    assert count == 0, f"expected {tid} purged from {table}, found {count} row(s)"
                control_count = await _count_rows(db, table, control_thread_id)
                assert control_count == 1, f"control row in {table} must survive, found {control_count}"
        print(f"assert OK: all {len(matching_thread_ids)} matching thread ids purged from all "
              f"{len(_CHECKPOINT_TABLES)} checkpoint tables; control row untouched in every table")

        # cleanup the control row (test hygiene, not part of the assertion)
        async with async_session_factory() as db:
            for table in _CHECKPOINT_TABLES:
                await db.execute(text(f"DELETE FROM {table} WHERE thread_id = :tid"), {"tid": control_thread_id})
            await db.commit()

        # --- 10. deleting a project deletes remaining sessions + purges their checkpoints ---
        print("\n--- 10. DELETE project deletes remaining sessions and purges their checkpoints ---")
        r = await client.post("/projects", json={"name": "Doomed Project"}, headers=_auth(token_a))
        assert r.status_code == 201
        doomed_project_id = r.json()["id"]
        r = await client.post(f"/projects/{doomed_project_id}/sessions", json={"title": "Doomed Session"}, headers=_auth(token_a))
        assert r.status_code == 201
        doomed_session_id = r.json()["id"]

        async with async_session_factory() as db:
            for table in _CHECKPOINT_TABLES:
                await _insert_fake_checkpoint_row(db, table, doomed_session_id)
            await db.commit()

        r = await client.delete(f"/projects/{doomed_project_id}", headers=_auth(token_a))
        assert r.status_code == 204, f"expected 204, got {r.status_code}: {r.text}"

        r = await client.get(f"/projects/{doomed_project_id}", headers=_auth(token_a))
        assert r.status_code == 404, "deleted project must 404 for its own former owner too"
        r = await client.get(f"/sessions/{doomed_session_id}", headers=_auth(token_a))
        assert r.status_code == 404, "deleted project's session row must be gone (DB cascade)"

        async with async_session_factory() as db:
            for table in _CHECKPOINT_TABLES:
                count = await _count_rows(db, table, doomed_session_id)
                assert count == 0, f"expected {doomed_session_id} purged from {table} via project delete, found {count}"
        print(f"assert OK: project deleted (404 on re-GET), its session gone (404), "
              f"session's checkpoints purged from all {len(_CHECKPOINT_TABLES)} tables")

        # --- 11. every endpoint without Authorization -> 401 ---
        print("\n--- 11. every endpoint without Authorization header -> expect 401 ---")
        unauthed_checks = [
            ("POST", "/projects", {"json": {"name": "x"}}),
            ("GET", "/projects", {}),
            ("GET", f"/projects/{project_a_id}", {}),
            ("DELETE", f"/projects/{project_a_id}", {}),
            ("POST", f"/projects/{project_a_id}/sessions", {"json": {"title": "x"}}),
            ("GET", f"/projects/{project_a_id}/sessions", {}),
            ("GET", f"/sessions/{session_2['id']}", {}),
            ("PATCH", f"/sessions/{session_2['id']}", {"json": {"title": "x"}}),
            ("DELETE", f"/sessions/{session_2['id']}", {}),
        ]
        for method, path, kwargs in unauthed_checks:
            r = await client.request(method, path, **kwargs)
            assert r.status_code == 401, f"{method} {path} without auth: expected 401, got {r.status_code}: {r.text}"
        print(f"assert OK: all {len(unauthed_checks)} endpoints return 401 without an Authorization header")

        # --- 12. /health and the auth smoke test still pass ---
        print("\n--- 12. /health still returns ok ---")
        r = await client.get("/health")
        assert r.status_code == 200 and r.json().get("db") == "ok" and r.json().get("storage") == "ok"
        print(f"assert OK: status={r.status_code} body={r.json()}")

    print("\n=== PROJECTS & SESSIONS SMOKE TEST SUITE PASSED (all 12 checks) ===")


if __name__ == "__main__":
    asyncio.run(main())
