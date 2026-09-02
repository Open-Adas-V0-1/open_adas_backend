"""Per-pattern resume payload validation (T6b Step 4) -- the security-critical part
of the resume endpoint. The client's raw JSON body is NEVER forwarded to the graph;
it is validated against the Pydantic model for the pattern that is ACTUALLY pending
(read from the checkpointer, never trusted from the client), and only the resulting
SANITIZED dict is ever passed to Command(resume=...).

Every action/field-name pair here was read directly from the node source that
consumes it (supervisor/plan_review.py, agents/sysml/nodes.py's requirement_review,
agents/sysml/middle_nodes.py's user_confirm_inputs, supervisor/plan.py's plan_node) --
not guessed. `confirm_diagram_type` is included for completeness (it's a real,
documented pattern in app/schemas/confirmations.py) but no code path in the graph
today actually sets pending_pattern to it -- see this step's chat report.

`plan_clarify` is a real, resumable interrupt (supervisor/plan.py's plan_node) that
is NOT among the 7 patterns this step's task named, but omitting it would make that
interrupt permanently unresumable through this endpoint -- included for completeness,
flagged in the report.
"""
import uuid
from typing import Literal

from pydantic import BaseModel, ValidationError, model_validator


class ResumeValidationError(Exception):
    """Raised for any resume-payload problem: unknown pattern, a Pydantic validation
    failure, or a selected id that wasn't actually offered for this interrupt. Always
    maps to HTTP 422 at the route -- the graph is never invoked when this is raised.
    """

    def __init__(self, message: str):
        self.message = message
        super().__init__(message)


def _require_uuid(value: str, field: str) -> None:
    try:
        uuid.UUID(value)
    except (ValueError, AttributeError, TypeError):
        raise ValueError(f"{field} must be a valid UUID, got {value!r}")


class PlanReviewResume(BaseModel):
    """supervisor/plan_review.py's plan_review node."""

    action: Literal["approve", "modify", "cancel"]
    tasks: list[dict] | None = None  # PlannedTask-shaped; re-validated by plan_review itself

    @model_validator(mode="after")
    def _check(self):
        if self.action == "modify" and not self.tasks:
            raise ValueError("action=modify requires a non-empty 'tasks' list")
        return self


class RequirementReviewResume(BaseModel):
    """agents/sysml/nodes.py's requirement_review node (route_from_review)."""

    action: Literal["approve", "regenerate", "question", "cancel"]
    feedback: str | None = None
    question: str | None = None

    @model_validator(mode="after")
    def _check(self):
        if self.action == "question" and not self.question:
            raise ValueError("action=question requires a non-empty 'question' field")
        return self


class SelectRequirementResume(BaseModel):
    """agents/sysml/middle_nodes.py's user_confirm_inputs, pattern=select_requirement
    (both the plain ambiguous-target case and resolve_level's select_level_source
    case -- same resume shape either way, selected_id)."""

    action: Literal["confirm", "modify", "cancel"]
    selected_id: str | None = None
    user_input: str | None = None

    @model_validator(mode="after")
    def _check(self):
        if self.action == "confirm":
            if not self.selected_id:
                raise ValueError("action=confirm requires 'selected_id'")
            _require_uuid(self.selected_id, "selected_id")
        if self.action == "modify" and not self.user_input:
            raise ValueError("action=modify requires 'user_input'")
        return self


class SelectRequirementsForDiagramResume(BaseModel):
    """agents/sysml/middle_nodes.py's user_confirm_inputs,
    pattern=select_requirements_for_diagram (multi-select, min_selected=1 enforced
    by the node itself on top of this -- this only checks non-empty/well-typed)."""

    action: Literal["confirm", "modify", "cancel"]
    selected_ids: list[str] | None = None
    select_all: bool = False
    user_input: str | None = None

    @model_validator(mode="after")
    def _check(self):
        if self.action == "confirm":
            if not self.select_all and not self.selected_ids:
                raise ValueError("action=confirm requires 'selected_ids' (non-empty) or select_all=true")
            for sid in self.selected_ids or []:
                _require_uuid(sid, "selected_ids[]")
        if self.action == "modify" and not self.user_input:
            raise ValueError("action=modify requires 'user_input'")
        return self


class ConfirmDiagramTypeResume(BaseModel):
    """agents/sysml/middle_nodes.py's user_confirm_inputs, pattern=confirm_diagram_type
    -- NOT currently reachable (no code sets this pending_pattern today), included
    for completeness/forward-compatibility per app/schemas/confirmations.py."""

    action: Literal["confirm", "cancel"]
    selected_diagram_type: str | None = None

    @model_validator(mode="after")
    def _check(self):
        if self.action == "confirm" and self.selected_diagram_type not in ("use_case", "state_machine", "sequence"):
            raise ValueError("action=confirm requires selected_diagram_type in {use_case, state_machine, sequence}")
        return self


class ConfirmActionResume(BaseModel):
    """agents/sysml/middle_nodes.py's user_confirm_inputs, pattern=confirm_action
    (e.g. resolve_level's missing_level_source pivot)."""

    action: Literal["confirm", "modify", "cancel"]
    user_input: str | None = None

    @model_validator(mode="after")
    def _check(self):
        if self.action == "modify" and not self.user_input:
            raise ValueError("action=modify requires 'user_input'")
        return self


class ClarifyRequestResume(BaseModel):
    """agents/sysml/middle_nodes.py's user_confirm_inputs, pattern=clarify_request.
    Only 'modify' is a meaningful reply per that node's own docstring (a rephrased
    user_input, looping back to middle_supervisor) -- 'confirm' is deliberately NOT
    offered here even though the underlying node wouldn't crash on it, because
    confirming a request the graph itself just said it couldn't understand doesn't
    mean anything; 'cancel' fails open to END exactly as for every other pattern.
    """

    action: Literal["modify", "cancel"]
    user_input: str | None = None

    @model_validator(mode="after")
    def _check(self):
        if self.action == "modify" and not self.user_input:
            raise ValueError("action=modify requires 'user_input'")
        return self


class PlanClarifyResume(BaseModel):
    """supervisor/plan.py's plan_node -- NOT one of this step's 7 named patterns, but
    a real, resumable interrupt (type=plan_clarify) with its OWN shape: no 'action'
    field at all, just {"user_input": "..."} (see plan_node's resume.get("user_input")).
    """

    user_input: str

    @model_validator(mode="after")
    def _check(self):
        if not self.user_input or not self.user_input.strip():
            raise ValueError("plan_clarify requires a non-empty 'user_input'")
        return self


_PATTERN_MODELS: dict[str, type[BaseModel]] = {
    "plan_review": PlanReviewResume,
    "requirement_review": RequirementReviewResume,
    "select_requirement": SelectRequirementResume,
    "select_requirements_for_diagram": SelectRequirementsForDiagramResume,
    "confirm_diagram_type": ConfirmDiagramTypeResume,
    "confirm_action": ConfirmActionResume,
    "clarify_request": ClarifyRequestResume,
    "plan_clarify": PlanClarifyResume,
}


def _format_validation_error(exc: ValidationError) -> str:
    parts = []
    for err in exc.errors():
        loc = ".".join(str(p) for p in err["loc"]) or "body"
        parts.append(f"{loc}: {err['msg']}")
    return "; ".join(parts)


def validate_resume_payload(pattern: str, raw_body: dict, pending_payload: dict) -> dict:
    """Validates `raw_body` against the model for `pattern` (the ACTUAL pending
    pattern, read from the checkpointer -- never client-declared), then cross-checks
    any selected id(s) against the options THIS SPECIFIC interrupt actually offered
    (not merely "exists somewhere in the DB"). Returns a sanitized dict, safe to pass
    to Command(resume=...) -- raises ResumeValidationError (-> 422) on any problem,
    the graph is never invoked in that case.
    """
    model_cls = _PATTERN_MODELS.get(pattern)
    if model_cls is None:
        raise ResumeValidationError(f"no resume validator registered for pattern {pattern!r}")

    try:
        parsed = model_cls.model_validate(raw_body)
    except ValidationError as exc:
        raise ResumeValidationError(_format_validation_error(exc)) from exc

    resume_payload = parsed.model_dump(exclude_none=True)

    offered_ids = {
        opt["id"] for opt in (pending_payload.get("options") or []) if isinstance(opt, dict) and "id" in opt
    }
    selected_id = resume_payload.get("selected_id")
    if selected_id and offered_ids and selected_id not in offered_ids:
        raise ResumeValidationError(f"selected_id {selected_id!r} was not offered for this interrupt")
    selected_ids = resume_payload.get("selected_ids")
    if selected_ids and offered_ids:
        unknown = [sid for sid in selected_ids if sid not in offered_ids]
        if unknown:
            raise ResumeValidationError(f"selected_ids contains ids not offered for this interrupt: {unknown}")

    return resume_payload
