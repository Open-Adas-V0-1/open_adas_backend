from enum import Enum
from typing import Literal

from pydantic import BaseModel, Field

from data.models import RequirementLevel


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


class TodoStatus(str, Enum):
    pending = "pending"
    in_progress = "in_progress"
    done = "done"


class TodoItem(BaseModel):
    """One entry in plan_state.tasks -- INTERNAL state data, not an external tool.
    intent is deliberately restricted to the two concrete SysML actions Layer-2/3
    execute (mirrors ProcessingInput.intent's same restriction) -- "modify" folds into
    "generate" the same way it does at the Layer-2/3 boundary.
    """

    id: str
    description: str
    intent: Literal["generate_requirement", "generate_diagram"]
    level: RequirementLevel | None = None
    # 1-based id of another task in the SAME plan this one depends on (e.g. a diagram
    # task depends on the requirement task it represents) -- None if independent. The
    # dependent task always appears LATER in `tasks` than what it depends on.
    depends_on: str | None = None
    status: TodoStatus = TodoStatus.pending
    # LIGHT reference only, filled in by Step 3's execution loop after this task runs
    # -- never full artifact content.
    result_ref: dict | None = None


class PlanState(BaseModel):
    """The TODO list plan_node builds for a needs_execution request. Internal
    orchestration data held in SupervisorState.plan_state -- never an external tool.
    """

    tasks: list[TodoItem]
    original_request: str


class PlannedTask(BaseModel):
    """One task as decided by the LLM during decomposition (plan_node) -- deliberately
    narrower than TodoItem: no id/status/result_ref, since those are the orchestrator's
    own bookkeeping, not something the LLM should invent.
    """

    description: str
    intent: Literal["generate_requirement", "generate_diagram"]
    level: RequirementLevel | None = Field(
        default=None,
        description="operational|functional|physical, ONLY when explicit or clearly implied. Leave unset to let it be derived later -- never guess.",
    )
    depends_on_task_number: int | None = Field(
        default=None,
        description=(
            "1-based position, WITHIN this same tasks list, of the task this one "
            "depends on (e.g. a diagram task's number for the requirement it "
            "represents). That task must appear EARLIER in the list. None if independent."
        ),
    )


class PlanDecision(BaseModel):
    """plan_node's structured decomposition decision: either an ordered task list, or
    a fail-open signal that the request is too vague to decompose (routes to a clarify
    interrupt rather than fabricating tasks).
    """

    sufficient: bool = Field(
        description="False if the request is too vague/ambiguous to decompose into concrete tasks."
    )
    tasks: list[PlannedTask] = Field(
        default_factory=list, description="Ordered tasks; empty when sufficient=False."
    )
    clarifying_message: str | None = Field(
        default=None, description="Set only when sufficient=False: what's missing, asked to the user."
    )
