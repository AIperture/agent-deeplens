"""Multi-step task planning for common DeepLens workflows.

Builds a deterministic plan of actions that can be executed without per-step
LLM calls. Falls back to LLM-based propose when the plan is exhausted or
an error occurs.
"""
from __future__ import annotations

from dataclasses import asdict
from typing import Any

from .types import LoopAction, TaskPlan, TaskShape


def should_make_plan(task: Any, state: Any) -> bool:
    if task.task_shape not in (TaskShape.MULTI_STEP, TaskShape.SINGLE_ACTION):
        return False
    if task.preferred_tool in {"ag.spawn_graph", "dl.create_lens", "dl.analysis", "dl.export_lens"}:
        return True
    if "step by step" in (task.user_goal or "").lower():
        return True
    if "plan" in (task.user_goal or "").lower():
        return True
    return False


def build_plan(task: Any, state: Any) -> TaskPlan | None:
    """Build a deterministic plan for known workflows. Returns None if not plannable."""
    if not should_make_plan(task, state):
        return None

    missing = list(getattr(task, "missing_fields", []) or [])
    preferred = getattr(task, "preferred_tool", None)

    if preferred == "dl.create_lens":
        steps = []
        if missing:
            steps.append("ask_user:missing_fields")
        steps.append("tool_call:dl.create_lens")
        steps.append("respond:summarize")
        return TaskPlan(goal=task.user_goal, steps=steps)

    if preferred == "ag.spawn_graph":
        steps = []
        if missing:
            steps.append("ask_user:missing_fields")
        steps.append("request_approval:ag.spawn_graph")
        steps.append("tool_call:ag.spawn_graph")
        return TaskPlan(goal=task.user_goal, steps=steps)

    if preferred == "dl.analysis":
        steps = []
        if missing:
            steps.append("ask_user:missing_fields")
        steps.append("tool_call:dl.analysis")
        steps.append("respond:summarize")
        return TaskPlan(goal=task.user_goal, steps=steps)

    if preferred == "dl.export_lens":
        steps = []
        if missing:
            steps.append("ask_user:missing_fields")
        steps.append("tool_call:dl.export_lens")
        steps.append("respond:summarize")
        return TaskPlan(goal=task.user_goal, steps=steps)

    return None


def plan_step_to_action(step: str, task: Any, state: Any) -> LoopAction | None:
    """Convert a plan step string to a concrete LoopAction."""
    if step.startswith("ask_user:"):
        from .loop_engine import _default_ask_prompt
        return {
            "kind": "ask_user",
            "name": None,
            "args": {"prompt": _default_ask_prompt(task)},
            "rationale": "Plan: gather missing inputs.",
        }

    if step.startswith("request_approval:"):
        tool_name = step.split(":", 1)[1]
        return {
            "kind": "request_approval",
            "name": tool_name,
            "args": {"approval_prompt": f"Ready to run `{tool_name}`. Approve?"},
            "rationale": "Plan: request approval before execution.",
        }

    if step.startswith("tool_call:"):
        tool_name = step.split(":", 1)[1]
        return {
            "kind": "tool_call",
            "name": tool_name,
            "args": {},
            "rationale": f"Plan: execute {tool_name}.",
        }

    if step.startswith("respond:"):
        return {
            "kind": "respond",
            "name": None,
            "args": {"text": "Done."},
            "rationale": "Plan: summarize results.",
        }

    return None


def advance_plan(plan: TaskPlan) -> str | None:
    """Advance to the next plan step. Returns the step string or None if exhausted."""
    if plan.current_step >= len(plan.steps):
        plan.status = "completed"
        return None
    step = plan.steps[plan.current_step]
    plan.current_step += 1
    plan.status = "executing"
    return step


def plan_to_dict(plan: TaskPlan | None) -> dict[str, Any] | None:
    if plan is None:
        return None
    return asdict(plan)


def plan_from_dict(data: dict[str, Any] | None) -> TaskPlan | None:
    if not data:
        return None
    return TaskPlan(
        goal=data.get("goal", ""),
        steps=data.get("steps", []),
        current_step=data.get("current_step", 0),
        status=data.get("status", "draft"),
        notes=data.get("notes", []),
    )
