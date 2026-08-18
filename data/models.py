import enum
import uuid
from datetime import datetime

from sqlalchemy import Enum, ForeignKey, Text, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class RequirementLevel(str, enum.Enum):
    operational = "operational"
    functional = "functional"
    physical = "physical"


class VersionStatus(str, enum.Enum):
    pending = "pending"
    active = "active"
    superseded = "superseded"
    rejected = "rejected"


class DiagramType(str, enum.Enum):
    use_case = "use_case"
    state_machine = "state_machine"
    sequence = "sequence"


class FileKind(str, enum.Enum):
    upload = "upload"
    generated = "generated"


class FileOrigin(str, enum.Enum):
    user = "user"
    sysml = "sysml"
    adas = "adas"


class User(Base):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    email: Mapped[str] = mapped_column(unique=True, index=True)
    password_hash: Mapped[str]
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())

    projects: Mapped[list["Project"]] = relationship(back_populates="user", cascade="all, delete-orphan")


class Project(Base):
    __tablename__ = "projects"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    name: Mapped[str]
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())

    user: Mapped["User"] = relationship(back_populates="projects")
    sessions: Mapped[list["Session"]] = relationship(back_populates="project", cascade="all, delete-orphan")


class Session(Base):
    __tablename__ = "sessions"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    project_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"))
    # session.id doubles as the LangGraph checkpointer thread_id (session_id = thread_id)
    thread_id: Mapped[str] = mapped_column(unique=True, index=True)
    title: Mapped[str | None] = mapped_column(default=None)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())

    project: Mapped["Project"] = relationship(back_populates="sessions")
    requirements: Mapped[list["Requirement"]] = relationship(
        back_populates="session", cascade="all, delete-orphan"
    )
    diagrams: Mapped[list["Diagram"]] = relationship(
        back_populates="session", cascade="all, delete-orphan"
    )
    published_requirements: Mapped[list["PublishedRequirement"]] = relationship(
        back_populates="session", cascade="all, delete-orphan"
    )
    files: Mapped[list["File"]] = relationship(back_populates="session", cascade="all, delete-orphan")


class Requirement(Base):
    __tablename__ = "requirements"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    session_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("sessions.id", ondelete="CASCADE"))
    content: Mapped[str] = mapped_column(Text)
    level: Mapped[RequirementLevel] = mapped_column(
        Enum(RequirementLevel, name="requirement_level"), default=RequirementLevel.functional
    )
    status: Mapped[VersionStatus] = mapped_column(
        Enum(VersionStatus, name="version_status"), default=VersionStatus.pending
    )
    version: Mapped[int] = mapped_column(default=1)
    parent_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("requirements.id", ondelete="SET NULL"), default=None
    )
    # Identity of the version lineage: a new requirement's root_id is its own id;
    # a new version's root_id is inherited from its parent. promote() only supersedes
    # other ACTIVE rows sharing the same root_id, so distinct requirements (different
    # lineages) can stay active together in the same session.
    root_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("requirements.id", ondelete="SET NULL"), index=True
    )
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())

    session: Mapped["Session"] = relationship(back_populates="requirements")
    parent: Mapped["Requirement | None"] = relationship(remote_side=[id], foreign_keys=[parent_id])
    diagrams: Mapped[list["Diagram"]] = relationship(back_populates="requirement")


class Diagram(Base):
    __tablename__ = "diagrams"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    session_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("sessions.id", ondelete="CASCADE"))
    requirement_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("requirements.id", ondelete="CASCADE"))
    type: Mapped[DiagramType] = mapped_column(Enum(DiagramType, name="diagram_type"))
    plantuml: Mapped[str] = mapped_column(Text)
    rendered_path: Mapped[str | None] = mapped_column(Text, default=None)
    status: Mapped[VersionStatus] = mapped_column(
        Enum(VersionStatus, name="version_status"), default=VersionStatus.pending
    )
    version: Mapped[int] = mapped_column(default=1)
    parent_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("diagrams.id", ondelete="SET NULL"), default=None
    )
    # Same lineage-identity scheme as Requirement.root_id — lets promote() supersede
    # only prior versions of THIS diagram, not other diagram types/lineages on the
    # same requirement.
    root_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("diagrams.id", ondelete="SET NULL"), index=True
    )
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())

    session: Mapped["Session"] = relationship(back_populates="diagrams")
    requirement: Mapped["Requirement"] = relationship(back_populates="diagrams")
    parent: Mapped["Diagram | None"] = relationship(remote_side=[id], foreign_keys=[parent_id])


class PublishedRequirement(Base):
    __tablename__ = "published_requirements"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    session_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("sessions.id", ondelete="CASCADE"))
    content: Mapped[str] = mapped_column(Text)
    level: Mapped[RequirementLevel] = mapped_column(Enum(RequirementLevel, name="requirement_level"))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())

    session: Mapped["Session"] = relationship(back_populates="published_requirements")


class File(Base):
    __tablename__ = "files"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    session_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("sessions.id", ondelete="CASCADE"))
    kind: Mapped[FileKind] = mapped_column(Enum(FileKind, name="file_kind"))
    origin: Mapped[FileOrigin] = mapped_column(Enum(FileOrigin, name="file_origin"))
    file_type: Mapped[str]
    # reference into object storage (T1's storage layer) — never the raw bytes
    storage_key: Mapped[str] = mapped_column(Text)
    linked_to_type: Mapped[str | None] = mapped_column(default=None)
    linked_to_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), default=None)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())

    session: Mapped["Session"] = relationship(back_populates="files")
