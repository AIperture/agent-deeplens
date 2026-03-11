from __future__ import annotations

from typing import Any

from .action_select import build_pending_interaction
from .types import (
    ConversationState,
    DomainHint,
    LoopAction,
    LoopActionKind,
    PendingInteractionKind,
    RepairDecision,
    RepairDecisionKind,
    TaskFrame,
    ToolResult,
)


# ---------------------------------------------------------------------
# What to implement here
# ---------------------------------------------------------------------
# - Narrow failed-action recovery
# - Deterministic repair first
# - Optional LLM repair second
# - Escalation when local recovery is no longer trustworthy
#
# What should NOT be included here
# - Master task switching
# - Full re-interpretation of the conversation
# - Multi-step planning
# - Large policy trees from v4
# ---------------------------------------------------------------------


def _patch_action_from_task(task: TaskFrame, action: LoopAction) -> LoopAction | None:
    if action.kind != LoopActionKind.TOOL_CALL:
        return None

    args = dict(action.args or {})
    changed = False

    if action.tool_name == "dl.analysis":
        if "lens_source" not in args and task.lens_source:
            args["lens_source"] = task.lens_source
            changed = True
        if "mode" not in args and task.analysis_request.get("mode"):
            args["mode"] = task.analysis_request["mode"]
            changed = True

    if action.tool_name == "dl.export_lens":
        if "lens_source" not in args and task.lens_source:
            args["lens_source"] = task.lens_source
            changed = True
        if "formats" not in args and task.delivery_request.get("formats"):
            args["formats"] = list(task.delivery_request["formats"])
            changed = True

    if action.tool_name == "ag.status":
        if "run_id" not in args and task.run_request.get("run_id"):
            args["run_id"] = task.run_request["run_id"]
            changed = True

    if not changed:
        return None

    return LoopAction(
        kind=LoopActionKind.TOOL_CALL,
        tool_name=action.tool_name,
        args=args,
        reason="Patched action arguments from task state.",
        priority=action.priority,
    )


def _merge_missing_fields(task: TaskFrame, result: ToolResult) -> None:
    for field_name in list(result.missing_fields or []):
        if field_name not in task.missing_fields:
            task.missing_fields.append(field_name)


async def repair_failed_action(
    *,
    task: TaskFrame,
    action: LoopAction,
    result: ToolResult,
    state: ConversationState,
    context: Any,
) -> RepairDecision:
    del state
    del context

    # Deterministic repair 1: tool disclosed missing inputs
    _merge_missing_fields(task, result)
    if task.missing_fields or result.needs_input:
        pending = build_pending_interaction(task)
        pending.kind = PendingInteractionKind.MISSING_INFO
        return RepairDecision(
            kind=RepairDecisionKind.ASK_USER,
            reason="Execution revealed missing required inputs.",
            task=task,
            pending_interaction=pending,
        )

    # Deterministic repair 2: patch arguments from current task
    patched_action = _patch_action_from_task(task, action)
    if patched_action is not None:
        return RepairDecision(
            kind=RepairDecisionKind.RETRY,
            reason="Patched tool arguments from task state.",
            task=task,
            action=patched_action,
        )

    # Deterministic repair 3: small adjacent-tool correction
    if action.tool_name == "ag.status" and not task.run_request.get("run_id"):
        return RepairDecision(
            kind=RepairDecisionKind.ASK_USER,
            reason="Need run_id before run-control action can proceed.",
            task=task,
            pending_interaction=build_pending_interaction(task),
        )

    # Optional LLM repair hook:
    # Implement later if deterministic repair is insufficient.
    # Keep it narrow:
    #   - propose defaults
    #   - propose a nearby tool
    #   - ask for a narrower clarification
    #
    # Do NOT add broad re-interpretation here.

    # Escalate if the local loop no longer has a safe small correction.
    return RepairDecision(
        kind=RepairDecisionKind.ESCALATE,
        reason=(
            f"Local repair could not safely recover from failed action "
            f"({action.tool_name or action.kind.value}, error={result.error_code})."
        ),
        task=task,
    )