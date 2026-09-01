"""Deterministic (router-as-code) helpers over plan_state -- the TODO list built by
plan_node (Step 2). No LLM calls here; pure reads/transforms of state already in hand.
Shared by top_level_supervisor (task selection) and sysml_middle_node (task execution).
"""


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
