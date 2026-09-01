from typing import Literal

from pydantic import BaseModel, Field


class RequirementOption(BaseModel):
    id: str
    summary: str


class SelectRequirementPattern(BaseModel):
    """Fixed structure the frontend renders as a list to pick from. `question` is the
    only LLM-phrased part; `options` are filled from the repository's active requirements.
    """

    pattern: Literal["select_requirement"] = "select_requirement"
    question: str
    options: list[RequirementOption]


class ConfirmDiagramTypePattern(BaseModel):
    """Fixed structure the frontend renders as buttons. `options` is always the same
    fixed list of diagram types — never LLM-generated.
    """

    pattern: Literal["confirm_diagram_type"] = "confirm_diagram_type"
    question: str
    options: list[str] = Field(default_factory=lambda: ["use_case", "state_machine", "sequence"])


class ConfirmActionPattern(BaseModel):
    """Fixed yes/no structure the frontend renders as two buttons."""

    pattern: Literal["confirm_action"] = "confirm_action"
    question: str
    options: list[str] = Field(default_factory=lambda: ["yes", "no"])


class SelectRequirementsForDiagramPattern(BaseModel):
    """Fixed multi-select (checkbox) structure the frontend renders for choosing WHICH
    requirements a diagram should represent. Triggered only when the user asked for a
    diagram, didn't name the target(s), and there's more than one candidate requirement
    in context — an explicit name, or exactly one candidate, skips this entirely.
    """

    pattern: Literal["select_requirements_for_diagram"] = "select_requirements_for_diagram"
    question: str
    options: list[RequirementOption]
    multi_select: bool = True
    min_selected: int = 1
    allow_all: bool = True


class ClarifyRequestPattern(BaseModel):
    """Open clarification ask — no fixed options; the frontend renders a free-text
    input rather than buttons. Used when validate_inputs finds the request isn't
    processable at all (unrecognized intent, broken context), where a yes/no or a
    pick-one-from-a-list doesn't fit. The user's reply is expected via the existing
    'modify' resume action (a rephrased user_input), which loops back to
    middle_supervisor for re-classification.
    """

    pattern: Literal["clarify_request"] = "clarify_request"
    question: str
