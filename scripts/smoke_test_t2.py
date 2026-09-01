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

        active = await RequirementRepo.list_active_for_session(db, session_id=session.id)
        assert [r.id for r in active] == [v2.id]
        print(f"active requirements for session: {[r.id for r in active]} (== [v2]: True)")

        # --- lineage isolation: a second, DISTINCT requirement must be able to be
        # active at the same time, and promoting/superseding one lineage must never
        # touch the other's active row. ---
        other_v1 = await RequirementRepo.create(db, session_id=session.id, content="The system shall other-v1")
        other_v1 = await RequirementRepo.promote(db, id=other_v1.id, session_id=session.id)
        assert other_v1.status == VersionStatus.active
        assert other_v1.root_id == other_v1.id
        print(f"other_v1 created+promoted (distinct lineage): id={other_v1.id} status={other_v1.status}")

        v2_reloaded = await RequirementRepo.get_by_id(db, id=v2.id, session_id=session.id)
        assert v2_reloaded.status == VersionStatus.active, "promoting a distinct lineage must not affect v2"
        print(f"v2 (other lineage) still active: status={v2_reloaded.status}")

        active = await RequirementRepo.list_active_for_session(db, session_id=session.id)
        active_ids = {r.id for r in active}
        assert active_ids == {v2.id, other_v1.id}, f"expected both lineages active, got {active_ids}"
        print(f"both distinct lineages active together: {active_ids}")

        other_v2 = await RequirementRepo.supersede_and_create_version(
            db, old_id=other_v1.id, new_content="The system shall other-v2", session_id=session.id
        )
        assert other_v2.root_id == other_v1.root_id == other_v1.id
        other_v2 = await RequirementRepo.promote(db, id=other_v2.id, session_id=session.id)

        other_v1_reloaded = await RequirementRepo.get_by_id(db, id=other_v1.id, session_id=session.id)
        v2_reloaded = await RequirementRepo.get_by_id(db, id=v2.id, session_id=session.id)
        assert other_v1_reloaded.status == VersionStatus.superseded, "own lineage's v1 must be superseded"
        assert v2_reloaded.status == VersionStatus.active, "unrelated lineage must be untouched"
        print(
            f"other_v2 promoted: own v1 superseded ({other_v1_reloaded.status}), "
            f"unrelated v2 untouched ({v2_reloaded.status})"
        )

        await db.rollback()  # smoke test data, don't persist

    print("\n=== SMOKE TEST PASSED ===")
    print(f"v1: version=1 status=superseded parent_id=None -> OK")
    print(f"v2: version=2 status=active parent_id={v1.id} -> OK")


if __name__ == "__main__":
    asyncio.run(main())
