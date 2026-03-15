# response_compose.py
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .types import ExecutionRequest, OutcomeType, RepairAction, RepairResult, TaskFrame, ToolResult


@dataclass
class ResponseBundle:
    task_frame: TaskFrame
    execution: ExecutionRequest | None
    tool_result: ToolResult | None
    repair_result: RepairResult | None
    state: Any | None = None

def _assumption_note(task: TaskFrame) -> str:
    inferred = []
    for key, fv in (task.field_map or {}).items():
        if getattr(fv, "inferred", False):
            inferred.append(key)
    if not inferred:
        return ""
    return "Assumed / inferred fields: " + ", ".join(sorted(inferred[:8])) + ". Please confirm if needed."

def _join_nonempty(parts: list[str]) -> str:
    return "\n\n".join([p for p in parts if p and p.strip()])


def _format_missing_fields(missing_fields: list[str]) -> str:
    if not missing_fields:
        return ""
    readable = ", ".join(missing_fields)
    return f"Missing inputs: {readable}."


def _next_steps_for_task(task: TaskFrame) -> str:
    if task.workflow_family.value == "analysis":
        return "Next steps: upload/select a lens source, specify an analysis mode, or ask to export the result."
    if task.workflow_family.value == "design":
        return "Next steps: provide FOV, F-number, and either focal length or image height."
    if task.workflow_family.value == "optimization":
        return "Next steps: provide/select a lens source and specify the optimization target if needed."
    if task.workflow_family.value == "export":
        return "Next steps: specify the export format if you have a preference."
    if task.workflow_family.value == "run_control":
        return "Next steps: ask for run status, cancel the run, or inspect produced artifacts."
    return ""


def _compose_direct(bundle: ResponseBundle) -> str:
    task = bundle.task_frame
    if task.intent.value == "ask":
        return "I can help with analysis, design, export, optimization status, or cancellation."
    if task.intent.value == "explain":
        return "I can explain DeepLens results, lens workflows, and what to do next."
    return "I interpreted the request, but no workflow execution was needed."


def _compose_repair(bundle: ResponseBundle) -> str | None:
    repair = bundle.repair_result
    task = bundle.task_frame
    if repair is None:
        return None

    if repair.action == RepairAction.ASK_USER:
        parts = [
            repair.ask_user_message or "I need a bit more information before continuing.",
            _format_missing_fields(task.missing_fields),
            _next_steps_for_task(task),
        ]
        return _join_nonempty(parts)

    if repair.action == RepairAction.FAIL:
        parts = [
            repair.failure_reason or "The operation failed and I could not recover automatically.",
            _format_missing_fields(task.missing_fields),
            _next_steps_for_task(task),
        ]
        return _join_nonempty(parts)

    if repair.action in {RepairAction.RETRY_SAME_TOOL, RepairAction.RETRY_WITH_UPDATED_ARGS}:
        return repair.retry_reason or "I repaired the request and retried the operation."

    return None


def _compose_run_submitted(bundle: ResponseBundle) -> str:
    result = bundle.tool_result
    task = bundle.task_frame
    assert result is not None

    parts = [
        result.user_visible_summary or result.summary or "Run submitted.",
    ]

    if result.run_id:
        parts.append(f"Run ID: {result.run_id}")

    if result.recommended_next_actions:
        parts.append(f"Suggested next actions: {', '.join(result.recommended_next_actions)}.")
    else:
        parts.append(_next_steps_for_task(task))

    return _join_nonempty(parts)


def _compose_needs_input(bundle: ResponseBundle) -> str:
    task = bundle.task_frame
    result = bundle.tool_result
    assert result is not None

    parts = [
        result.user_visible_summary or result.summary or "More input is required.",
    ]

    merged_missing = list(task.missing_fields or [])
    for item in result.missing_fields or []:
        if item not in merged_missing:
            merged_missing.append(item)

    parts.append(_format_missing_fields(merged_missing))
    parts.append(_next_steps_for_task(task))
    return _join_nonempty(parts)


def _compose_failure(bundle: ResponseBundle) -> str:
    task = bundle.task_frame
    result = bundle.tool_result
    assert result is not None

    parts = [
        result.user_visible_summary or result.summary or "The operation failed.",
    ]

    if result.error_code:
        parts.append(f"Error code: {result.error_code}")

    if result.warnings:
        parts.append("Warnings: " + "; ".join(result.warnings))

    parts.append(_format_missing_fields(task.missing_fields))
    parts.append(_next_steps_for_task(task))
    return _join_nonempty(parts)


def _compose_success(bundle: ResponseBundle) -> str:
    task = bundle.task_frame
    result = bundle.tool_result
    assert result is not None

    parts = [
        result.user_visible_summary or result.summary or "The operation completed successfully.",
    ]

    if result.run_id:
        parts.append(f"Run ID: {result.run_id}")

    if result.artifacts:
        artifact_names = []
        for art in result.artifacts[:5]:
            name = art.get("name") or art.get("filename") or art.get("artifact_id") or art.get("uri")
            if name:
                artifact_names.append(str(name))
        if artifact_names:
            parts.append("Artifacts: " + ", ".join(artifact_names))

    assumption_note = _assumption_note(task)
    if assumption_note:
        parts.append(assumption_note)

    if result.recommended_next_actions:
        parts.append("Suggested next actions: " + ", ".join(result.recommended_next_actions))
    else:
        next_steps = _next_steps_for_task(task)
        if next_steps:
            parts.append(next_steps)

    return _join_nonempty(parts)


def compose_reply(bundle: ResponseBundle) -> str:
    repair_text = _compose_repair(bundle)
    if repair_text:
        return repair_text

    result = bundle.tool_result
    task = bundle.task_frame

    if result is None:
        return _compose_direct(bundle)

    if result.outcome_type == OutcomeType.RUN_SUBMITTED:
        return _compose_run_submitted(bundle)

    if result.outcome_type == OutcomeType.NEEDS_INPUT or result.needs_input:
        return _compose_needs_input(bundle)

    if not result.ok or result.outcome_type == OutcomeType.FAILED:
        return _compose_failure(bundle)

    if task.task_shape.value == "direct_answer":
        return _compose_direct(bundle)

    return _compose_success(bundle)
