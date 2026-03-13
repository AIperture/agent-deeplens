from __future__ import annotations

from copy import deepcopy
from typing import Any

from .tool_registry import get_tool_spec
from .types import BindingResult, BoundAction, DeepLensTask, PlanStep, RuntimeState


def _merge_dict(base: dict[str, Any], extra: dict[str, Any]) -> dict[str, Any]:
    merged = deepcopy(base)
    for key, value in extra.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _merge_dict(merged[key], value)
        else:
            merged[key] = value
    return merged


def _task_inputs(task: DeepLensTask, state: RuntimeState) -> dict[str, Any]:
    return {
        "attachments": list(task.attachments),
        "lens_source": task.lens_source or state.active_source_ref,
        "design_spec": _merge_dict(state.design_draft, task.design_spec),
        "analysis_request": dict(task.analysis_request),
        "run_request": dict(task.run_request),
        "delivery_request": dict(task.delivery_request),
        "response_request": dict(task.response_request),
        "run_id": task.run_request.get("run_id") or state.active_run_id,
        "active_lens_ref": state.active_lens_ref,
        "active_source_ref": state.active_source_ref,
        "use_stub": bool(task.run_request.get("use_stub")),
    }


def _missing_for_step(step: PlanStep, task: DeepLensTask, state: RuntimeState) -> list[str]:
    inputs = _task_inputs(task, state)
    design_spec = inputs.get("design_spec") or {}
    missing: list[str] = []
    for field_name in step.required_fields:
        if field_name == "fov" and design_spec.get("fov") is None:
            missing.append("fov")
        elif field_name == "fnum" and design_spec.get("fnum") is None:
            missing.append("fnum")
        elif field_name == "foclen_or_imgh" and design_spec.get("foclen") is None and design_spec.get("imgh") is None:
            missing.append("foclen_or_imgh")
        elif field_name == "lens_source":
            lens_source = inputs.get("lens_source") or {}
            has_attachment = bool(task.attachments)
            if not lens_source and not state.active_source_ref and not has_attachment:
                missing.append("lens_source")
        elif field_name == "run_id" and not inputs.get("run_id"):
            missing.append("run_id")
    return missing


def bind_step(step: PlanStep, task: DeepLensTask, state: RuntimeState) -> BindingResult:
    spec = get_tool_spec(step.tool_name)
    args = _task_inputs(task, state)
    if step.tool_name == "ag.spawn_graph":
        args["graph_id"] = step.arg_overrides.get("graph_id") or spec.defaults.get("graph_id")
    if step.tool_name == "dl.export_lens":
        args["formats"] = task.delivery_request.get("formats") or step.arg_overrides.get("formats") or ["json", "zmx"]
    if step.tool_name == "dl.create_lens":
        args["formats"] = task.delivery_request.get("formats") or step.arg_overrides.get("formats") or ["json", "zmx"]
    args = _merge_dict(args, step.arg_overrides)
    missing = _missing_for_step(step, task, state)
    if missing:
        return BindingResult(
            ok=False,
            missing_fields=missing,
            message=f"Missing required input: {', '.join(missing)}.",
        )
    return BindingResult(
        ok=True,
        action=BoundAction(step_id=step.step_id, tool_name=step.tool_name, args=args, missing_fields=[]),
    )
