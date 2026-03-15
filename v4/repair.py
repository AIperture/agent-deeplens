from __future__ import annotations

import json
from dataclasses import asdict, replace
from typing import Any

from .interpretation import _derive_execution_request, _finalize_missing_fields
from .types import (
    DeepLensState,
    ExecutionRequest,
    OutcomeType,
    RepairAction,
    RepairResult,
    TaskFrame,
    WorkflowFamily,
)

REPAIR_JSON_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "action": {
            "type": "string",
            "enum": [
                RepairAction.RETRY_SAME_TOOL.value,
                RepairAction.RETRY_WITH_UPDATED_ARGS.value,
                RepairAction.ASK_USER.value,
                RepairAction.REFRAME_TASK.value,
                RepairAction.FAIL.value,
            ],
        },
        "reason": {"type": "string"},
        "ask_user_message": {"type": ["string", "null"]},
        "retry_reason": {"type": ["string", "null"]},
        "failure_reason": {"type": ["string", "null"]},
        "task_patch": {
            "type": "object",
            "properties": {
                "preferred_tool": {"type": ["string", "null"]},
                "design_spec": {
                    "type": ["object", "null"],
                    "properties": {
                        "fov": {"type": ["number", "null"]},
                        "fnum": {"type": ["number", "null"]},
                        "foclen": {"type": ["number", "null"]},
                        "imgh": {"type": ["number", "null"]},
                        "bfl": {"type": ["number", "null"]},
                        "thickness": {"type": ["number", "null"]},
                        "aperture": {"type": ["number", "null"]},
                        "save_name": {"type": ["string", "null"]},
                        "baseline_analysis": {"type": ["boolean", "null"]},
                    },
                    "required": ["fov", "fnum", "foclen", "imgh", "bfl", "thickness", "aperture", "save_name", "baseline_analysis"],
                    "additionalProperties": False,
                },
                "analysis_request": {
                    "type": ["object", "null"],
                    "properties": {
                        "mode": {"type": ["string", "null"]},
                    },
                    "required": ["mode"],
                    "additionalProperties": False,
                },
                "run_request": {
                    "type": ["object", "null"],
                    "properties": {
                        "run_id": {"type": ["string", "null"]},
                        "timeout_s": {"type": ["number", "null"]},
                        "use_stub": {"type": ["boolean", "null"]},
                    },
                    "required": ["run_id", "timeout_s", "use_stub"],
                    "additionalProperties": False,
                },
                "delivery_request": {
                    "type": ["object", "null"],
                    "properties": {
                        "export_format": {"type": ["string", "null"]},
                        "formats": {"type": ["array", "null"], "items": {"type": "string"}},
                    },
                    "required": ["export_format", "formats"],
                    "additionalProperties": False,
                },
                "lens_source": {
                    "type": ["object", "null"],
                    "properties": {
                        "name": {"type": ["string", "null"]},
                        "kind": {"type": ["string", "null"]},
                        "path": {"type": ["string", "null"]},
                        "artifact_id": {"type": ["string", "null"]},
                    },
                    "required": ["name", "kind", "path", "artifact_id"],
                    "additionalProperties": False,
                },
                "missing_fields": {"type": "array", "items": {"type": "string"}},
                "notes": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["preferred_tool", "design_spec", "analysis_request", "run_request", "delivery_request", "lens_source", "missing_fields", "notes"],
            "additionalProperties": False,
        },
        "execution_patch": {
            "type": ["object", "null"],
            "properties": {
                "selected_tool": {"type": ["string", "null"]},
                "normalized_args": {
                    "type": ["object", "null"],
                    "properties": {
                        "mode": {"type": ["string", "null"]},
                        "run_id": {"type": ["string", "null"]},
                        "timeout_s": {"type": ["number", "null"]},
                        "use_stub": {"type": ["boolean", "null"]},
                        "formats": {"type": ["array", "null"], "items": {"type": "string"}},
                    },
                    "required": ["mode", "run_id", "timeout_s", "use_stub", "formats"],
                    "additionalProperties": False,
                },
                "should_execute": {"type": ["boolean", "null"]},
            },
            "required": ["selected_tool", "normalized_args", "should_execute"],
            "additionalProperties": False,
        },
        "missing_fields": {"type": "array", "items": {"type": "string"}},
        "notes": {"type": "array", "items": {"type": "string"}},
    },
    "required": [
        "action",
        "reason",
        "ask_user_message",
        "retry_reason",
        "failure_reason",
        "task_patch",
        "execution_patch",
        "missing_fields",
        "notes",
    ],
    "additionalProperties": False,
}


def _has_lens_source(task: TaskFrame) -> bool:
    return bool(
        task.lens_source
        or task.analysis_request.get("source_ref")
        or task.analysis_request.get("attachment")
        or task.delivery_request.get("source_ref")
        or ("lens_source" in task.field_map and task.field_map["lens_source"].value not in (None, "", []))
    )


def _append_missing(task: TaskFrame, field_name: str) -> None:
    if field_name not in task.missing_fields:
        task.missing_fields.append(field_name)


def _make_ask_user_result(
    *,
    task: TaskFrame,
    execution: ExecutionRequest | None,
    message: str,
    notes: list[str] | None = None,
) -> RepairResult:
    return RepairResult(
        action=RepairAction.ASK_USER,
        repaired_task_frame=asdict(task),
        repaired_execution=asdict(execution) if execution else None,
        ask_user_message=message,
        notes=notes or [],
    )


def _make_retry_result(
    *,
    task: TaskFrame,
    execution: ExecutionRequest,
    retry_reason: str,
    notes: list[str] | None = None,
) -> RepairResult:
    return RepairResult(
        action=RepairAction.RETRY_WITH_UPDATED_ARGS,
        repaired_task_frame=asdict(task),
        repaired_execution=asdict(execution),
        retry_reason=retry_reason,
        notes=notes or [],
    )


def _make_fail_result(
    *,
    task: TaskFrame,
    execution: ExecutionRequest | None,
    failure_reason: str,
    notes: list[str] | None = None,
) -> RepairResult:
    return RepairResult(
        action=RepairAction.FAIL,
        repaired_task_frame=asdict(task),
        repaired_execution=asdict(execution) if execution else None,
        failure_reason=failure_reason,
        notes=notes or [],
    )


def _repair_missing_input(
    *,
    task: TaskFrame,
    execution: ExecutionRequest | None,
    tool_result: Any,
) -> RepairResult | None:
    del tool_result

    if task.workflow_family in {
        WorkflowFamily.ANALYSIS,
        WorkflowFamily.OPTIMIZATION,
        WorkflowFamily.EXPORT,
    }:
        if not _has_lens_source(task):
            _append_missing(task, "lens_source")
            return _make_ask_user_result(
                task=task,
                execution=execution,
                message="I need a lens source first. Please upload a lens file or select an active lens/design to continue.",
                notes=["Repair detected missing lens source."],
            )

    if task.workflow_family == WorkflowFamily.DESIGN:
        has_fov = "fov" in task.design_spec
        has_fnum = "fnum" in task.design_spec
        has_foc_or_imgh = ("foclen" in task.design_spec) or ("imgh" in task.design_spec)

        if not has_fov:
            _append_missing(task, "fov")
        if not has_fnum:
            _append_missing(task, "fnum")
        if not has_foc_or_imgh:
            _append_missing(task, "foclen_or_imgh")

        if task.missing_fields:
            return None

    return None


def _repair_tool_argument_shape(
    *,
    task: TaskFrame,
    execution: ExecutionRequest | None,
    tool_result: Any,
) -> RepairResult | None:
    del tool_result

    if execution is None:
        return None

    if execution.selected_tool == "dl.analysis":
        args = dict(execution.normalized_args)
        changed = False

        if "lens_source" not in args and task.analysis_request.get("source_ref"):
            args["lens_source"] = task.analysis_request["source_ref"]
            changed = True

        if "lens_source" not in args and task.analysis_request.get("attachment"):
            args["lens_source"] = task.analysis_request["attachment"]
            changed = True

        if "mode" not in args and task.analysis_request.get("mode"):
            args["mode"] = task.analysis_request["mode"]
            changed = True

        if changed:
            new_exec = replace(execution, normalized_args=args)
            return _make_retry_result(
                task=task,
                execution=new_exec,
                retry_reason="Filled missing analysis input arguments from task frame.",
                notes=["Repair patched analysis execution args from task frame."],
            )

    if execution.selected_tool == "dl.export_lens":
        args = dict(execution.normalized_args)
        changed = False

        if "lens_source" not in args and task.delivery_request.get("source_ref"):
            args["lens_source"] = task.delivery_request["source_ref"]
            changed = True

        if "formats" not in args and task.delivery_request.get("formats"):
            args["formats"] = list(task.delivery_request["formats"])
            changed = True
        elif "formats" not in args and task.delivery_request.get("export_format"):
            args["formats"] = [task.delivery_request["export_format"]]
            changed = True

        if changed:
            new_exec = replace(execution, normalized_args=args)
            return _make_retry_result(
                task=task,
                execution=new_exec,
                retry_reason="Filled missing export arguments from task frame.",
                notes=["Repair patched export execution args from task frame."],
            )

    return None


def _merge_task_patch(task: TaskFrame, patch: dict[str, Any]) -> TaskFrame:
    if not patch:
        return task

    if "preferred_tool" in patch and patch["preferred_tool"]:
        task.preferred_tool = patch["preferred_tool"]

    if "design_spec" in patch and isinstance(patch["design_spec"], dict):
        task.design_spec.update(patch["design_spec"])

    if "analysis_request" in patch and isinstance(patch["analysis_request"], dict):
        task.analysis_request.update(patch["analysis_request"])

    if "run_request" in patch and isinstance(patch["run_request"], dict):
        task.run_request.update(patch["run_request"])

    if "delivery_request" in patch and isinstance(patch["delivery_request"], dict):
        task.delivery_request.update(patch["delivery_request"])

    if "lens_source" in patch and isinstance(patch["lens_source"], dict):
        task.lens_source = dict(patch["lens_source"])

    for mf in patch.get("missing_fields", []) or []:
        _append_missing(task, mf)

    for note in patch.get("notes", []) or []:
        task.notes.append(f"repair: {note}")

    _finalize_missing_fields(task)
    return task


def _merge_execution_patch(
    execution: ExecutionRequest | None,
    patch: dict[str, Any],
    task: TaskFrame,
) -> ExecutionRequest | None:
    if execution is None:
        execution = _derive_execution_request(task)

    if execution is None:
        return None

    if not patch:
        return execution

    args = dict(execution.normalized_args or {})
    args.update(dict(patch.get("normalized_args", {}) or {}))

    selected_tool = patch.get("selected_tool") or execution.selected_tool
    should_execute = patch.get("should_execute", execution.should_execute)

    return replace(
        execution,
        selected_tool=selected_tool,
        normalized_args=args,
        should_execute=should_execute,
    )


def _needs_llm_repair(task: TaskFrame, tool_result: Any, latest_message: str | None) -> bool:
    if getattr(tool_result, "ok", False):
        return False

    if task.workflow_family == WorkflowFamily.DESIGN:
        return True

    if getattr(tool_result, "retryable", False):
        return True

    if getattr(tool_result, "outcome_type", None) == OutcomeType.NEEDS_INPUT:
        return True

    text = (latest_message or "").lower()
    if any(x in text for x in ["default", "defaults", "reasonable", "sensible", "assume", "pick for me", "choose for me"]):
        return True

    return False


def _build_llm_repair_messages(
    *,
    task: TaskFrame,
    execution: ExecutionRequest | None,
    tool_result: Any,
    state: DeepLensState,
    latest_message: str | None,
) -> list[dict[str, Any]]:
    system_text = (
        "You are a bounded repair helper for a DeepLens workflow agent.\n"
        "You are repairing a failed or under-specified workflow step.\n"
        "Prefer deterministic salvage when possible.\n"
        "If the user explicitly allowed assumptions/defaults for design, you may propose tentative design_spec values.\n"
        "Do not invent lens_source or run_id.\n"
        "Return JSON only.\n"
    )
    user_payload = {
        "latest_message": latest_message or "",
        "task": asdict(task),
        "execution": asdict(execution) if execution else None,
        "tool_result": {
            "ok": getattr(tool_result, "ok", False),
            "summary": getattr(tool_result, "summary", ""),
            "error_code": getattr(tool_result, "error_code", None),
            "outcome_type": getattr(getattr(tool_result, "outcome_type", None), "value", None),
            "missing_fields": list(getattr(tool_result, "missing_fields", []) or []),
            "retryable": bool(getattr(tool_result, "retryable", False)),
        },
        "state": {
            "active_source_ref": state.active_source_ref,
            "design_draft": state.design_draft,
            "requested_next_step": state.requested_next_step,
        },
    }
    return [
        {"role": "system", "content": system_text},
        {"role": "user", "content": json.dumps(user_payload, ensure_ascii=False)},
    ]


async def _maybe_llm_repair(
    *,
    task: TaskFrame,
    execution: ExecutionRequest | None,
    tool_result: Any,
    state: DeepLensState,
    context: Any,
    latest_message: str | None,
) -> RepairResult | None:
    if not _needs_llm_repair(task, tool_result, latest_message):
        return None

    try:
        llm = context.llm(profile="fast")
        resp_text, _usage = await llm.chat(
            messages=_build_llm_repair_messages(
                task=task,
                execution=execution,
                tool_result=tool_result,
                state=state,
                latest_message=latest_message,
            ),
            output_format="json_schema",
            json_schema=REPAIR_JSON_SCHEMA,
            schema_name="deeplens_repair",
            strict_schema=True,
            validate_json=True,
            max_output_tokens=700,
        )
        payload = json.loads(resp_text) if isinstance(resp_text, str) else resp_text
    except Exception:
        return None

    action_raw = payload.get("action", RepairAction.FAIL.value)
    try:
        action = RepairAction(action_raw)
    except Exception:
        action = RepairAction.FAIL

    task = _merge_task_patch(task, dict(payload.get("task_patch", {}) or {}))
    for mf in payload.get("missing_fields", []) or []:
        _append_missing(task, mf)
    _finalize_missing_fields(task)

    repaired_execution = _merge_execution_patch(
        execution,
        dict(payload.get("execution_patch", {}) or {}),
        task,
    )

    if action in {RepairAction.RETRY_SAME_TOOL, RepairAction.RETRY_WITH_UPDATED_ARGS, RepairAction.REFRAME_TASK}:
        if repaired_execution is None:
            repaired_execution = _derive_execution_request(task)
        if repaired_execution is None:
            action = RepairAction.ASK_USER

    if action == RepairAction.ASK_USER:
        return RepairResult(
            action=RepairAction.ASK_USER,
            repaired_task_frame=asdict(task),
            repaired_execution=asdict(repaired_execution) if repaired_execution else None,
            ask_user_message=payload.get("ask_user_message") or "I still need a bit more information before continuing.",
            notes=list(payload.get("notes", []) or []),
        )

    if action in {RepairAction.RETRY_SAME_TOOL, RepairAction.RETRY_WITH_UPDATED_ARGS, RepairAction.REFRAME_TASK}:
        return RepairResult(
            action=action,
            repaired_task_frame=asdict(task),
            repaired_execution=asdict(repaired_execution) if repaired_execution else None,
            retry_reason=payload.get("retry_reason") or payload.get("reason") or "Repaired the task and retrying.",
            notes=list(payload.get("notes", []) or []),
        )

    return RepairResult(
        action=RepairAction.FAIL,
        repaired_task_frame=asdict(task),
        repaired_execution=asdict(repaired_execution) if repaired_execution else None,
        failure_reason=payload.get("failure_reason") or payload.get("reason") or "Repair could not recover safely.",
        notes=list(payload.get("notes", []) or []),
    )


async def repair_task(
    *,
    task: TaskFrame,
    execution: ExecutionRequest | None,
    tool_result: Any,
    state: DeepLensState,
    context: Any,
    latest_message: str | None = None,
) -> RepairResult:
    if getattr(tool_result, "ok", False):
        return RepairResult(action=RepairAction.NONE, notes=["No repair needed; tool result is ok."])

    outcome_type = getattr(tool_result, "outcome_type", None)
    error_code = getattr(tool_result, "error_code", None)
    missing_fields = list(getattr(tool_result, "missing_fields", []) or [])

    for field_name in missing_fields:
        _append_missing(task, field_name)

    if outcome_type == OutcomeType.NEEDS_INPUT or getattr(tool_result, "needs_input", False):
        repaired = _repair_missing_input(task=task, execution=execution, tool_result=tool_result)
        if repaired is not None and not task.missing_fields:
            return repaired

    if error_code in {"missing_input", "missing_source", "missing_design_spec"}:
        repaired = _repair_missing_input(task=task, execution=execution, tool_result=tool_result)
        if repaired is not None and not task.missing_fields:
            return repaired

    repaired = _repair_tool_argument_shape(task=task, execution=execution, tool_result=tool_result)
    if repaired is not None:
        return repaired

    llm_repaired = await _maybe_llm_repair(
        task=task,
        execution=execution,
        tool_result=tool_result,
        state=state,
        context=context,
        latest_message=latest_message,
    )
    if llm_repaired is not None:
        return llm_repaired

    if task.workflow_family == WorkflowFamily.DESIGN and task.missing_fields:
        return _make_ask_user_result(
            task=task,
            execution=execution,
            message=(
                "I couldn't safely infer all design defaults from the request. "
                "Please provide at least FOV, F-number, and either focal length or image height."
            ),
            notes=["LLM repair could not safely complete the design specification."],
        )

    summary = getattr(tool_result, "summary", "") or "The requested operation failed."
    return _make_fail_result(
        task=task,
        execution=execution,
        failure_reason=summary,
        notes=["Repair could not deterministically or heuristically recover from tool failure."],
    )
