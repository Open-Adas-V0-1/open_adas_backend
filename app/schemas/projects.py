import uuid
from datetime import datetime

from pydantic import BaseModel, Field


class ProjectCreate(BaseModel):
    name: str = Field(min_length=1)
    description: str | None = None


class ProjectOut(BaseModel):
    model_config = {"from_attributes": True}

    id: uuid.UUID
    user_id: uuid.UUID
    name: str
    description: str | None
    created_at: datetime


class SessionCreate(BaseModel):
    title: str | None = None


class SessionRename(BaseModel):
    title: str = Field(min_length=1)


class SessionOut(BaseModel):
    """The session `id` IS the identifier the client uses for chat (step 3) -- it
    doubles as the LangGraph thread_id. No separate field is exposed for it.
    """

    model_config = {"from_attributes": True}

    id: uuid.UUID
    project_id: uuid.UUID
    title: str | None
    created_at: datetime
    updated_at: datetime
