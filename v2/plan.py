from .types import TaskShape 


def should_make_plan(task, state) -> bool:
    if task.task_shape != TaskShape.MULTI_STEP:
        return False
    if "step by step" in task.user_goal.lower():
        return True
    if "plan" in task.user_goal.lower():
        return True
    if task.preferred_tool in {"dl.submit_optimization", "ag.spawn_graph"}:
        return True
    if state.active_run_id:
        return True
    return False