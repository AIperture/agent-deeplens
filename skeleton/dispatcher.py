"""Tool dispatch pipeline: spec lookup -> approval -> arg resolution -> validate -> execute -> state updates.

Generic version of v6's tool_dispatch.py. Input resolution is fully driven by tool specs
instead of manually setting 15+ domain-specific defaults.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from . import policy
from .arg_resolver import maybe_refine_with_llm, resolve_tool_args
from .tools.fake_tools import EXECUTOR_MAP
from .tools.registry import get_tool_spec
from .types import (
    ActionAttempt,
    AgentState,
    ApprovalLevel,
    FailureKind,
    Task,
    ToolResult,
)

logger = logging.getLogger(f"ag.{policy.AGENT_ID}.dispatcher")


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _failure_result(
    *,
    tool_name: str,
    summary: str,
    status: str = "failed",
    error_code: str | None = None,
    failure_kind: FailureKind = FailureKind.UNKNOWN,
    blocking: bool = False,
    repairable: bool = False,
    needs_replan: bool = False,
    needs_user_input: bool = False,
    missing_fields: list[str] | None = None,
    invalid_fields: dict[str, str] | None = None,
    repair_hints: list[str] | None = None,
    diagnostics: dict[str, Any] | None = None,
    human_escalation_reason: str | None = None,
) -> ToolResult:
    return ToolResult(
        ok=False,
        tool_name=tool_name,
        summary=summary,
        status=status,
        error_code=error_code,
        retryable=repairable,
        failure_kind=failure_kind,
        blocking=blocking,
        repairable=repairable,
        needs_replan=needs_replan,
        needs_user_input=needs_user_input,
        missing_fields=list(missing_fields or []),
        invalid_fields=dict(invalid_fields or {}),
        repair_hints=list(repair_hints or []),
        diagnostics=dict(diagnostics or {}),
        human_escalation_reason=human_escalation_reason,
    )


def _failure_signature(action_id: str, result: ToolResult) -> str:
    return "|".join([
        action_id or "unknown_action",
        str(result.tool_name or "unknown_tool"),
        str(result.failure_kind.value if result.failure_kind is not None else "none"),
        str(result.error_code or "none"),
        str(result.summary or "")[:160],
    ])


def _next_attempt_index(state: AgentState, action_id: str, tool_name: str) -> int:
    history = list(getattr(state, "attempt_history", []) or [])
    matches = [item for item in history if item.get("action_id") == action_id and item.get("tool_name") == tool_name]
    return len(matches) + 1


def _record_attempt_snapshot(state: AgentState, *, attempt: ActionAttempt) -> None:
    payload = attempt.to_dict()
    state.last_attempt = payload
    state.attempt_history.append(payload)
    state.attempt_history = state.attempt_history[-policy.ATTEMPT_HISTORY_LIMIT:]


def _update_attempt_result(state: AgentState, result: ToolResult) -> None:
    if not isinstance(state.last_attempt, dict):
        return
    attempt = dict(state.last_attempt)
    attempt["result"] = {
        "ok": result.ok,
        "summary": result.summary,
        "status": result.status,
        "error_code": result.error_code,
        "failure_kind": result.failure_kind.value if result.failure_kind is not None else None,
        "missing_fields": list(result.missing_fields),
        "invalid_fields": dict(result.invalid_fields),
    }
    if not result.ok:
        attempt["failure_signature"] = _failure_signature(str(attempt.get("action_id") or ""), result)
    state.last_attempt = attempt
    if state.attempt_history:
        state.attempt_history[-1] = attempt


async def _record_tool_memory(context: Any, tool_name: str, inputs: dict[str, Any], result: ToolResult) -> None:
    await context.memory().record_tool_result(
        tool=tool_name,
        inputs=[inputs],
        outputs=[{
            "ok": result.ok,
            "status": result.status,
            "summary": result.summary,
            "data": result.data,
            "warnings": result.warnings,
            "error_code": result.error_code,
            "attempt_id": result.attempt_id,
            "failure_kind": result.failure_kind.value if result.failure_kind is not None else None,
            "missing_fields": result.missing_fields,
            "invalid_fields": result.invalid_fields,
        }],
        message=result.summary,
        tags=[f"ag.{policy.AGENT_ID}.tool", f"ag.{policy.AGENT_ID}.action:{tool_name}"],
    )


async def _maybe_approval(spec: Any, action: dict[str, Any], state: AgentState, context: Any) -> bool:
    if spec.approval_level == ApprovalLevel.NONE:
        return True
    if state.approved_action == spec.name:
        state.approved_action = None
        return True
    if spec.approval_level == ApprovalLevel.HARD:
        return True
    prompt = action.get("args", {}).get("approval_prompt") or f"Approve `{spec.name}`?"
    resp = await context.channel("ui:session").ask_approval(
        prompt=prompt,
        options=["Approve", "Cancel"],
    )
    return bool(resp.get("approved"))


def _apply_state_updates(state: AgentState, result: ToolResult) -> None:
    """Apply state_updates from tool result, supporting dotted paths for domain_state."""
    updates = result.data.get("state_updates") if isinstance(result.data, dict) else None
    if isinstance(updates, dict):
        for key, value in updates.items():
            if value is None:
                continue
            # Support dotted paths like "domain_state.last_calculation"
            if "." in key:
                parts = key.split(".", 1)
                root, sub_key = parts[0], parts[1]
                if hasattr(state, root):
                    container = getattr(state, root)
                    if isinstance(container, dict):
                        container[sub_key] = value
            elif hasattr(state, key):
                setattr(state, key, value)

    state.last_tool_result = {
        "attempt_id": result.attempt_id,
        "tool_name": result.tool_name,
        "ok": result.ok,
        "status": result.status,
        "summary": result.summary,
        "failure_kind": result.failure_kind.value if result.failure_kind is not None else None,
        "error_code": result.error_code,
        "missing_fields": list(result.missing_fields),
        "invalid_fields": dict(result.invalid_fields),
        "repair_hints": list(result.repair_hints),
    }
    _update_attempt_result(state, result)

    if result.missing_fields:
        for field_name in result.missing_fields:
            if field_name not in state.runtime_missing_fields:
                state.runtime_missing_fields.append(field_name)
        state.last_prompt_reason = "runtime_missing_fields"
    if result.invalid_fields:
        state.runtime_invalid_fields.update(result.invalid_fields)
        state.last_prompt_reason = "runtime_invalid_fields"
    if result.ok:
        state.runtime_missing_fields = []
        state.runtime_invalid_fields = {}
        state.last_prompt_reason = None
    if not result.ok:
        state.failure_history.append({
            "attempt_id": result.attempt_id,
            "tool_name": result.tool_name,
            "status": result.status,
            "summary": result.summary,
            "failure_kind": result.failure_kind.value if result.failure_kind is not None else None,
            "error_code": result.error_code,
        })
        state.failure_history = state.failure_history[-policy.FAILURE_HISTORY_LIMIT:]


def _validate_inputs(tool_name: str, spec: Any, resolved_inputs: dict[str, Any], invalid_fields: dict[str, str]) -> ToolResult | None:
    """Generic validation: check invalid fields and required params."""
    if invalid_fields:
        return _failure_result(
            tool_name=tool_name,
            summary=f"`{tool_name}` received invalid inputs: {', '.join(invalid_fields.keys())}.",
            error_code="invalid_inputs",
            failure_kind=FailureKind.INVALID_INPUTS,
            blocking=True,
            repairable=True,
            invalid_fields=invalid_fields,
            repair_hints=["normalize_inputs", "retry_action"],
        )

    # Check required params from spec
    for param_name, param in spec.parameters.items():
        if param.required and resolved_inputs.get(param_name) is None:
            return _failure_result(
                tool_name=tool_name,
                summary=f"`{tool_name}` missing required field: {param_name}.",
                error_code="missing_required_field",
                failure_kind=FailureKind.MISSING_INPUTS,
                blocking=True,
                repairable=True,
                needs_user_input=True,
                missing_fields=[param_name],
                repair_hints=["ask_user"],
            )

    return None


async def dispatch_tool_action(
    *,
    action: dict[str, Any],
    task: Task,
    state: AgentState,
    context_bundle: Any,
    context: Any,
) -> ToolResult:
    """Full tool dispatch pipeline."""
    tool_name = action.get("name") or ""
    action_id = str(action.get("action_id") or "")
    spec = get_tool_spec(tool_name)

    # Approval check
    approved = await _maybe_approval(spec, action, state, context)
    if not approved:
        state.approved_action = None
        return ToolResult(ok=True, tool_name=tool_name, status="canceled", summary=f"Skipped `{tool_name}` because approval was not granted.", should_end_turn=True)

    # Resolve inputs using the spec-driven arg resolver
    action_args = dict(action.get("args") or {})
    resolved_inputs, invalid_fields = resolve_tool_args(spec, action_args, task, state)

    # Optional LLM refinement
    resolved_inputs = await maybe_refine_with_llm(
        spec=spec,
        draft_inputs=resolved_inputs,
        task=task,
        state=state,
        context_bundle=context_bundle,
        context=context,
    )

    # Record attempt
    attempt = ActionAttempt(
        attempt_id=f"{action_id or tool_name}:{_next_attempt_index(state, action_id or tool_name, tool_name)}",
        action_id=action_id or tool_name,
        tool_name=tool_name,
        resolved_inputs=resolved_inputs,
        invalid_fields=invalid_fields,
        attempt_index=_next_attempt_index(state, action_id or tool_name, tool_name),
    )
    _record_attempt_snapshot(state, attempt=attempt)

    # Validate
    validation_failure = _validate_inputs(tool_name, spec, resolved_inputs, invalid_fields)
    if validation_failure is not None:
        validation_failure.attempt_id = attempt.attempt_id
        validation_failure.diagnostics.setdefault("failure_signature", _failure_signature(attempt.action_id, validation_failure))
        _apply_state_updates(state, validation_failure)
        state.approved_action = None
        await _record_tool_memory(context, tool_name, resolved_inputs, validation_failure)
        return validation_failure

    # Execute
    executor = EXECUTOR_MAP.get(spec.executor_key)
    if executor is None:
        result = _failure_result(
            tool_name=tool_name,
            summary=f"No executor found for `{tool_name}` (executor_key={spec.executor_key}).",
            error_code="missing_executor",
            failure_kind=FailureKind.INTERNAL_ERROR,
            blocking=True,
            needs_replan=True,
        )
    else:
        try:
            result = await executor(
                tool_name=tool_name,
                resolved_inputs=resolved_inputs,
                task=task,
                state=state,
                context_bundle=context_bundle,
                context=context,
            )
        except Exception as exc:
            logger.error("Unhandled error in tool executor %s", tool_name, exc_info=True)
            result = _failure_result(
                tool_name=tool_name,
                summary=f"`{tool_name}` failed: {exc}",
                error_code="unhandled_executor_error",
                failure_kind=FailureKind.INTERNAL_ERROR,
                blocking=True,
                needs_replan=True,
                diagnostics={"exception_type": type(exc).__name__},
            )

    result.attempt_id = attempt.attempt_id
    if not result.ok:
        result.diagnostics.setdefault("failure_signature", _failure_signature(attempt.action_id, result))
    _apply_state_updates(state, result)
    state.approved_action = None
    await _record_tool_memory(context, tool_name, resolved_inputs, result)
    return result
