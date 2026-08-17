"""Smoke test for T2: versioning + promotion logic in the repository layer.

Run manually against the local Postgres: python scripts/smoke_test_t2.py
"""
import asyncio
import uuid

from data.db import async_session_factory
from data.models import VersionStatus
from data.repository import ProjectRepo, RequirementRepo, SessionRepo, UserRepo


async def main() -> None:
    async with async_session_factory() as db:
        user = await UserRepo.create(db, email=f"smoke-{uuid.uuid4()}@test.dev", password_hash="hashed")
        project = await ProjectRepo.create(db, user_id=user.id, name="Smoke Project")
        session = await SessionRepo.create(
            db, project_id=project.id, thread_id=str(uuid.uuid4()), title="Smoke Session"
        )

        v1 = await RequirementRepo.create(db, session_id=session.id, content="The system shall v1")
        assert v1.status == VersionStatus.pending
        assert v1.version == 1
        print(f"v1 created: id={v1.id} status={v1.status} version={v1.version}")

        v1 = await RequirementRepo.promote(db, id=v1.id, session_id=session.id)
        assert v1.status == VersionStatus.active
        print(f"v1 promoted: status={v1.status}")

        v2 = await RequirementRepo.supersede_and_create_version(
            db, old_id=v1.id, new_content="The system shall v2", session_id=session.id
        )
        assert v2.version == 2
        assert v2.parent_id == v1.id
        assert v2.status == VersionStatus.pending
        print(f"v2 created: id={v2.id} status={v2.status} version={v2.version} parent_id={v2.parent_id}")

        v1_reloaded = await RequirementRepo.get_by_id(db, id=v1.id, session_id=session.id)
        assert v1_reloaded.status == VersionStatus.superseded
        print(f"v1 after supersede: status={v1_reloaded.status}")

        v2 = await RequirementRepo.promote(db, id=v2.id, session_id=session.id)
        assert v2.status == VersionStatus.active
        print(f"v2 promoted: status={v2.status}")

        v1_reloaded = await RequirementRepo.get_by_id(db, id=v1.id, session_id=session.id)
        assert v1_reloaded.status == VersionStatus.superseded
        print(f"v1 remains: status={v1_reloaded.status}")

        active = await RequirementRepo.get_active_for_session(db, session_id=session.id)
        assert active.id == v2.id
        print(f"single active requirement for session: id={active.id} (== v2: {active.id == v2.id})")

        await db.rollback()  # smoke test data, don't persist

    print("\n=== SMOKE TEST PASSED ===")
    print(f"v1: version=1 status=superseded parent_id=None -> OK")
    print(f"v2: version=2 status=active parent_id={v1.id} -> OK")


if __name__ == "__main__":
    asyncio.run(main())
