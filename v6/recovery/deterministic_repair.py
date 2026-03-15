from __future__ import annotations

from copy import deepcopy
from typing import Any

from ..extraction import build_missing_prompt
from ..planning.agenda_planner import build_action_agenda
from ..tools.tool_arg_refine import normalize_tool_inputs
from ..types import AgendaAction, DeepLensTask, FailureKind, RecoveryDecision, RecoveryDecisionKind, ToolResult


def _merge_runtime_findings(task: DeepLensTask, state: Any, result: ToolResult) -> None:
    for field_name in result.missing_fields:
        if field_name not in task.missing_fields:
            task.missing_fields.append(field_name)
        if field_name not in state.runtime_missing_fields:
            state.runtime_missing_fields.append(field_name)
    if result.invalid_fields:
        task.notes.append(f"runtime_invalid_fields:{result.invalid_fields}")
        state.runtime_invalid_fields.update(result.invalid_fields)


def _last_attempt_inputs(state: Any, tool_name: str) -> dict[str, Any]:
    attempt = getattr(state, "last_attempt", None) or {}
    if not isinstance(attempt, dict):
        return {}
    if attempt.get("tool_name") != tool_name:
        return {}
    inputs = attempt.get("resolved_inputs") or {}
    return deepcopy(inputs) if isinstance(inputs, dict) else {}


def _clone_action(action: AgendaAction) -> AgendaAction:
    return AgendaAction(
        action_id=action.action_id,
        kind=action.kind,
        name=action.name,
        args=deepcopy(action.args),
        rationale=action.rationale,
        status=action.status,
    )


def _patch_action_from_task(task: DeepLensTask, state: Any, action: AgendaAction) -> AgendaAction | None:
    if action.kind != "tool_call":
        return None
    patched = _clone_action(action)
    base_inputs = _last_attempt_inputs(state, action.name or "")
    if base_inputs:
        patched.args = base_inputs
    changed = False
    if action.name == "dl.create_lens":
        existing = dict(patched.args.get("design_spec") or {})
        merged = deepcopy(task.design_spec)
        merged.update(existing)
        if merged:
            patched.args["design_spec"] = merged
            changed = patched.args.get("design_spec") != action.args.get("design_spec")
    if action.name in {"dl.analysis", "dl.export_lens"} and task.lens_source:
        patched.args["lens_source"] = deepcopy(task.lens_source)
        changed = True
    if action.name == "dl.export_lens" and task.delivery_request.get("formats"):
        patched.args["formats"] = list(task.delivery_request["formats"])
        changed = True
    return patched if changed else None


def _normalize_retry_action(state: Any, action: AgendaAction) -> AgendaAction | None:
    if action.kind != "tool_call" or action.name != "dl.create_lens":
        return None
    patched = _clone_action(action)
    prior_inputs = _last_attempt_inputs(state, action.name or "")
    if not prior_inputs:
        return None
    patched.args = prior_inputs
    normalized, invalid_fields = normalize_tool_inputs(spec=type("Spec", (), {"name": action.name})(), refined_inputs=patched.args)
    if invalid_fields:
        return None
    if normalized != prior_inputs and normalized.get("design_spec"):
        patched.args = normalized
        return patched
    return None


async def deterministic_recovery(
    *,
    task: DeepLensTask,
    state: Any,
    agenda: Any,
    failed_action: AgendaAction,
    result: ToolResult,
    context_bundle: Any,
    context: Any,
) -> RecoveryDecision | None:
    _merge_runtime_findings(task, state, result)

    if result.needs_user_input or result.missing_fields:
        state.last_prompt_reason = "runtime_missing_fields"
        return RecoveryDecision(
            kind=RecoveryDecisionKind.ASK_USER,
            reason="Execution revealed missing required inputs.",
            ask_user_prompt=build_missing_prompt(task, state),
            task=task,
        )

    normalized_retry = _normalize_retry_action(state, failed_action)
    if normalized_retry is not None:
        return RecoveryDecision(
            kind=RecoveryDecisionKind.RETRY_ACTION,
            reason="Normalized tool inputs for retry.",
            action=normalized_retry,
            task=task,
            retry_patch=deepcopy(normalized_retry.args),
        )

    patched = _patch_action_from_task(task, state, failed_action)
    if patched is not None and patched.args != failed_action.args:
        return RecoveryDecision(
            kind=RecoveryDecisionKind.RETRY_ACTION,
            reason="Patched action arguments from task state.",
            action=patched,
            task=task,
            retry_patch=deepcopy(patched.args),
        )

    if (
        result.failure_kind == FailureKind.MISSING_DEPENDENCY
        and failed_action.name in {"dl.analysis", "dl.export_lens"}
        and state.active_source_ref
    ):
        task.lens_source = dict(state.active_source_ref)
        patched = _patch_action_from_task(task, failed_action)
        if patched is not None:
            return RecoveryDecision(
                kind=RecoveryDecisionKind.RETRY_ACTION,
                reason="Retried using the active lens source already in state.",
                action=patched,
                task=task,
                retry_patch=deepcopy(patched.args),
            )

    if (
        failed_action.name == "dl.create_lens"
        and result.blocking
        and state.active_source_ref
        and any(cap in {"analysis", "export"} for cap in task.requested_capabilities)
    ):
        task.lens_source = dict(state.active_source_ref)
        fallback_task = deepcopy(task)
        fallback_task.requested_capabilities = [cap for cap in task.requested_capabilities if cap != "design"]
        fallback_task.notes.append("deterministic_replan:using_active_source_after_design_failure")
        replacement_agenda = await build_action_agenda(task=fallback_task, state=state, context_bundle=context_bundle, context=context)
        return RecoveryDecision(
            kind=RecoveryDecisionKind.REPLACE_REMAINING_AGENDA,
            reason="Switched to the active lens source after design creation failed.",
            replacement_actions=replacement_agenda.actions,
            task=fallback_task,
            replacement_reason="use_active_source_after_design_failure",
        )

    return None
