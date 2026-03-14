from __future__ import annotations

from copy import deepcopy
from typing import Any

from .tool_registry import get_tool_spec
from .types import BindingResult, BoundAction, DeepLensTask, PlanStep, RuntimeState, ToolSpec


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


def _get_path(data: Any, path: str) -> Any:
    current = data
    for part in path.split("."):
        if not isinstance(current, dict):
            return None
        current = current.get(part)
        if current is None:
            return None
    return current


def _set_path(data: dict[str, Any], path: str, value: Any) -> None:
    current = data
    parts = path.split(".")
    for part in parts[:-1]:
        next_value = current.get(part)
        if not isinstance(next_value, dict):
            next_value = {}
            current[part] = next_value
        current = next_value
    current[parts[-1]] = value


def _resolve_latest_artifact(*, state: RuntimeState, kind: str) -> dict[str, Any] | None:
    allowed = {"file", "json", "text"} if kind == "file" else {kind}
    for artifact in reversed(state.last_artifacts):
        if str(artifact.get("kind") or "") in allowed:
            return artifact
        name = str(artifact.get("name") or "")
        if kind == "file" and name:
            return artifact
    return None


def _resolve_artifact_selector(args: dict[str, Any], state: RuntimeState, spec: ToolSpec) -> dict[str, Any]:
    selector = args.get("artifact_selector")
    if not isinstance(selector, dict) or not spec.artifact_selector_kind:
        return args
    selected = _resolve_latest_artifact(state=state, kind=str(selector.get("kind") or spec.artifact_selector_kind))
    if not selected:
        return args
    resolved = deepcopy(args)
    resolved["url"] = selected.get("uri") or selected.get("path") or resolved.get("url")
    resolved["title"] = resolved.get("title") or selected.get("name")
    if spec.name == "ag.send_file":
        resolved["filename"] = resolved.get("filename") or selected.get("name")
    if spec.name == "ag.send_image":
        resolved["name"] = resolved.get("name") or selected.get("name")
    return resolved


def _missing_paths(spec: ToolSpec, resolved_args: dict[str, Any]) -> list[str]:
    missing: list[str] = []
    for path in spec.required_input_paths:
        value = _get_path(resolved_args, path)
        if value in (None, "", []):
            missing.append(path)
    return missing


def _normalize_missing_name(path: str) -> str:
    return path.split(".")[-1]


def resolve_step_args(step: PlanStep, task: DeepLensTask, state: RuntimeState) -> dict[str, Any]:
    spec = get_tool_spec(step.tool_name)
    args = _merge_dict(spec.defaults, _task_inputs(task, state))
    args = _merge_dict(args, step.arg_overrides)
    for plan_path in spec.plan_arg_paths:
        value = _get_path(args, plan_path)
        if value is not None:
            _set_path(args, plan_path, value)
    return _resolve_artifact_selector(args, state, spec)


def bind_step(step: PlanStep, task: DeepLensTask, state: RuntimeState) -> BindingResult:
    spec = get_tool_spec(step.tool_name)
    resolved_args = resolve_step_args(step, task, state)
    missing = [_normalize_missing_name(path) for path in _missing_paths(spec, resolved_args)]
    if missing:
        return BindingResult(
            ok=False,
            missing_fields=missing,
            message=f"Missing required input: {', '.join(missing)}.",
            resolved_args=resolved_args,
        )
    return BindingResult(
        ok=True,
        action=BoundAction(step_id=step.step_id, tool_name=step.tool_name, args=resolved_args, missing_fields=[]),
        resolved_args=resolved_args,
    )
