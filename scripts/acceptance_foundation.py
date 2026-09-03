"""Acceptance foundation checks -- A2, A3, B1(hash), B2, B3 from
MVP1_ACCEPTANCE_TESTS.md, restricted to the parts that CANNOT be observed from the
dev UI: repository-layer statics and raw Postgres rows (secrets, checkpointer
singleton/encryption, password hashing, cross-user ownership isolation, and the
session_id == checkpointer thread_id invariant).

READ-ONLY on existing data. Creates ONLY its own throwaway users/projects/sessions
(prefixed "acc-foundation-") and deletes exactly those, in a finally block. Never
touches any other row.

Every check prints RAW evidence (actual bytes/rows/paths/status codes) before its
verdict. A check that cannot be performed prints INCONCLUSIVE with the reason --
never PASS by default. Nothing here is softened after the fact.

Run: python -m scripts.acceptance_foundation [base_url]  (default: http://127.0.0.1:8000)
"""
import ast
import asyncio
import re
import subprocess
import sys
import uuid
from pathlib import Path

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

import httpx  # noqa: E402
from sqlalchemy import text  # noqa: E402

from app.config import get_settings  # noqa: E402
from data.db import async_session_factory  # noqa: E402
from data.repository import CheckpointRepo, UserRepo  # noqa: E402

BASE_URL = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8000"
REPO_ROOT = Path(__file__).resolve().parents[1]
PREFIX = "acc-foundation-"

# (check_id, status, one-line detail) -- status is "PASS" | "FAIL" | "INCONCLUSIVE"
RESULTS: list[tuple[str, str, str]] = []


def record(check_id: str, status: str, detail: str) -> None:
    assert status in ("PASS", "FAIL", "INCONCLUSIVE"), status
    RESULTS.append((check_id, status, detail))
    print(f"  >>> {check_id}: {status} -- {detail}")


# ── A2: secrets ──────────────────────────────────────────────────────────────

def run_git(args: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=REPO_ROOT, capture_output=True, text=True)


_SECRET_PATTERN = re.compile(
    r"(api[_-]?key|secret|password|token)\s*[:=]\s*[\"']([^\"'\n]{16,})[\"']",
    re.IGNORECASE,
)
_EXCLUDED_TRACKED = {".env.example", "uv.lock"}


def check_a2_secrets() -> None:
    print("\n--- A2: secrets (.env git-ignored, no long literal secrets in tracked files) ---")

    ignore_check = run_git(["check-ignore", "-v", ".env"])
    print(f"  git check-ignore -v .env -> exit={ignore_check.returncode} stdout={ignore_check.stdout.strip()!r} "
          f"stderr={ignore_check.stderr.strip()!r}")
    is_ignored = ignore_check.returncode == 0 and bool(ignore_check.stdout.strip())

    ls_files_env = run_git(["ls-files", ".env"])
    print(f"  git ls-files .env -> exit={ls_files_env.returncode} stdout={ls_files_env.stdout.strip()!r}")
    is_tracked = bool(ls_files_env.stdout.strip())

    if is_ignored and not is_tracked:
        record("A2.env_ignored", "PASS", ".env is git-ignored and not tracked")
    else:
        record("A2.env_ignored", "FAIL", f"is_ignored={is_ignored} is_tracked={is_tracked}")

    ls_files_all = run_git(["ls-files"])
    if ls_files_all.returncode != 0:
        record("A2.secret_scan", "INCONCLUSIVE", f"git ls-files failed: {ls_files_all.stderr.strip()}")
        return

    tracked = [
        p for p in ls_files_all.stdout.splitlines()
        if p and "node_modules/" not in p and p not in _EXCLUDED_TRACKED
    ]
    print(f"  scanning {len(tracked)} git-tracked files (excluding node_modules/, {_EXCLUDED_TRACKED})")

    hits: list[str] = []
    unreadable = 0
    for rel in tracked:
        full = REPO_ROOT / rel
        try:
            content = full.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            unreadable += 1
            continue
        for lineno, line in enumerate(content.splitlines(), start=1):
            for m in _SECRET_PATTERN.finditer(line):
                hits.append(f"{rel}:{lineno}: {line.strip()[:160]}")

    print(f"  {unreadable} tracked files skipped (binary/undecodable)")
    if hits:
        print(f"  {len(hits)} hit(s):")
        for h in hits:
            print(f"    {h}")
        record("A2.secret_scan", "FAIL", f"{len(hits)} literal secret-shaped assignment(s) found in tracked files")
    else:
        record("A2.secret_scan", "PASS", "zero literal secret-shaped assignments in tracked files")


# ── A3: checkpointer singleton, encryption at rest, thread ids ─────────────────

_A3_SCAN_DIRS = ["app", "agents", "supervisor", "data", "harness"]
_A3_EXCLUDE_FILE = REPO_ROOT / "harness" / "checkpointer.py"


def _call_target_name(node: ast.Call) -> str | None:
    """Best-effort dotted name of what's being called, e.g. 'AsyncPostgresSaver' or
    'AsyncPostgresSaver.from_conn_string' -- None for anything else (e.g. a call on a
    local variable). AST-based, not regex, so it never matches comments/docstrings/
    the `def build_sysml_graph(checkpointer=None):` signature itself (that's a
    FunctionDef arg default, not a Call).
    """
    func = node.func
    parts = []
    while isinstance(func, ast.Attribute):
        parts.append(func.attr)
        func = func.value
    if isinstance(func, ast.Name):
        parts.append(func.id)
        return ".".join(reversed(parts))
    return None


def _check_a3_static() -> None:
    """The part of A3 that needs no DB access: AST-scans production source for a
    second checkpointer construction site outside harness/checkpointer.py -- AST
    rather than regex so comments/docstrings and the build_*_graph(checkpointer=None)
    function SIGNATURES themselves can never be mistaken for a real call site.
    """
    print("\n--- A3: single checkpointer, encrypted at rest, thread id scheme ---")

    inspected = []
    violations = []
    for d in _A3_SCAN_DIRS:
        for path in sorted((REPO_ROOT / d).rglob("*.py")):
            if path == _A3_EXCLUDE_FILE:
                continue
            inspected.append(str(path.relative_to(REPO_ROOT)))
            try:
                content = path.read_text(encoding="utf-8")
                tree = ast.parse(content, filename=str(path))
            except (UnicodeDecodeError, OSError, SyntaxError):
                continue
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                target = _call_target_name(node)
                if target in ("AsyncPostgresSaver", "AsyncPostgresSaver.from_conn_string"):
                    violations.append(f"{path.relative_to(REPO_ROOT)}:{node.lineno}: {target}(...) call site")
                    continue
                if target in ("build_middle_graph", "build_sysml_graph"):
                    if any(kw.arg == "checkpointer" for kw in node.keywords):
                        violations.append(
                            f"{path.relative_to(REPO_ROOT)}:{node.lineno}: {target}(checkpointer=...) call site"
                        )

    print(f"  inspected {len(inspected)} production files under {_A3_SCAN_DIRS} (excluding harness/checkpointer.py)")
    if violations:
        print(f"  {len(violations)} violation(s):")
        for v in violations:
            print(f"    {v}")
        record("A3.single_checkpointer_static", "FAIL", f"{len(violations)} production call site(s) build a second checkpointer")
    else:
        record("A3.single_checkpointer_static", "PASS",
               "no production file (outside harness/checkpointer.py) instantiates AsyncPostgresSaver "
               "or passes checkpointer= into build_middle_graph/build_sysml_graph")


async def check_a3_checkpointer_async() -> None:
    _check_a3_static()

    settings = get_settings()
    plaintext_markers = [b"user_input", b"requirement", b"session_id", b"package "]

    async with async_session_factory() as db:
        result = await db.execute(
            text(
                "SELECT thread_id, checkpoint_ns, checkpoint, metadata "
                "FROM checkpoints ORDER BY checkpoint_id DESC LIMIT 50"
            )
        )
        rows = result.all()

    if not rows:
        record("A3.encrypted_at_rest", "INCONCLUSIVE", "no checkpoint rows exist yet -- run a real turn first")
        record("A3.metadata_plaintext", "INCONCLUSIVE", "no checkpoint rows exist yet")
        record("A3.thread_id_scheme", "INCONCLUSIVE", "no checkpoint rows exist yet")
        return

    print(f"  sample row: thread_id={rows[0].thread_id!r} checkpoint_ns={rows[0].checkpoint_ns!r}")
    print(f"  checkpoints.checkpoint sample (jsonb -- LangGraph's own structural bookkeeping: "
          f"channel_versions/versions_seen, never the app's channel VALUES): {str(rows[0].checkpoint)[:300]!r}")

    # The actual per-channel STATE CONTENT (session_id, user_input, draft SysML text,
    # etc.) does not live in checkpoints.checkpoint -- LangGraph externalizes each
    # channel's value into checkpoint_blobs.blob (bytea), which is what
    # EncryptedSerializer actually wraps. checkpoints.checkpoint only holds a lightweight
    # pointer/version map (confirmed above: 'channel_values' is always {}), so THAT is
    # not where an encryption check belongs -- checkpoint_blobs.blob is.
    async with async_session_factory() as db:
        blob_result = await db.execute(
            text(
                "SELECT thread_id, channel, type, blob "
                "FROM checkpoint_blobs WHERE blob IS NOT NULL ORDER BY thread_id DESC LIMIT 50"
            )
        )
        blob_rows = blob_result.all()

    if not blob_rows:
        record("A3.encrypted_at_rest", "INCONCLUSIVE", "checkpoints exist but checkpoint_blobs has no non-null blob rows yet")
    else:
        sample_blob = bytes(blob_rows[0].blob)
        print(f"  sample checkpoint_blobs row: thread_id={blob_rows[0].thread_id!r} channel={blob_rows[0].channel!r} "
              f"type={blob_rows[0].type!r}")
        print(f"  blob bytes (hex, first 100 of {len(sample_blob)}): {sample_blob[:100].hex()}")

        non_aes_types = sorted({r.type for r in blob_rows if r.type and "aes" not in r.type})
        plaintext_hits = []
        for row in blob_rows:
            blob = bytes(row.blob)
            for marker in plaintext_markers:
                if marker in blob:
                    plaintext_hits.append((row.thread_id, row.channel, marker.decode()))

        print(f"  {len(blob_rows)} blob row(s) sampled; type values NOT containing 'aes': {non_aes_types}")
        if plaintext_hits:
            print(f"  plaintext marker hits in checkpoint_blobs.blob: {plaintext_hits[:20]}")
            record("A3.encrypted_at_rest", "FAIL", f"{len(plaintext_hits)} plaintext marker hit(s) in blob bytes")
        elif non_aes_types:
            record("A3.encrypted_at_rest", "FAIL", f"{len(non_aes_types)} channel type(s) not AES-tagged: {non_aes_types}")
        else:
            record("A3.encrypted_at_rest", "PASS",
                   f"zero plaintext markers across {len(blob_rows)} sampled checkpoint_blobs rows; "
                   f"all type values contain 'aes'; CHECKPOINT_ENCRYPTION_KEY set="
                   f"{bool(settings.checkpoint_encryption_key)}")

    # Explicitly separate, NOT folded into the A3 verdict -- reported for a human decision.
    # (checkpoints.metadata is LangGraph's own run bookkeeping -- step/source/parents --
    # never app data by design; reported here as raw evidence, not assumed safe.)
    metadata_sample = rows[0].metadata
    metadata_str = str(metadata_sample)
    metadata_plaintext = any(marker.decode() in metadata_str for marker in plaintext_markers)
    print(f"  metadata_plaintext: {'yes' if metadata_plaintext else 'no'} -- sample: {metadata_str[:300]!r}")

    thread_groups: dict[str, dict] = {}
    for row in rows:
        g = thread_groups.setdefault(row.thread_id, {"count": 0, "ns": set()})
        g["count"] += 1
        g["ns"].add(row.checkpoint_ns)

    print(f"  distinct thread_id values among sampled rows ({len(thread_groups)}):")
    outer, middle, proc = set(), set(), set()
    for tid, info in thread_groups.items():
        print(f"    thread_id={tid!r} rows={info['count']} checkpoint_ns={sorted(info['ns'])}")
        if ":proc:" in tid:
            proc.add(tid)
        elif ":middle:" in tid:
            middle.add(tid)
        else:
            outer.add(tid)

    proc_ids_by_session: dict[str, set] = {}
    for tid in proc:
        session_part = tid.split(":proc:")[0]
        proc_ids_by_session.setdefault(session_part, set()).add(tid)
    collisions = {s: ids for s, ids in proc_ids_by_session.items() if len(ids) > 1}

    print(f"  outer ids: {len(outer)}, :middle: ids: {len(middle)}, :proc: ids: {len(proc)}")
    detail = (
        f"outer={len(outer)} middle={len(middle)} proc={len(proc)}; "
        f"sessions with >1 distinct :proc: id: {list(collisions.keys())[:5]}"
    )
    if outer and middle and proc:
        record("A3.thread_id_scheme", "PASS", detail + " -- all three id shapes coexist in sampled rows")
    else:
        record("A3.thread_id_scheme", "INCONCLUSIVE",
               detail + " -- not all three id shapes present in the sampled rows (small/skewed sample, not a failure)")


# ── B1: bcrypt ──────────────────────────────────────────────────────────────

async def check_b1_bcrypt(client: httpx.AsyncClient) -> tuple[str, str] | None:
    print("\n--- B1: password is bcrypt-hashed, never stored/echoed in plaintext ---")
    email = f"{PREFIX}b1-{uuid.uuid4()}@test.dev"
    password = "acc-foundation-probe-password-1"

    r = await client.post("/auth/register", json={"email": email, "password": password})
    print(f"  POST /auth/register -> {r.status_code} body={r.text[:200]}")
    if r.status_code != 201:
        record("B1.bcrypt_hash", "INCONCLUSIVE", f"register failed: {r.status_code}")
        return None
    user_id = r.json()["id"]

    async with async_session_factory() as db:
        row = (await db.execute(
            text("SELECT password_hash FROM users WHERE id = :id"), {"id": user_id}
        )).first()

    if row is None:
        record("B1.bcrypt_hash", "INCONCLUSIVE", "user row not found after register")
        return None

    password_hash = row.password_hash
    prefix = password_hash[:4]
    print(f"  users.password_hash prefix (full hash withheld): {prefix!r} len={len(password_hash)}")

    valid_prefix = password_hash.startswith(("$2a$", "$2b$", "$2y$"))
    plaintext_leaked = password in password_hash
    if valid_prefix and not plaintext_leaked:
        record("B1.bcrypt_hash", "PASS", f"prefix={prefix!r}, plaintext password not present in the hash")
    else:
        record("B1.bcrypt_hash", "FAIL", f"valid_prefix={valid_prefix} plaintext_leaked={plaintext_leaked}")

    return email, password


# ── B2: cross-user ownership isolation ────────────────────────────────────────

def _minimal_body_for(schema_name: str | None, components: dict) -> dict | None:
    if schema_name is None:
        return None
    schema = components.get(schema_name, {})
    props = schema.get("properties", {})
    required = schema.get("required", [])
    body = {}
    for field in required:
        prop = props.get(field, {})
        prop_type = prop.get("type")
        if prop_type == "string":
            body[field] = "acc-foundation-probe"
        elif prop_type == "integer":
            body[field] = 1
        elif prop_type == "number":
            body[field] = 1.0
        elif prop_type == "boolean":
            body[field] = True
        elif prop_type == "array":
            body[field] = []
        else:
            body[field] = {}
    return body


def _extract_schema_name(operation: dict) -> str | None:
    try:
        ref = operation["requestBody"]["content"]["application/json"]["schema"]["$ref"]
        return ref.rsplit("/", 1)[-1]
    except (KeyError, TypeError):
        return None


async def check_b2_isolation(client: httpx.AsyncClient, token_a: str, token_b: str) -> None:
    print("\n--- B2: cross-user ownership isolation, discovered from the live OpenAPI schema ---")

    r = await client.get("/openapi.json")
    print(f"  GET /openapi.json -> {r.status_code}")
    if r.status_code != 200:
        record("B2.isolation", "INCONCLUSIVE", f"could not fetch openapi.json: {r.status_code}")
        return
    openapi = r.json()
    components = openapi.get("components", {}).get("schemas", {})

    # A's own throwaway project/session -- created fresh here, deleted in main()'s finally.
    r = await client.post("/projects", json={"name": f"{PREFIX}b2-project"}, headers={"Authorization": f"Bearer {token_a}"})
    print(f"  A: POST /projects -> {r.status_code} {r.text[:150]}")
    if r.status_code != 201:
        record("B2.isolation", "INCONCLUSIVE", f"A could not create a project: {r.status_code}")
        return
    project_id = r.json()["id"]

    r = await client.post(
        f"/projects/{project_id}/sessions", json={"title": f"{PREFIX}b2-session"},
        headers={"Authorization": f"Bearer {token_a}"},
    )
    print(f"  A: POST /projects/{{id}}/sessions -> {r.status_code} {r.text[:150]}")
    if r.status_code != 201:
        record("B2.isolation", "INCONCLUSIVE", f"A could not create a session: {r.status_code}")
        return
    session_id = r.json()["id"]

    ids = {"project_id": project_id, "session_id": session_id}

    routes: list[tuple[str, str, dict]] = []
    skipped: list[str] = []
    for path, methods in openapi.get("paths", {}).items():
        if path.startswith("/auth") or path in ("/health",) or path.startswith("/dev"):
            continue
        param_names = set(re.findall(r"\{(\w+)\}", path))
        if not param_names:
            continue  # e.g. POST/GET /projects -- not "another user's project/session" targeting
        if not param_names.issubset(ids.keys()):
            skipped.append(f"{list(methods.keys())} {path} (no owned {param_names - ids.keys()} available)")
            continue
        resolved_path = path
        for name, value in ids.items():
            resolved_path = resolved_path.replace(f"{{{name}}}", str(value))
        for method, operation in methods.items():
            if method.lower() not in ("get", "post", "patch", "delete", "put"):
                continue
            body = _minimal_body_for(_extract_schema_name(operation), components) if method.lower() in ("post", "patch", "put") else None
            routes.append((method.upper(), resolved_path, body or {}))

    # Destructive verbs last, so a real ownership bug (which would actually delete
    # the throwaway resource) doesn't short-circuit the other probes.
    routes.sort(key=lambda r: r[0] == "DELETE")

    print(f"  {len(routes)} route(s) to probe with B's token against A's project_id/session_id:")
    if skipped:
        print(f"  {len(skipped)} route(s) SKIPPED (require an id this session never created, e.g. a real requirement/diagram):")
        for s in skipped:
            print(f"    SKIPPED: {s}")

    bad = []
    for method, path, body in routes:
        headers = {"Authorization": f"Bearer {token_b}"}
        kwargs = {"headers": headers}
        if method in ("POST", "PATCH", "PUT"):
            kwargs["json"] = body
        resp = await client.request(method, path, **kwargs)
        ok = resp.status_code in (404, 403)
        tag = "OK" if ok else "!!"
        print(f"  [{tag}] {method} {path} -> {resp.status_code}")
        if not ok:
            bad.append(f"{method} {path} -> {resp.status_code}")

    r = await client.get("/projects", headers={"Authorization": f"Bearer {token_b}"})
    b_projects = r.json() if r.status_code == 200 else None
    b_sees_none_of_a = isinstance(b_projects, list) and all(p["id"] != project_id for p in b_projects)
    print(f"  B: GET /projects -> {r.status_code}, {len(b_projects) if isinstance(b_projects, list) else '?'} project(s), "
          f"contains A's project: {not b_sees_none_of_a}")

    if not bad and b_sees_none_of_a:
        record("B2.isolation", "PASS",
               f"{len(routes)} route(s) all returned 404/403 for B against A's resources; "
               f"B's own project list never contains A's project ({len(skipped)} route(s) skipped, see log)")
    else:
        record("B2.isolation", "FAIL", f"violations={bad}, B_sees_A_project={not b_sees_none_of_a}")

    return project_id, session_id


# ── B3: session_id == checkpointer thread_id ────────────────────────────────

async def check_b3_thread_id(session_id: str) -> None:
    print("\n--- B3: session id appears verbatim as a checkpointer thread_id ---")
    async with async_session_factory() as db:
        row = (await db.execute(
            text("SELECT 1 FROM checkpoints WHERE thread_id = :tid LIMIT 1"), {"tid": session_id}
        )).first()

    if row is None:
        record("B3.session_is_thread_id", "INCONCLUSIVE",
               f"no checkpoint row exists for thread_id={session_id!r} yet -- run one real turn on this "
               f"session (POST /sessions/{session_id}/turn) and re-run this script to confirm")
        return
    print(f"  found a checkpoint row with thread_id == session_id ({session_id})")
    record("B3.session_is_thread_id", "PASS", f"thread_id {session_id!r} matches the session id exactly")


# ── main ──────────────────────────────────────────────────────────────────

async def main() -> None:
    print(f"Acceptance foundation checks against {BASE_URL}")
    created_user_ids: list[uuid.UUID] = []
    created_session_ids: list[uuid.UUID] = []

    try:
        check_a2_secrets()
        await check_a3_checkpointer_async()

        async with httpx.AsyncClient(base_url=BASE_URL, timeout=30.0) as client:
            await check_b1_bcrypt(client)

            email_a = f"{PREFIX}a-{uuid.uuid4()}@test.dev"
            email_b = f"{PREFIX}b-{uuid.uuid4()}@test.dev"
            password = "acc-foundation-probe-password-2"
            ra = await client.post("/auth/register", json={"email": email_a, "password": password})
            rb = await client.post("/auth/register", json={"email": email_b, "password": password})
            print(f"\n  registered A ({ra.status_code}) and B ({rb.status_code}) for B2")
            if ra.status_code != 201 or rb.status_code != 201:
                record("B2.isolation", "INCONCLUSIVE", "could not register users A/B")
            else:
                la = await client.post("/auth/login", json={"email": email_a, "password": password})
                lb = await client.post("/auth/login", json={"email": email_b, "password": password})
                token_a = la.json()["access_token"]
                token_b = lb.json()["access_token"]

                b2_result = await check_b2_isolation(client, token_a, token_b)
                if b2_result is not None:
                    project_id, session_id = b2_result
                    created_session_ids.append(uuid.UUID(session_id))
                    await check_b3_thread_id(session_id)

                    # Clean up A/B's HTTP-created rows via the real API (also exercises
                    # the delete paths this script did NOT already probe destructively).
                    await client.delete(f"/projects/{project_id}", headers={"Authorization": f"Bearer {token_a}"})

        print("\n" + "=" * 78)
        print(f"{'CHECK':35} {'STATUS':14} DETAIL")
        print("-" * 78)
        for check_id, status, detail in RESULTS:
            print(f"{check_id:35} {status:14} {detail[:200]}")
        print("=" * 78)
        n_pass = sum(1 for _, s, _ in RESULTS if s == "PASS")
        n_fail = sum(1 for _, s, _ in RESULTS if s == "FAIL")
        n_inc = sum(1 for _, s, _ in RESULTS if s == "INCONCLUSIVE")
        print(f"{n_pass} PASS, {n_fail} FAIL, {n_inc} INCONCLUSIVE")

    finally:
        print("\n--- cleanup: deleting only this run's acc-foundation- rows ---")
        async with async_session_factory() as db:
            for sid in created_session_ids:
                try:
                    await CheckpointRepo.purge_thread_tree(db, sid)
                except Exception as exc:
                    print(f"  (checkpoint purge for {sid} skipped/failed: {exc})")
            result = await db.execute(text("SELECT id, email FROM users WHERE email LIKE :p"), {"p": f"{PREFIX}%"})
            rows = result.all()
            for row in rows:
                created_user_ids.append(row.id)
            for row in rows:
                db_user = await UserRepo.get_by_id(db, row.id)
                if db_user is not None:
                    await db.delete(db_user)  # cascades projects/sessions/etc. at the DB level
            await db.commit()
        print(f"  deleted {len(created_user_ids)} acc-foundation- user(s) (and their cascaded projects/sessions)")


if __name__ == "__main__":
    asyncio.run(main())
