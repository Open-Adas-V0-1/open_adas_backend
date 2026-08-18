import uuid

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from data.models import (
    Diagram,
    DiagramType,
    File,
    FileKind,
    FileOrigin,
    Project,
    PublishedRequirement,
    Requirement,
    RequirementLevel,
    Session,
    User,
    VersionStatus,
)


class UserRepo:
    @staticmethod
    async def create(db: AsyncSession, email: str, password_hash: str) -> User:
        user = User(email=email, password_hash=password_hash)
        db.add(user)
        await db.flush()
        return user

    @staticmethod
    async def get_by_email(db: AsyncSession, email: str) -> User | None:
        result = await db.execute(select(User).where(User.email == email))
        return result.scalar_one_or_none()

    @staticmethod
    async def get_by_id(db: AsyncSession, id: uuid.UUID) -> User | None:
        result = await db.execute(select(User).where(User.id == id))
        return result.scalar_one_or_none()


class ProjectRepo:
    @staticmethod
    async def create(db: AsyncSession, user_id: uuid.UUID, name: str) -> Project:
        project = Project(user_id=user_id, name=name)
        db.add(project)
        await db.flush()
        return project

    @staticmethod
    async def list_by_user(db: AsyncSession, user_id: uuid.UUID) -> list[Project]:
        result = await db.execute(select(Project).where(Project.user_id == user_id))
        return list(result.scalars().all())

    @staticmethod
    async def get_by_id(db: AsyncSession, id: uuid.UUID, user_id: uuid.UUID) -> Project | None:
        result = await db.execute(
            select(Project).where(Project.id == id, Project.user_id == user_id)
        )
        return result.scalar_one_or_none()


class SessionRepo:
    @staticmethod
    async def create(
        db: AsyncSession, project_id: uuid.UUID, thread_id: str, title: str | None = None
    ) -> Session:
        session = Session(project_id=project_id, thread_id=thread_id, title=title)
        db.add(session)
        await db.flush()
        return session

    @staticmethod
    async def get_by_thread_id(db: AsyncSession, thread_id: str) -> Session | None:
        result = await db.execute(select(Session).where(Session.thread_id == thread_id))
        return result.scalar_one_or_none()

    @staticmethod
    async def list_by_project(db: AsyncSession, project_id: uuid.UUID) -> list[Session]:
        result = await db.execute(select(Session).where(Session.project_id == project_id))
        return list(result.scalars().all())


class RequirementRepo:
    @staticmethod
    async def create(
        db: AsyncSession,
        session_id: uuid.UUID,
        content: str,
        level: RequirementLevel = RequirementLevel.functional,
    ) -> Requirement:
        new_id = uuid.uuid4()
        requirement = Requirement(
            id=new_id,
            session_id=session_id,
            content=content,
            level=level,
            status=VersionStatus.pending,
            version=1,
            root_id=new_id,  # a fresh requirement is the root of its own lineage
        )
        db.add(requirement)
        await db.flush()
        return requirement

    @staticmethod
    async def get_by_id(
        db: AsyncSession, id: uuid.UUID, session_id: uuid.UUID
    ) -> Requirement | None:
        result = await db.execute(
            select(Requirement).where(Requirement.id == id, Requirement.session_id == session_id)
        )
        return result.scalar_one_or_none()

    @staticmethod
    async def list_active_for_session(db: AsyncSession, session_id: uuid.UUID) -> list[Requirement]:
        """All active requirements in the session — one per distinct lineage."""
        result = await db.execute(
            select(Requirement).where(
                Requirement.session_id == session_id,
                Requirement.status == VersionStatus.active,
            )
        )
        return list(result.scalars().all())

    @staticmethod
    async def supersede_and_create_version(
        db: AsyncSession, old_id: uuid.UUID, new_content: str, session_id: uuid.UUID
    ) -> Requirement:
        old = await RequirementRepo.get_by_id(db, old_id, session_id)
        if old is None:
            raise ValueError(f"Requirement {old_id} not found in session {session_id}")

        old.status = VersionStatus.superseded

        new = Requirement(
            session_id=session_id,
            content=new_content,
            level=old.level,
            status=VersionStatus.pending,
            version=old.version + 1,
            parent_id=old.id,
            root_id=old.root_id,  # same lineage as the row it supersedes
        )
        db.add(new)
        await db.flush()
        return new

    @staticmethod
    async def promote(db: AsyncSession, id: uuid.UUID, session_id: uuid.UUID) -> Requirement:
        target = await RequirementRepo.get_by_id(db, id, session_id)
        if target is None:
            raise ValueError(f"Requirement {id} not found in session {session_id}")

        # Supersede only other ACTIVE rows in the SAME lineage (root_id) — distinct
        # requirements (different lineages) stay active together.
        await db.execute(
            update(Requirement)
            .where(
                Requirement.session_id == session_id,
                Requirement.root_id == target.root_id,
                Requirement.status == VersionStatus.active,
            )
            .values(status=VersionStatus.superseded)
        )
        target.status = VersionStatus.active
        await db.flush()
        return target

    @staticmethod
    async def list_by_session(db: AsyncSession, session_id: uuid.UUID) -> list[Requirement]:
        result = await db.execute(
            select(Requirement).where(Requirement.session_id == session_id)
        )
        return list(result.scalars().all())


class DiagramRepo:
    @staticmethod
    async def create(
        db: AsyncSession,
        session_id: uuid.UUID,
        requirement_id: uuid.UUID,
        type: DiagramType,
        plantuml: str,
        rendered_path: str | None = None,
    ) -> Diagram:
        new_id = uuid.uuid4()
        diagram = Diagram(
            id=new_id,
            session_id=session_id,
            requirement_id=requirement_id,
            type=type,
            plantuml=plantuml,
            rendered_path=rendered_path,
            status=VersionStatus.pending,
            version=1,
            root_id=new_id,  # a fresh diagram is the root of its own lineage
        )
        db.add(diagram)
        await db.flush()
        return diagram

    @staticmethod
    async def get_by_requirement(
        db: AsyncSession, requirement_id: uuid.UUID, session_id: uuid.UUID
    ) -> list[Diagram]:
        result = await db.execute(
            select(Diagram).where(
                Diagram.requirement_id == requirement_id, Diagram.session_id == session_id
            )
        )
        return list(result.scalars().all())

    @staticmethod
    async def promote(db: AsyncSession, id: uuid.UUID, session_id: uuid.UUID) -> Diagram:
        target_result = await db.execute(
            select(Diagram).where(Diagram.id == id, Diagram.session_id == session_id)
        )
        target = target_result.scalar_one_or_none()
        if target is None:
            raise ValueError(f"Diagram {id} not found in session {session_id}")

        # Supersede only other ACTIVE rows in the SAME lineage (root_id) — a requirement
        # can have several distinct diagram types (or several diagrams) active together.
        await db.execute(
            update(Diagram)
            .where(
                Diagram.session_id == session_id,
                Diagram.root_id == target.root_id,
                Diagram.status == VersionStatus.active,
            )
            .values(status=VersionStatus.superseded)
        )
        target.status = VersionStatus.active
        await db.flush()
        return target

    @staticmethod
    async def supersede_and_create_version(
        db: AsyncSession, old_id: uuid.UUID, new_plantuml: str, session_id: uuid.UUID
    ) -> Diagram:
        result = await db.execute(
            select(Diagram).where(Diagram.id == old_id, Diagram.session_id == session_id)
        )
        old = result.scalar_one_or_none()
        if old is None:
            raise ValueError(f"Diagram {old_id} not found in session {session_id}")

        old.status = VersionStatus.superseded

        new = Diagram(
            session_id=session_id,
            requirement_id=old.requirement_id,
            type=old.type,
            plantuml=new_plantuml,
            status=VersionStatus.pending,
            version=old.version + 1,
            parent_id=old.id,
            root_id=old.root_id,  # same lineage as the row it supersedes
        )
        db.add(new)
        await db.flush()
        return new


class PublishedRequirementRepo:
    @staticmethod
    async def create(
        db: AsyncSession, session_id: uuid.UUID, content: str, level: RequirementLevel
    ) -> PublishedRequirement:
        published = PublishedRequirement(session_id=session_id, content=content, level=level)
        db.add(published)
        await db.flush()
        return published

    @staticmethod
    async def list_by_session(db: AsyncSession, session_id: uuid.UUID) -> list[PublishedRequirement]:
        result = await db.execute(
            select(PublishedRequirement).where(PublishedRequirement.session_id == session_id)
        )
        return list(result.scalars().all())

    @staticmethod
    async def get_by_id(
        db: AsyncSession, id: uuid.UUID, session_id: uuid.UUID
    ) -> PublishedRequirement | None:
        result = await db.execute(
            select(PublishedRequirement).where(
                PublishedRequirement.id == id, PublishedRequirement.session_id == session_id
            )
        )
        return result.scalar_one_or_none()


class FileRepo:
    @staticmethod
    async def create(
        db: AsyncSession,
        session_id: uuid.UUID,
        kind: FileKind,
        origin: FileOrigin,
        file_type: str,
        storage_key: str,
        linked_to_type: str | None = None,
        linked_to_id: uuid.UUID | None = None,
    ) -> File:
        file = File(
            session_id=session_id,
            kind=kind,
            origin=origin,
            file_type=file_type,
            storage_key=storage_key,
            linked_to_type=linked_to_type,
            linked_to_id=linked_to_id,
        )
        db.add(file)
        await db.flush()
        return file

    @staticmethod
    async def list_by_session(db: AsyncSession, session_id: uuid.UUID) -> list[File]:
        result = await db.execute(select(File).where(File.session_id == session_id))
        return list(result.scalars().all())
