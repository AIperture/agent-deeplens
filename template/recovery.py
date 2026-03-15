from __future__ import annotations

import json
from copy import deepcopy
from typing import Any

from .types import AgendaAction, FailureKind, RecoveryDecision, RecoveryDecisionKind, ResponseOutcomeKind, TemplateTask, ToolResult


def _build_retry_action(failed_action: AgendaAction, patched_args: dict[str, Any] | None = None) -> AgendaAction:
    action = deepcopy(failed_action)
    if patched_args is not None:
        action.capability = deepcopy(failed_action.capability)
        if action.capability is not None:
            action.capability.planner_args = dict(patched_args)
    return action


def _normalize_invalid_fields(failed_action: AgendaAction, result: ToolResult) -> dict[str, Any]:
    patched_args = dict((failed_action.capability.planner_args if failed_action.capability else {}) or {})
    for key in result.invalid_fields:
        value = patched_args.get(key)
        if isinstance(value, str) and "," in value:
            patched_args[key] = [item.strip() for item in value.split(",") if item.strip()]
        elif isinstance(value, list):
            patched_args[key] = [item for item in value if str(item).lower() != "explode"]
        elif isinstance(value, str):
            patched_args[key] = value.strip()
    return patched_args


async def _llm_recovery(
    *,
    failed_action: AgendaAction,
    result: ToolResult,
    task: TemplateTask,
    context: Any,
) -> RecoveryDecision | None:
    try:
        llm = context.llm("fast")
        schema = {
            "type": "object",
            "properties": {
                "decision": {"type": "string", "enum": ["ask_user", "replace_remaining_agenda", "escalate", "fail"]},
                "reason": {"type": "string"},
                "prompt": {"type": ["string", "null"]},
            },
            "required": ["decision", "reason", "prompt"],
            "additionalProperties": False,
        }
        payload = {
            "goal": task.user_goal,
            "failed_capability": failed_action.capability.to_dict() if failed_action.capability else {},
            "failure_kind": result.failure_kind.value if result.failure_kind else "unknown",
            "summary": result.summary,
        }
        response, _usage = await llm.chat(
            messages=[
                {"role": "system", "content": "Choose recovery: ask_user, replace_remaining_agenda, escalate, or fail. Stay in the same domain."},
                {"role": "user", "content": json.dumps(payload)},
            ],
            output_format="json_schema",
            json_schema=schema,
            schema_name="TemplateRecovery",
            strict_schema=True,
            validate_json=True,
            max_output_tokens=120,
        )
        obj = json.loads(response) if isinstance(response, str) else response
        decision = str(obj.get("decision") or "fail")
        reason = str(obj.get("reason") or "LLM recovery")
        prompt = obj.get("prompt")
        if decision == "ask_user":
            return RecoveryDecision(kind=RecoveryDecisionKind.ASK_USER, reason=reason, ask_user_prompt=str(prompt or reason), task=task)
        if decision == "escalate":
            return RecoveryDecision(kind=RecoveryDecisionKind.ESCALATE, reason=reason, outcome_kind=ResponseOutcomeKind.ESCALATE, task=task)
        if decision == "replace_remaining_agenda":
            replacement = AgendaAction(action_id="r1", kind="respond", args={"text": str(prompt or reason)}, rationale=reason)
            return RecoveryDecision(kind=RecoveryDecisionKind.REPLACE_REMAINING_AGENDA, reason=reason, replacement_actions=[replacement], task=task)
        return RecoveryDecision(kind=RecoveryDecisionKind.FAIL, reason=reason, outcome_kind=ResponseOutcomeKind.FAILED, task=task)
    except Exception:
        return None


async def recover_failed_action(
    *,
    task: TemplateTask,
    state: Any,
    agenda: Any,
    failed_action: AgendaAction,
    result: ToolResult,
    context_bundle: Any,
    context: Any | None,
) -> RecoveryDecision:
    del agenda, context_bundle
    state.recent_failures.append({
        "action_id": failed_action.action_id,
        "tool_name": failed_action.name or (failed_action.capability.capability_name if failed_action.capability else None),
        "failure_kind": result.failure_kind.value if result.failure_kind else None,
        "summary": result.summary,
    })
    state.recent_failures = state.recent_failures[-8:]

    if result.failure_kind in {FailureKind.BINDING_FAILURE, FailureKind.MISSING_INPUTS} or result.needs_user_input:
        return RecoveryDecision(
            kind=RecoveryDecisionKind.ASK_USER,
            reason="Need user clarification before execution can continue.",
            ask_user_prompt=result.summary,
            task=task,
        )

    if result.failure_kind == FailureKind.INVALID_INPUTS and result.invalid_fields:
        patched_args = _normalize_invalid_fields(failed_action, result)
        return RecoveryDecision(
            kind=RecoveryDecisionKind.RETRY_ACTION,
            reason="Normalized invalid fields and prepared a retry.",
            action=_build_retry_action(failed_action, patched_args),
            task=task,
        )

    if result.failure_kind == FailureKind.POLICY_FAILURE:
        return RecoveryDecision(
            kind=RecoveryDecisionKind.REQUEST_APPROVAL,
            reason="Execution was blocked by policy approval.",
            approval_prompt=result.summary,
            task=task,
        )

    if result.failure_kind == FailureKind.TRANSIENT_ERROR and result.retryable:
        return RecoveryDecision(
            kind=RecoveryDecisionKind.RETRY_ACTION,
            reason="Retrying a transient failure.",
            action=_build_retry_action(failed_action),
            task=task,
        )

    if context is not None:
        llm = await _llm_recovery(failed_action=failed_action, result=result, task=task, context=context)
        if llm is not None:
            return llm

    return RecoveryDecision(
        kind=RecoveryDecisionKind.ESCALATE,
        reason=result.summary,
        outcome_kind=ResponseOutcomeKind.ESCALATE,
        task=task,
    )
