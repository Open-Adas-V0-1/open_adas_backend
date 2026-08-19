from enum import Enum

from pydantic import BaseModel, Field


class AgentTarget(str, Enum):
    sysml = "sysml"
    # adas, sql_qa are dispatched here once those agents exist


class TopDecision(BaseModel):
    """The top-level supervisor's planner/orchestrator decision for this visit."""

    active_agent: AgentTarget | None = Field(
        default=None, description="Which agent to dispatch to next, if any."
    )
    intent_complete: bool = Field(
        description="True if the user's overall request has already been fully satisfied."
    )
    message: str | None = Field(
        default=None,
        description="Clarifying message when there's nothing actionable to dispatch.",
    )
