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
