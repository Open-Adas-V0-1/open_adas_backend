from enum import Enum

from pydantic import BaseModel, Field


class AgentTarget(str, Enum):
    sysml = "sysml"
    # adas, sql_qa are dispatched here once those agents exist


class TopDecision(BaseModel):
    """The top-level PLANNER's orchestrator decision for one visit -- used by the
    conditional planning path (Step 2+), not by the hub's first-pass classification.
    """

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


class HubClassification(str, Enum):
    """The top-level HUB's first-pass classification of a user message -- decides the
    PATH (answer directly / dispatch for real work / ask for clarification), not the
    work itself.
    """

    simple_response = "simple_response"
    needs_execution = "needs_execution"
    unclear = "unclear"


class HubDecision(BaseModel):
    """Structured output for top_level_supervisor's hub classification (Layer-1
    rebuild, Step 1). No planning happens here -- this only decides whether the hub can
    answer directly, must route to real work (planning/delegation, built in Steps 2-3),
    or needs to ask the user to clarify.
    """

    classification: HubClassification = Field(
        description="simple_response | needs_execution | unclear -- see prompt protocol."
    )
    response: str | None = Field(
        default=None,
        description=(
            "For simple_response: the direct answer to give the user. For unclear: the "
            "clarifying question to ask. Left unset for needs_execution."
        ),
    )
