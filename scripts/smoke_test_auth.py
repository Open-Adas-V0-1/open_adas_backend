"""T6b Step 1: JWT authentication layer (register / login / me).

Runs against the REAL FastAPI app + REAL Postgres (via httpx, over HTTP -- not the
ASGI-in-process shortcut used by unit tests, since this is meant to prove the actual
running service works end to end, same spirit as this project's other integration
smoke tests). Start the app first:

    uvicorn app.main:app --host 127.0.0.1 --port 8123

Then: python -m scripts.smoke_test_auth [base_url]  (default base_url: http://127.0.0.1:8123)
"""
import asyncio
import sys
import uuid
from datetime import datetime, timedelta, timezone

import httpx

from app.config import get_settings

BASE_URL = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8123"


def _make_expired_token() -> str:
    """A token signed with the app's REAL secret/algorithm but with an exp already in
    the past -- proves expiry is actually enforced, not just "any garbage token fails".
    """
    from jose import jwt

    settings = get_settings()
    payload = {
        "sub": str(uuid.uuid4()),
        "exp": datetime.now(timezone.utc) - timedelta(minutes=5),
    }
    return jwt.encode(payload, settings.jwt_secret_key, algorithm=settings.jwt_algorithm)


async def main() -> None:
    async with httpx.AsyncClient(base_url=BASE_URL, timeout=10.0) as client:
        email = f"smoke-{uuid.uuid4()}@test.dev"
        password = "correct-horse-battery-staple"

        # --- 1. register a new user ---
        print(f"\n--- 1. register {email!r} ---")
        r = await client.post("/auth/register", json={"email": email, "password": password})
        assert r.status_code in (200, 201), f"expected 200/201, got {r.status_code}: {r.text}"
        body = r.json()
        assert "id" in body and body["email"] == email
        assert "password" not in body and "password_hash" not in body and "hash" not in body, (
            f"response must NEVER include the password hash: {body}"
        )
        user_id = body["id"]
        print(f"assert OK: status={r.status_code} id={user_id} email={body['email']} (no hash in response)")

        # --- 2. duplicate email -> 409 ---
        print(f"\n--- 2. register the SAME email again -> expect 409 ---")
        r = await client.post("/auth/register", json={"email": email, "password": password})
        assert r.status_code == 409, f"expected 409, got {r.status_code}: {r.text}"
        print(f"assert OK: status={r.status_code} detail={r.json().get('detail')!r}")

        # --- 3. login with correct password ---
        print(f"\n--- 3. login with the correct password ---")
        r = await client.post("/auth/login", json={"email": email, "password": password})
        assert r.status_code == 200, f"expected 200, got {r.status_code}: {r.text}"
        token_body = r.json()
        assert token_body.get("token_type") == "bearer"
        access_token = token_body["access_token"]
        assert access_token, "expected a non-empty access_token"
        print(f"assert OK: status={r.status_code} token_type={token_body['token_type']!r} "
              f"access_token={access_token[:20]}...(truncated)")

        # --- 4. login with wrong password -> 401 ---
        print(f"\n--- 4. login with a WRONG password -> expect 401 ---")
        r = await client.post("/auth/login", json={"email": email, "password": "not-the-password"})
        assert r.status_code == 401, f"expected 401, got {r.status_code}: {r.text}"
        wrong_password_detail = r.json().get("detail")
        print(f"assert OK: status={r.status_code} detail={wrong_password_detail!r}")

        # --- 5. login with unknown email -> 401, SAME message (no user enumeration) ---
        print(f"\n--- 5. login with an UNKNOWN email -> expect 401, SAME message as step 4 ---")
        r = await client.post(
            "/auth/login", json={"email": f"nobody-{uuid.uuid4()}@test.dev", "password": password}
        )
        assert r.status_code == 401, f"expected 401, got {r.status_code}: {r.text}"
        unknown_email_detail = r.json().get("detail")
        assert unknown_email_detail == wrong_password_detail, (
            f"user enumeration risk: wrong-password detail={wrong_password_detail!r} != "
            f"unknown-email detail={unknown_email_detail!r}"
        )
        print(f"assert OK: status={r.status_code} detail={unknown_email_detail!r} "
              f"(identical to step 4 -- no user enumeration)")

        # --- 6. GET /auth/me with the token ---
        print(f"\n--- 6. GET /auth/me with the token ---")
        r = await client.get("/auth/me", headers={"Authorization": f"Bearer {access_token}"})
        assert r.status_code == 200, f"expected 200, got {r.status_code}: {r.text}"
        me_body = r.json()
        assert me_body["id"] == user_id, f"expected id={user_id}, got {me_body['id']}"
        assert "password" not in me_body and "password_hash" not in me_body
        print(f"assert OK: status={r.status_code} id={me_body['id']} email={me_body['email']} "
              f"(matches step 1's user, no hash)")

        # --- 7. GET /auth/me with no header -> 401 ---
        print(f"\n--- 7. GET /auth/me with NO Authorization header -> expect 401 ---")
        r = await client.get("/auth/me")
        assert r.status_code == 401, f"expected 401, got {r.status_code}: {r.text}"
        print(f"assert OK: status={r.status_code} detail={r.json().get('detail')!r}")

        # --- 8. GET /auth/me with a garbage token -> 401 ---
        print(f"\n--- 8. GET /auth/me with a GARBAGE token -> expect 401 ---")
        r = await client.get("/auth/me", headers={"Authorization": "Bearer not.a.real.jwt"})
        assert r.status_code == 401, f"expected 401, got {r.status_code}: {r.text}"
        print(f"assert OK: status={r.status_code} detail={r.json().get('detail')!r}")

        # --- 9. GET /auth/me with an EXPIRED token -> 401 ---
        print(f"\n--- 9. GET /auth/me with an EXPIRED token (exp in the past) -> expect 401 ---")
        expired_token = _make_expired_token()
        r = await client.get("/auth/me", headers={"Authorization": f"Bearer {expired_token}"})
        assert r.status_code == 401, f"expected 401, got {r.status_code}: {r.text}"
        print(f"assert OK: status={r.status_code} detail={r.json().get('detail')!r} "
              f"(real secret/algorithm, exp already elapsed)")

        # --- 10. /health still returns ok ---
        print(f"\n--- 10. /health still returns ok ---")
        r = await client.get("/health")
        assert r.status_code == 200, f"expected 200, got {r.status_code}: {r.text}"
        health_body = r.json()
        assert health_body.get("db") == "ok" and health_body.get("storage") == "ok"
        print(f"assert OK: status={r.status_code} body={health_body}")

    print("\n=== AUTH SMOKE TEST SUITE PASSED (all 10 checks) ===")


if __name__ == "__main__":
    asyncio.run(main())
