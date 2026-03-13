"""Generic tool argument resolver driven by ToolParam declarations.

Replaces v6's three modules (field_resolution, extraction, tool_arg_refine) with a
single resolver that reads tool spec parameter declarations mechanically.

Resolution order for each parameter:
1. action_args (from the agenda action)
2. task.parsed_args
3. task.domain_data (via spec.source_paths)
4. state.domain_state (via spec.source_paths)
5. spec.parameters[param].default

Then: heuristic coercion -> optional LLM refinement -> validate required fields.
"""
from __future__ import annotations

import json
import re
from typing import Any

from . import policy
from .tools.registry import ToolSpec, get_tool_spec
from .types import AgentState, Task


def _resolve_dotted_path(obj: Any, path: str) -> Any:
    """Resolve a dotted path like 'task.parsed_args.query' against an object or dict."""
    parts = path.split(".")
    current = obj
    for part in parts:
        if current is None:
            return None
        if isinstance(current, dict):
            current = current.get(part)
        elif hasattr(current, part):
            current = getattr(current, part)
        else:
            return None
    return current


def _coerce(value: Any, expected_type: str) -> Any:
    """Heuristic type coercion."""
    if value is None:
        return None
    if expected_type == "float":
        if isinstance(value, (int, float)):
            return float(value)
        if isinstance(value, str):
            # Strip units like "10mm", "5.0 cm"
            cleaned = re.sub(r"[a-zA-Z°%]+$", "", value.strip())
            try:
                return float(cleaned)
            except ValueError:
                return value
    if expected_type == "int":
        if isinstance(value, int):
            return value
        if isinstance(value, float):
            return int(value)
        if isinstance(value, str):
            cleaned = re.sub(r"[a-zA-Z°%]+$", "", value.strip())
            try:
                return int(float(cleaned))
            except ValueError:
                return value
    if expected_type == "bool":
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            return value.lower() in ("true", "1", "yes", "on")
        return bool(value)
    if expected_type == "str":
        return str(value) if value is not None else None
    return value


def resolve_tool_args(
    spec: ToolSpec,
    action_args: dict[str, Any],
    task: Task,
    state: AgentState,
) -> tuple[dict[str, Any], dict[str, str]]:
    """Resolve tool arguments from multiple sources, filling defaults.

    Returns:
        (resolved_args, invalid_fields) where invalid_fields maps param_name -> reason.
    """
    resolved: dict[str, Any] = {}
    invalid_fields: dict[str, str] = {}

    # Build lookup contexts
    lookup_contexts = {"task": task, "state": state}

    for param_name, param in spec.parameters.items():
        value = None

        # 1. action_args (highest priority)
        if param_name in action_args and action_args[param_name] is not None:
            value = action_args[param_name]

        # 2. task.parsed_args
        if value is None and param_name in (task.parsed_args or {}):
            value = task.parsed_args[param_name]

        # 3. source_paths (walk through dotted paths)
        if value is None:
            for path in param.source_paths:
                root_key = path.split(".")[0]
                root_obj = lookup_contexts.get(root_key)
                if root_obj is not None:
                    # Resolve from the root object using the path after the root key
                    sub_path = ".".join(path.split(".")[1:])
                    if sub_path:
                        candidate = _resolve_dotted_path(root_obj, sub_path)
                    else:
                        candidate = root_obj
                    if candidate is not None:
                        value = candidate
                        break

        # 4. default
        if value is None and param.default is not None:
            value = param.default

        # Coerce to expected type
        if value is not None:
            coerced = _coerce(value, param.type)
            if coerced is not None:
                value = coerced
            elif param.required:
                invalid_fields[param_name] = f"Could not coerce '{value}' to {param.type}"
                value = None

        resolved[param_name] = value

    # Also carry over any extra keys from action_args that aren't in the spec
    # (e.g., "user_goal", "task_shape" for context)
    for key, val in action_args.items():
        if key not in resolved:
            resolved[key] = val

    return resolved, invalid_fields


def compute_missing_fields(task: Task, tool_name: str) -> list[str]:
    """Check which required params are missing for a given tool."""
    spec = get_tool_spec(tool_name)
    missing: list[str] = []
    for param_name, param in spec.parameters.items():
        if not param.required:
            continue
        # Check if the value is available from any source
        value = task.parsed_args.get(param_name)
        if value is None:
            value = task.domain_data.get(param_name)
        if value is None:
            missing.append(param_name)
    return missing


def build_missing_prompt(task: Task, state: AgentState) -> str:
    """Generate a user-facing prompt listing what's needed, using spec descriptions."""
    missing = list(task.missing_fields or state.runtime_missing_fields or [])
    if not missing:
        return "I need some additional information to proceed."

    lines: list[str] = ["I need the following information to continue:"]
    # Try to find descriptions from tool specs
    seen: set[str] = set()
    for tool_name in policy.CAPABILITY_TOOL_MAP.values():
        spec = get_tool_spec(tool_name)
        for param_name, param in spec.parameters.items():
            if param_name in missing and param_name not in seen:
                desc = param.description or param_name
                lines.append(f"  - **{param_name}**: {desc}")
                seen.add(param_name)

    # Any remaining missing fields without descriptions
    for field_name in missing:
        if field_name not in seen:
            lines.append(f"  - **{field_name}**")

    return "\n".join(lines)


async def maybe_refine_with_llm(
    spec: ToolSpec,
    draft_inputs: dict[str, Any],
    task: Task,
    state: AgentState,
    context_bundle: Any,
    context: Any,
) -> dict[str, Any]:
    """Optionally refine tool inputs using LLM when the tool is marked as refinable.

    Falls back to draft_inputs on any error.
    """
    if not spec.refinable:
        return draft_inputs

    try:
        llm = context.llm("fast")
        skills = context.skills()
        system_prompt = skills.compile_prompt(
            policy.SKILL_ID,
            "skeleton.system",
            "skeleton.tool_args",
            separator="\n\n",
            fallback_keys=["skeleton.system"],
        )

        # Build refinement schema from tool params
        properties: dict[str, Any] = {}
        required: list[str] = []
        for param_name, param in spec.parameters.items():
            type_map = {"str": "string", "int": "integer", "float": "number", "bool": "boolean"}
            json_type = type_map.get(param.type, "string")
            properties[param_name] = {"type": [json_type, "null"], "description": param.description}
            if param.required:
                required.append(param_name)

        schema = {
            "type": "object",
            "properties": properties,
            "required": required,
            "additionalProperties": False,
        }

        payload = {
            "tool_name": spec.name,
            "tool_description": spec.description,
            "user_goal": task.user_goal,
            "draft_inputs": {k: v for k, v in draft_inputs.items() if k in spec.parameters},
            "working_state": context_bundle.working_state if context_bundle else {},
        }

        response, _usage = await llm.chat(
            messages=[
                {"role": "system", "content": f"{system_prompt}\n\nRefine the tool inputs. Fill missing values from context. Do not invent data."},
                {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
            ],
            output_format="json_schema",
            json_schema=schema,
            schema_name=f"Refine_{spec.name}",
            strict_schema=True,
            validate_json=True,
            max_output_tokens=200,
            reasoning_effort="low",
        )
        obj = json.loads(response) if isinstance(response, str) else response
        # Merge: LLM values override draft only when non-None
        merged = dict(draft_inputs)
        for k, v in obj.items():
            if v is not None:
                merged[k] = v
        return merged
    except Exception:
        if hasattr(context, "logger"):
            context.logger().warning("skeleton: LLM arg refinement failed", exc_info=True)
        return draft_inputs
