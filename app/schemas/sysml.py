from enum import Enum

from pydantic import BaseModel, Field

from data.models import DiagramType, RequirementLevel


class Intent(str, Enum):
    generate_requirement = "generate_requirement"
    modify_requirement = "modify_requirement"
    generate_diagram = "generate_diagram"
    modify_diagram = "modify_diagram"
    apply_published_delta = "apply_published_delta"
    conversation = "conversation"
    no_action = "no_action"


class IntentDecision(BaseModel):
    intent: Intent = Field(description="The single next action the graph should take.")
    level: RequirementLevel | None = Field(
        default=None,
        description="operational|functional|physical — only when explicit or clearly implied.",
    )
    diagram_type: DiagramType | None = Field(
        default=None, description="Only set when intent concerns a diagram."
    )
    message: str | None = Field(
        default=None,
        description="Clarifying message to show the user when intent is no_action or conversation.",
    )
