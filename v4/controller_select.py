# controller_select.py
from __future__ import annotations

from .types import ControllerKind, TaskFrame, TaskShape, WorkflowFamily


def select_controller(task: TaskFrame) -> ControllerKind:
    if task.task_shape == TaskShape.DIRECT_ANSWER:
        return ControllerKind.DIRECT

    if task.workflow_family == WorkflowFamily.RUN_CONTROL:
        return ControllerKind.RUN_CONTROL

    return ControllerKind.WORKFLOW