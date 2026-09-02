from pydantic import BaseModel, Field


class TurnRequest(BaseModel):
    message: str = Field(min_length=1)
