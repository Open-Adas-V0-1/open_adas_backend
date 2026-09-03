"""Deterministic (router-as-code) helpers over plan_state -- the TODO list built by
plan_node (Step 2). No LLM calls here; pure reads/transforms of state already in hand.
Shared by top_level_supervisor (task selection), sysml_middle_node (task execution),
plan_node (initial build), and plan_review (the light re-validation path for edits).
"""
import uuid

from app.schemas.supervisor import PlannedTask, TodoItem, TodoStatus

# More than this many tasks counts as a "complex" plan needing plan_review's HITL
# approval; a single-task plan is "simple" and skips straight to execution. Easy to
# adjust as the definition of "worth confirming" evolves.
PLAN_REVIEW_TASK_THRESHOLD = 1


def is_complex_plan(plan_state: dict, threshold: int = PLAN_REVIEW_TASK_THRESHOLD) -> bool:
    return len(plan_state.get("tasks") or []) > threshold


def build_todo_items(planned_tasks: list[PlannedTask]) -> list[TodoItem]:
    """Deterministically builds fresh TodoItems (sequential ids, remapped
    dependencies, all reset to pending) from an ordered PlannedTask list. Used by BOTH
    plan_node (after the LLM's initial decomposition) and plan_review (after the user
    edits the plan) -- the SAME construction logic either way, so edits stay just as
    internally consistent as a freshly-decomposed plan (ids/dependencies always
    re-derived from CURRENT order, never left dangling from a prior edit).
    """
    return [
        TodoItem(
            id=f"task-{i}",
            description=t.description,
            intent=t.intent,
            level=t.level,
            depends_on=f"task-{t.depends_on_task_number}" if t.depends_on_task_number else None,
            status=TodoStatus.pending,
            result_ref=None,
            gen_id=str(uuid.uuid4()),
        )
        for i, t in enumerate(planned_tasks, start=1)
    ]


def next_pending_task(plan_state: dict) -> dict | None:
    """The next task ready to run: pending, with its dependency (if any) already done.
    Respects declaration order -- the first eligible task wins.
    """
    tasks = plan_state.get("tasks") or []
    done_ids = {t["id"] for t in tasks if t.get("status") == "done"}
    for task in tasks:
        if task.get("status") != "pending":
            continue
        depends_on = task.get("depends_on")
        if depends_on and depends_on not in done_ids:
            continue  # dependency not satisfied yet -- not eligible this visit
        return task
    return None


def in_progress_task(plan_state: dict) -> dict | None:
    for task in plan_state.get("tasks") or []:
        if task.get("status") == "in_progress":
            return task
    return None


def with_task_status(plan_state: dict, task_id: str, status: str, result_ref: dict | None = None) -> dict:
    """Returns a NEW plan_state dict with the given task's status (and, when provided,
    result_ref) updated -- never mutates the input in place.
    """
    updated_tasks = []
    for task in plan_state.get("tasks") or []:
        if task["id"] == task_id:
            updated = {**task, "status": status}
            if result_ref is not None:
                updated["result_ref"] = result_ref
            updated_tasks.append(updated)
        else:
            updated_tasks.append(task)
    return {**plan_state, "tasks": updated_tasks}
