from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from .tools.registry import ToolSpec, get_tool_spec
from .types import ApprovalLevel, ExecutableAction, FieldResolution, FieldSource, TemplateState, TemplateTask


@dataclass
class BindingResult:
    executable_action: ExecutableAction
    suggested_action: str | None = None
    prompt: str | None = None
    events: list[dict[str, Any]] = field(default_factory=list)


def _get_from_path(root: Any, path: str) -> Any:
    current = root
    for part in path.split("."):
        if current is None:
            return None
        if isinstance(current, dict):
            current = current.get(part)
        else:
            current = getattr(current, part, None)
    return current


def _coerce(value: Any, expected_type: str) -> tuple[Any, bool]:
    if value is None:
        return None, True
    if expected_type == "str":
        return str(value), True
    if expected_type == "int":
        if isinstance(value, int):
            return value, True
        if isinstance(value, str):
            cleaned = re.sub(r"[^\d-]", "", value)
            if cleaned:
                return int(cleaned), True
        return value, False
    if expected_type == "float":
        if isinstance(value, (int, float)):
            return float(value), True
        if isinstance(value, str):
            cleaned = re.sub(r"[^0-9.\-]", "", value)
            if cleaned:
                return float(cleaned), True
        return value, False
    if expected_type == "bool":
        if isinstance(value, bool):
            return value, True
        if isinstance(value, str):
            lowered = value.lower().strip()
            if lowered in {"true", "yes", "1", "on"}:
                return True, True
            if lowered in {"false", "no", "0", "off"}:
                return False, True
        return value, False
    if expected_type == "list":
        if isinstance(value, list):
            return value, True
        if isinstance(value, str):
            return [item.strip() for item in value.split(",") if item.strip()], True
        return value, False
    return value, True


def _validate(value: Any, rule: str | None) -> str | None:
    if rule is None:
        return None
    if rule == "nonempty" and not str(value or "").strip():
        return "value must be non-empty"
    if rule.startswith("oneof:"):
        allowed = {item.strip() for item in rule.split(":", 1)[1].split(",")}
        if str(value) not in allowed:
            return f"value must be one of {sorted(allowed)}"
    return None


async def _llm_infer_field(*, field_name: str, task: TemplateTask, context_bundle: Any, context: Any) -> Any:
    try:
        llm = context.llm("fast")
        schema = {
            "type": "object",
            "properties": {"value": {"type": ["string", "array", "null"], "items": {"type": "string"}}},
            "required": ["value"],
            "additionalProperties": False,
        }
        payload = {
            "field_name": field_name,
            "user_goal": task.user_goal,
            "parsed_args": task.parsed_args,
            "candidate_field_values": getattr(context_bundle, "candidate_field_values", {}),
        }
        response, _usage = await llm.chat(
            messages=[
                {"role": "system", "content": "Infer a missing field only if directly supported by the request context."},
                {"role": "user", "content": json.dumps(payload)},
            ],
            output_format="json_schema",
            json_schema=schema,
            schema_name=f"TemplateInfer_{field_name}",
            strict_schema=True,
            validate_json=True,
            max_output_tokens=80,
        )
        obj = json.loads(response) if isinstance(response, str) else response
        return obj.get("value")
    except Exception:
        return None


def _build_prompt(spec: ToolSpec, missing_fields: list[str], invalid_fields: dict[str, str]) -> str:
    lines: list[str] = []
    if missing_fields:
        lines.append("I need these fields before I can continue:")
        for field_name in missing_fields:
            param = spec.parameters[field_name]
            lines.append(f"- {field_name}: {param.ask_hint or param.description or field_name}")
    if invalid_fields:
        lines.append("These fields need correction:")
        for field_name, reason in invalid_fields.items():
            lines.append(f"- {field_name}: {reason}")
    return "\n".join(lines) or "I need more information to continue."


async def bind_capability_action(
    *,
    action_id: str,
    capability_name: str,
    planner_args: dict[str, Any],
    task: TemplateTask,
    state: TemplateState,
    context_bundle: Any,
    context: Any | None,
) -> BindingResult:
    spec = get_tool_spec(capability_name)
    resolved_inputs: dict[str, Any] = {}
    resolutions: dict[str, FieldResolution] = {}
    missing_fields: list[str] = []
    invalid_fields: dict[str, str] = {}
    events: list[dict[str, Any]] = []

    for param_name, param in spec.parameters.items():
        value = None
        resolution = FieldResolution()
        if planner_args.get(param_name) is not None:
            value = planner_args[param_name]
            resolution.source = FieldSource.PLANNER_ARGS
            resolution.confidence = 1.0
        elif task.parsed_args.get(param_name) is not None:
            value = task.parsed_args[param_name]
            resolution.source = FieldSource.TASK_ARGS
            resolution.confidence = 0.95
        elif param_name in task.field_map and task.field_map[param_name].value is not None:
            value = task.field_map[param_name].value
            resolution.source = FieldSource.TASK_FIELD_MAP
            resolution.confidence = task.field_map[param_name].confidence
            resolution.confirmed = task.field_map[param_name].confirmed
        elif task.working_state.get(param_name) is not None:
            value = task.working_state[param_name]
            resolution.source = FieldSource.WORKING_STATE
            resolution.confidence = 0.8
        elif state.memory_bag.get(param_name) is not None:
            value = state.memory_bag[param_name]
            resolution.source = FieldSource.MEMORY_BAG
            resolution.confidence = 0.75
        else:
            for path in param.source_paths:
                root_name, _, remainder = path.partition(".")
                root = {"task": task, "state": state}.get(root_name)
                if root is None:
                    continue
                candidate = _get_from_path(root, remainder)
                if candidate is not None:
                    value = candidate
                    resolution.source = FieldSource.PRIOR_TOOL_OUTPUT if "prior_tool_outputs" in path else FieldSource.DETERMINISTIC_INFERENCE
                    resolution.confidence = 0.7
                    break
        if value is None and param_name == "objective" and task.user_goal:
            value = task.user_goal
            resolution.source = FieldSource.DETERMINISTIC_INFERENCE
            resolution.confidence = 0.65
        if value is None and context is not None and spec.refinable:
            inferred = await _llm_infer_field(field_name=param_name, task=task, context_bundle=context_bundle, context=context)
            if inferred is not None:
                value = inferred
                resolution.source = FieldSource.LLM_INFERENCE
                resolution.confidence = 0.55
        if value is None and param.default is not None and spec.supports_defaults:
            value = param.default
            resolution.source = FieldSource.DEFAULT
            resolution.defaulted = True
            resolution.confidence = 0.5

        coerced, ok = _coerce(value, param.type)
        if value is not None and not ok:
            invalid_fields[param_name] = f"expected {param.type}"
            resolution.valid = False
            resolution.message = invalid_fields[param_name]
        else:
            value = coerced
            validation_error = _validate(value, param.validation_rule)
            if validation_error is not None:
                invalid_fields[param_name] = validation_error
                resolution.valid = False
                resolution.message = validation_error

        if value is None and param.required:
            missing_fields.append(param_name)
            resolution.valid = False
            resolution.message = "missing required field"

        if param_name in spec.requires_confirmation_fields and resolution.source in {FieldSource.DEFAULT, FieldSource.LLM_INFERENCE}:
            resolution.confirmed = False
        elif value is not None:
            resolution.confirmed = resolution.confirmed or resolution.source in {
                FieldSource.PLANNER_ARGS,
                FieldSource.TASK_ARGS,
                FieldSource.TASK_FIELD_MAP,
            }

        resolved_inputs[param_name] = value
        resolutions[param_name] = resolution
        event_type = "field_missing" if param_name in missing_fields else "field_invalid" if param_name in invalid_fields else "field_defaulted" if resolution.defaulted else "field_resolved"
        events.append({"event_type": event_type, "field_name": param_name, "value": value, "source": resolution.source.value})

    binding_status = "ready"
    suggested_action = None
    prompt = None
    if missing_fields or invalid_fields:
        binding_status = "blocked"
        suggested_action = "ask_user"
        prompt = _build_prompt(spec, missing_fields, invalid_fields)
    elif spec.approval_level != ApprovalLevel.NONE and not state.approval_tokens.get(action_id):
        binding_status = "approval_required"
        suggested_action = "request_approval"
        prompt = f"Approve `{spec.name}` for this request?"
        events.append({"event_type": "approval_required", "tool_name": spec.name})

    executable = ExecutableAction(
        action_id=action_id,
        capability_name=capability_name,
        tool_name=spec.name,
        resolved_inputs=resolved_inputs,
        field_resolutions=resolutions,
        missing_fields=missing_fields,
        invalid_fields=invalid_fields,
        approval_required=spec.approval_level,
        binding_status=binding_status,
        provenance={"planner_args": dict(planner_args)},
    )
    return BindingResult(executable_action=executable, suggested_action=suggested_action, prompt=prompt, events=events)
