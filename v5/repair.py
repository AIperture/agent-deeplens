from __future__ import annotations

import json
from typing import Any

from .action_select import build_pending_interaction
from .types import (
    ConversationState,
    LoopAction,
    LoopActionKind,
    PendingInteractionKind,
    RepairDecision,
    RepairDecisionKind,
    TaskFrame,
    ToolResult,
)


def _patch_action_from_task(task: TaskFrame, action: LoopAction) -> LoopAction | None:
    if action.kind != LoopActionKind.TOOL_CALL:
        return None
    args = dict(action.args or {})
    changed = False
    if action.tool_name in {"dl.analysis", "dl.export_lens", "ag.spawn_graph"} and "lens_source" not in args and task.lens_source:
        args["lens_source"] = dict(task.lens_source)
        changed = True
    if action.tool_name == "dl.analysis" and "mode" not in args and task.analysis_request.get("mode"):
        args["mode"] = task.analysis_request["mode"]
        changed = True
    if action.tool_name == "dl.export_lens" and "formats" not in args and task.delivery_request.get("formats"):
        args["formats"] = list(task.delivery_request["formats"])
        changed = True
    if action.tool_name in {"ag.status", "ag.cancel"} and "run_id" not in args and task.run_request.get("run_id"):
        args["run_id"] = task.run_request["run_id"]
        changed = True
    if action.tool_name == "dl.create_lens" and "design_spec" not in args and task.design_spec:
        args["design_spec"] = dict(task.design_spec)
        changed = True
    if not changed:
        return None
    return LoopAction(
        kind=LoopActionKind.TOOL_CALL,
        tool_name=action.tool_name,
        args=args,
        reason="Patched tool arguments from current task state.",
        priority=action.priority,
    )


def _merge_missing_fields(task: TaskFrame, result: ToolResult) -> None:
    for field_name in list(result.missing_fields or []):
        if field_name not in task.missing_fields:
            task.missing_fields.append(field_name)


def _repair_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "kind": {"type": "string", "enum": ["retry", "ask_user", "escalate", "fail"]},
            "tool_name": {"type": ["string", "null"]},
            "args_json": {"type": "string"},
            "missing_fields": {"type": "array", "items": {"type": "string"}},
            "reason": {"type": "string"},
            "prompt": {"type": ["string", "null"]},
        },
        "required": ["kind", "tool_name", "args_json", "missing_fields", "reason", "prompt"],
        "additionalProperties": False,
    }


async def _llm_repair(
    *,
    task: TaskFrame,
    action: LoopAction,
    result: ToolResult,
    context: Any,
) -> RepairDecision | None:
    llm = context.llm("fast")
    payload = {
        "task": {
            "domain_hint": task.domain_hint.value,
            "task_shape": task.task_shape.value,
            "preferred_tool": task.preferred_tool,
            "missing_fields": task.missing_fields,
            "design_spec": task.design_spec,
            "analysis_request": task.analysis_request,
            "run_request": task.run_request,
            "delivery_request": task.delivery_request,
            "lens_source": task.lens_source,
        },
        "action": {
            "kind": action.kind.value,
            "tool_name": action.tool_name,
            "args": action.args,
        },
        "tool_result": {
            "summary": result.summary,
            "error_code": result.error_code,
            "missing_fields": result.missing_fields,
            "retryable": result.retryable,
        },
    }
    try:
        response, _usage = await llm.chat(
            messages=[
                {
                    "role": "system",
                    "content": (
                        "Repair only the failed atomic action. Do not switch tasks. "
                        "Choose one of retry, ask_user, escalate, or fail."
                    ),
                },
                {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
            ],
            output_format="json_schema",
            json_schema=_repair_schema(),
            schema_name="DeepLensV5Repair",
            strict_schema=True,
            validate_json=True,
            max_output_tokens=260,
            reasoning_effort="low",
        )
        obj = json.loads(response) if isinstance(response, str) else response
    except Exception:
        context.logger().warning("deeplens_v5: repair llm failed", exc_info=True)
        return None

    kind = RepairDecisionKind(obj["kind"])
    if kind == RepairDecisionKind.RETRY:
        try:
            args = json.loads(obj.get("args_json") or "{}")
        except Exception:
            args = {}
        return RepairDecision(
            kind=kind,
            reason=str(obj.get("reason") or "LLM proposed a patched retry."),
            task=task,
            action=LoopAction(
                kind=LoopActionKind.TOOL_CALL,
                tool_name=obj.get("tool_name") or action.tool_name,
                args=args if isinstance(args, dict) else {},
                reason="LLM repair patch.",
                priority=action.priority,
            ),
        )
    if kind == RepairDecisionKind.ASK_USER:
        for field_name in obj.get("missing_fields") or []:
            if field_name not in task.missing_fields:
                task.missing_fields.append(str(field_name))
        pending = build_pending_interaction(task)
        if obj.get("prompt"):
            pending.prompt = str(obj["prompt"])
        pending.kind = PendingInteractionKind.MISSING_INFO
        return RepairDecision(
            kind=kind,
            reason=str(obj.get("reason") or "Need a narrower clarification."),
            task=task,
            pending_interaction=pending,
        )
    return RepairDecision(
        kind=kind,
        reason=str(obj.get("reason") or "Could not safely repair the failed action."),
        task=task,
    )


async def repair_failed_action(
    *,
    task: TaskFrame,
    action: LoopAction,
    result: ToolResult,
    state: ConversationState,
    context: Any,
) -> RepairDecision:
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

    patched_action = _patch_action_from_task(task, action)
    if patched_action is not None:
        return RepairDecision(
            kind=RepairDecisionKind.RETRY,
            reason="Patched tool arguments from task state.",
            task=task,
            action=patched_action,
        )

    repeated_failures = state.retry_counters.get(result.tool_name or "tool", 0)
    if repeated_failures >= 2 or not result.retryable:
        llm_decision = await _llm_repair(
            task=task,
            action=action,
            result=result,
            context=context,
        )
        if llm_decision is not None:
            return llm_decision

    return RepairDecision(
        kind=RepairDecisionKind.ESCALATE,
        reason=(
            f"Local repair could not safely recover from failed action "
            f"({action.tool_name or action.kind.value}, error={result.error_code})."
        ),
        task=task,
    )
