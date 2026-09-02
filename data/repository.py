import uuid
from datetime import datetime, timezone

from sqlalchemy import select, text, update
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
    ThreadActivity,
    User,
    VersionStatus,
)

# Mirrors agents/sysml/middle_nodes.py's OWN _SOURCE_LEVEL_FOR (the forward-only
# Op->Func->Phys derivation chain) -- duplicated here rather than imported, since the
# repository layer must not depend on the graph layer. Used ONLY by
# RequirementRepo.find_likely_derivation_source's read-only heuristic (T6b Step 5).
_SOURCE_LEVEL_FOR = {"functional": "operational", "physical": "functional"}


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
    async def create(db: AsyncSession, user_id: uuid.UUID, name: str, description: str | None = None) -> Project:
        project = Project(user_id=user_id, name=name, description=description)
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

    @staticmethod
    async def delete(db: AsyncSession, project: Project) -> None:
        """Deletes the project row. requirements/diagrams/files/published_requirements
        of each of its sessions cascade at the DB level (ondelete=CASCADE all the way
        down: projects -> sessions -> {requirements, diagrams, files, ...}). Does NOT
        purge LangGraph checkpoints -- those have no FK to sessions at all, so the
        caller must purge each session's checkpoint tree (CheckpointRepo) BEFORE
        calling this, one session at a time (see app/api/routes/projects.py).
        """
        await db.delete(project)
        await db.flush()


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
    async def get_by_id(db: AsyncSession, id: uuid.UUID) -> Session | None:
        return await db.get(Session, id)

    @staticmethod
    async def get_by_thread_id(db: AsyncSession, thread_id: str) -> Session | None:
        result = await db.execute(select(Session).where(Session.thread_id == thread_id))
        return result.scalar_one_or_none()

    @staticmethod
    async def list_by_project(db: AsyncSession, project_id: uuid.UUID) -> list[Session]:
        """Most recently updated first (updated_at bumps on rename -- see rename())."""
        result = await db.execute(
            select(Session).where(Session.project_id == project_id).order_by(Session.updated_at.desc())
        )
        return list(result.scalars().all())

    @staticmethod
    async def rename(db: AsyncSession, session: Session, title: str) -> Session:
        """Sets the new title; updated_at bumps automatically (onupdate=func.now()
        on the model, server-side) since this always emits an UPDATE for the row.
        """
        session.title = title
        await db.flush()
        await db.refresh(session)
        return session

    @staticmethod
    async def delete(db: AsyncSession, session: Session) -> None:
        """Deletes the session row. requirements/diagrams/files/published_requirements
        cascade at the DB level (ondelete=CASCADE). Does NOT purge LangGraph
        checkpoints -- callers must call CheckpointRepo.purge_thread_tree(db,
        session.id) first, in the SAME transaction (see app/api/routes/projects.py).
        """
        await db.delete(session)
        await db.flush()


class CheckpointRepo:
    """Raw SQL against the LangGraph-owned checkpointer tables (checkpoints,
    checkpoint_blobs, checkpoint_writes) -- these are NOT mapped by our ORM models
    (LangGraph's AsyncPostgresSaver owns and creates them), so this is the one place
    in the repository layer that uses text() instead of the query builder.
    """

    _TABLES = ("checkpoints", "checkpoint_blobs", "checkpoint_writes")

    @staticmethod
    async def purge_thread_tree(db: AsyncSession, session_id: uuid.UUID) -> None:
        """Deletes every checkpoint row, across all three tables, for the session's
        OWN thread_id (str(session_id) -- session.id doubles as the outer Layer-1
        thread_id) AND every thread_id DERIVED from it via the ':' prefix scheme
        (f"{session_id}:middle:...", f"{session_id}:proc:...") -- Layer-2/Layer-3
        sub-threads have NO foreign key to sessions, so a plain FK cascade can never
        reach them; this is the only way to actually purge them.

        Caller is responsible for committing in the same transaction as the session/
        project row delete (no commit here -- this only flushes via execute).
        """
        sid = str(session_id)
        prefix = f"{sid}:%"
        for table in CheckpointRepo._TABLES:
            await db.execute(
                text(f"DELETE FROM {table} WHERE thread_id = :sid OR thread_id LIKE :prefix"),
                {"sid": sid, "prefix": prefix},
            )


class RequirementRepo:
    @staticmethod
    async def create(
        db: AsyncSession,
        session_id: uuid.UUID,
        content: str,
        level: RequirementLevel = RequirementLevel.functional,
        source_published_requirement_id: uuid.UUID | None = None,
        metadata: dict | None = None,
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
            source_published_requirement_id=source_published_requirement_id,
            metadata_=metadata,
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

    @staticmethod
    async def finalize(
        db: AsyncSession,
        session_id: uuid.UUID,
        content: str,
        level: RequirementLevel,
        metadata: dict | None = None,
        source_published_requirement_id: uuid.UUID | None = None,
    ) -> Requirement:
        """Persist an APPROVED requirement keyed by (session_id == thread_id, level).
        Deliberately NOT the promote()/active-supersedes-active dance: levels accumulate
        rather than superseding each other, so this always inserts a fresh row, directly
        active, with no sibling sweep.
        """
        new_id = uuid.uuid4()
        requirement = Requirement(
            id=new_id,
            session_id=session_id,
            content=content,
            level=level,
            status=VersionStatus.active,
            version=1,
            root_id=new_id,
            source_published_requirement_id=source_published_requirement_id,
            metadata_=metadata,
        )
        db.add(requirement)
        await db.flush()
        return requirement

    @staticmethod
    async def list_by_session_and_level(
        db: AsyncSession, session_id: uuid.UUID, level: RequirementLevel
    ) -> list[Requirement]:
        """Active requirements at a given level in this thread (session_id == thread_id),
        most recent first — used by resolve_level to find the SOURCE for a derivation
        (e.g. the operational requirement(s) a functional one can derive from).
        """
        result = await db.execute(
            select(Requirement)
            .where(
                Requirement.session_id == session_id,
                Requirement.level == level,
                Requirement.status == VersionStatus.active,
            )
            .order_by(Requirement.created_at.desc())
        )
        return list(result.scalars().all())

    @staticmethod
    async def level_progress(db: AsyncSession, session_id: uuid.UUID) -> list[str]:
        """Which levels have at least one APPROVED (active) requirement in this thread —
        the forward-only Op->Func->Phys progress snapshot resolve_level reads.
        """
        result = await db.execute(
            select(Requirement.level)
            .where(Requirement.session_id == session_id, Requirement.status == VersionStatus.active)
            .distinct()
        )
        return sorted({level.value for level in result.scalars().all()})

    # ── T6b Step 5: read API support ──────────────────────────────────────────

    @staticmethod
    async def get_by_id_any_session(db: AsyncSession, id: uuid.UUID) -> Requirement | None:
        """NOT scoped to a session -- used ONLY by the ownership-resolving dependency
        (app/api/deps.py's get_owned_requirement), which must look the row up FIRST to
        discover which session it belongs to, before it can even check ownership.
        Every other call site keeps using the session-scoped get_by_id.
        """
        return await db.get(Requirement, id)

    @staticmethod
    async def list_by_session_filtered(
        db: AsyncSession,
        session_id: uuid.UUID,
        active_only: bool = True,
        level: RequirementLevel | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[Requirement]:
        stmt = select(Requirement).where(Requirement.session_id == session_id)
        if active_only:
            stmt = stmt.where(Requirement.status == VersionStatus.active)
        if level is not None:
            stmt = stmt.where(Requirement.level == level)
        stmt = stmt.order_by(Requirement.created_at.desc()).limit(limit).offset(offset)
        result = await db.execute(stmt)
        return list(result.scalars().all())

    @staticmethod
    async def list_versions_by_root(db: AsyncSession, root_id: uuid.UUID, session_id: uuid.UUID) -> list[Requirement]:
        """The FULL lineage sharing this root_id, oldest version first -- how the UI
        shows a requirement's history. Includes rejected/pending rows too (a complete
        history, not just active/superseded) -- callers that only want the two real
        version states can filter status != rejected/pending themselves if needed.
        """
        result = await db.execute(
            select(Requirement)
            .where(Requirement.session_id == session_id, Requirement.root_id == root_id)
            .order_by(Requirement.version.asc())
        )
        return list(result.scalars().all())

    @staticmethod
    async def find_likely_derivation_source(db: AsyncSession, requirement: Requirement) -> Requirement | None:
        """BEST-EFFORT, read-only heuristic for 'which requirement was this one derived
        FROM' (forward-only level derivation -- operational -> functional -> physical),
        DISTINCT from parent_id (which tracks VERSION lineage, v1 -> v2 of the SAME
        requirement, a real stored FK). No column or metadata field in this schema
        captures the level-derivation link today (resolve_level's own resolved_source_id
        is ephemeral MiddleState, never persisted onto the finalized row) -- adding one
        would require a graph/node change, out of scope for this read-only step. This
        mirrors resolve_level's OWN deterministic rule instead: the single active
        requirement one level down, in the same session, if unambiguous. Returns None
        for operational (top of chain, no source) or when the candidate set isn't
        exactly one (ambiguous or missing) -- fails safe, never guesses.
        """
        source_level = _SOURCE_LEVEL_FOR.get(requirement.level.value)
        if source_level is None:
            return None
        candidates = await RequirementRepo.list_by_session_and_level(
            db, session_id=requirement.session_id, level=RequirementLevel(source_level)
        )
        return candidates[0] if len(candidates) == 1 else None


class DiagramRepo:
    @staticmethod
    async def create(
        db: AsyncSession,
        session_id: uuid.UUID,
        requirement_id: uuid.UUID,
        type: DiagramType,
        sysml_text: str,
        mermaid: str | None = None,
        rendered_path: str | None = None,
        metadata: dict | None = None,
    ) -> Diagram:
        new_id = uuid.uuid4()
        diagram = Diagram(
            id=new_id,
            session_id=session_id,
            requirement_id=requirement_id,
            type=type,
            sysml_text=sysml_text,
            mermaid=mermaid,
            rendered_path=rendered_path,
            status=VersionStatus.pending,
            version=1,
            root_id=new_id,  # a fresh diagram is the root of its own lineage
            metadata_=metadata,
        )
        db.add(diagram)
        await db.flush()
        return diagram

    @staticmethod
    async def finalize(
        db: AsyncSession,
        session_id: uuid.UUID,
        requirement_id: uuid.UUID,
        type: DiagramType,
        sysml_text: str,
        mermaid: str | None,
        metadata: dict | None = None,
    ) -> Diagram:
        """Persist an APPROVED diagram keyed by (session_id == thread_id, level via its
        linked requirement). Same no-supersede contract as RequirementRepo.finalize.
        """
        new_id = uuid.uuid4()
        diagram = Diagram(
            id=new_id,
            session_id=session_id,
            requirement_id=requirement_id,
            type=type,
            sysml_text=sysml_text,
            mermaid=mermaid,
            status=VersionStatus.active,
            version=1,
            root_id=new_id,
            metadata_=metadata,
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
        db: AsyncSession, old_id: uuid.UUID, new_sysml_text: str, session_id: uuid.UUID
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
            sysml_text=new_sysml_text,
            status=VersionStatus.pending,
            version=old.version + 1,
            parent_id=old.id,
            root_id=old.root_id,  # same lineage as the row it supersedes
        )
        db.add(new)
        await db.flush()
        return new

    # ── T6b Step 5: read API support ──────────────────────────────────────────

    @staticmethod
    async def get_by_id_any_session(db: AsyncSession, id: uuid.UUID) -> Diagram | None:
        """NOT scoped to a session -- used ONLY by the ownership-resolving dependency
        (app/api/deps.py's get_owned_diagram); see RequirementRepo's twin method.
        """
        return await db.get(Diagram, id)

    @staticmethod
    async def list_active_for_session(db: AsyncSession, session_id: uuid.UUID) -> list[Diagram]:
        result = await db.execute(
            select(Diagram).where(Diagram.session_id == session_id, Diagram.status == VersionStatus.active)
        )
        return list(result.scalars().all())

    @staticmethod
    async def list_by_session_filtered(
        db: AsyncSession,
        session_id: uuid.UUID,
        active_only: bool = True,
        limit: int = 50,
        offset: int = 0,
    ) -> list[Diagram]:
        stmt = select(Diagram).where(Diagram.session_id == session_id)
        if active_only:
            stmt = stmt.where(Diagram.status == VersionStatus.active)
        stmt = stmt.order_by(Diagram.created_at.desc()).limit(limit).offset(offset)
        result = await db.execute(stmt)
        return list(result.scalars().all())


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


class ThreadActivityRepo:
    """Tracks last_accessed for CHECKPOINTER thread_ids only — never touches
    requirements/diagrams. See harness/thread_ttl.py for the TTL policy built on top.
    """

    @staticmethod
    async def touch(db: AsyncSession, thread_id: str, session_id: uuid.UUID) -> ThreadActivity:
        # naive UTC, matching every other timestamp column in this schema (none use
        # timezone=True) — harness/thread_ttl.py treats naive values as UTC on read.
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        existing = await db.get(ThreadActivity, thread_id)
        if existing is None:
            existing = ThreadActivity(thread_id=thread_id, session_id=session_id, last_accessed=now)
            db.add(existing)
        else:
            existing.last_accessed = now
        await db.flush()
        return existing

    @staticmethod
    async def get_last_accessed(db: AsyncSession, thread_id: str):
        row = await db.get(ThreadActivity, thread_id)
        return row.last_accessed if row else None
