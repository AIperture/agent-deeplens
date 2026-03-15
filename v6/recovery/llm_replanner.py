from __future__ import annotations

import json
from copy import deepcopy
from typing import Any

from ..planning.agenda_planner import _mk_action
from ..tools.tool_registry import TOOL_REGISTRY
from ..types import AgendaAction, DeepLensTask, RecoveryDecision, RecoveryDecisionKind, ResponseOutcomeKind, ToolResult


def _string_array_schema() -> dict[str, Any]:
    return {"type": "array", "items": {"type": "string"}}


def _lens_source_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "artifact_id": {"type": ["string", "null"]},
            "uri": {"type": ["string", "null"]},
            "name": {"type": ["string", "null"]},
        },
        "required": ["artifact_id", "uri", "name"],
        "additionalProperties": False,
    }


def _design_spec_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "fov": {"type": ["number", "string", "null"]},
            "fnum": {"type": ["number", "string", "null"]},
            "foclen": {"type": ["number", "string", "null"]},
            "imgh": {"type": ["number", "string", "null"]},
            "bfl": {"type": ["number", "string", "null"]},
            "thickness": {"type": ["number", "string", "null"]},
            "save_name": {"type": ["string", "null"]},
            "baseline_analysis": {"type": ["boolean", "null"]},
            "sensor_width_mm": {"type": ["number", "string", "null"]},
            "sensor_height_mm": {"type": ["number", "string", "null"]},
            "surf_list": {
                "type": ["array", "null"],
                "items": {"type": "object"},
            },
        },
        "additionalProperties": False,
    }


def _analysis_request_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "mode": {"type": ["string", "null"], "enum": ["full", "spot", "mtf", "rms", None]},
        },
        "additionalProperties": False,
    }


def _run_request_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "goal": {"type": ["string", "null"]},
            "constraints": _string_array_schema(),
            "excluded_objectives": _string_array_schema(),
            "iterations": {"type": ["integer", "number", "string", "null"]},
            "checkpoint_every": {"type": ["integer", "number", "string", "null"]},
            "export_formats": _string_array_schema(),
        },
        "additionalProperties": False,
    }


def _delivery_request_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "mode": {"type": ["string", "null"]},
            "formats": _string_array_schema(),
            "include": _string_array_schema(),
        },
        "additionalProperties": False,
    }


def _task_patch_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "design_spec": _design_spec_schema(),
            "analysis_request": _analysis_request_schema(),
            "run_request": _run_request_schema(),
            "delivery_request": _delivery_request_schema(),
            "lens_source": _lens_source_schema(),
            "requested_capabilities": _string_array_schema(),
            "notes": _string_array_schema(),
        },
        "additionalProperties": False,
    }


def _retry_patch_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "approval_prompt": {"type": ["string", "null"]},
            "prompt": {"type": ["string", "null"]},
            "run_id": {"type": ["string", "null"]},
            "timeout_s": {"type": ["integer", "number", "string", "null"]},
            "graph_id": {"type": ["string", "null"]},
            "use_stub": {"type": ["boolean", "null"]},
            "create_if_missing": {"type": ["boolean", "null"]},
            "formats": _string_array_schema(),
            "lens_source": _lens_source_schema(),
            "design_spec": _design_spec_schema(),
            "analysis_request": _analysis_request_schema(),
            "run_request": _run_request_schema(),
            "delivery_request": _delivery_request_schema(),
            "invalid_fields": {"type": "object", "additionalProperties": {"type": "string"}},
            "missing_fields": _string_array_schema(),
        },
        "additionalProperties": False,
    }


def _schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "decision_kind": {"type": "string", "enum": ["retry_action", "replace_remaining_agenda", "ask_user", "escalate", "fail"]},
            "reason": {"type": "string"},
            "updated_task_patch": _task_patch_schema(),
            "retry_patch": _retry_patch_schema(),
            "replacement_actions": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "kind": {"type": "string", "enum": ["ask_user", "request_approval", "tool_call", "respond", "finish", "fail"]},
                        "name": {"type": ["string", "null"]},
                        "prompt": {"type": ["string", "null"]},
                        "rationale": {"type": "string"},
                    },
                    "required": ["kind", "name", "prompt", "rationale"],
                    "additionalProperties": False,
                },
            },
            "ask_user_prompt": {"type": ["string", "null"]},
            "escalate_reason": {"type": ["string", "null"]},
        },
        "required": ["decision_kind", "reason", "updated_task_patch", "retry_patch", "replacement_actions", "ask_user_prompt", "escalate_reason"],
        "additionalProperties": False,
    }


def _apply_task_patch(task: DeepLensTask, patch: dict[str, Any]) -> DeepLensTask:
    updated = deepcopy(task)
    for key in ("design_spec", "analysis_request", "run_request", "delivery_request", "lens_source"):
        value = patch.get(key)
        if isinstance(value, dict):
            current = getattr(updated, key)
            if isinstance(current, dict):
                current.update(value)
    if isinstance(patch.get("requested_capabilities"), list):
        updated.requested_capabilities = [str(item) for item in patch["requested_capabilities"]]
    if isinstance(patch.get("notes"), list):
        updated.notes.extend(str(item) for item in patch["notes"])
    return updated


def _decode_actions(items: list[dict[str, Any]]) -> list[AgendaAction]:
    actions: list[AgendaAction] = []
    for idx, item in enumerate(items, start=1):
        name = item.get("name")
        if name is not None and name not in TOOL_REGISTRY:
            continue
        args: dict[str, Any] = {}
        prompt = item.get("prompt")
        if prompt:
            args["approval_prompt" if item.get("kind") == "request_approval" else "prompt"] = prompt
        actions.append(_mk_action(idx, str(item.get("kind") or "fail"), name, args=args, rationale=str(item.get("rationale") or "")))
    return actions


def _apply_retry_patch(action: AgendaAction, retry_patch: dict[str, Any]) -> AgendaAction:
    patched = deepcopy(action)
    if isinstance(retry_patch, dict) and retry_patch:
        merged = dict(action.args)
        merged.update(retry_patch)
        patched.args = merged
    return patched


async def llm_replan_after_failure(
    *,
    task: DeepLensTask,
    state: Any,
    agenda: Any,
    failed_action: AgendaAction,
    result: ToolResult,
    context_bundle: Any,
    context: Any,
) -> RecoveryDecision | None:
    llm = context.llm("fast")
    skills = context.skills()
    system_prompt = skills.compile_prompt(
        "aethergraph-agent-deeplens-v6",
        "deeplens.system",
        "deeplens.loop",
        "deeplens.style",
        separator="\n\n",
        fallback_keys=["deeplens.system"],
    )
    payload = {
        "goal": task.user_goal,
        "domain_hint": task.domain_hint.value,
        "requested_capabilities": task.requested_capabilities,
        "failed_action": {"kind": failed_action.kind, "name": failed_action.name, "args": failed_action.args},
        "tool_result": {
            "summary": result.summary,
            "failure_kind": result.failure_kind.value if result.failure_kind is not None else None,
            "missing_fields": result.missing_fields,
            "invalid_fields": result.invalid_fields,
            "repair_hints": result.repair_hints,
            "dependency_failures": result.dependency_failures,
            "diagnostics": result.diagnostics,
        },
        "last_attempt": state.last_attempt,
        "failure_history_tail": state.failure_history[-4:],
        "working_state": context_bundle.working_state,
        "completed_actions": [action.name or action.kind for action in agenda.actions if action.status.value == "completed"],
        "allowed_tools": list(TOOL_REGISTRY.keys()),
        "policy": {
            "same_domain_only": True,
            "approval_policy_must_hold": True,
            "do_not_continue_blocked_tail": True,
        },
    }
    try:
        response, _usage = await llm.chat(
            messages=[
                {
                    "role": "system",
                    "content": (
                        f"{system_prompt}\n\n"
                        "You are replanning within the same DeepLens task after a failed action. "
                        "You may retry, replace the remaining agenda, ask the user, escalate, or fail. "
                        "Do not switch task/domain, invent new tools, or bypass approval gates."
                    ),
                },
                {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
            ],
            output_format="json_schema",
            json_schema=_schema(),
            schema_name="DeepLensV6FailureReplan",
            strict_schema=True,
            validate_json=True,
            max_output_tokens=360,
            reasoning_effort="low",
        )
        obj = json.loads(response) if isinstance(response, str) else response
    except Exception:
        context.logger().warning("deeplens_v6: llm replanner failed", exc_info=True)
        return None

    task_patch = obj.get("updated_task_patch") or {}
    retry_patch = obj.get("retry_patch") or {}
    patched_task = _apply_task_patch(task, task_patch if isinstance(task_patch, dict) else {})
    decision_kind = RecoveryDecisionKind(str(obj.get("decision_kind") or "fail"))
    replacement_actions = _decode_actions(list(obj.get("replacement_actions") or []))

    if decision_kind == RecoveryDecisionKind.RETRY_ACTION:
        retry_action = _apply_retry_patch(failed_action, retry_patch if isinstance(retry_patch, dict) else {})
        return RecoveryDecision(
            kind=decision_kind,
            reason=str(obj.get("reason") or "Retry the failed action with a patched input set."),
            action=retry_action,
            task=patched_task,
            retry_patch=retry_patch if isinstance(retry_patch, dict) else {},
        )
    if decision_kind == RecoveryDecisionKind.REPLACE_REMAINING_AGENDA:
        return RecoveryDecision(kind=decision_kind, reason=str(obj.get("reason") or "Replace the remaining agenda after the failure."), replacement_actions=replacement_actions, task=patched_task, replacement_reason=str(obj.get("reason") or "Replace the remaining agenda after the failure."))
    if decision_kind == RecoveryDecisionKind.ASK_USER:
        return RecoveryDecision(kind=decision_kind, reason=str(obj.get("reason") or "Need a narrower clarification to continue."), ask_user_prompt=str(obj.get("ask_user_prompt") or ""), task=patched_task, outcome_kind=ResponseOutcomeKind.WAITING)
    if decision_kind == RecoveryDecisionKind.ESCALATE:
        return RecoveryDecision(kind=decision_kind, reason=str(obj.get("escalate_reason") or obj.get("reason") or "Need human assistance to proceed."), task=patched_task, outcome_kind=ResponseOutcomeKind.ESCALATE, escalation_diagnostics_ref=str((state.last_attempt or {}).get("attempt_id") or ""))
    return RecoveryDecision(kind=RecoveryDecisionKind.FAIL, reason=str(obj.get("reason") or "The failed action could not be recovered safely."), task=patched_task, outcome_kind=ResponseOutcomeKind.FAILED, escalation_diagnostics_ref=str((state.last_attempt or {}).get("attempt_id") or ""))
