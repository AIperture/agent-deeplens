from __future__ import annotations

from dataclasses import asdict
from typing import Any

from ..repair import repair_task
from ..response_compose import ResponseBundle, compose_reply
from ..tool_dispatch import dispatch_execution
from ..tool_results import apply_tool_result_to_state
from ..types import DeepLensState, ExecutionRequest, InterpretationResult, RepairAction, RepairResult, TaskFrame


MAX_WORKFLOW_STEPS = 5


def _record_trace(state: DeepLensState, *, event: str, payload: dict[str, Any] | None = None) -> None:
    state.loop_trace.append({"event": event, "payload": payload or {}})
    state.loop_trace = state.loop_trace[-80:]


def _store_active_objects(*, state: DeepLensState, task: TaskFrame, execution: ExecutionRequest | None) -> None:
    state.active_task_frame = asdict(task)
    state.active_execution = asdict(execution) if execution else None


def _repairresult_to_objects(
    repair: RepairResult,
    current_task: TaskFrame,
    current_execution: ExecutionRequest | None,
) -> tuple[TaskFrame, ExecutionRequest | None]:
    task = TaskFrame.from_dict(repair.repaired_task_frame) if repair.repaired_task_frame else current_task
    execution = ExecutionRequest.from_dict(repair.repaired_execution) if repair.repaired_execution else current_execution
    return task, execution


def _compose_output(
    *,
    task: TaskFrame,
    execution: ExecutionRequest | None,
    tool_result: Any,
    repair_result: RepairResult | None,
    state: DeepLensState,
) -> dict[str, Any]:
    reply = compose_reply(
        ResponseBundle(
            task_frame=task,
            execution=execution,
            tool_result=tool_result,
            repair_result=repair_result,
            state=state,
        )
    )
    return {
        "reply": reply,
        "state": state,
        "task_frame": task,
        "execution": execution,
        "tool_result": tool_result,
        "repair_result": repair_result,
    }


async def run_workflow_controller(
    *,
    interpretation: InterpretationResult,
    state: DeepLensState,
    context_bundle: Any,
    context: Any,
) -> dict[str, Any]:
    task = interpretation.task_frame
    execution = interpretation.execution

    if execution is None or not execution.should_execute:
        return _compose_output(task=task, execution=execution, tool_result=None, repair_result=None, state=state)

    _store_active_objects(state=state, task=task, execution=execution)
    _record_trace(
        state,
        event="workflow_controller.start",
        payload={
            "workflow_family": task.workflow_family.value,
            "tool": execution.selected_tool,
            "args_keys": sorted(list((execution.normalized_args or {}).keys())),
        },
    )

    last_result = None
    last_repair: RepairResult | None = None

    for step_idx in range(MAX_WORKFLOW_STEPS):
        if execution is None or not execution.should_execute:
            break

        _record_trace(
            state,
            event="workflow_controller.dispatch",
            payload={
                "step": step_idx,
                "tool": execution.selected_tool,
                "args_keys": sorted(list((execution.normalized_args or {}).keys())),
            },
        )

        tool_result = await dispatch_execution(
            execution=execution,
            task=task,
            state=state,
            context_bundle=context_bundle,
            context=context,
        )
        apply_tool_result_to_state(result=tool_result, state=state)

        _record_trace(
            state,
            event="workflow_controller.tool_result",
            payload={
                "step": step_idx,
                "tool_name": tool_result.tool_name,
                "ok": tool_result.ok,
                "outcome_type": tool_result.outcome_type.value,
                "error_code": tool_result.error_code,
                "missing_fields": list(tool_result.missing_fields or []),
                "run_id": tool_result.run_id,
            },
        )

        last_result = tool_result
        last_repair = None

        if tool_result.ok:
            _store_active_objects(state=state, task=task, execution=execution)
            return _compose_output(
                task=task,
                execution=execution,
                tool_result=tool_result,
                repair_result=None,
                state=state,
            )

        repair = await repair_task(
            task=task,
            execution=execution,
            tool_result=tool_result,
            state=state,
            context=context,
            latest_message=interpretation.envelope.cleaned_message,
        )
        last_repair = repair

        state.repair_history.append(
            {
                "step": step_idx,
                "action": repair.action.value,
                "notes": list(repair.notes or []),
                "retry_reason": repair.retry_reason,
                "failure_reason": repair.failure_reason,
            }
        )
        state.repair_history = state.repair_history[-30:]

        _record_trace(
            state,
            event="workflow_controller.repair",
            payload={
                "step": step_idx,
                "action": repair.action.value,
                "retry_reason": repair.retry_reason,
                "failure_reason": repair.failure_reason,
            },
        )

        if repair.action in {RepairAction.ASK_USER, RepairAction.FAIL}:
            task, execution = _repairresult_to_objects(repair, task, execution)
            _store_active_objects(state=state, task=task, execution=execution)
            return _compose_output(
                task=task,
                execution=execution,
                tool_result=tool_result,
                repair_result=repair,
                state=state,
            )

        if repair.action in {
            RepairAction.RETRY_SAME_TOOL,
            RepairAction.RETRY_WITH_UPDATED_ARGS,
            RepairAction.REFRAME_TASK,
        }:
            task, execution = _repairresult_to_objects(repair, task, execution)
            _store_active_objects(state=state, task=task, execution=execution)
            continue

        break

    _store_active_objects(state=state, task=task, execution=execution)
    return _compose_output(
        task=task,
        execution=execution,
        tool_result=last_result,
        repair_result=last_repair,
        state=state,
    )